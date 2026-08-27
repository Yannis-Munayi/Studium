"""Run execution, grading and fixtures (evaluation §17 Tier 1, §4, §16).

No database and no model calls: the agents are fakes and ``persist`` is not
called. What this covers is the part §16 is about -- that a failing entry
fails *that entry* and the run continues -- plus the fixture derivation §3
depends on for two runs of one entry to be comparable at all.
"""

from __future__ import annotations

import uuid

import pytest

from studium.agents.base import AgentInput, AgentOutput, StreamChunk
from studium.eval import fixtures, runner
from studium.eval.datasets import Entry, parse_dataset
from studium.eval.grading import Grader, MetaGrader, aggregate_score
from studium.eval.runner import RunAborted


def dataset(tmp_path, properties: list[str], *, entries: int = 1, kind="agent_output"):
    lines = [
        "dataset:",
        "  slug: fixture_dataset",
        f"  kind: {kind}",
        "  agent: lecturer",
        "  description: A fixture dataset.",
        "entries:",
    ]
    for index in range(entries):
        lines += [
            f"  - id: entry_{index:03d}",
            "    input:",
            "      concept: {title: Beta-reduction}",
            "      stance: formal",
            "      retrieved_passages:",
            "        - {id: chunk_a, text: first passage}",
            "        - {id: chunk_b, text: second passage}",
            "    expected:",
            "      properties:",
        ]
        lines += [f"        {line}" for line in properties]
    path = tmp_path / "d.yaml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return parse_dataset(path)


CITES_BOTH = ["- name: citations_resolve", "  check: citation_targets_valid"]


class FakeLecturer:
    """A streaming agent, because deliver_segment streams (agent runtime §20)."""

    identity = "lecturer"

    def __init__(self, text: str = "Grounded claim [P1] and another [P2].") -> None:
        self.text = text
        self.retriever = None
        self.seen: list[AgentInput] = []

    async def handle_streaming(self, agent_input: AgentInput):
        self.seen.append(agent_input)
        yield StreamChunk.text_chunk(self.text)
        yield StreamChunk.ended(turn_id=None)


class ExplodingLecturer(FakeLecturer):
    async def handle_streaming(self, agent_input: AgentInput):
        raise RuntimeError("provider is down")
        yield  # pragma: no cover -- makes this an async generator


class FakeCurator:
    """A non-streaming, structured agent."""

    identity = "curator"

    async def handle(self, agent_input: AgentInput) -> AgentOutput:
        from studium.agents.schemas import MetaGrade

        return AgentOutput(text="", structured=MetaGrade(score=0.9, verdict="fine"))


class TestFixtureContexts:
    def test_ids_are_derived_so_two_runs_are_comparable(self) -> None:
        """§3 pins the runtime context by seeded fixture. A uuid4 here would
        change the prefix on every run, defeating the prompt cache and making
        two runs of one entry incomparable at the byte level."""
        first = fixtures.build_context({}, dataset_slug="d", entry_key="e")
        second = fixtures.build_context({}, dataset_slug="d", entry_key="e")
        assert first.session_id == second.session_id
        assert first.user_id == second.user_id

    def test_different_entries_get_different_ids(self) -> None:
        a = fixtures.build_context({}, dataset_slug="d", entry_key="a")
        b = fixtures.build_context({}, dataset_slug="d", entry_key="b")
        assert a.session_id != b.session_id

    def test_started_at_is_frozen(self) -> None:
        """SessionContext.minutes_remaining reads it and the Curator's pacing
        reads that; a wall clock would make the prompt differ by time of day."""
        context = fixtures.build_context({}, dataset_slug="d", entry_key="e")
        assert context.session["started_at"] == fixtures.FIXTURE_STARTED_AT

    def test_no_concept_is_valid(self) -> None:
        """§8.7's Orchestrator dataset is utterance + state -> intent, with no
        focus concept."""
        context = fixtures.build_context({}, dataset_slug="d", entry_key="e")
        assert context.focus_concept_id is None

    def test_passages_keep_the_order_decided_at_parse_time(self, tmp_path) -> None:
        """Re-sorting here on the derived chunk_id would reorder against the
        authored order -- uuid5 output is unrelated to the slug it came from --
        so [P1] in the agent's context would name a different chunk than the
        citation check resolves it to, and the check would pass anyway."""
        entry = dataset(tmp_path, CITES_BOTH).entries[0]
        context = fixtures.build_context(
            entry.input, dataset_slug="d", entry_key=entry.key
        )
        authored = [p["id"] for p in entry.input["retrieved_passages"]]
        derived = [
            fixtures._chunk_uuid(name, "d", entry.key) for name in authored
        ]
        assert [p.chunk_id for p in context.passages] == derived

    def test_mastery_defaults_to_zero_for_the_focus_concept(self) -> None:
        context = fixtures.build_context(
            {"concept": {"title": "T"}}, dataset_slug="d", entry_key="e"
        )
        assert context.focus_mastery == 0.0


class TestFixtureRetriever:
    async def test_returns_the_entry_passages_verbatim(self, tmp_path) -> None:
        """§8.1's dataset shape makes the passages an input the author
        controls. Lecturer.handle_streaming always calls its retriever, so
        without this the run grounds against the live corpus and reports the
        result as a prompt score."""
        entry = dataset(tmp_path, CITES_BOTH).entries[0]
        context = fixtures.build_context(entry.input, dataset_slug="d", entry_key="e")
        retriever = fixtures.FixtureRetriever(list(context.passages))

        result = await retriever.retrieve_passages(uuid.uuid4(), stance="formal", k=6)
        assert [p.chunk_id for p in result.passages] == [
            p.chunk_id for p in context.passages
        ]

    async def test_does_not_truncate_to_k(self, tmp_path) -> None:
        """The author already decided what this entry supplies; truncating
        would make a deliberately-thin entry look well-grounded or the
        reverse."""
        entry = dataset(tmp_path, CITES_BOTH).entries[0]
        context = fixtures.build_context(entry.input, dataset_slug="d", entry_key="e")
        retriever = fixtures.FixtureRetriever(list(context.passages))
        result = await retriever.retrieve_passages(uuid.uuid4(), k=1)
        assert len(result.passages) == 2

    async def test_thin_grounding_is_detected_but_not_enqueued(self) -> None:
        """A fixture's thin grounding is the author's choice; writing a review
        row for it would fill the reviewer's queue with the harness's own test
        cases."""
        retriever = fixtures.FixtureRetriever([])
        result = await retriever.retrieve_passages(uuid.uuid4())
        assert result.thin_grounding
        assert result.review_queue_id is None


class TestRunDataset:
    async def test_a_passing_entry(self, tmp_path) -> None:
        report = await runner.run_dataset(
            dataset(tmp_path, CITES_BOTH), agent=FakeLecturer(), live=False
        )
        assert report.entries_passed == 1
        assert report.aggregate_score == 1.0

    async def test_a_failing_entry(self, tmp_path) -> None:
        report = await runner.run_dataset(
            dataset(tmp_path, CITES_BOTH),
            agent=FakeLecturer("Invented citation [P9]."),
            live=False,
        )
        assert report.entries_failed == 1
        assert report.aggregate_score == 0.0
        assert "[P9]" in report.results[0].grade.notes

    async def test_an_agent_failure_fails_that_entry_and_the_run_continues(
        self, tmp_path
    ) -> None:
        """§16 row 1. A run that aborted on entry 3 of 20 has spent money and
        produced no baseline, which is the worst of both."""
        report = await runner.run_dataset(
            dataset(tmp_path, CITES_BOTH, entries=3),
            agent=ExplodingLecturer(),
            live=False,
        )
        assert len(report.results) == 3
        assert report.entries_failed == 3
        assert all("failed after retries" in r.grade.notes for r in report.results)

    async def test_one_bad_entry_among_good_ones(self, tmp_path) -> None:
        good = dataset(tmp_path, CITES_BOTH, entries=3)
        report = await runner.run_dataset(good, agent=FakeLecturer(), live=False)
        assert report.entries_passed == 3
        assert len(report.outcome().entries) == 3

    async def test_a_missing_agent_is_refused_before_anything_runs(
        self, tmp_path
    ) -> None:
        report = await runner.run_dataset(dataset(tmp_path, CITES_BOTH), agent=None, live=False)
        assert report.entries_failed == 1
        assert "needs an agent under test" in report.results[0].grade.notes

    async def test_the_retriever_is_swapped_for_the_fixture_one(
        self, tmp_path
    ) -> None:
        agent = FakeLecturer()
        await runner.run_dataset(
            dataset(tmp_path, CITES_BOTH), agent=agent, live=False
        )
        assert isinstance(agent.retriever, fixtures.FixtureRetriever)

    async def test_a_stub_entry_is_skipped_rather_than_scored(self, tmp_path) -> None:
        """Grading a stub scores whatever the agent did against nothing and
        reports a pass for the very defect the stub was created from."""
        path = tmp_path / "d.yaml"
        path.write_text(
            "dataset: {slug: stub_dataset, kind: agent_output, agent: lecturer,"
            " description: x}\n"
            "entries:\n"
            "  - id: defect_abc\n"
            "    input: {concept: {title: T}}\n"
            "    expected: {stub: true}\n",
            encoding="utf-8",
        )
        report = await runner.run_dataset(parse_dataset(path), agent=FakeLecturer(), live=False)
        assert not report.results[0].grade.passed
        assert "stub" in report.results[0].grade.notes

    async def test_prompt_hash_is_always_64_characters(self, tmp_path) -> None:
        """evaluation_runs.prompt_hash is NOT NULL with a length CHECK."""
        report = await runner.run_dataset(
            dataset(tmp_path, CITES_BOTH), agent=FakeLecturer(), live=False
        )
        assert len(report.prompt_hash) == 64

    async def test_actual_output_records_what_the_checks_scored(
        self, tmp_path
    ) -> None:
        """So a reviewer looking at a failed check sees the exact string, not a
        re-derivation of which of text/structured was used."""
        report = await runner.run_dataset(
            dataset(tmp_path, CITES_BOTH), agent=FakeLecturer(), live=False
        )
        assert report.results[0].actual_output["graded_text"]


class TestRetrievalRuns:
    async def test_a_retrieval_dataset_needs_a_retriever(self, tmp_path) -> None:
        path = tmp_path / "r.yaml"
        path.write_text(
            "dataset: {slug: retrieval_fixture, kind: retrieval_quality,"
            " description: x}\n"
            "entries:\n"
            "  - id: retrieval_001\n"
            "    input: {concept_id: c, k: 6}\n"
            "    expected: {relevant_chunks: [a]}\n",
            encoding="utf-8",
        )
        report = await runner.run_dataset(parse_dataset(path), retriever=None, live=False)
        assert "needs a retriever" in report.results[0].grade.notes


class TestGrading:
    async def test_a_hybrid_entry_averages_its_properties_unweighted(
        self, tmp_path
    ) -> None:
        """§7.2 declares hybrid entries and never says how the halves combine.
        Weighting would need a per-property weight the YAML has no field for,
        and a default would silently express an opinion about which kind of
        evidence is better."""
        entry = Entry(
            key="e",
            index=0,
            input={"retrieved_passages": [{"id": "a", "text": "x"}]},
            expected={
                "properties": [
                    {"name": "citations_resolve", "check": "citation_targets_valid"},
                    {"name": "m", "check": "meta_graded", "rubric": "r"},
                ]
            },
            grading_kind="hybrid",
        )
        grade = await Grader().grade_entry(entry, output="[P1]")
        # Deterministic passes (1.0); meta has no grader configured (0.0).
        assert grade.score == 0.5
        assert not grade.passed

    async def test_an_entry_passes_only_when_every_property_passes(
        self, tmp_path
    ) -> None:
        """A property-level pass rate would let an entry with nine trivial
        properties and one invalid citation report 90% -- and §8.1 blocks
        citation validity below 100%."""
        entry = Entry(
            key="e",
            index=0,
            input={"retrieved_passages": [{"id": "a", "text": "x"}]},
            expected={
                "properties": [
                    {"name": "citations_resolve", "check": "citation_targets_valid"},
                    {"name": "words", "check": "word_count_between", "min": 500, "max": 900},
                ]
            },
            grading_kind="deterministic",
        )
        grade = await Grader().grade_entry(entry, output="[P1]")
        assert not grade.passed
        assert grade.score > 0.0

    async def test_a_meta_grader_failure_fails_the_property_with_a_reason(
        self,
    ) -> None:
        """§16 row 2. Skipping the property instead would make an Evaluator
        outage look like a clean run with fewer properties."""

        class Exploding:
            async def handle(self, agent_input):
                raise RuntimeError("evaluator is down")

        entry = Entry(
            key="e",
            index=0,
            input={},
            expected={"properties": [{"name": "m", "check": "meta_graded", "rubric": "r"}]},
            grading_kind="meta_graded",
        )
        grader = Grader(meta_grader=MetaGrader(Exploding()))
        grade = await grader.grade_entry(
            entry,
            output="x",
            # A real context: AgentInput validates it, so object() would
            # make this exercise pydantic rather than §16 row 2 -- and the
            # note reads "meta-grader failed" either way.
            context=fixtures.build_context({}, dataset_slug="d", entry_key="e"),
            agent_under_test="lecturer",
        )
        assert not grade.passed
        assert "meta-grader failed" in grade.notes

    async def test_meta_grade_scores_are_clamped(self) -> None:
        """MetaGrade declares no ge/le -- the structured-output API carries no
        numeric bounds -- so the clamp is here, in front of the column's CHECK."""
        from studium.agents.schemas import MetaGrade

        class Overshooting:
            async def handle(self, agent_input):
                return AgentOutput(structured=MetaGrade(score=1.7, verdict="v"))

        entry = Entry(
            key="e",
            index=0,
            input={},
            expected={"properties": [{"name": "m", "check": "meta_graded", "rubric": "r"}]},
            grading_kind="meta_graded",
        )
        grader = Grader(meta_grader=MetaGrader(Overshooting()))
        grade = await grader.grade_entry(
            entry,
            output="x",
            context=fixtures.build_context({}, dataset_slug="d", entry_key="e"),
        )
        assert grade.score == 1.0

    def test_ungradable_entries_count_as_zero_in_the_aggregate(self) -> None:
        """Excluding them would let an outage that broke half the entries
        report the aggregate of the surviving half, which reads as a pass."""
        from studium.eval.grading import EntryGrade

        grades = [
            EntryGrade("a", error="agent call failed after retries"),
            EntryGrade("b", error="agent call failed after retries"),
        ]
        assert aggregate_score(grades) == 0.0


class TestModelResolution:
    def test_a_retrieval_dataset_pins_the_configuration_not_a_prompt(
        self, tmp_path
    ) -> None:
        """There is no prompt to pin, and §3's point is that a result is
        meaningless unless what produced it is pinned. For retrieval that is
        the provider and the rerank setting."""
        path = tmp_path / "r.yaml"
        path.write_text(
            "dataset: {slug: retrieval_fixture, kind: retrieval_quality,"
            " description: x}\n"
            "entries:\n"
            "  - id: retrieval_001\n"
            "    input: {concept_id: c}\n"
            "    expected: {relevant_chunks: [a]}\n",
            encoding="utf-8",
        )
        assert "rerank" in runner._resolve_model(parse_dataset(path), rerank=True)

    def test_an_agent_dataset_resolves_through_the_routing_table(
        self, tmp_path
    ) -> None:
        model = runner._resolve_model(dataset(tmp_path, CITES_BOTH), rerank=True)
        assert model and "rerank" not in model


class TestRunAborted:
    def test_persisting_an_unsynced_dataset_names_the_fix(self) -> None:
        assert "eval sync" in str(
            RunAborted("dataset 'x' is not in the database; run `studium eval sync` first")
        )


@pytest.mark.parametrize("agent", sorted(runner.DEFAULT_KIND))
def test_every_agent_has_a_default_invocation_kind(agent: str) -> None:
    """A dataset whose entries do not name a kind must still run. An agent
    missing from this table raises RunAborted at the first entry, after the
    run row has been created."""
    assert runner.DEFAULT_KIND[agent]
    assert agent in runner.FIXTURE_MODE
