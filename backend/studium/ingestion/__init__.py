"""Content authoring and ingestion (spec subsystem 5).

Two concerns that share schemas and share nothing else.

The **pipeline** (:mod:`.pipeline`) is automatic: a PDF is uploaded, extracted
(:mod:`.extract`), normalised (:mod:`.normalize`), chunked by retrieval's
algorithm, and handed to retrieval's embedding worker. It runs when a source
arrives, finishes without a human, and publishes nothing.

The **authoring workflows** (:mod:`.authoring`, :mod:`.publish`) are manual: the
concept graph, the rubric criteria, and the concept-source pointers that turn a
pile of chunks into something teachable. They run when a domain expert has time
to sit with the material, which may be weeks after the pipeline finished.

Between the two sit the two human gates. :mod:`.licensing` holds the license
determination that no classifier is allowed to make, and :mod:`.queue` holds
the review queue every failure and ambiguity in either path reports to.

The invariant that governs all of it is in :mod:`.provenance`.
"""

from __future__ import annotations

# Every submodule that registers an ``@ingestion_writer`` is imported here, not
# only the ones whose names are re-exported below. Registration is a side
# effect of import, so a writer in a module nothing imports is invisible to the
# §13.4 enforcement test -- it would report green while checking nothing. The
# test walks the package independently for the same reason; this keeps
# ``import studium.ingestion`` sufficient on its own.
from . import authoring, licensing, publish, queue  # noqa: F401
from .extract import (
    ExtractedContent,
    ExtractedPage,
    ExtractionError,
    Extractor,
    ExtractorUnavailable,
    PdfPlumberExtractor,
    get_extractor,
)
from .normalize import (
    NORMALIZER_VERSION,
    NormalizationResult,
    normalize_document,
    normalize_page,
)
from .pipeline import (
    DuplicateSource,
    SourceTooLarge,
    StageResult,
    UnsupportedFormat,
    UploadRejected,
    run_chunk,
    run_extract,
    run_normalize,
    run_pipeline,
    run_worker_once,
    upload,
)
from .provenance import CRITICAL_COLUMNS, MissingProvenance, ingestion_writer, writers
from .publish import PublishResult, publish_subject, validate
from .queue import QueueItem, flag, pending

__all__ = [
    "CRITICAL_COLUMNS",
    "NORMALIZER_VERSION",
    "DuplicateSource",
    "ExtractedContent",
    "ExtractedPage",
    "ExtractionError",
    "Extractor",
    "ExtractorUnavailable",
    "MissingProvenance",
    "NormalizationResult",
    "PdfPlumberExtractor",
    "PublishResult",
    "QueueItem",
    "SourceTooLarge",
    "StageResult",
    "UnsupportedFormat",
    "UploadRejected",
    "flag",
    "get_extractor",
    "ingestion_writer",
    "normalize_document",
    "normalize_page",
    "pending",
    "publish_subject",
    "run_chunk",
    "run_extract",
    "run_normalize",
    "run_pipeline",
    "run_worker_once",
    "upload",
    "validate",
    "writers",
]
