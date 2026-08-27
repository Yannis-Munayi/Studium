"""The ingestion-side provenance gap invariant (ingestion §13).

**No ingestion-side writer creates a row where a required-for-attribution
column is silently populated with a fallback value.** Every ingestion boundary
that could produce ambiguous provenance either raises with the specific gap
named, or writes to the ingestion review queue with the gap visible to a
reviewer.

This is a spec invariant rather than three more per-site fixes because the same
failure has now appeared three times:

* **V2.** ``content_artifacts`` had no join path for cost attribution, so
  ingestion-time cost was booked against whoever happened to be in context.
* **The draft loader.** ``model`` and ``generated_at`` were silently dropped on
  carry-forward, so 33 artifacts arrived claiming provenance they did not have.
* **S3.** ``content_review_queue`` rejected the row the embedding worker needed
  to write, so a chunk that never embedded left no trace anywhere a reviewer
  would look.

Each was fixed where it was found. The third instance is the trigger: the
pattern is architectural, not incidental, so it gets an invariant and a test
that fails on the *next* instance instead of a fourth postmortem.

What the invariant is not: a rule that every column must be populated.
Provenance-critical means "needed to trace where this row came from". A
reviewer would like ``sources.publication_year``; nothing breaks when it is
null. ``sources.uploaded_by`` is different -- a source with no uploader cannot
be attributed, budgeted, or taken down on request.

The enforcement mechanism is :func:`writers` plus the Tier 1 test that walks
it. Registration is what makes a writer visible to the test, so a writer that
forgets ``@ingestion_writer`` is *not* checked -- see §18 open question 4 and
the coverage guard in the test module, which fails when the registry is empty.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

#: Columns that must be passed explicitly by any ingestion writer that fills
#: them. Adding a column here is a deliberate act: the §13.4 test reads this
#: list, so an entry with no corresponding writer parameter fails the build.
#:
#: Each entry is the bare column name rather than ``table.column`` because
#: writers name their parameters after the column, not the table -- and the
#: same name means the same thing across ingestion's tables (a ``source_id``
#: is a source everywhere).
CRITICAL_COLUMNS: frozenset[str] = frozenset(
    {
        # Who put this in the corpus. Without it a source cannot be attributed,
        # budgeted against, or removed on the uploader's request.
        "uploaded_by",
        # Which code produced the text. Without these a corpus-wide
        # re-extraction cannot tell what has already been done.
        "extractor_version",
        "normalizer_version",
        # Which model produced a vector or an artifact.
        "model",
        "model_version",
        # Which source a chunk came from, and which chunk a citation resolves
        # to. A citation pointing at "some chunk" is not a citation.
        "source_id",
        "source_chunk_id",
        # Who a cost is booked to. The V2 instance in full.
        "attributable_to",
    }
)

#: Every function decorated with :func:`ingestion_writer`, in registration
#: order. The §13.4 test walks this.
_WRITERS: list[Callable[..., Any]] = []


class MissingProvenance(ValueError):
    """A writer was asked to create a row without provenance it requires.

    Carries the column name rather than a prose message so a caller can react
    to *which* gap it hit -- the pipeline turns some of these into review queue
    rows and lets others propagate.
    """

    def __init__(self, column: str, *, detail: str | None = None) -> None:
        self.column = column
        message = f"ingestion writer requires {column!r} and was given nothing"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)


def ingestion_writer[F: Callable[..., Any]](fn: F) -> F:
    """Register ``fn`` as an ingestion writer subject to the §13 invariant.

    Registration only. The decorator deliberately adds no runtime wrapper: a
    wrapper that checked arguments would make the invariant a runtime failure
    on real data, and the whole point is that it fails in CI on the code. The
    check lives in the Tier 1 test, which reads this registry.
    """
    _WRITERS.append(fn)
    return fn


def writers() -> tuple[Callable[..., Any], ...]:
    """Every registered ingestion writer. Read by the §13.4 enforcement test."""
    return tuple(_WRITERS)


def provenance_parameters(fn: Callable[..., Any]) -> dict[str, inspect.Parameter]:
    """The parameters of ``fn`` whose names are attribution-critical."""
    signature = inspect.signature(fn)
    return {
        name: parameter
        for name, parameter in signature.parameters.items()
        if name in CRITICAL_COLUMNS
    }


def require(column: str, value: Any, *, detail: str | None = None) -> Any:
    """Return ``value``, or raise :class:`MissingProvenance` naming ``column``.

    The one-liner writers use at their own boundary. It exists so the failure
    names the column at the point the gap is real, rather than surfacing later
    as a NOT NULL violation naming a table -- which tells a reader what broke
    but not which caller failed to pass what.
    """
    if value is None:
        raise MissingProvenance(column, detail=detail)
    return value
