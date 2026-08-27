"""Regression tolerance and affected-dataset detection (evaluation §13).

Pure functions over run outcomes. No database, no model calls -- §17 lists
``should_block(current, previous, tolerance)`` as a Tier 1 test for exactly
that reason, and the gate that decides whether a prompt change may ship is the
last thing that should need a network.

**Two ambiguities in §13.2 are resolved here, both in the strict direction.**

*Is ``aggregate_score_drop_max: 0.05`` absolute or relative?* The comment reads
"5% aggregate drop", and scores are 0.0-1.0, so 0.05 could be five points of
score or five percent of the previous value. Taken as **absolute**. Relative
would give the weakest datasets the tightest gate: a dataset sitting at 0.20
would tolerate a drop to 0.19 while one at 0.95 tolerated a drop to 0.9025 --
so the dataset with the most room to get worse would be the one guarded most
jealously, which is backwards. Absolute also matches the unit the number is
written in.

*Is a drop of exactly the maximum a block?* No. §13.2 says "5% aggregate drop
**before** blocking" and "up to 2 previously-passing entries **may** fail", so
both limits are inclusive and the comparison is strictly greater-than.

**A first run cannot regress.** With no baseline there is nothing to have
regressed from, so :func:`should_block` returns "not blocked" and says why. The
per-agent thresholds in :mod:`studium.eval.metrics` still apply -- those are
absolute floors, not comparisons, and they are what stops a brand-new dataset's
first run from shipping a Lecturer that invents citations.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

#: Slack on the aggregate comparison, absorbing binary floating-point error.
#:
#: Not a fudge factor. §13.2's limits are inclusive -- "5% aggregate drop
#: *before* blocking" -- and ``0.80 - 0.75`` is ``0.050000000000000044`` in
#: IEEE 754, so a drop of exactly the stated tolerance blocks. The gate would
#: then be tighter than the number it prints, on a boundary an author lands on
#: precisely when they have tuned a change to sit at the limit. Nothing about
#: evaluation scores is meaningful at the twelfth decimal place, so the
#: comparison is made at a precision that is.
FLOAT_SLACK = 1e-9


@dataclass(frozen=True, slots=True)
class EntryOutcome:
    """One entry's result within a run, as the gate needs to see it."""

    key: str
    passed: bool
    score: float


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """A completed run, reduced to what §13.2 compares.

    ``aggregate_score`` is the mean of per-entry scores, which is what
    ``evaluation_runs.aggregate_score`` holds. Kept as its own field rather
    than recomputed from ``entries`` so a baseline loaded from the database
    (where the entries may have been pruned by retention) still carries the
    number the gate needs.
    """

    dataset_slug: str
    aggregate_score: float
    entries: tuple[EntryOutcome, ...] = ()

    @property
    def by_key(self) -> dict[str, EntryOutcome]:
        return {e.key: e for e in self.entries}

    @property
    def failed(self) -> tuple[EntryOutcome, ...]:
        return tuple(e for e in self.entries if not e.passed)


@dataclass(frozen=True, slots=True)
class GateResult:
    """Whether a change may proceed without reviewer approval (§13.1 step 5)."""

    blocked: bool
    #: One line per violated limit. Empty when nothing blocks.
    reasons: tuple[str, ...] = ()
    #: Entries that passed in the baseline and fail now. The reviewer's diff
    #: (§13.1 step 6) is built from these.
    newly_failing: tuple[str, ...] = ()
    #: Entries that failed in the baseline and pass now. Not a gate input --
    #: reported because a change that fixes four entries and breaks one is a
    #: different conversation from one that only breaks one.
    newly_passing: tuple[str, ...] = ()
    score_delta: float = 0.0

    def render(self) -> str:
        lines = [
            f"aggregate {self.score_delta:+.4f}",
            f"newly failing: {len(self.newly_failing)}"
            + (f" ({', '.join(self.newly_failing)})" if self.newly_failing else ""),
            f"newly passing: {len(self.newly_passing)}"
            + (f" ({', '.join(self.newly_passing)})" if self.newly_passing else ""),
        ]
        lines.extend(f"BLOCKED: {reason}" for reason in self.reasons)
        if not self.blocked:
            lines.append("within tolerance")
        return "\n".join(f"  {line}" for line in lines)


def should_block(
    current: RunOutcome,
    previous: RunOutcome | None,
    tolerance: Mapping[str, float],
) -> GateResult:
    """§13.2's gate. Either violation blocks; both limits are inclusive.

    ``previous`` is the last *complete* run for the same dataset (§13.1 step
    4). ``None`` means there is no baseline, which is not a regression.
    """
    if previous is None:
        return GateResult(
            blocked=False,
            reasons=(),
            newly_failing=(),
            newly_passing=(),
            score_delta=0.0,
        )

    drop_max = float(tolerance.get("aggregate_score_drop_max", 0.05))
    failure_max = int(tolerance.get("per_entry_failure_max", 2))

    delta = current.aggregate_score - previous.aggregate_score
    drop = -delta if delta < 0 else 0.0

    baseline = previous.by_key
    now = current.by_key

    # Only entries present in both runs are compared. An entry added since the
    # baseline has nothing to have regressed from, and one removed cannot fail.
    shared = sorted(set(baseline) & set(now))
    newly_failing = tuple(k for k in shared if baseline[k].passed and not now[k].passed)
    newly_passing = tuple(k for k in shared if not baseline[k].passed and now[k].passed)

    reasons: list[str] = []
    # `> drop_max + slack`, not `> drop_max`: the limit is inclusive and
    # binary floats do not represent 0.05 exactly. See FLOAT_SLACK.
    if drop > drop_max + FLOAT_SLACK and not math.isclose(
        drop, drop_max, rel_tol=0.0, abs_tol=FLOAT_SLACK
    ):
        reasons.append(
            f"aggregate score dropped {drop:.4f} "
            f"({previous.aggregate_score:.4f} -> {current.aggregate_score:.4f}), "
            f"above the {drop_max} tolerance"
        )
    if len(newly_failing) > failure_max:
        reasons.append(
            f"{len(newly_failing)} previously-passing entries now fail "
            f"({', '.join(newly_failing)}), above the {failure_max} tolerance"
        )

    return GateResult(
        blocked=bool(reasons),
        reasons=tuple(reasons),
        newly_failing=newly_failing,
        newly_passing=newly_passing,
        score_delta=delta,
    )


# --- §13.4: which datasets a change affects --------------------------------

#: Where each agent's prompt lives. §13.1: "The prompt lives in code -- Python
#: string constants in the relevant agent module."
#:
#: ``studium/llm/prompts.py`` is listed against every agent because
#: ``build_prefix`` assembles all seven prefixes there; a change to it is
#: precisely §13.4's "change to a helper function that affects multiple
#: prompts", and the honest answer is that it affects all of them.
AGENT_PROMPT_PATHS: dict[str, tuple[str, ...]] = {
    "orchestrator": ("studium/agents/orchestrator.py",),
    "curator": ("studium/agents/curator.py",),
    "lecturer": ("studium/agents/lecturer.py",),
    "tutor": ("studium/agents/tutor.py",),
    "evaluator": ("studium/agents/evaluator.py",),
    "confusion_tracker": ("studium/agents/confusion_tracker.py",),
    "reviewer": ("studium/agents/reviewer.py",),
}

#: Paths that affect every agent's prompt, so a change to one selects the whole
#: suite rather than nothing.
SHARED_PROMPT_PATHS: tuple[str, ...] = (
    "studium/llm/prompts.py",
    "studium/llm/client.py",
    "studium/agents/base.py",
    "studium/agents/schemas.py",
)

#: Paths that affect retrieval quality without touching any agent prompt.
#: §13.4 calls the general case "manual for complex cases"; these three are the
#: ones that are mechanical enough to automate, and they are the ones a
#: retrieval change actually goes through.
RETRIEVAL_PATHS: tuple[str, ...] = (
    "studium/retrieval/",
    "studium/models/corpus.py",
)


@dataclass(frozen=True, slots=True)
class AffectedSet:
    """Which datasets a diff selects, and what could not be resolved."""

    slugs: tuple[str, ...] = ()
    #: Changed paths that touch prompts or retrieval but resolve to no dataset.
    #: Surfaced rather than dropped: §13.4 makes the reviewer the fallback, and
    #: a path that selects nothing is exactly the case where a change ships
    #: unevaluated because the tooling shrugged.
    unresolved: tuple[str, ...] = ()
    #: Why each dataset was selected, for the CLI to print.
    because: dict[str, str] = field(default_factory=dict)


def affected_datasets(
    changed_paths: Sequence[str],
    datasets: Sequence[tuple[str, str, str | None]],
) -> AffectedSet:
    """Resolve a diff to the datasets it should run (§13.4).

    ``datasets`` is ``(slug, kind, agent)`` triples -- taken as data rather than
    read from the database so this stays a Tier 1 function.

    Automated for the simple case: a prompt string changed, the agent identity
    is known from the path, and every active dataset naming that agent is
    affected. Everything else is reported as unresolved for the reviewer to
    settle with ``--datasets``, which is what §13.4 asks for. It never returns
    an empty set silently for a path it recognised as prompt-adjacent.
    """
    normalised = [_normalise(p) for p in changed_paths]
    by_agent: dict[str, list[str]] = {}
    retrieval_slugs: list[str] = []
    for slug, kind, agent in datasets:
        if kind == "retrieval_quality":
            retrieval_slugs.append(slug)
        elif agent:
            by_agent.setdefault(agent, []).append(slug)

    selected: dict[str, str] = {}
    unresolved: list[str] = []

    for path in normalised:
        if any(path.startswith(shared) for shared in SHARED_PROMPT_PATHS):
            for slugs in by_agent.values():
                for slug in slugs:
                    selected.setdefault(slug, f"{path} is shared by every agent prompt")
            if not by_agent:
                unresolved.append(path)
            continue

        agent = _agent_for_path(path)
        if agent is not None:
            slugs = by_agent.get(agent, [])
            if not slugs:
                unresolved.append(path)
            for slug in slugs:
                selected.setdefault(slug, f"{path} is the {agent} prompt")
            continue

        if any(path.startswith(prefix) for prefix in RETRIEVAL_PATHS):
            if not retrieval_slugs:
                unresolved.append(path)
            for slug in retrieval_slugs:
                selected.setdefault(slug, f"{path} changes retrieval")

    return AffectedSet(
        slugs=tuple(sorted(selected)),
        unresolved=tuple(sorted(set(unresolved))),
        because=selected,
    )


def _agent_for_path(path: str) -> str | None:
    for agent, paths in AGENT_PROMPT_PATHS.items():
        if any(path.startswith(candidate) for candidate in paths):
            return agent
    return None


_BACKEND_PREFIX = re.compile(r"^backend/")


def _normalise(path: str) -> str:
    """``backend/studium/...`` and ``studium/...`` name the same file.

    ``git diff --name-only`` from the repo root emits the first form and from
    ``backend/`` the second; the gate is run from both.
    """
    return _BACKEND_PREFIX.sub("", path.replace("\\", "/").strip())
