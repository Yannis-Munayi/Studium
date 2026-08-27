"""Evaluation and assessment harness (spec subsystem 6).

Two concerns under one name, sharing infrastructure because they share a
computational shape -- input, expected output, actual output, verdict -- and
sharing nothing else (§1).

**System-facing evaluation** keeps the agents honest as prompts change and
models swap. Golden datasets are authored in YAML (:mod:`.datasets`),
materialised to the database (:mod:`.sync`), executed against a pinned prompt
and model (:mod:`.runner`), graded deterministically (:mod:`.checks`) or by the
Evaluator (:mod:`.grading`), scored against §8's per-agent thresholds
(:mod:`.metrics`), and gated against the last passing run (:mod:`.regression`).
Retrieval gets its own metric set (:mod:`.retrieval_eval`). The reviewer works
the queue through :mod:`.review`.

**Learner-facing assessment** is the closed-book examination (:mod:`.summative`)
and the signed credential it produces (:mod:`.credentials`).

    from studium.eval import parse_directory, run_dataset, should_block

    datasets = parse_directory(Path("content/evaluation"))
    report = await run_dataset(datasets[0], agent=lecturer)
    gate = should_block(report.outcome(), baseline, datasets[0].regression_tolerance)

Module map:

* :mod:`~studium.eval.datasets` -- §7.2/§7.3. Authoring format and validation.
* :mod:`~studium.eval.checks` -- §7.2. The deterministic check registry.
* :mod:`~studium.eval.fixtures` -- §3. Seeded runtime contexts, pinned by
  derivation so two runs of one entry are byte-comparable.
* :mod:`~studium.eval.grading` -- §4/§7.2. Deterministic and meta-graded.
* :mod:`~studium.eval.runner` -- §4/§16. Execution and persistence.
* :mod:`~studium.eval.metrics` -- §8. Per-agent metrics and blocking thresholds.
* :mod:`~studium.eval.retrieval_eval` -- §9. recall@k, precision@k, the
  reranker A/B, the provider-swap gate.
* :mod:`~studium.eval.regression` -- §13. Tolerance arithmetic and affected-set
  detection.
* :mod:`~studium.eval.sync` -- §7.3 step 5. YAML to database.
* :mod:`~studium.eval.review` -- §10. The content review surface.
* :mod:`~studium.eval.summative` -- §11. The closed-book assessment flow.
* :mod:`~studium.eval.credentials` -- §12. Ed25519 signing and verification.
* :mod:`~studium.eval.cli` -- §10.2/§13.1. The reviewer's commands.

**This package must import without an API key or a signing key**, because
``studium.api.app`` imports it for the §12.4 verify endpoint. Both are resolved
at use: :func:`~studium.eval.credentials.load_identity` raises
``SigningKeyUnavailable``, and the CLI builds its client lazily.
"""

from __future__ import annotations

from .checks import REGISTRY, CheckError, CheckOutcome, known
from .credentials import (
    CredentialError,
    SigningKeyUnavailable,
    canonicalize,
    issue_credential,
    sign,
    verify,
    verify_item,
)
from .datasets import (
    Dataset,
    DatasetError,
    Entry,
    order_passages,
    parse_dataset,
    parse_directory,
)
from .grading import EntryGrade, Grader, MetaGrader, aggregate_score
from .metrics import METRICS, Metric, MetricValue, coverage_gaps, violations
from .regression import (
    AffectedSet,
    EntryOutcome,
    GateResult,
    RunOutcome,
    affected_datasets,
    should_block,
)
from .retrieval_eval import (
    ABResult,
    Aggregate,
    EntryMetrics,
    Judgment,
    compare_reranker,
    evaluate_entry,
    provider_swap_gate,
)
from .runner import RunReport, run_dataset
from .summative import (
    CLOSED_BOOK,
    AssessmentDefinition,
    AssessmentError,
    ClosedBookViolation,
    assert_closed_book,
    parse_definition,
)
from .sync import SyncResult

__all__ = [
    "CLOSED_BOOK",
    "METRICS",
    "REGISTRY",
    "ABResult",
    "AffectedSet",
    "Aggregate",
    "AssessmentDefinition",
    "AssessmentError",
    "CheckError",
    "CheckOutcome",
    "ClosedBookViolation",
    "CredentialError",
    "Dataset",
    "DatasetError",
    "Entry",
    "EntryGrade",
    "EntryMetrics",
    "EntryOutcome",
    "GateResult",
    "Grader",
    "Judgment",
    "MetaGrader",
    "Metric",
    "MetricValue",
    "RunOutcome",
    "RunReport",
    "SigningKeyUnavailable",
    "SyncResult",
    "affected_datasets",
    "aggregate_score",
    "assert_closed_book",
    "canonicalize",
    "compare_reranker",
    "coverage_gaps",
    "evaluate_entry",
    "issue_credential",
    "known",
    "order_passages",
    "parse_dataset",
    "parse_definition",
    "parse_directory",
    "provider_swap_gate",
    "run_dataset",
    "should_block",
    "sign",
    "verify",
    "verify_item",
    "violations",
]
