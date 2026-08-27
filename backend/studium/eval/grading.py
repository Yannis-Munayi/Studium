"""Grading one dataset entry (evaluation §4 "Grading", §7.2).

Two paths, and an entry may take both:

* **Deterministic.** A check function in :mod:`studium.eval.checks` evaluates
  the output against declared properties. Free, instant, reproducible.
* **Meta-graded.** The Evaluator judges against a rubric the YAML supplies.
  Costs an Evaluator call per property (§15.1) and, being a model call, is the
  part of this subsystem that can disagree with itself between runs -- §19 open
  question 2, which is what the ``grading_calibration`` dataset kind exists to
  watch.

**A hybrid entry's score is the mean of its properties, unweighted.** §7.2
declares hybrid entries and never says how the halves combine. Unweighted
because weighting would need a per-property weight the YAML has no field for,
and inventing a default (deterministic counts double? meta counts double?)
would silently express an opinion about which kind of evidence is better. The
author already controls the balance by choosing how many properties of each
kind to write.

**A failed meta-grader fails the property; it does not fail the entry
silently.** §16's second row: "Regression run: meta-grader fails -- the entry
is marked failed with a specific error." Scoring 0 with a note naming the
failure, rather than skipping the property, is what stops an Evaluator outage
from looking like a clean run with fewer properties.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from studium.agents.base import AgentInput
from studium.eval import checks
from studium.eval.checks import META_GRADED, CheckError, CheckOutcome
from studium.eval.datasets import Entry

log = logging.getLogger(__name__)

#: An entry that produced no gradable properties scores this. Not 0.0: a zero
#: would read as "the agent did badly" when what happened is that the harness
#: had nothing to measure, and §13.2's aggregate would treat the two the same.
#: Callers check :attr:`EntryGrade.gradable` instead.
NO_PROPERTIES_SCORE = 0.0

#: §8's blocking thresholds are proportions of entries, and an entry passes
#: only when every property it declares passes. A property-level pass rate
#: would let an entry with nine trivial properties and one invalid citation
#: report 90% -- and §8.1 blocks citation validity below 100%.
STRICT_ENTRY_PASS = True


@dataclass(frozen=True, slots=True)
class PropertyGrade:
    """One property's outcome within one entry."""

    name: str
    check: str
    outcome: CheckOutcome
    meta_graded: bool = False
    cost_usd: float = 0.0
    trace_id: Any = None

    def render(self) -> str:
        mark = "pass" if self.outcome.passed else "FAIL"
        tag = " (meta)" if self.meta_graded else ""
        return f"{mark} {self.name}{tag}: {self.outcome.note}"


@dataclass(frozen=True, slots=True)
class EntryGrade:
    """What one entry contributes to a run."""

    entry_key: str
    properties: tuple[PropertyGrade, ...] = ()
    #: Set when the entry could not be graded at all -- the agent call failed
    #: past its retries (§16 row 1), or the entry is a stub. Distinct from
    #: "graded and failed".
    error: str | None = None

    @property
    def gradable(self) -> bool:
        return self.error is None and bool(self.properties)

    @property
    def score(self) -> float:
        if not self.properties:
            return NO_PROPERTIES_SCORE
        return sum(p.outcome.score for p in self.properties) / len(self.properties)

    @property
    def passed(self) -> bool:
        if self.error is not None or not self.properties:
            return False
        return all(p.outcome.passed for p in self.properties)

    @property
    def cost_usd(self) -> float:
        return sum(p.cost_usd for p in self.properties)

    @property
    def notes(self) -> str:
        if self.error is not None:
            return self.error
        return "; ".join(p.render() for p in self.properties)

    def property_scores(self) -> dict[str, float]:
        """Property name -> score, for :func:`studium.eval.metrics.compute`."""
        return {p.name: p.outcome.score for p in self.properties}


class MetaGrader:
    """The Evaluator in meta-grading mode (§7.2).

    §19 files this as "a new invocation kind on an existing agent ... belongs in
    subsystem 2 v1.1". The kind is ``meta_grade`` and it is added to
    ``Evaluator.kinds``; everything specific to evaluation stays here so
    subsystem 2 gains one dispatch branch rather than a dependency on this
    package.

    The prefix is built here rather than in ``studium.llm.prompts`` because
    every builder in that module requires a focus concept from the session
    context, and meta-grading has none -- it judges an agent's output against a
    rubric string, with no concept in play.
    """

    def __init__(self, agent: Any) -> None:
        self.agent = agent

    async def grade(
        self,
        *,
        rubric: str,
        output: str,
        context: Any,
        agent_under_test: str,
        property_name: str,
    ) -> tuple[CheckOutcome, float, Any]:
        """Score ``output`` against ``rubric``. Returns outcome, cost, trace id."""
        result = await self.agent.handle(
            AgentInput(
                session_context=context,
                kind="meta_grade",
                payload={
                    "rubric": rubric,
                    "candidate_output": output,
                    "agent_under_test": agent_under_test,
                    "property_name": property_name,
                },
            )
        )
        verdict = result.structured
        score = float(getattr(verdict, "score", 0.0))
        note = str(getattr(verdict, "verdict", "") or result.text)
        cost = float(getattr(result.trace, "cost_usd", 0.0) or 0.0)
        return (
            CheckOutcome(
                # §7.2: "The Evaluator returns a score 0-1 with a written
                # verdict". The pass line is 0.5 -- the midpoint, because a
                # rubric judgment has no natural threshold and the spec names
                # none. Meta-graded properties are non-blocking in §8 for
                # exactly this reason: the number informs the reviewer rather
                # than gating a deploy on a model's opinion of a model.
                passed=score >= 0.5,
                score=max(0.0, min(1.0, score)),
                note=note,
            ),
            cost,
            getattr(result, "turn_id", None),
        )


@dataclass
class Grader:
    """Grades entries. Holds the meta-grader so deterministic runs need none."""

    meta_grader: MetaGrader | None = None
    #: Populated as properties are graded, for §15.1's cost attribution.
    total_cost_usd: float = field(default=0.0, init=False)

    async def grade_entry(
        self,
        entry: Entry,
        *,
        output: str,
        context: Any = None,
        agent_under_test: str = "",
    ) -> EntryGrade:
        """Grade one entry's ``output`` against its declared properties."""
        if entry.is_stub:
            return EntryGrade(
                entry.key,
                error=(
                    "entry is a stub: it captures a fixture input but no "
                    "expectation. §3 requires a human to say what the correct "
                    "output would have been before it can score anything."
                ),
            )

        grades: list[PropertyGrade] = []
        for prop in entry.properties:
            name = str(prop.get("name", "unnamed"))
            check_name = str(prop.get("check", ""))

            if check_name == META_GRADED:
                grades.append(
                    await self._meta_grade(
                        prop,
                        name=name,
                        entry=entry,
                        output=output,
                        context=context,
                        agent_under_test=agent_under_test,
                    )
                )
                continue

            grades.append(self._deterministic(prop, name=name, entry=entry, output=output))

        return EntryGrade(entry.key, properties=tuple(grades))

    def _deterministic(
        self, prop: Mapping[str, Any], *, name: str, entry: Entry, output: str
    ) -> PropertyGrade:
        check_name = str(prop.get("check", ""))
        try:
            spec = checks.get(check_name)
            outcome = spec.fn(output, prop, entry.input)
        except CheckError as exc:
            # Reached only if validation was skipped -- `studium eval validate`
            # resolves every check before a run starts. Scored 0 with the
            # reason rather than raised, so one malformed property does not
            # discard the nineteen entries already paid for.
            log.error("check %r failed on %s: %s", check_name, entry.key, exc)
            outcome = CheckOutcome(False, 0.0, f"check error: {exc}")
        return PropertyGrade(name=name, check=check_name, outcome=outcome)

    async def _meta_grade(
        self,
        prop: Mapping[str, Any],
        *,
        name: str,
        entry: Entry,
        output: str,
        context: Any,
        agent_under_test: str,
    ) -> PropertyGrade:
        rubric = str(prop.get("rubric") or "").strip()
        if not rubric and entry.rubric:
            rubric = str(entry.rubric.get("text") or entry.rubric.get("rubric") or "")

        if self.meta_grader is None or context is None:
            return PropertyGrade(
                name=name,
                check=META_GRADED,
                meta_graded=True,
                outcome=CheckOutcome(
                    False,
                    0.0,
                    "no meta-grader configured; this property needs an "
                    "Evaluator call (§7.2) and the run was started without one",
                ),
            )

        try:
            outcome, cost, trace_id = await self.meta_grader.grade(
                rubric=rubric,
                output=output,
                context=context,
                agent_under_test=agent_under_test,
                property_name=name,
            )
        except Exception as exc:  # noqa: BLE001 -- §16 row 2
            log.exception("meta-grader failed on %s/%s", entry.key, name)
            return PropertyGrade(
                name=name,
                check=META_GRADED,
                meta_graded=True,
                outcome=CheckOutcome(
                    False, 0.0, f"meta-grader failed after retries: {exc}"
                ),
            )

        self.total_cost_usd += cost
        return PropertyGrade(
            name=name,
            check=META_GRADED,
            outcome=outcome,
            meta_graded=True,
            cost_usd=cost,
            trace_id=trace_id,
        )


def aggregate_score(grades: Sequence[EntryGrade]) -> float:
    """A run's aggregate: the mean of per-entry scores (§5's column).

    Entries that could not be graded at all (§16's agent-call failure) count as
    0. They are genuine failures of the run -- the agent did not produce
    output -- and excluding them would let an outage that broke half the
    entries report the aggregate of the surviving half, which reads as a pass.
    """
    if not grades:
        return 0.0
    return sum(g.score for g in grades) / len(grades)


def property_scores(grades: Sequence[EntryGrade]) -> dict[str, list[float]]:
    """Property name -> every score it produced, for §8's metric aggregation."""
    collected: dict[str, list[float]] = {}
    for grade in grades:
        for name, score in grade.property_scores().items():
            collected.setdefault(name, []).append(score)
    return collected
