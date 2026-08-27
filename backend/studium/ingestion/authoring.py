"""YAML authoring for the concept graph, rubrics and concept-sources (§9, §10).

Three workflows, one shape: parse YAML, validate it, diff it against what is in
the database, show the diff, apply on confirmation. They are here together
because the diff-and-confirm machinery is the same for all three and the
validation rules are what differ.

**Why YAML and not a web UI** (§9.1). The graph is diff-able in version
control, scriptable in bulk, and needs no interface built before the
pedagogical work can start. For MVP the graph is authored once and edited
rarely, which is exactly the case a file beats a form. A web UI becomes right
when ongoing edits are the primary mode -- §18 open question 3 is watching for
that.

**Why authoring is separate from the pipeline** (§3). Ingesting a source is
automatic and finishes in minutes. Authoring against it is domain-expert work
that happens when someone has time to sit with the material -- possibly weeks
later. Nothing in the pipeline holds state open waiting for it, and nothing
here assumes the pipeline just ran.

**Validation is split into errors and warnings, and the split is load-bearing.**
A cycle in the prerequisite graph is an error because the Curator's sequencing
walks those edges and would not terminate. An orphan concept is a warning
because it is usually a concept the author has not connected *yet*, and
blocking the import would mean the graph could not be built incrementally.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import text as sql
from sqlalchemy.orm import Session

from .provenance import ingestion_writer
from .queue import flag

#: §9.1 concept depth band, matching the data layer's CHECK.
MIN_DEPTH, MAX_DEPTH = 1, 5
#: Rubric weights the schema accepts.
VALID_WEIGHTS = (1, 2, 3)

CONCEPT_EDGE_KINDS = (
    "prerequisite",
    "dependency",
    "generalization",
    "application",
    "related",
)
CONCEPT_SOURCE_ROLES = (
    "canonical_definition",
    "primary_exposition",
    "worked_example",
    "exercise",
    "historical",
    "alternative_stance",
)


#: The data layer's slug format, as a CHECK on both ``subjects`` and
#: ``concepts``: lowercase alphanumerics and hyphens, 2-81 characters, not
#: starting with a hyphen. Validated here as well as there so a bad slug is
#: reported alongside the file's other problems with the offending value
#: quoted, rather than as an IntegrityError naming a constraint.
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,80}$")


def _is_slug(value: str) -> bool:
    return bool(_SLUG.match(value))


class AuthoringError(ValueError):
    """A YAML file cannot be imported. Carries every problem, not just the first.

    Reporting one error per run turns fixing a 60-concept graph into 60 edit-run
    cycles. The author wants the whole list.
    """

    def __init__(self, problems: Sequence[str], *, path: Path | None = None) -> None:
        self.problems = list(problems)
        self.path = path
        where = f"{path}: " if path else ""
        joined = "\n  - ".join(self.problems)
        super().__init__(f"{where}{len(self.problems)} problem(s):\n  - {joined}")


@dataclass(frozen=True, slots=True)
class Diff:
    """What an import would change. Shown before anything is written (§9.2 step 7)."""

    added: tuple[str, ...] = ()
    modified: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.modified or self.removed)

    def render(self) -> str:
        lines: list[str] = []
        for label, items in (
            ("+", self.added),
            ("~", self.modified),
            ("-", self.removed),
        ):
            lines.extend(f"  {label} {item}" for item in items)
        lines.extend(f"  ! {warning}" for warning in self.warnings)
        return "\n".join(lines) if lines else "  (no changes)"


@dataclass(frozen=True, slots=True)
class ImportResult:
    diff: Diff
    applied: bool = False
    flagged: tuple[uuid.UUID, ...] = ()
    subject_id: uuid.UUID | None = None
    counts: dict[str, int] = field(default_factory=dict)


def load_yaml(path: Path) -> dict[str, Any]:
    """Parse one YAML file, with a message that names the file and the line.

    ``yaml.safe_load``, never ``load``: these files come from a content
    directory that a reviewer edits, and full ``load`` can construct arbitrary
    Python objects. There is no reason an authoring file needs that, and one
    day the directory will contain something someone else wrote.
    """
    import yaml

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AuthoringError([f"cannot read: {exc}"], path=path) from exc

    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise AuthoringError([f"invalid YAML: {exc}"], path=path) from exc

    if data is None:
        raise AuthoringError(["file is empty"], path=path)
    if not isinstance(data, dict):
        raise AuthoringError(
            [f"expected a mapping at the top level, found {type(data).__name__}"],
            path=path,
        )
    return data


def yaml_files(directory: Path) -> list[Path]:
    """Every ``.yaml``/``.yml`` file under ``directory``, in a stable order.

    Sorted because an import's diff is read by a human and reviewed in a
    terminal: filesystem order varies between machines, and a diff that
    reorders itself between runs cannot be compared to the last one.
    """
    if not directory.exists():
        raise AuthoringError([f"no such directory: {directory}"])
    return sorted(
        [*directory.glob("*.yaml"), *directory.glob("*.yml")], key=lambda p: p.name
    )


# --- §9 concept graph ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParsedGraph:
    slug: str
    title: str
    version: int
    short_description: str
    long_description: str
    concepts: tuple[dict[str, Any], ...]
    edges: tuple[dict[str, Any], ...]


def parse_graph(paths: Sequence[Path]) -> ParsedGraph:
    """Parse and validate graph YAML (§9.2 steps 1-5).

    Several files may describe one subject -- §9.1 allows splitting a large
    graph by module. Exactly one of them carries the ``subject:`` block; the
    rest contribute concepts and edges. Requiring it in every file would make
    the split files disagree about the subject's own metadata, and allowing it
    in none would leave the subject undefined.
    """
    problems: list[str] = []
    subject: dict[str, Any] | None = None
    subject_path: Path | None = None
    concepts: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []

    for path in paths:
        data = load_yaml(path)
        if "subject" in data:
            if subject is not None:
                problems.append(
                    f"{path.name}: a second 'subject:' block "
                    f"(already defined in {subject_path.name if subject_path else '?'})"
                )
            else:
                subject, subject_path = data["subject"], path
        concepts.extend(_as_list(data.get("concepts"), path, "concepts", problems))
        edges.extend(_as_list(data.get("edges"), path, "edges", problems))

    if subject is None:
        problems.append("no 'subject:' block in any file")
    if problems:
        raise AuthoringError(problems)
    assert subject is not None

    problems.extend(_validate_subject(subject))
    problems.extend(_validate_concepts(concepts))
    problems.extend(_validate_edges(edges, {c.get("slug") for c in concepts}))
    if problems:
        raise AuthoringError(problems)

    return ParsedGraph(
        slug=str(subject["slug"]),
        title=str(subject.get("title") or subject["slug"]),
        version=int(subject.get("version", 1)),
        short_description=str(subject.get("short_description") or "").strip(),
        long_description=str(subject.get("long_description") or "").strip(),
        concepts=tuple(concepts),
        edges=tuple(edges),
    )


def _as_list(
    value: Any, path: Path, key: str, problems: list[str]
) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        problems.append(f"{path.name}: '{key}:' must be a list")
        return []
    out: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            problems.append(f"{path.name}: {key}[{index}] must be a mapping")
            continue
        out.append(item)
    return out


def _validate_subject(subject: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if not subject.get("slug"):
        problems.append("subject has no slug")
    elif not _is_slug(str(subject["slug"])):
        problems.append(f"subject slug {subject['slug']!r} is not a valid slug")
    return problems


def _validate_concepts(concepts: Sequence[dict[str, Any]]) -> list[str]:
    """§9.2 step 2: unique slugs, valid depths, well-formed rows."""
    problems: list[str] = []
    seen: dict[str, int] = {}

    for index, concept in enumerate(concepts):
        slug = concept.get("slug")
        if not slug:
            problems.append(f"concepts[{index}] has no slug")
            continue
        slug = str(slug)
        if not _is_slug(slug):
            problems.append(f"concept slug {slug!r} is not a valid slug")
        if slug in seen:
            problems.append(
                f"duplicate concept slug {slug!r} "
                f"(also at concepts[{seen[slug]}])"
            )
        seen[slug] = index

        if not concept.get("title"):
            problems.append(f"concept {slug!r} has no title")

        depth = concept.get("depth", 1)
        if not isinstance(depth, int) or not MIN_DEPTH <= depth <= MAX_DEPTH:
            problems.append(
                f"concept {slug!r} has depth {depth!r}; "
                f"expected an integer {MIN_DEPTH}-{MAX_DEPTH}"
            )

        minutes = concept.get("estimated_minutes", 30)
        if not isinstance(minutes, int) or minutes <= 0:
            problems.append(
                f"concept {slug!r} has estimated_minutes {minutes!r}; "
                f"expected a positive integer"
            )

    return problems


def _validate_edges(
    edges: Sequence[dict[str, Any]], concept_slugs: set[Any]
) -> list[str]:
    """§9.2 steps 2-3: no self-edges, no duplicates, no dangling ends, no cycles."""
    problems: list[str] = []
    seen: set[tuple[str, str, str]] = set()
    prerequisites: dict[str, list[str]] = {}

    for index, edge in enumerate(edges):
        source, target = edge.get("from"), edge.get("to")
        kind = str(edge.get("kind") or "prerequisite")

        if not source or not target:
            problems.append(f"edges[{index}] needs both 'from' and 'to'")
            continue
        source, target = str(source), str(target)

        if kind not in CONCEPT_EDGE_KINDS:
            problems.append(
                f"edge {source} -> {target} has kind {kind!r}; "
                f"expected one of {', '.join(CONCEPT_EDGE_KINDS)}"
            )
        if source == target:
            problems.append(f"self-edge on {source!r}")
            continue
        for end, label in ((source, "from"), (target, "to")):
            if end not in concept_slugs:
                problems.append(
                    f"edge {source} -> {target} has {label} {end!r}, "
                    f"which is not a concept in this graph"
                )

        triple = (source, target, kind)
        if triple in seen:
            problems.append(f"duplicate edge {source} -> {target} ({kind})")
        seen.add(triple)

        if kind == "prerequisite":
            prerequisites.setdefault(source, []).append(target)

    cycle = _find_cycle(prerequisites)
    if cycle:
        # A hard error, not a warning. The Curator sequences a subject by
        # walking prerequisite edges, and a cycle means there is no order in
        # which the concepts can be taught -- the walk does not terminate.
        problems.append(
            "prerequisite cycle: " + " -> ".join(cycle)
        )

    return problems


def _find_cycle(adjacency: dict[str, list[str]]) -> list[str] | None:
    """The first prerequisite cycle, as a path. Iterative depth-first search.

    Iterative rather than recursive: a deep chain in a large subject would hit
    Python's recursion limit, and "maximum recursion depth exceeded" is a
    terrible way to learn that your graph has 1,100 concepts in a line.
    """
    WHITE, GREY, BLACK = 0, 1, 2
    colour: dict[str, int] = {}

    for root in sorted(adjacency):
        if colour.get(root, WHITE) != WHITE:
            continue
        stack: list[tuple[str, int]] = [(root, 0)]
        path: list[str] = [root]
        colour[root] = GREY

        while stack:
            node, cursor = stack[-1]
            children = adjacency.get(node, ())
            if cursor >= len(children):
                colour[node] = BLACK
                stack.pop()
                path.pop()
                continue
            stack[-1] = (node, cursor + 1)
            child = children[cursor]
            state = colour.get(child, WHITE)
            if state == GREY:
                start = path.index(child)
                return [*path[start:], child]
            if state == WHITE:
                colour[child] = GREY
                stack.append((child, 0))
                path.append(child)
    return None


def graph_warnings(graph: ParsedGraph) -> list[str]:
    """§9.2 steps 4-5. Real problems that must not block an incremental build."""
    warnings: list[str] = []
    connected: set[str] = set()
    incoming: dict[str, list[str]] = {}

    for edge in graph.edges:
        source, target = str(edge["from"]), str(edge["to"])
        connected.update((source, target))
        if str(edge.get("kind") or "prerequisite") == "prerequisite":
            incoming.setdefault(target, []).append(source)

    depths = {str(c["slug"]): int(c.get("depth", 1)) for c in graph.concepts}

    for concept in graph.concepts:
        slug = str(concept["slug"])
        if slug not in connected:
            warnings.append(
                f"concept {slug!r} has no edges; it is unreachable from any "
                f"learning path"
            )
        depth = depths.get(slug, 1)
        if depth >= 3 and not any(
            depths.get(parent, 1) < depth for parent in incoming.get(slug, ())
        ):
            warnings.append(
                f"concept {slug!r} is depth {depth} with no shallower "
                f"prerequisite; a learner can reach it without the groundwork"
            )

    return warnings


def diff_graph(session: Session, graph: ParsedGraph) -> Diff:
    """What importing ``graph`` would change (§9.2 step 6)."""
    existing = session.execute(
        sql(
            """
            SELECT c.slug, c.title, c.depth, c.is_load_bearing,
                   c.estimated_minutes, c.module_slug
              FROM concepts c
              JOIN subjects s ON s.id = c.subject_id
             WHERE s.slug = :slug AND s.deleted_at IS NULL
            """
        ),
        {"slug": graph.slug},
    ).all()
    by_slug = {row.slug: row for row in existing}

    added: list[str] = []
    modified: list[str] = []
    for concept in graph.concepts:
        slug = str(concept["slug"])
        current = by_slug.get(slug)
        if current is None:
            added.append(f"concept {slug}")
            continue
        changes = _concept_changes(current, concept)
        if changes:
            modified.append(f"concept {slug} ({', '.join(changes)})")

    incoming = {str(c["slug"]) for c in graph.concepts}
    removed = [
        f"concept {slug} (in the database, absent from the files)"
        for slug in sorted(by_slug)
        if slug not in incoming
    ]

    return Diff(
        added=tuple(added),
        modified=tuple(modified),
        removed=tuple(removed),
        warnings=tuple(graph_warnings(graph)),
    )


def _concept_changes(current: Any, incoming: dict[str, Any]) -> list[str]:
    changes: list[str] = []
    comparisons = (
        ("title", current.title, incoming.get("title")),
        ("depth", int(current.depth), int(incoming.get("depth", 1))),
        (
            "is_load_bearing",
            bool(current.is_load_bearing),
            bool(incoming.get("is_load_bearing", False)),
        ),
        (
            "estimated_minutes",
            int(current.estimated_minutes),
            int(incoming.get("estimated_minutes", 30)),
        ),
        ("module", current.module_slug, incoming.get("module")),
    )
    for name, was, now in comparisons:
        if now is not None and was != now:
            changes.append(f"{name}: {was!r} -> {now!r}")
    return changes


@ingestion_writer
def apply_graph(session: Session, *, graph: ParsedGraph, diff: Diff) -> ImportResult:
    """Write the graph in one transaction (§9.2 step 8).

    Concepts are upserted, never deleted. A concept absent from the files is
    reported in the diff and left alone: ``concepts`` cascades to
    ``concept_mastery``, so deleting one erases every learner's recorded
    progress on it, and a slug typo'd out of a YAML file is not consent for
    that. Retiring a concept is a deliberate act with its own workflow.
    """
    subject_id = _upsert_subject(session, graph=graph, diff=diff)

    concept_ids: dict[str, uuid.UUID] = {}
    for position, concept in enumerate(graph.concepts):
        concept_ids[str(concept["slug"])] = _upsert_concept(
            session, subject_id=subject_id, concept=concept, position=position
        )

    # Edges are replaced wholesale rather than diffed. They carry no learner
    # state -- nothing references concept_edges -- so a delete-and-reinsert is
    # safe, and it is the only way a *removed* edge actually goes away.
    session.execute(
        sql("DELETE FROM concept_edges WHERE subject_id = :subject_id"),
        {"subject_id": subject_id},
    )
    for edge in graph.edges:
        session.execute(
            sql(
                """
                INSERT INTO concept_edges
                    (subject_id, from_concept_id, to_concept_id, kind, note)
                VALUES (:subject_id, :from_id, :to_id,
                        CAST(:kind AS concept_edge_kind), :note)
                ON CONFLICT DO NOTHING
                """
            ),
            {
                "subject_id": subject_id,
                "from_id": concept_ids[str(edge["from"])],
                "to_id": concept_ids[str(edge["to"])],
                "kind": str(edge.get("kind") or "prerequisite"),
                "note": edge.get("note"),
            },
        )

    session.execute(
        sql("SELECT refresh_subject_metadata(:subject_id)"), {"subject_id": subject_id}
    )

    flagged = tuple(
        flag(
            session,
            flag_source="graph_validation_error",
            subject_id=subject_id,
            reason=warning,
            severity=1,
            payload={"stage": "graph_import", "accepted": True},
        )
        for warning in diff.warnings
    )

    return ImportResult(
        diff=diff,
        applied=True,
        flagged=flagged,
        subject_id=subject_id,
        counts={"concepts": len(graph.concepts), "edges": len(graph.edges)},
    )


def _upsert_subject(session: Session, *, graph: ParsedGraph, diff: Diff) -> uuid.UUID:
    """Create or update the subject row.

    §9.2 step 8: the version bumps only for a substantial change -- concepts
    added or removed. A point edit to a title or an estimate does not, because
    ``subjects.version`` is what tells a learner their path was reshaped under
    them, and firing that for a typo fix trains everyone to ignore it.
    """
    substantial = bool(diff.added or diff.removed)
    row = session.execute(
        sql(
            """
            INSERT INTO subjects
                (slug, title, short_description, long_description, version, status)
            VALUES (:slug, :title, :short_description, :long_description, :version,
                    'draft')
            ON CONFLICT (slug) DO UPDATE
               SET title = EXCLUDED.title,
                   short_description = EXCLUDED.short_description,
                   long_description = EXCLUDED.long_description,
                   version = subjects.version + CASE WHEN :substantial THEN 1 ELSE 0 END
            RETURNING id
            """
        ),
        {
            "slug": graph.slug,
            "title": graph.title,
            "short_description": graph.short_description,
            "long_description": graph.long_description,
            "version": graph.version,
            "substantial": substantial,
        },
    ).scalar_one()
    return row


def _upsert_concept(
    session: Session,
    *,
    subject_id: uuid.UUID,
    concept: dict[str, Any],
    position: int,
) -> uuid.UUID:
    return session.execute(
        sql(
            """
            INSERT INTO concepts
                (subject_id, slug, title, short_description, long_description,
                 depth, is_load_bearing, estimated_minutes, position, module_slug)
            VALUES
                (:subject_id, :slug, :title, :short_description, :long_description,
                 :depth, :is_load_bearing, :estimated_minutes, :position, :module_slug)
            ON CONFLICT (subject_id, slug) DO UPDATE
               SET title = EXCLUDED.title,
                   short_description = EXCLUDED.short_description,
                   long_description = EXCLUDED.long_description,
                   depth = EXCLUDED.depth,
                   is_load_bearing = EXCLUDED.is_load_bearing,
                   estimated_minutes = EXCLUDED.estimated_minutes,
                   position = EXCLUDED.position,
                   module_slug = EXCLUDED.module_slug
            RETURNING id
            """
        ),
        {
            "subject_id": subject_id,
            "slug": str(concept["slug"]),
            "title": str(concept["title"]),
            "short_description": str(concept.get("short_description") or "").strip(),
            "long_description": str(concept.get("long_description") or "").strip(),
            "depth": int(concept.get("depth", 1)),
            "is_load_bearing": bool(concept.get("is_load_bearing", False)),
            "estimated_minutes": int(concept.get("estimated_minutes", 30)),
            "position": position,
            "module_slug": concept.get("module"),
        },
    ).scalar_one()


# --- rubric criteria -------------------------------------------------------


def parse_rubrics(paths: Sequence[Path]) -> list[dict[str, Any]]:
    """Parse rubric YAML into flat criterion records.

        subject: lambda-calculus
        criteria:
          - concept: beta-reduction
            slug: identifies-redex
            prompt: "Can the learner identify a redex ...?"
            key_points: [...]
            weight: 3
    """
    problems: list[str] = []
    criteria: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for path in paths:
        data = load_yaml(path)
        for index, item in enumerate(
            _as_list(data.get("criteria"), path, "criteria", problems)
        ):
            concept = item.get("concept")
            slug = item.get("slug")
            if not concept or not slug:
                problems.append(
                    f"{path.name}: criteria[{index}] needs both 'concept' and 'slug'"
                )
                continue
            key = (str(concept), str(slug))
            if key in seen:
                problems.append(
                    f"duplicate rubric criterion {slug!r} on concept {concept!r}"
                )
            seen.add(key)

            if not item.get("prompt"):
                problems.append(f"rubric {slug!r} has no prompt")

            weight = item.get("weight", 2)
            if weight not in VALID_WEIGHTS:
                problems.append(
                    f"rubric {slug!r} has weight {weight!r}; "
                    f"expected one of {VALID_WEIGHTS}"
                )

            key_points = item.get("key_points") or []
            if not isinstance(key_points, list):
                problems.append(f"rubric {slug!r}: key_points must be a list")
                key_points = []

            criteria.append(
                {
                    "concept": str(concept),
                    "slug": str(slug),
                    "prompt": str(item.get("prompt") or ""),
                    "key_points": key_points,
                    "weight": int(weight) if weight in VALID_WEIGHTS else 2,
                    "min_words": int(item.get("min_words", 20)),
                }
            )

    if problems:
        raise AuthoringError(problems)
    return criteria


@ingestion_writer
def apply_rubrics(
    session: Session, *, subject_slug: str, criteria: Sequence[dict[str, Any]]
) -> ImportResult:
    """Upsert rubric criteria (§14: a missing concept is a hard error)."""
    import json

    concept_ids = _concept_ids(session, subject_slug)
    missing = sorted({c["concept"] for c in criteria if c["concept"] not in concept_ids})
    if missing:
        raise AuthoringError(
            [
                f"rubric references concept {slug!r}, which is not in subject "
                f"{subject_slug!r}"
                for slug in missing
            ]
        )

    added: list[str] = []
    for criterion in criteria:
        session.execute(
            sql(
                """
                INSERT INTO rubric_criteria
                    (concept_id, slug, prompt, key_points, weight, min_words, status)
                VALUES
                    (:concept_id, :slug, :prompt, CAST(:key_points AS jsonb),
                     :weight, :min_words, 'draft')
                ON CONFLICT (concept_id, slug) DO UPDATE
                   SET prompt = EXCLUDED.prompt,
                       key_points = EXCLUDED.key_points,
                       weight = EXCLUDED.weight,
                       min_words = EXCLUDED.min_words
                """
            ),
            {
                "concept_id": concept_ids[criterion["concept"]],
                "slug": criterion["slug"],
                "prompt": criterion["prompt"],
                "key_points": json.dumps(criterion["key_points"]),
                "weight": criterion["weight"],
                "min_words": criterion["min_words"],
            },
        )
        added.append(f"rubric {criterion['concept']}/{criterion['slug']}")

    return ImportResult(
        diff=Diff(added=tuple(added)),
        applied=True,
        counts={"criteria": len(criteria)},
    )


# --- §10 concept-sources ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class ResolvedLink:
    concept_slug: str
    role: str
    chunk_ids: tuple[uuid.UUID, ...]
    note: str | None = None


def parse_concept_sources(paths: Sequence[Path]) -> list[dict[str, Any]]:
    """Parse concept-source YAML into per-file link records (§10.1)."""
    problems: list[str] = []
    documents: list[dict[str, Any]] = []

    for path in paths:
        data = load_yaml(path)
        source = data.get("source")
        subject = data.get("subject")
        if not source or not subject:
            problems.append(f"{path.name}: needs both 'source:' and 'subject:'")
            continue

        links: list[dict[str, Any]] = []
        for index, link in enumerate(
            _as_list(data.get("links"), path, "links", problems)
        ):
            concept = link.get("concept")
            role = str(link.get("role") or "")
            if not concept:
                problems.append(f"{path.name}: links[{index}] has no 'concept'")
                continue
            if role not in CONCEPT_SOURCE_ROLES:
                problems.append(
                    f"{path.name}: link on {concept!r} has role {role!r}; "
                    f"expected one of {', '.join(CONCEPT_SOURCE_ROLES)}"
                )
            selectors = link.get("chunks") or []
            if not isinstance(selectors, list) or not selectors:
                problems.append(
                    f"{path.name}: link on {concept!r} has no chunk selectors"
                )
                continue
            links.append(
                {
                    "concept": str(concept),
                    "role": role,
                    "chunks": selectors,
                    "note": link.get("note"),
                }
            )

        documents.append(
            {"source": str(source), "subject": str(subject), "links": links, "path": path}
        )

    if problems:
        raise AuthoringError(problems)
    return documents


def resolve_links(
    session: Session, *, document: dict[str, Any]
) -> tuple[uuid.UUID, list[ResolvedLink]]:
    """Turn chunk selectors into concrete chunk ids (§10.2 steps 2-3).

    Every failure here is hard (§14). A selector that matches nothing, or a
    ``by_text_match`` that matches several chunks, means the author's intent is
    not recoverable -- and a concept-source link is *curated* grounding, the
    thing retrieval prefers over its own search. Guessing which chunk was meant
    would put a passage the author did not choose at the top of a lecture's
    evidence, which is worse than a failed import by a wide margin.
    """
    problems: list[str] = []
    source_id = _source_id_for(session, document["source"], document["subject"])
    if source_id is None:
        raise AuthoringError(
            [
                f"no ingested source matching {document['source']!r} in subject "
                f"{document['subject']!r}"
            ],
            path=document.get("path"),
        )

    chunks = session.execute(
        sql(
            """
            SELECT id, chunk_index, text, page_start, page_end
              FROM source_chunks
             WHERE source_id = :source_id
               AND superseded_at IS NULL
             ORDER BY chunk_index
            """
        ),
        {"source_id": source_id},
    ).all()

    resolved: list[ResolvedLink] = []
    for link in document["links"]:
        ids: list[uuid.UUID] = []
        for selector in link["chunks"]:
            try:
                ids.extend(_resolve_selector(selector, chunks))
            except AuthoringError as exc:
                problems.extend(
                    f"link on {link['concept']!r}: {problem}" for problem in exc.problems
                )
        # De-duplicated in order: two selectors on one link legitimately
        # overlap (a page range plus a text match inside it), and the array
        # column would otherwise carry the same chunk twice.
        seen: set[uuid.UUID] = set()
        unique = tuple(i for i in ids if not (i in seen or seen.add(i)))
        if not unique and not problems:
            problems.append(
                f"link on {link['concept']!r} resolved to no chunks"
            )
        resolved.append(
            ResolvedLink(
                concept_slug=link["concept"],
                role=link["role"],
                chunk_ids=unique,
                note=link.get("note"),
            )
        )

    if problems:
        raise AuthoringError(problems, path=document.get("path"))
    return source_id, resolved


def _resolve_selector(selector: Any, chunks: Sequence[Any]) -> list[uuid.UUID]:
    if not isinstance(selector, dict) or len(selector) != 1:
        raise AuthoringError(
            [f"selector {selector!r} must be a single-key mapping"]
        )
    (key, value), = selector.items()

    if key == "by_page_range":
        if not isinstance(value, list) or len(value) != 2:
            raise AuthoringError([f"by_page_range must be [start, end], got {value!r}"])
        start, end = int(value[0]), int(value[1])
        matched = [
            chunk.id
            for chunk in chunks
            if _overlaps_pages(chunk, start, end)
        ]
        if not matched:
            raise AuthoringError(
                [f"by_page_range [{start}, {end}] matched no chunks"]
            )
        return matched

    if key == "by_text_match":
        needle = str(value)
        matched = [chunk.id for chunk in chunks if needle in (chunk.text or "")]
        if not matched:
            raise AuthoringError([f"by_text_match {needle!r} matched no chunk"])
        if len(matched) > 1:
            raise AuthoringError(
                [
                    f"by_text_match {needle!r} matched {len(matched)} chunks; "
                    f"it must identify exactly one -- lengthen the excerpt"
                ]
            )
        return matched

    if key == "by_chunk_ids":
        if not isinstance(value, list):
            raise AuthoringError([f"by_chunk_ids must be a list, got {value!r}"])
        known = {str(chunk.id) for chunk in chunks}
        out: list[uuid.UUID] = []
        for raw in value:
            if str(raw) not in known:
                raise AuthoringError(
                    [f"chunk id {raw!r} is not a live chunk of this source"]
                )
            out.append(uuid.UUID(str(raw)))
        return out

    raise AuthoringError(
        [
            f"unknown selector {key!r}; expected by_page_range, by_text_match "
            f"or by_chunk_ids"
        ]
    )


def _overlaps_pages(chunk: Any, start: int, end: int) -> bool:
    """§10.1: a chunk is in range when either endpoint falls inside it.

    Either, not both, and not "contained in": a chunk spanning pages 4-6
    against a range of [3, 5] is material the author pointed at, and requiring
    containment would silently drop exactly the chunks that straddle the
    boundary the author drew.
    """
    first = chunk.page_start
    last = chunk.page_end
    if first is None and last is None:
        return False
    first = first if first is not None else last
    last = last if last is not None else first
    return start <= first <= end or start <= last <= end


@ingestion_writer
def apply_concept_sources(
    session: Session,
    *,
    subject_slug: str,
    source_id: uuid.UUID | None,
    links: Sequence[ResolvedLink],
) -> ImportResult:
    """Write ``concept_sources`` rows (§10.2 step 6)."""
    from .provenance import require

    require("source_id", source_id)
    concept_ids = _concept_ids(session, subject_slug)

    problems = [
        f"link references concept {link.concept_slug!r}, which is not in "
        f"subject {subject_slug!r}"
        for link in links
        if link.concept_slug not in concept_ids
    ]
    if problems:
        raise AuthoringError(problems)

    applied: list[str] = []
    for link in links:
        session.execute(
            sql(
                """
                INSERT INTO concept_sources
                    (concept_id, source_id, chunk_ids, role, note)
                VALUES (:concept_id, :source_id, :chunk_ids,
                        CAST(:role AS concept_source_role), :note)
                ON CONFLICT (concept_id, source_id, role) DO UPDATE
                   SET chunk_ids = EXCLUDED.chunk_ids,
                       note = EXCLUDED.note
                """
            ),
            {
                "concept_id": concept_ids[link.concept_slug],
                "source_id": source_id,
                "chunk_ids": list(link.chunk_ids),
                "role": link.role,
                "note": link.note,
            },
        )
        applied.append(
            f"{link.concept_slug} <- {len(link.chunk_ids)} chunk(s) as {link.role}"
        )

    return ImportResult(
        diff=Diff(added=tuple(applied)),
        applied=True,
        counts={"links": len(links)},
    )


def _concept_ids(session: Session, subject_slug: str) -> dict[str, uuid.UUID]:
    rows = session.execute(
        sql(
            """
            SELECT c.slug, c.id
              FROM concepts c
              JOIN subjects s ON s.id = c.subject_id
             WHERE s.slug = :slug AND s.deleted_at IS NULL
            """
        ),
        {"slug": subject_slug},
    ).all()
    return {row.slug: row.id for row in rows}


def _source_id_for(
    session: Session, source: str, subject_slug: str
) -> uuid.UUID | None:
    """Find a source by id, storage path suffix, or title.

    §10.1's example names ``1_TuringMachines.pdf`` -- a filename, which is not
    a column. Matching on the storage path's tail is what makes the authoring
    format work as written, and the title match is what makes it work after a
    reviewer has given the source a real title.
    """
    try:
        candidate = uuid.UUID(source)
    except ValueError:
        candidate = None

    row = session.execute(
        sql(
            """
            SELECT sc.id
              FROM sources sc
              JOIN subjects s ON s.id = sc.subject_id
             WHERE s.slug = :subject
               AND sc.deleted_at IS NULL
               AND (
                     (:by_id IS NOT NULL AND sc.id = :by_id)
                  OR sc.storage_path LIKE :like
                  OR sc.title = :source
               )
             ORDER BY sc.created_at
             LIMIT 1
            """
        ),
        {
            "subject": subject_slug,
            "by_id": candidate,
            "like": f"%{source}",
            "source": source,
        },
    ).one_or_none()
    return row.id if row else None
