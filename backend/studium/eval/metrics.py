"""Per-agent metrics and their blocking thresholds (evaluation §8).

§8 names, for each of the seven agents, what a good dataset measures and which
of those measurements blocks a deploy. This module makes those thresholds
executable.

**The join §8 leaves implicit.** §8 names metrics ("grounding rate", "citation
validity"); §7.2 names *properties* on a dataset entry ("cites_provided_
passages", "within_word_limit"). Nothing in the spec says which property
produces which metric, so a threshold like "citation validity < 100% blocks
deploy" has no input unless the two vocabularies are pinned together. They are
pinned here, in :data:`METRICS`, and a dataset whose properties do not produce
an agent's blocking metrics is reported by :func:`coverage_gaps` rather than
quietly scoring 100% on a metric nothing measured. See DIVERGENCES-EVALUATION
(E7) -- an unmeasured blocking threshold is worse than no threshold, because it
reads green.

**Thresholds are floors on measurement, not comparisons against a baseline.**
That is what makes them different from §13.2's regression tolerance: tolerance
asks "did this change make things worse", a threshold asks "is this good
enough to ship at all". A first run with no baseline is exempt from the former
and fully subject to the latter.

§19 open question 1 is explicit that these numbers are educated guesses awaiting
calibration. They are constants in one place for that reason.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Metric:
    """One measured dimension of an agent's behaviour (§8's "key metrics")."""

    key: str
    agent: str
    #: The ``expected.properties[].name`` a dataset entry must use to produce
    #: this metric. This is the join §8 leaves implicit.
    property_name: str
    description: str
    #: ``min`` blocks when the value falls below ``threshold``; ``max`` blocks
    #: when it rises above. §8 states each threshold in one direction or the
    #: other and the two are not interchangeable.
    direction: str = "min"
    threshold: float | None = None
    #: True when the metric counts a *bad* thing (fabrication, leakage) and the
    #: underlying check scores 1.0 for the good case. The value reported is
    #: then ``1 - mean(score)``.
    invert: bool = False
    #: False for the metrics §8 sends "to reviewer for judgment" instead.
    blocking: bool = False
    #: Meta-graded metrics cost an Evaluator call per entry (§15.1).
    meta_graded: bool = False

    def violated(self, value: float) -> bool:
        if not self.blocking or self.threshold is None:
            return False
        return value < self.threshold if self.direction == "min" else value > self.threshold

    def render_threshold(self) -> str:
        if self.threshold is None:
            return "reviewer judgment"
        symbol = "<" if self.direction == "min" else ">"
        return f"blocks at {symbol} {self.threshold:.0%}"


#: §8, agent by agent. Every blocking threshold in the spec appears here; the
#: non-blocking metrics are here too, because §6.2's dashboard reads them and a
#: metric that is only computed when it can block is a metric nobody can
#: calibrate (§19 open question 1).
METRICS: tuple[Metric, ...] = (
    # --- §8.1 Lecturer ----------------------------------------------------
    Metric(
        "grounding_rate",
        "lecturer",
        "cites_provided_passages",
        "Percentage of claims backed by a citation.",
        direction="min",
        threshold=0.90,
        blocking=True,
    ),
    Metric(
        "citation_validity",
        "lecturer",
        "citations_resolve",
        "Percentage of [Pn] markers that resolve to a provided passage.",
        direction="min",
        threshold=1.0,
        blocking=True,
    ),
    Metric(
        "stance_adherence",
        "lecturer",
        "uses_requested_stance",
        "Does the segment adopt the stance it was asked for?",
        meta_graded=True,
    ),
    Metric(
        "length_compliance",
        "lecturer",
        "within_word_limit",
        "Percentage within the 200-400 word range.",
    ),
    # --- §8.2 Tutor -------------------------------------------------------
    Metric(
        "fabrication_rate",
        "tutor",
        "no_fabrication",
        "Claims made without grounding or the broader-context flag.",
        direction="max",
        threshold=0.05,
        invert=True,
        blocking=True,
    ),
    Metric(
        "diagnostic_accuracy",
        "tutor",
        "classifies_confusion",
        "Does the Tutor identify the right class of confusion?",
        meta_graded=True,
    ),
    Metric(
        "move_appropriateness",
        "tutor",
        "chooses_fitting_move",
        "Does the chosen pedagogical move fit the classification?",
        meta_graded=True,
    ),
    Metric(
        "tone_adherence",
        "tutor",
        "tone_is_patient",
        "Patient, concise, not condescending.",
        meta_graded=True,
    ),
    # --- §8.3 Evaluator ---------------------------------------------------
    Metric(
        "scoring_accuracy",
        "evaluator",
        "grade_matches_ground_truth",
        "Exact match with the ground-truth grade.",
        direction="min",
        threshold=0.80,
        blocking=True,
    ),
    Metric(
        "direction_accuracy",
        "evaluator",
        "grade_direction_correct",
        "Correct answers scored above incorrect ones, even when the grade differs.",
        direction="min",
        threshold=0.95,
        blocking=True,
    ),
    Metric(
        "feedback_usefulness",
        "evaluator",
        "feedback_names_gaps",
        "Does the feedback name specific gaps the learner can act on?",
        meta_graded=True,
    ),
    # --- §8.4 Curator -----------------------------------------------------
    Metric(
        "unlock_respect",
        "curator",
        "prerequisites_satisfied",
        "next_topic choices where every prerequisite is above 0.85.",
        direction="min",
        threshold=1.0,
        blocking=True,
    ),
    Metric(
        "load_bearing_priority",
        "curator",
        "load_bearing_first",
        "Load-bearing concepts chosen before their dependents.",
    ),
    Metric(
        "agenda_coherence",
        "curator",
        "agenda_is_coherent",
        "Is the session agenda a sensible sequence?",
        meta_graded=True,
    ),
    # --- §8.5 Confusion-Tracker -------------------------------------------
    Metric(
        "tracker_precision",
        "confusion_tracker",
        "flag_is_a_real_gap",
        "Flagged entries a reviewer confirms as real gaps.",
        direction="min",
        threshold=0.60,
        blocking=True,
    ),
    Metric(
        "tracker_recall",
        "confusion_tracker",
        "authored_gap_flagged",
        "Authored-gap entries that get flagged.",
        direction="min",
        threshold=0.70,
        blocking=True,
    ),
    Metric(
        "hypothesis_quality",
        "confusion_tracker",
        "hypothesis_is_specific",
        "Does the hypothesis name a misconception rather than a topic?",
        meta_graded=True,
    ),
    # --- §8.6 Reviewer ----------------------------------------------------
    Metric(
        "answer_leakage",
        "reviewer",
        "no_answer_leakage",
        "Does the prompt contain its own answer?",
        direction="max",
        threshold=0.0,
        invert=True,
        blocking=True,
    ),
    Metric(
        "production_requirement",
        "reviewer",
        "requires_production",
        "Does the prompt ask the learner to produce rather than recognise?",
        meta_graded=True,
    ),
    Metric(
        "prompt_variety",
        "reviewer",
        "prompts_vary",
        "Different kinds of prompt across cards for one concept.",
        meta_graded=True,
    ),
    # --- §8.7 Orchestrator ------------------------------------------------
    Metric(
        "classification_accuracy",
        "orchestrator",
        "intent_matches_ground_truth",
        "Exact match with the ground-truth intent.",
        direction="min",
        threshold=0.90,
        blocking=True,
    ),
    Metric(
        "confidence_calibration",
        "orchestrator",
        "confidence_is_calibrated",
        "High confidence when correct, lower when uncertain.",
    ),
)

BY_KEY: dict[str, Metric] = {m.key: m for m in METRICS}
BY_PROPERTY: dict[tuple[str, str], Metric] = {(m.agent, m.property_name): m for m in METRICS}


def for_agent(agent: str) -> tuple[Metric, ...]:
    return tuple(m for m in METRICS if m.agent == agent)


def blocking_for_agent(agent: str) -> tuple[Metric, ...]:
    return tuple(m for m in for_agent(agent) if m.blocking)


@dataclass(frozen=True, slots=True)
class MetricValue:
    metric: Metric
    value: float
    #: How many entries contributed. A metric measured on two of twenty
    #: entries is reported with its ``n`` so nobody reads 1.0 from a sample of
    #: one as a clean bill of health.
    n: int

    @property
    def violated(self) -> bool:
        return self.metric.violated(self.value)

    def render(self) -> str:
        mark = "BLOCK" if self.violated else "ok"
        return (
            f"{mark:5} {self.metric.key:24} {self.value:6.1%}  "
            f"(n={self.n}, {self.metric.render_threshold()})"
        )


def compute(
    agent: str, property_scores: Mapping[str, Sequence[float]]
) -> list[MetricValue]:
    """Aggregate per-property scores into §8's metrics for one agent.

    ``property_scores`` maps ``expected.properties[].name`` to the scores that
    property produced across the run's entries. Properties with no matching
    metric are ignored rather than errored: a dataset may assert things §8 does
    not name as a metric, and that is authoring latitude rather than a mistake.
    """
    values: list[MetricValue] = []
    for metric in for_agent(agent):
        scores = property_scores.get(metric.property_name)
        if not scores:
            continue
        mean = sum(scores) / len(scores)
        values.append(
            MetricValue(
                metric=metric,
                value=(1.0 - mean) if metric.invert else mean,
                n=len(scores),
            )
        )
    return values


def coverage_gaps(agent: str, property_names: Sequence[str]) -> list[str]:
    """Blocking metrics this dataset's properties would never produce.

    The reason this exists: a Lecturer dataset with no ``citations_resolve``
    property leaves §8.1's "citation validity < 100% blocks deploy" with no
    input. :func:`compute` skips the metric, :func:`violations` finds nothing to
    violate, and the gate reports green -- having measured nothing. That is
    strictly worse than having no threshold, because the green is read as
    evidence.

    Reported as a warning by ``studium eval validate`` rather than an error, so
    a dataset can be built up incrementally; the CI gate treats a gap on a
    blocking metric as a failure, which is where it needs to bite.
    """
    declared = set(property_names)
    return [
        f"no property named {m.property_name!r}, so {m.key} "
        f"({m.render_threshold()}) is never measured"
        for m in blocking_for_agent(agent)
        if m.property_name not in declared
    ]


def violations(values: Sequence[MetricValue]) -> list[str]:
    """§8's blocking thresholds, as gate reasons."""
    return [
        f"{v.metric.key} is {v.value:.1%} over {v.n} entries, "
        f"which {v.metric.render_threshold()} (§8, {v.metric.agent})"
        for v in values
        if v.violated
    ]
