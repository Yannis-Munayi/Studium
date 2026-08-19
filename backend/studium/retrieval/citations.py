"""Citation markers and their resolution (retrieval §12).

An agent writes ``[P3]`` into its prose. This module turns that back into a
specific ``source_chunks`` row with enough provenance for a hover card, at read
time, without the passage number ever being stored.

That last part is the design: passage numbers are per-call ephemera and chunk
ids are stable, so ``content_citations`` stores only the chunk id and the
numbering is *reconstructed* from it. The reconstruction is sound only because
numbering is ``chunk_id``-sorted in both directions (§3, §12) -- the same
ordering that makes the Lecturer's cached prefix byte-stable. If either side
ever sorted differently, every citation in every stored artifact would resolve
to the wrong passage, silently, because both lists are the same length.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass

from sqlalchemy import text as sql
from sqlalchemy.orm import Session

log = logging.getLogger(__name__)

#: §12: exactly two forms. ``[P3]`` and ``[P3-P5]``. Comma-separated forms are
#: not supported and the agent prompts instruct against them, so a ``[P1, P4]``
#: in output is a prompt-compliance problem rather than something to parse.
CITATION_PATTERN = re.compile(r"\[P(\d+)(?:\s*-\s*P?(\d+))?\]")

#: How much chunk text the hover card gets before the learner clicks through.
EXCERPT_CHARS = 400


def parse_citation_markers(text: str, *, maximum: int | None = None) -> list[int]:
    """Passage numbers cited in ``text``, deduplicated and ascending.

    Adversarial input is dropped rather than raising. A model that writes
    ``[P9]`` when six passages were supplied has produced an ungrounded claim,
    and the response to that is a review flag -- not an exception in the middle
    of a stream the learner is reading.

    ``[P5-P3]``, a reversed range, is read as the single passage 5: the
    alternative readings are to expand it backwards (inventing an intent the
    model did not express) or to drop it entirely (losing a citation that names
    a real passage). Taking the first bound keeps the one claim the marker
    unambiguously makes.
    """
    numbers: list[int] = []
    seen: set[int] = set()

    for match in CITATION_PATTERN.finditer(text):
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else start
        if end < start:
            end = start
        for number in range(start, end + 1):
            if number < 1 or (maximum is not None and number > maximum):
                continue
            if number not in seen:
                seen.add(number)
                numbers.append(number)

    return sorted(numbers)


def has_invalid_citation(text: str, passage_count: int) -> bool:
    """Whether ``text`` cites a passage number that was never supplied.

    Separate from :func:`parse_citation_markers` because the two answer
    different questions: that one asks "what did it cite", this asks "did it
    invent". An invented citation reaching a learner is a severity-3 flag.
    """
    for match in CITATION_PATTERN.finditer(text):
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else start
        if start < 1 or max(start, end) > passage_count:
            return True
    return False


@dataclass(frozen=True, slots=True)
class ResolvedCitation:
    """One resolved marker, shaped for §12's endpoint response."""

    marker: str
    chunk_id: uuid.UUID
    source_id: uuid.UUID
    source_title: str
    source_authors: list[str]
    page_start: int | None
    page_end: int | None
    section_path: list[str]
    excerpt: str
    excerpt_start_offset: int
    excerpt_end_offset: int
    #: §12: a retired or soft-deleted source renders as an inactive marker
    #: rather than a broken link. The citation itself is never removed --
    #: ``ON DELETE RESTRICT`` on the chunk FK blocks that at the schema level.
    source_deleted: bool = False

    def as_json(self) -> dict[str, object]:
        return {
            "marker": self.marker,
            "chunk_id": str(self.chunk_id),
            "source_id": str(self.source_id),
            "source_title": self.source_title,
            "source_authors": self.source_authors,
            "page_start": self.page_start,
            "page_end": self.page_end,
            "section_path": self.section_path,
            "excerpt": self.excerpt,
            "excerpt_start_offset": self.excerpt_start_offset,
            "excerpt_end_offset": self.excerpt_end_offset,
            "source_deleted": self.source_deleted,
        }


def resolve_artifact_citations(
    session: Session, artifact_id: uuid.UUID
) -> list[ResolvedCitation]:
    """Every citation on one artifact, numbered ``P1..Pn`` (§12).

    One query per artifact, not per marker: a lecture segment carries six or
    more citations and the frontend renders them all at once, so per-marker
    round trips would be six requests to draw one paragraph.

    A soft-deleted or retired source still resolves -- with ``source_deleted``
    set and the excerpt withheld. The row is retained (that is what
    ``RESTRICT`` guarantees), but showing text from a source that was retired
    for being wrong is the failure retirement exists to prevent.
    """
    rows = session.execute(
        sql(
            """
            SELECT cc.source_chunk_id            AS chunk_id,
                   cc.quoted_span                AS quoted_span,
                   sc.text                       AS text,
                   sc.section_path               AS section_path,
                   sc.page_start                 AS page_start,
                   sc.page_end                   AS page_end,
                   s.id                          AS source_id,
                   s.title                       AS source_title,
                   s.authors                     AS source_authors,
                   (s.deleted_at IS NOT NULL
                    OR s.status = 'retired')     AS source_deleted
              FROM content_citations cc
              JOIN source_chunks sc ON sc.id = cc.source_chunk_id
              JOIN sources s        ON s.id = sc.source_id
             WHERE cc.artifact_id = :artifact_id
             ORDER BY cc.source_chunk_id
            """
        ),
        {"artifact_id": artifact_id},
    ).all()

    resolved: list[ResolvedCitation] = []
    for index, row in enumerate(rows, start=1):
        deleted = bool(row.source_deleted)
        excerpt, start, end = _excerpt(row.text or "", row.quoted_span)
        resolved.append(
            ResolvedCitation(
                marker=f"P{index}",
                chunk_id=row.chunk_id,
                source_id=row.source_id,
                source_title=row.source_title or "",
                source_authors=list(row.source_authors or []),
                page_start=row.page_start,
                page_end=row.page_end,
                section_path=_as_str_list(row.section_path),
                excerpt="" if deleted else excerpt,
                excerpt_start_offset=0 if deleted else start,
                excerpt_end_offset=0 if deleted else end,
                source_deleted=deleted,
            )
        )
    return resolved


def _excerpt(text: str, quoted_span: object) -> tuple[str, int, int]:
    """The hover card's preview, with offsets back into the chunk text.

    A ``quoted_span`` recorded at citation time wins: it is the passage the
    agent actually leaned on, which is more useful than the chunk's opening.
    Otherwise the first :data:`EXCERPT_CHARS` characters, cut at a word
    boundary so the preview does not end mid-word.

    Offsets are returned so subsystem 4 can highlight the excerpt inside the
    full chunk text when the card is expanded.
    """
    if isinstance(quoted_span, str) and quoted_span.strip():
        start = text.find(quoted_span)
        if start >= 0:
            return quoted_span, start, start + len(quoted_span)
        return quoted_span, 0, len(quoted_span)

    if len(text) <= EXCERPT_CHARS:
        return text, 0, len(text)

    window = text[:EXCERPT_CHARS]
    space = window.rfind(" ")
    end = space if space > EXCERPT_CHARS // 2 else EXCERPT_CHARS
    return window[:end].rstrip() + "...", 0, end


def _as_str_list(raw: object) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [s if isinstance(s, str) else str(s) for s in raw]
    return [str(raw)]
