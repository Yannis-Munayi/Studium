"""Regression tolerance and affected-set detection (evaluation §17 Tier 1).

§17: "Regression tolerance arithmetic: ``should_block(current, previous,
tolerance)`` returns correct results across boundary cases."

The boundary cases are the point. §13.2 says "5% aggregate drop **before**
blocking" and "up to 2 previously-passing entries **may** fail", so both limits
are inclusive -- a drop of exactly 0.05 proceeds and a drop of 0.0500001 does
not. An off-by-one here either blocks changes that should ship or ships changes
that should not, and neither is visible without a test that sits on the line.
"""

from __future__ import annotations

import pytest

from studium.eval.regression import (
    EntryOutcome,
    RunOutcome,
    affected_datasets,
    should_block,
)

TOLERANCE = {"aggregate_score_drop_max": 0.05, "per_entry_failure_max": 2}


def run(score: float, *entries: tuple[str, bool]) -> RunOutcome:
    return RunOutcome(
        dataset_slug="d",
        aggregate_score=score,
        entries=tuple(
            EntryOutcome(key=k, passed=p, score=1.0 if p else 0.0) for k, p in entries
        ),
    )


class TestAggregateDrop:
    def test_no_baseline_cannot_regress(self) -> None:
        """A first run has nothing to have regressed from. §8's absolute
        thresholds still apply -- they are a different gate."""
        result = should_block(run(0.1), None, TOLERANCE)
        assert not result.blocked

    def test_improvement_never_blocks(self) -> None:
        assert not should_block(run(0.9), run(0.5), TOLERANCE).blocked

    def test_identical_scores_do_not_block(self) -> None:
        assert not should_block(run(0.8), run(0.8), TOLERANCE).blocked

    @pytest.mark.parametrize(
        ("previous", "current"),
        [
            # 0.80 - 0.75 is 0.050000000000000044 in IEEE 754. Without the
            # slack in should_block, a drop of exactly the stated tolerance
            # blocks -- making the gate tighter than the number it prints, on
            # precisely the boundary an author lands on when they have tuned a
            # change to sit at the limit.
            (0.80, 0.75),
            (0.90, 0.85),
            (0.35, 0.30),
            (1.00, 0.95),
        ],
    )
    def test_drop_exactly_at_the_limit_proceeds(
        self, previous: float, current: float
    ) -> None:
        """§13.2: "5% aggregate drop *before* blocking" -- inclusive."""
        result = should_block(run(current), run(previous), TOLERANCE)
        assert not result.blocked, result.reasons

    def test_drop_past_the_limit_blocks(self) -> None:
        result = should_block(run(0.7499), run(0.80), TOLERANCE)
        assert result.blocked
        assert "aggregate score dropped" in result.reasons[0]

    def test_the_tolerance_is_absolute_not_relative(self) -> None:
        """A relative reading would give the weakest dataset the tightest gate:
        one at 0.20 tolerating a drop to 0.19 while one at 0.95 tolerated a
        drop to 0.9025. Backwards, and not what the unit says."""
        # 0.20 -> 0.16 is 0.04 absolute (allowed) but 20% relative (would block).
        assert not should_block(run(0.16), run(0.20), TOLERANCE).blocked

    def test_score_delta_is_reported_signed(self) -> None:
        assert should_block(run(0.9), run(0.8), TOLERANCE).score_delta == pytest.approx(0.1)


class TestPerEntryFailures:
    def test_two_newly_failing_entries_proceed(self) -> None:
        """§13.2: "up to 2 previously-passing entries *may* fail"."""
        previous = run(1.0, ("a", True), ("b", True), ("c", True))
        current = run(1.0, ("a", False), ("b", False), ("c", True))
        result = should_block(current, previous, TOLERANCE)
        assert not result.blocked
        assert result.newly_failing == ("a", "b")

    def test_three_newly_failing_entries_block(self) -> None:
        previous = run(1.0, ("a", True), ("b", True), ("c", True))
        current = run(1.0, ("a", False), ("b", False), ("c", False))
        result = should_block(current, previous, TOLERANCE)
        assert result.blocked
        assert "previously-passing" in result.reasons[0]

    def test_an_entry_that_was_already_failing_is_not_newly_failing(self) -> None:
        previous = run(0.5, ("a", False), ("b", True))
        current = run(0.5, ("a", False), ("b", True))
        assert should_block(current, previous, TOLERANCE).newly_failing == ()

    def test_newly_passing_entries_are_reported_but_do_not_gate(self) -> None:
        """A change that fixes four entries and breaks one is a different
        conversation from one that only breaks one."""
        previous = run(0.5, ("a", False), ("b", False))
        current = run(1.0, ("a", True), ("b", True))
        result = should_block(current, previous, TOLERANCE)
        assert result.newly_passing == ("a", "b")
        assert not result.blocked

    def test_entries_added_since_the_baseline_are_ignored(self) -> None:
        """A new entry has nothing to have regressed from; counting its failure
        as a regression would make adding coverage a blocking act.

        The aggregate is held level on purpose. Three new failing entries drag
        the real aggregate down and would block on *that* limit -- which is
        correct behaviour and a different rule. Varying both at once would let
        this pass while the entry comparison was broken.
        """
        previous = run(1.0, ("a", True))
        current = run(1.0, ("a", True), ("b", False), ("c", False), ("d", False))
        result = should_block(current, previous, TOLERANCE)
        assert result.newly_failing == ()
        assert not result.blocked

    def test_a_dropped_aggregate_still_blocks_when_the_entries_are_new(self) -> None:
        """The other half of the rule above: new entries are exempt from the
        per-entry limit and their effect on the aggregate is not."""
        previous = run(1.0, ("a", True))
        current = run(0.25, ("a", True), ("b", False), ("c", False), ("d", False))
        assert should_block(current, previous, TOLERANCE).blocked

    def test_entries_removed_since_the_baseline_are_ignored(self) -> None:
        previous = run(1.0, ("a", True), ("b", True))
        current = run(1.0, ("a", True))
        assert should_block(current, previous, TOLERANCE).newly_failing == ()


class TestEitherViolationBlocks:
    def test_score_ok_entries_bad(self) -> None:
        previous = run(1.0, ("a", True), ("b", True), ("c", True))
        current = run(1.0, ("a", False), ("b", False), ("c", False))
        assert should_block(current, previous, TOLERANCE).blocked

    def test_entries_ok_score_bad(self) -> None:
        assert should_block(run(0.1), run(0.9), TOLERANCE).blocked

    def test_both_bad_reports_both_reasons(self) -> None:
        """§13.2: "Both must hold ... Either violation blocks." The reviewer
        seeing one reason would fix it and hit the other."""
        previous = run(1.0, ("a", True), ("b", True), ("c", True))
        current = run(0.1, ("a", False), ("b", False), ("c", False))
        assert len(should_block(current, previous, TOLERANCE).reasons) == 2

    def test_a_custom_tolerance_is_honoured(self) -> None:
        strict = {"aggregate_score_drop_max": 0.0, "per_entry_failure_max": 0}
        previous = run(1.0, ("a", True))
        current = run(0.99, ("a", False))
        assert should_block(current, previous, strict).blocked


class TestAffectedDatasets:
    DATASETS = [
        ("lecturer_grounding", "agent_output", "lecturer"),
        ("tutor_socratic", "agent_output", "tutor"),
        ("retrieval_quality", "retrieval_quality", None),
    ]

    def test_an_agent_prompt_selects_that_agent(self) -> None:
        result = affected_datasets(["studium/agents/lecturer.py"], self.DATASETS)
        assert result.slugs == ("lecturer_grounding",)
        assert not result.unresolved

    def test_the_backend_prefix_is_normalised(self) -> None:
        """`git diff --name-only` emits `backend/studium/...` from the repo
        root and `studium/...` from backend/, and the gate runs from both."""
        assert affected_datasets(
            ["backend/studium/agents/lecturer.py"], self.DATASETS
        ).slugs == ("lecturer_grounding",)

    def test_windows_separators_are_normalised(self) -> None:
        assert affected_datasets(
            ["studium\\agents\\lecturer.py"], self.DATASETS
        ).slugs == ("lecturer_grounding",)

    def test_a_shared_prompt_helper_selects_every_agent(self) -> None:
        """§13.4's "change to a helper function that affects multiple prompts".
        build_prefix assembles all seven, so the honest answer is all of them."""
        result = affected_datasets(["studium/llm/prompts.py"], self.DATASETS)
        assert set(result.slugs) == {"lecturer_grounding", "tutor_socratic"}

    def test_a_retrieval_change_selects_the_retrieval_dataset(self) -> None:
        assert affected_datasets(
            ["studium/retrieval/search.py"], self.DATASETS
        ).slugs == ("retrieval_quality",)

    def test_a_prompt_with_no_dataset_is_unresolved_not_empty(self) -> None:
        """The failure mode this exists to prevent: a change ships unevaluated
        because the tooling shrugged and reported "nothing affected"."""
        result = affected_datasets(
            ["studium/agents/curator.py"], self.DATASETS
        )
        assert result.slugs == ()
        assert result.unresolved == ("studium/agents/curator.py",)

    def test_an_unrelated_path_selects_nothing_and_is_not_unresolved(self) -> None:
        result = affected_datasets(["README.md"], self.DATASETS)
        assert result.slugs == () and result.unresolved == ()

    def test_selection_reasons_are_reported(self) -> None:
        result = affected_datasets(["studium/agents/lecturer.py"], self.DATASETS)
        assert "lecturer prompt" in result.because["lecturer_grounding"]

    def test_two_paths_selecting_one_dataset_do_not_duplicate_it(self) -> None:
        result = affected_datasets(
            ["studium/agents/lecturer.py", "studium/llm/prompts.py"], self.DATASETS
        )
        assert len(result.slugs) == len(set(result.slugs))
