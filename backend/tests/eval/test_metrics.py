"""Per-agent metrics and retrieval metrics (evaluation §17 Tier 1, §8, §9.2).

Two things are being guarded here that the spec leaves implicit.

**Every blocking threshold §8 states must be representable.** §8 states them in
prose, in two directions ("< 90% blocks", "> 5% blocks"), against metrics that
sometimes count a good thing and sometimes a bad one. A metric wired up with
the wrong direction or a missing ``invert`` fails silently: it reports a number
and never fires. The parametrised sweep below asserts each one fires on the
wrong side of its own threshold and does not fire on the right side.

**A metric nothing measures is worse than no metric**, because the green is
read as evidence. :func:`coverage_gaps` is what catches that, and it is tested
here rather than trusted.
"""

from __future__ import annotations

import pytest

from studium.eval import metrics
from studium.eval.metrics import BY_KEY, METRICS, Metric, compute, coverage_gaps


class TestMetricTable:
    def test_every_agent_has_at_least_one_metric(self) -> None:
        """§8 gives all seven a "key metrics" list."""
        agents = {m.agent for m in METRICS}
        assert agents == {
            "lecturer",
            "tutor",
            "evaluator",
            "curator",
            "confusion_tracker",
            "reviewer",
            "orchestrator",
        }

    def test_every_agent_has_at_least_one_blocking_threshold(self) -> None:
        """§8 states one for each of the seven. An agent with none can ship any
        regression at all as long as the aggregate holds."""
        for agent in {m.agent for m in METRICS}:
            assert metrics.blocking_for_agent(agent), f"{agent} has no blocking metric"

    @pytest.mark.parametrize("metric", METRICS, ids=lambda m: m.key)
    def test_blocking_metrics_declare_a_threshold(self, metric: Metric) -> None:
        assert not metric.blocking or metric.threshold is not None

    @pytest.mark.parametrize("metric", METRICS, ids=lambda m: m.key)
    def test_non_blocking_metrics_do_not_fire(self, metric: Metric) -> None:
        """§8 sends these "to reviewer for judgment". A non-blocking metric
        that returned True from violated() would gate on a model's opinion."""
        assert metric.blocking or not metric.violated(0.0)

    @pytest.mark.parametrize("metric", METRICS, ids=lambda m: m.key)
    def test_property_names_are_unique_per_agent(self, metric: Metric) -> None:
        """Two metrics on one agent sharing a property name would both read the
        same scores, and one of them would be measuring the wrong thing."""
        clashes = [
            m
            for m in METRICS
            if m.agent == metric.agent
            and m.property_name == metric.property_name
            and m.key != metric.key
        ]
        assert not clashes, f"{metric.key} shares a property with {[c.key for c in clashes]}"


class TestThresholdsMatchTheSpec:
    """§8's numbers, each asserted on both sides of its own line."""

    @pytest.mark.parametrize(
        ("key", "fires", "holds"),
        [
            # §8.1 "Grounding rate < 90% blocks deploy."
            ("grounding_rate", 0.89, 0.90),
            # §8.1 "Citation validity < 100% blocks deploy."
            ("citation_validity", 0.99, 1.0),
            # §8.2 "Fabrication rate > 5% blocks deploy."
            ("fabrication_rate", 0.06, 0.05),
            # §8.3 "Scoring accuracy < 80% blocks deploy."
            ("scoring_accuracy", 0.79, 0.80),
            # §8.3 "Direction accuracy < 95% blocks deploy."
            ("direction_accuracy", 0.94, 0.95),
            # §8.4 "Unlock respect < 100% blocks deploy."
            ("unlock_respect", 0.99, 1.0),
            # §8.5 "Precision < 60% or recall < 70% blocks deploy."
            ("tracker_precision", 0.59, 0.60),
            ("tracker_recall", 0.69, 0.70),
            # §8.6 "Answer leakage > 0% blocks deploy (categorical)."
            ("answer_leakage", 0.01, 0.0),
            # §8.7 "Classification accuracy < 90% blocks deploy."
            ("classification_accuracy", 0.89, 0.90),
        ],
    )
    def test_threshold(self, key: str, fires: float, holds: float) -> None:
        metric = BY_KEY[key]
        assert metric.blocking, f"{key} must be a blocking metric"
        assert metric.violated(fires), f"{key} should block at {fires}"
        assert not metric.violated(holds), f"{key} should not block at {holds}"


class TestCompute:
    def test_a_metric_reads_its_declared_property(self) -> None:
        values = compute("lecturer", {"cites_provided_passages": [1.0, 0.5]})
        assert [v.metric.key for v in values] == ["grounding_rate"]
        assert values[0].value == pytest.approx(0.75)
        assert values[0].n == 2

    def test_inverted_metrics_count_the_bad_thing(self) -> None:
        """The check scores 1.0 for "no fabrication"; §8.2's metric is the
        fabrication *rate*. Without the inversion a clean run would report a
        100% fabrication rate and block every deploy."""
        values = compute("tutor", {"no_fabrication": [1.0, 1.0, 1.0, 1.0]})
        rate = next(v for v in values if v.metric.key == "fabrication_rate")
        assert rate.value == 0.0
        assert not rate.violated

    def test_an_inverted_metric_fires_when_the_bad_thing_happens(self) -> None:
        values = compute("tutor", {"no_fabrication": [1.0] * 9 + [0.0]})
        rate = next(v for v in values if v.metric.key == "fabrication_rate")
        assert rate.value == pytest.approx(0.10)
        assert rate.violated

    def test_answer_leakage_is_categorical(self) -> None:
        """§8.6: "> 0% blocks deploy". One leak in twenty is still a leak."""
        values = compute("reviewer", {"no_answer_leakage": [1.0] * 19 + [0.0]})
        leak = next(v for v in values if v.metric.key == "answer_leakage")
        assert leak.violated

    def test_properties_with_no_matching_metric_are_ignored(self) -> None:
        """A dataset may assert things §8 does not name; that is authoring
        latitude, not an error."""
        assert compute("lecturer", {"some_bespoke_property": [1.0]}) == []

    def test_a_metric_with_no_scores_is_absent_not_zero(self) -> None:
        """Absent means unmeasured. Reporting 0.0 would block every deploy on a
        metric the dataset never exercised."""
        values = compute("lecturer", {"cites_provided_passages": [1.0]})
        assert "citation_validity" not in {v.metric.key for v in values}

    def test_n_is_reported_so_a_tiny_sample_is_visible(self) -> None:
        """1.0 from a sample of one is not a clean bill of health."""
        values = compute("lecturer", {"cites_provided_passages": [1.0]})
        assert values[0].n == 1
        assert "n=1" in values[0].render()


class TestCoverageGaps:
    def test_a_dataset_missing_a_blocking_property_is_flagged(self) -> None:
        """The failure this exists to prevent: compute() skips the metric,
        violations() finds nothing to violate, and the gate reports green
        having measured nothing. DIVERGENCES-EVALUATION (E7)."""
        gaps = coverage_gaps("lecturer", ["cites_provided_passages"])
        assert any("citations_resolve" in g for g in gaps)

    def test_a_fully_covered_dataset_has_no_gaps(self) -> None:
        gaps = coverage_gaps(
            "lecturer", ["cites_provided_passages", "citations_resolve"]
        )
        assert gaps == []

    def test_non_blocking_metrics_are_not_gaps(self) -> None:
        """§8 sends them to the reviewer; a dataset that skips one has made a
        choice, not a mistake."""
        gaps = coverage_gaps(
            "lecturer", ["cites_provided_passages", "citations_resolve"]
        )
        assert not any("stance" in g or "length" in g for g in gaps)

    @pytest.mark.parametrize("agent", sorted({m.agent for m in METRICS}))
    def test_an_empty_dataset_gaps_every_blocking_metric(self, agent: str) -> None:
        assert len(coverage_gaps(agent, [])) == len(metrics.blocking_for_agent(agent))


class TestViolations:
    def test_a_violation_names_the_metric_the_value_and_the_rule(self) -> None:
        """The reviewer reads this in CI output and has to act on it."""
        values = compute("lecturer", {"citations_resolve": [1.0, 0.0]})
        reported = metrics.violations(values)
        assert len(reported) == 1
        assert "citation_validity" in reported[0]
        assert "50.0%" in reported[0]
        assert "§8" in reported[0]

    def test_a_clean_run_reports_nothing(self) -> None:
        values = compute("lecturer", {"citations_resolve": [1.0, 1.0]})
        assert metrics.violations(values) == []
