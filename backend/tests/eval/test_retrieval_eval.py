"""Retrieval quality metrics (evaluation §17 Tier 1, §9).

The arithmetic of §9.2, the A/B of §9.3, and the swap gate of §9.4 -- none of
which touch a provider, which is what lets them live in Tier 1 while the
measurements they enable live in Tier 3.

The two departures from §9.2's stated formulas are asserted here rather than
only argued in a docstring, because both are the kind of thing a later reader
would "fix" back to the spec's wording.
"""

from __future__ import annotations

import pytest

from studium.eval.retrieval_eval import (
    Aggregate,
    EntryMetrics,
    Judgment,
    aggregate,
    compare_reranker,
    evaluate_entry,
    provider_swap_gate,
    ranked_ids,
)

JUDGMENT = Judgment(
    relevant=frozenset({"a", "b", "c"}), misleading=frozenset({"x"})
)


class TestRecall:
    def test_all_relevant_found(self) -> None:
        assert evaluate_entry(["a", "b", "c"], JUDGMENT, k=6).recall == 1.0

    def test_divides_by_the_relevant_set_not_the_retrieved_count(self) -> None:
        """§9.2's formula is |retrieved ∩ relevant| / |relevant|. Its prose
        says something else ("of the k retrieved chunks, how many are in the
        relevant list") which describes precision. The formula is right."""
        assert evaluate_entry(["a"], JUDGMENT, k=6).recall == pytest.approx(1 / 3)

    def test_junk_does_not_reduce_recall(self) -> None:
        assert evaluate_entry(
            ["a", "b", "c", "z", "y"], JUDGMENT, k=6
        ).recall == 1.0

    def test_nothing_retrieved_is_zero_not_an_error(self) -> None:
        """Returning nothing is a real answer retrieval is allowed to give
        (retrieval §6: "never raises for lack of results")."""
        assert evaluate_entry([], JUDGMENT, k=6).recall == 0.0

    def test_only_the_top_k_count(self) -> None:
        assert evaluate_entry(["z", "y", "a"], JUDGMENT, k=2).recall == 0.0


class TestPrecision:
    def test_divides_by_what_was_returned_not_by_k(self) -> None:
        """DIVERGENCES-EVALUATION (E8). §9.2 divides by k. Retrieval returns
        fewer than k routinely -- retrieval §13 makes thin grounding a normal
        flagged state -- and dividing by k charges the retriever for passages
        that do not exist. Here: two relevant chunks exist, both returned.
        That is precision 1.0, not 2/6."""
        thin = Judgment(relevant=frozenset({"a", "b"}))
        assert evaluate_entry(["a", "b"], thin, k=6).precision == 1.0

    def test_junk_reduces_precision(self) -> None:
        assert evaluate_entry(["a", "z"], JUDGMENT, k=6).precision == 0.5

    def test_nothing_retrieved_is_zero_not_a_division_error(self) -> None:
        assert evaluate_entry([], JUDGMENT, k=6).precision == 0.0


class TestMisleadingRate:
    def test_counts_only_the_judged_misleading_set(self) -> None:
        """§9.2's point: an off-topic chunk and merely-adjacent material are
        different failures, and precision punishes them identically."""
        assert evaluate_entry(["a", "x"], JUDGMENT, k=6).misleading_rate == 0.5

    def test_adjacent_material_is_neither_relevant_nor_misleading(self) -> None:
        """The third, unnamed tier. It costs precision and not the misleading
        rate, which is the distinction the second metric exists to draw."""
        measured = evaluate_entry(["a", "unjudged"], JUDGMENT, k=6)
        assert measured.misleading_rate == 0.0
        assert measured.precision == 0.5

    def test_zero_when_nothing_misleading_returned(self) -> None:
        assert evaluate_entry(["a", "b"], JUDGMENT, k=6).misleading_rate == 0.0


class TestCuratedPreference:
    def test_none_when_the_entry_expressed_no_stance_preference(self) -> None:
        """None rather than 0.0: an entry that set no test must not drag the
        mean down as though retrieval had failed one."""
        assert evaluate_entry(["a"], JUDGMENT, k=6).curated_preference is None

    def test_reciprocal_rank_of_the_first_stance_appropriate_chunk(self) -> None:
        judgment = Judgment(
            relevant=frozenset({"a", "b"}), stance_appropriate=frozenset({"b"})
        )
        assert evaluate_entry(["a", "b"], judgment, k=6).curated_preference == 0.5
        assert evaluate_entry(["b", "a"], judgment, k=6).curated_preference == 1.0

    def test_zero_when_no_stance_appropriate_chunk_is_returned(self) -> None:
        judgment = Judgment(
            relevant=frozenset({"a"}), stance_appropriate=frozenset({"b"})
        )
        assert evaluate_entry(["a"], judgment, k=6).curated_preference == 0.0


class TestEntryScore:
    def test_bounded_to_zero_one(self) -> None:
        """evaluation_results.score has a CHECK on the range."""
        worst = evaluate_entry(["x", "x"], JUDGMENT, k=6)
        assert 0.0 <= worst.score <= 1.0

    def test_misleading_results_score_below_merely_imprecise_ones(self) -> None:
        misleading = evaluate_entry(["a", "x"], JUDGMENT, k=6)
        adjacent = evaluate_entry(["a", "unjudged"], JUDGMENT, k=6)
        assert misleading.score < adjacent.score


class TestRankedIds:
    def test_sorts_by_relevance_descending(self) -> None:
        """RetrievalResult.passages is in chunk_id order (retrieval §12's
        citation contract). Scoring that order grades the reranker as though it
        never ran -- which is the one thing §9.3 exists to measure."""

        class P:
            def __init__(self, chunk_id: str, score: float | None) -> None:
                self.chunk_id, self.relevance_score = chunk_id, score

        assert ranked_ids([P("a", 0.1), P("b", 0.9)]) == ["b", "a"]

    def test_unscored_passages_sort_last_in_stable_order(self) -> None:
        class P:
            def __init__(self, chunk_id: str, score: float | None) -> None:
                self.chunk_id, self.relevance_score = chunk_id, score

        assert ranked_ids([P("a", None), P("b", None), P("c", 0.5)]) == ["c", "a", "b"]


class TestAggregate:
    def test_empty_is_zeroed_rather_than_erroring(self) -> None:
        assert aggregate([]).entries == 0

    def test_means_across_entries(self) -> None:
        result = aggregate(
            [
                EntryMetrics(recall=1.0, precision=1.0, misleading_rate=0.0),
                EntryMetrics(recall=0.0, precision=0.0, misleading_rate=1.0),
            ]
        )
        assert result.recall == 0.5
        assert result.misleading_rate == 0.5
        assert result.entries == 2

    def test_stance_preference_averages_only_entries_that_set_one(self) -> None:
        result = aggregate(
            [
                EntryMetrics(recall=1, precision=1, misleading_rate=0, curated_preference=1.0),
                EntryMetrics(recall=1, precision=1, misleading_rate=0, curated_preference=None),
            ]
        )
        assert result.curated_preference == 1.0


class TestRerankerAB:
    """§9.3, answering retrieval §19 open question 3."""

    def _arm(self, recall: float) -> list[EntryMetrics]:
        return [EntryMetrics(recall=recall, precision=recall, misleading_rate=0.0)]

    def test_reports_deltas_without_drawing_the_conclusion(self) -> None:
        """§9.3 step 4 gives the decision to the reviewer, who has the per-call
        cost this module cannot see."""
        result = compare_reranker(self._arm(0.8), self._arm(0.6))
        assert result.recall_delta == pytest.approx(0.2)
        assert "reviewer" in result.render()

    def test_per_entry_deltas_expose_a_mean_driven_by_one_entry(self) -> None:
        with_rerank = [
            EntryMetrics(recall=1.0, precision=1, misleading_rate=0),
            EntryMetrics(recall=0.5, precision=1, misleading_rate=0),
        ]
        without = [
            EntryMetrics(recall=0.0, precision=1, misleading_rate=0),
            EntryMetrics(recall=0.5, precision=1, misleading_rate=0),
        ]
        result = compare_reranker(with_rerank, without)
        assert result.entries_improved == 1
        assert result.entries_worsened == 0

    def test_mismatched_arms_are_refused(self) -> None:
        """An A/B over different entries compares two different questions."""
        with pytest.raises(ValueError, match="same entries"):
            compare_reranker(self._arm(0.8), self._arm(0.6) * 2)


class TestProviderSwapGate:
    """§9.4."""

    BASE = Aggregate(recall=0.80, precision=0.70, misleading_rate=0.10, entries=20)

    def test_an_identical_provider_passes(self) -> None:
        assert provider_swap_gate(self.BASE, self.BASE).allowed

    def test_a_small_recall_drop_is_allowed(self) -> None:
        candidate = Aggregate(recall=0.76, precision=0.70, misleading_rate=0.10, entries=20)
        assert provider_swap_gate(self.BASE, candidate).allowed

    def test_a_recall_drop_past_the_tolerance_blocks(self) -> None:
        candidate = Aggregate(recall=0.70, precision=0.70, misleading_rate=0.10, entries=20)
        result = provider_swap_gate(self.BASE, candidate)
        assert not result.allowed
        assert "recall@6" in result.reasons[0]

    def test_any_increase_in_the_misleading_rate_blocks(self) -> None:
        """§9.4 permits no increase, and the asymmetry against recall is the
        spec's and is right: more nearby-but-wrong content reaching learners is
        the failure retrieval §13 exists to catch. There is no exchange rate."""
        candidate = Aggregate(recall=0.80, precision=0.70, misleading_rate=0.11, entries=20)
        result = provider_swap_gate(self.BASE, candidate)
        assert not result.allowed
        assert "misleading" in result.reasons[0]

    def test_an_improvement_everywhere_passes(self) -> None:
        candidate = Aggregate(recall=0.90, precision=0.80, misleading_rate=0.05, entries=20)
        assert provider_swap_gate(self.BASE, candidate).allowed

    def test_differing_entry_counts_block(self) -> None:
        """A gate run over a different entry set would pass or fail for reasons
        unrelated to the provider."""
        candidate = Aggregate(recall=0.90, precision=0.80, misleading_rate=0.0, entries=5)
        assert not provider_swap_gate(self.BASE, candidate).allowed

    def test_an_empty_candidate_blocks(self) -> None:
        assert not provider_swap_gate(self.BASE, Aggregate()).allowed
