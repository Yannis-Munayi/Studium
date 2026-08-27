"""The reviewer's command line (ingestion §9.2, §10.2, §11.3, §12.2).

    studium ingest pdf <path> --subject <slug> [--extractor <name>]
    studium ingest graph <dir>
    studium ingest rubrics <dir>
    studium ingest sources <dir>
    studium publish subject <slug>
    studium sources list [--subject <slug>] [--unclassified]
    studium sources show <source_id>
    studium sources classify <source_id> --license <kind> --note "..."
    studium browse chunks --subject <slug> --concept <slug>
    studium queue list [--severity N] [--source <id>]
    studium queue show <queue_id>
    studium queue resolve <queue_id> --note "..."
    studium queue dismiss <queue_id> --note "..."
    studium queue escalate <queue_id> --severity N

CLI-primary rather than web-primary for MVP (§12.2), because it is the surface
that matches how the work is actually done: the reviewer is already in a
terminal and an editor, authoring YAML. A web UI that required leaving that
loop to click through a queue would be slower than the thing it replaced. §18
open question 3 is watching for the point where this stops being true.

**Every mutating command confirms before it writes.** Imports show a diff and
wait (§9.2 step 7). ``--yes`` skips the prompt for scripted use; there is no
way to skip it accidentally.

argparse rather than click or typer: this is the only CLI in the backend and it
is not worth a dependency. If a second one appears, revisit.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from studium.db import SessionLocal
from studium.models.identity import SYSTEM_USER_ID

from . import authoring, licensing, pipeline, publish
from . import queue as review_queue
from .authoring import AuthoringError


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "handler", None):
        parser.print_help()
        return 2
    try:
        return int(args.handler(args) or 0)
    except AuthoringError as exc:
        # The expected failure: a YAML file the author has to fix. Printed as
        # the list it is, with no traceback -- a stack trace here says nothing
        # about which line of which file is wrong.
        print(f"\n{exc}\n", file=sys.stderr)
        return 1
    except (LookupError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="studium", description=__doc__)
    subparsers = parser.add_subparsers(dest="command")

    _add_ingest(subparsers)
    _add_publish(subparsers)
    _add_sources(subparsers)
    _add_browse(subparsers)
    _add_queue(subparsers)
    return parser


def _add_ingest(subparsers: Any) -> None:
    ingest = subparsers.add_parser("ingest", help="import content")
    kinds = ingest.add_subparsers(dest="kind")

    pdf = kinds.add_parser("pdf", help="upload and run a PDF through the pipeline")
    pdf.add_argument("path", type=Path)
    pdf.add_argument("--subject", required=True, help="subject slug")
    pdf.add_argument("--extractor", default=None)
    pdf.add_argument("--title", default=None)
    pdf.add_argument(
        "--no-embed",
        action="store_true",
        help="stop after chunking; embedding costs money per token",
    )
    pdf.set_defaults(handler=_ingest_pdf)

    graph = kinds.add_parser("graph", help="import a concept graph from YAML")
    graph.add_argument("directory", type=Path)
    graph.add_argument("--yes", action="store_true", help="skip the confirmation")
    graph.set_defaults(handler=_ingest_graph)

    rubrics = kinds.add_parser("rubrics", help="import rubric criteria from YAML")
    rubrics.add_argument("directory", type=Path)
    rubrics.add_argument("--subject", required=True)
    rubrics.add_argument("--yes", action="store_true")
    rubrics.set_defaults(handler=_ingest_rubrics)

    sources = kinds.add_parser("sources", help="import concept-source links")
    sources.add_argument("directory", type=Path)
    sources.add_argument("--yes", action="store_true")
    sources.set_defaults(handler=_ingest_sources)


def _add_publish(subparsers: Any) -> None:
    parser = subparsers.add_parser("publish", help="publish authored content")
    kinds = parser.add_subparsers(dest="kind")
    subject = kinds.add_parser("subject", help="make a subject learner-visible")
    subject.add_argument("slug")
    subject.add_argument(
        "--check",
        action="store_true",
        help="run the validations and report, without publishing",
    )
    subject.set_defaults(handler=_publish_subject)


def _add_sources(subparsers: Any) -> None:
    parser = subparsers.add_parser("sources", help="inspect and classify sources")
    kinds = parser.add_subparsers(dest="kind")

    listing = kinds.add_parser("list")
    listing.add_argument("--subject", default=None)
    listing.add_argument(
        "--unclassified",
        action="store_true",
        help="only sources still awaiting a license determination",
    )
    listing.set_defaults(handler=_sources_list)

    show = kinds.add_parser("show")
    show.add_argument("source_id", type=uuid.UUID)
    show.set_defaults(handler=_sources_show)

    classify = kinds.add_parser("classify")
    classify.add_argument("source_id", type=uuid.UUID)
    classify.add_argument("--license", required=True, choices=licensing.LICENSE_KINDS)
    classify.add_argument(
        "--note",
        required=True,
        help="the basis for the determination; a conclusion without one is a guess",
    )
    classify.set_defaults(handler=_sources_classify)


def _add_browse(subparsers: Any) -> None:
    parser = subparsers.add_parser("browse", help="read the corpus while authoring")
    kinds = parser.add_subparsers(dest="kind")
    chunks = kinds.add_parser("chunks", help="candidate chunks for a concept (§10.3)")
    chunks.add_argument("--subject", required=True)
    chunks.add_argument("--concept", required=True)
    chunks.add_argument("--limit", type=int, default=10)
    chunks.set_defaults(handler=_browse_chunks)


def _add_queue(subparsers: Any) -> None:
    parser = subparsers.add_parser("queue", help="the ingestion review queue")
    kinds = parser.add_subparsers(dest="kind")

    listing = kinds.add_parser("list")
    listing.add_argument("--severity", type=int, choices=(1, 2, 3), default=None)
    listing.add_argument("--source", type=uuid.UUID, default=None)
    listing.add_argument("--limit", type=int, default=50)
    listing.set_defaults(handler=_queue_list)

    show = kinds.add_parser("show")
    show.add_argument("queue_id", type=uuid.UUID)
    show.set_defaults(handler=_queue_show)

    for name in ("resolve", "dismiss"):
        action = kinds.add_parser(name)
        action.add_argument("queue_id", type=uuid.UUID)
        action.add_argument("--note", required=True)
        action.set_defaults(handler=_queue_close, status=f"{name}d".replace("dismissd", "dismissed"))

    escalate = kinds.add_parser("escalate")
    escalate.add_argument("queue_id", type=uuid.UUID)
    escalate.add_argument("--severity", type=int, required=True, choices=(1, 2, 3))
    escalate.set_defaults(handler=_queue_escalate)


# --- handlers --------------------------------------------------------------


def _ingest_pdf(args: argparse.Namespace) -> int:
    data = args.path.read_bytes()

    with SessionLocal() as session:
        subject_id = _subject_id(session, args.subject)
        source_id = pipeline.upload(
            session,
            data=data,
            filename=args.path.name,
            subject_id=subject_id,
            # §13.2: not a default buried in the signature. A CLI import is
            # genuinely the system acting, and saying so explicitly here is
            # what distinguishes it from a request that lost its user.
            uploaded_by=SYSTEM_USER_ID,
            title=args.title,
        )
        session.commit()

    print(f"source {source_id} created (draft, {licensing.HONEST_DEFAULT})")

    results = asyncio.run(
        pipeline.run_pipeline(
            source_id, extractor_name=args.extractor, embed=not args.no_embed
        )
    )
    for result in results:
        marker = "ok  " if result.ok else "FAIL"
        print(f"  {marker} {result.kind}: {result.detail}")
        for flagged in result.flagged:
            print(f"       queued for review: {flagged}")

    print(
        f"\nsource {source_id} is still draft. Classify its license "
        f"(`studium sources classify`) and publish the subject when the "
        f"authoring is done."
    )
    return 0 if all(r.ok for r in results) else 1


def _ingest_graph(args: argparse.Namespace) -> int:
    graph = authoring.parse_graph(authoring.yaml_files(args.directory))

    with SessionLocal() as session:
        diff = authoring.diff_graph(session, graph)
        print(f"subject {graph.slug} ({graph.title})")
        print(diff.render())

        if diff.is_empty and not diff.warnings:
            print("\nnothing to do")
            return 0
        if not _confirm(args.yes, "apply this graph?"):
            return 1

        result = authoring.apply_graph(session, graph=graph, diff=diff)
        session.commit()

    print(
        f"\nimported {result.counts['concepts']} concepts and "
        f"{result.counts['edges']} edges into {graph.slug} (draft)"
    )
    for flagged in result.flagged:
        print(f"  logged warning for review: {flagged}")
    return 0


def _ingest_rubrics(args: argparse.Namespace) -> int:
    criteria = authoring.parse_rubrics(authoring.yaml_files(args.directory))
    print(f"{len(criteria)} rubric criteria for {args.subject}")
    for criterion in criteria:
        print(f"  + {criterion['concept']}/{criterion['slug']} (weight {criterion['weight']})")
    if not _confirm(args.yes, "apply these rubrics?"):
        return 1

    with SessionLocal() as session:
        authoring.apply_rubrics(
            session, subject_slug=args.subject, criteria=criteria
        )
        session.commit()
    print(f"\nimported {len(criteria)} criteria (draft)")
    return 0


def _ingest_sources(args: argparse.Namespace) -> int:
    documents = authoring.parse_concept_sources(authoring.yaml_files(args.directory))

    with SessionLocal() as session:
        plans = []
        for document in documents:
            source_id, links = authoring.resolve_links(session, document=document)
            plans.append((document, source_id, links))
            print(f"{document['source']} -> {document['subject']}")
            for link in links:
                print(
                    f"  + {link.concept_slug} as {link.role} "
                    f"({len(link.chunk_ids)} chunk(s))"
                )

        if not _confirm(args.yes, "apply these links?"):
            return 1

        total = 0
        for document, source_id, links in plans:
            result = authoring.apply_concept_sources(
                session,
                subject_slug=document["subject"],
                source_id=source_id,
                links=links,
            )
            total += result.counts["links"]
        session.commit()

    print(f"\nimported {total} concept-source link(s)")
    return 0


def _publish_subject(args: argparse.Namespace) -> int:
    with SessionLocal() as session:
        if args.check:
            _, checks = publish.validate(session, args.slug)
            result = publish.PublishResult(
                subject_slug=args.slug, published=False, checks=tuple(checks)
            )
            print(result.render())
            return 0 if not result.failures else 1

        result = publish.publish_subject(session, subject_slug=args.slug)
        session.commit()

    print(result.render())
    if result.published:
        print(f"\n{args.slug} is active")
        return 0

    print(
        f"\npublish blocked by {len(result.failures)} check(s). "
        f"Fix them and run again."
    )
    return 1


def _sources_list(args: argparse.Namespace) -> int:
    with SessionLocal() as session:
        subject_id = _subject_id(session, args.subject) if args.subject else None
        if args.unclassified:
            states = licensing.unclassified(session, subject_id=subject_id)
        else:
            states = _all_sources(session, subject_id)

    if not states:
        print("no sources")
        return 0
    for state in states:
        mark = " " if state.is_classified else "!"
        print(f"{mark} {state.source_id}  {state.status:8} {state.license:18} {state.title}")
    if any(not s.is_classified for s in states):
        print("\n! = no license determination yet; publish is blocked until there is one")
    return 0


def _sources_show(args: argparse.Namespace) -> int:
    with SessionLocal() as session:
        state = licensing.state(session, args.source_id)
        if state is None:
            raise LookupError(f"no source {args.source_id}")
        counts = _chunk_counts(session, args.source_id)
        extraction = _extraction_info(session, args.source_id)

    print(f"{state.title}")
    print(f"  id         {state.source_id}")
    print(f"  status     {state.status}")
    print(f"  license    {state.license}"
          f"{'' if state.is_classified else '   (UNCLASSIFIED)'}")
    print(f"  notes      {state.license_notes or '-'}")
    print(f"  extractor  {extraction.get('extractor_version') or '-'}")
    print(f"  normalizer {extraction.get('normalizer_version') or '-'}")
    print(f"  chunks     {counts['live']} live, {counts['superseded']} superseded")
    return 0


def _sources_classify(args: argparse.Namespace) -> int:
    with SessionLocal() as session:
        state = licensing.classify(
            session,
            source_id=args.source_id,
            license=args.license,
            note=args.note,
        )
        session.commit()
    print(f"{state.title}: {state.license}")
    print(f"  basis: {state.license_notes}")
    return 0


def _browse_chunks(args: argparse.Namespace) -> int:
    """§10.3's authoring helper: show candidate chunks for a concept.

    Keyword-ranked against the concept's own title and description rather than
    vector-ranked. The vector path would be better and costs an embedding call
    per invocation, which is the wrong trade for a command the reviewer runs
    dozens of times in an afternoon while writing YAML. The v1.1 web UI is
    where similarity scoring earns its cost.
    """
    from sqlalchemy import text as sql

    with SessionLocal() as session:
        rows = session.execute(
            sql(
                """
                WITH target AS (
                    SELECT c.id, c.title || ' ' || c.short_description AS query
                      FROM concepts c
                      JOIN subjects s ON s.id = c.subject_id
                     WHERE s.slug = :subject AND c.slug = :concept
                )
                SELECT sc.id, sc.chunk_index, sc.page_start, sc.chunk_type,
                       LEFT(sc.text, 220) AS preview, src.title AS source_title,
                       ts_rank_cd(sc.tsvector_text,
                                  plainto_tsquery('english', t.query)) AS score
                  FROM target t
                  JOIN subjects s   ON s.slug = :subject
                  JOIN sources src  ON src.subject_id = s.id AND src.deleted_at IS NULL
                  JOIN source_chunks sc ON sc.source_id = src.id
                 WHERE sc.superseded_at IS NULL
                   AND sc.chunk_type NOT IN ('heading', 'reference')
                   AND sc.tsvector_text @@ plainto_tsquery('english', t.query)
                 ORDER BY score DESC, sc.chunk_index
                 LIMIT :limit
                """
            ),
            {"subject": args.subject, "concept": args.concept, "limit": args.limit},
        ).all()

    if not rows:
        raise LookupError(
            f"no chunks matched {args.concept!r} in {args.subject!r}; the "
            f"source may not be chunked yet"
        )
    for row in rows:
        print(f"\n{row.id}  p{row.page_start or '?'}  [{row.chunk_type}]  {row.source_title}")
        print(f"  {row.preview.strip()}...")
    print(
        "\nPaste an id into a concept-source YAML as "
        "`- by_chunk_ids: [\"...\"]`, or use a page range."
    )
    return 0


def _queue_list(args: argparse.Namespace) -> int:
    with SessionLocal() as session:
        items = review_queue.pending(
            session, severity=args.severity, source_id=args.source, limit=args.limit
        )
        counts = review_queue.depth(session)

    if not items:
        print("queue is empty")
        return 0
    for item in items:
        print(f"[{item.severity}] {item.id}  {item.flag_source:24} {item.target}")
        print(f"      {item.reason}")
    print("\npending by kind: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    return 0


def _queue_show(args: argparse.Namespace) -> int:
    with SessionLocal() as session:
        item = review_queue.get(session, args.queue_id)
    if item is None:
        raise LookupError(f"no queue item {args.queue_id}")

    print(f"{item.flag_source}  severity {item.severity}  {item.status}")
    print(f"  target   {item.target}")
    print(f"  reason   {item.reason}")
    if item.resolution_note:
        print(f"  note     {item.resolution_note}")
    for key, value in (item.payload or {}).items():
        print(f"  {key:8} {value}")
    return 0


def _queue_close(args: argparse.Namespace) -> int:
    with SessionLocal() as session:
        changed = review_queue.resolve(
            session, args.queue_id, note=args.note, status=args.status
        )
        session.commit()
    if not changed:
        print(f"{args.queue_id} was already {args.status}")
        return 1
    print(f"{args.queue_id} {args.status}")
    return 0


def _queue_escalate(args: argparse.Namespace) -> int:
    with SessionLocal() as session:
        changed = review_queue.escalate(session, args.queue_id, severity=args.severity)
        session.commit()
    if not changed:
        print(
            f"{args.queue_id} is already at severity {args.severity} or higher; "
            f"escalation only raises (§12.3)"
        )
        return 1
    print(f"{args.queue_id} escalated to severity {args.severity}")
    return 0


# --- helpers ---------------------------------------------------------------


def _confirm(skip: bool, question: str) -> bool:
    if skip:
        return True
    if not sys.stdin.isatty():
        print(
            f"{question} -- refusing to guess on a non-interactive stdin; "
            f"pass --yes to apply",
            file=sys.stderr,
        )
        return False
    answer = input(f"\n{question} [y/N] ").strip().lower()
    return answer in {"y", "yes"}


def _subject_id(session: Session, slug: str) -> uuid.UUID:
    from sqlalchemy import text as sql

    row = session.execute(
        sql("SELECT id FROM subjects WHERE slug = :slug AND deleted_at IS NULL"),
        {"slug": slug},
    ).one_or_none()
    if row is None:
        raise LookupError(
            f"no subject {slug!r}; import its graph first "
            f"(`studium ingest graph`)"
        )
    return row.id


def _all_sources(session: Session, subject_id: uuid.UUID | None) -> list[Any]:
    from sqlalchemy import text as sql

    rows = session.execute(
        sql(
            """
            SELECT id, title, license, license_notes, status
              FROM sources
             WHERE deleted_at IS NULL
               AND (:no_subject OR subject_id = :subject_id)
             ORDER BY created_at
            """
        ),
        {"no_subject": subject_id is None, "subject_id": subject_id},
    ).all()
    return [
        licensing.LicenseState(
            source_id=row.id,
            title=row.title,
            license=row.license,
            license_notes=row.license_notes,
            status=row.status,
        )
        for row in rows
    ]


def _chunk_counts(session: Session, source_id: uuid.UUID) -> dict[str, int]:
    from sqlalchemy import text as sql

    row = session.execute(
        sql(
            """
            SELECT COUNT(*) FILTER (WHERE superseded_at IS NULL)     AS live,
                   COUNT(*) FILTER (WHERE superseded_at IS NOT NULL) AS superseded
              FROM source_chunks WHERE source_id = :id
            """
        ),
        {"id": source_id},
    ).one()
    return {"live": int(row.live), "superseded": int(row.superseded)}


def _extraction_info(session: Session, source_id: uuid.UUID) -> dict[str, Any]:
    from sqlalchemy import text as sql

    row = session.execute(
        sql(
            "SELECT extractor_version, normalizer_version FROM sources WHERE id = :id"
        ),
        {"id": source_id},
    ).one_or_none()
    if row is None:
        return {}
    return {
        "extractor_version": row.extractor_version,
        "normalizer_version": row.normalizer_version,
    }


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
