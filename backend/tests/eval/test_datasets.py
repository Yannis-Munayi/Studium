"""Golden dataset parsing and validation (evaluation §17 Tier 1).

§17: "YAML parsing: valid golden dataset YAML parses cleanly; malformed YAML
raises with specific errors" and "Check function registry: every check name in
a YAML entry exists in studium.eval.checks".

The shipped datasets under ``content/evaluation/`` are parsed here too. A
dataset that stopped parsing would otherwise be discovered by the CI gate,
after it had spent money getting there.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from studium.eval.datasets import (
    DEFAULT_TOLERANCE,
    DatasetError,
    order_passages,
    parse_dataset,
    parse_directory,
)

CONTENT_ROOT = Path(__file__).resolve().parents[2] / "content" / "evaluation"


def write(tmp_path: Path, body: str, name: str = "d.yaml") -> Path:
    path = tmp_path / name
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    return path


VALID = """
    dataset:
      slug: lecturer_test
      kind: agent_output
      agent: lecturer
      version: 1
      description: A test dataset.
    entries:
      - id: entry_001
        input:
          concept: {title: Beta-reduction}
          retrieved_passages:
            - {id: chunk_b, text: second}
            - {id: chunk_a, text: first}
        expected:
          properties:
            - name: citations_resolve
              check: citation_targets_valid
"""


class TestValidParsing:
    def test_minimal_dataset_parses(self, tmp_path: Path) -> None:
        dataset = parse_dataset(write(tmp_path, VALID))
        assert dataset.slug == "lecturer_test"
        assert dataset.agent == "lecturer"
        assert len(dataset.entries) == 1

    def test_grading_kind_is_derived_from_the_properties(self, tmp_path: Path) -> None:
        assert parse_dataset(write(tmp_path, VALID)).entries[0].grading_kind == "deterministic"

    def test_tolerance_defaults_to_the_spec_example(self, tmp_path: Path) -> None:
        assert parse_dataset(write(tmp_path, VALID)).regression_tolerance == DEFAULT_TOLERANCE

    def test_passages_are_ordered_at_parse_time(self, tmp_path: Path) -> None:
        """The citation numbering is decided once. If it were decided twice --
        here and again in fixtures or checks -- the two agreeing would be a
        coincidence, and a `from`-restricted check would resolve ordinals to
        the wrong chunks while still passing."""
        entry = parse_dataset(write(tmp_path, VALID)).entries[0]
        ids = [p["id"] for p in entry.input["retrieved_passages"]]
        assert ids == ["chunk_a", "chunk_b"], "authored order was not normalised"

    def test_order_passages_is_idempotent(self) -> None:
        once = order_passages({"retrieved_passages": [{"id": "b"}, {"id": "a"}]})
        assert order_passages(once) == once

    def test_order_passages_leaves_other_inputs_alone(self) -> None:
        assert order_passages({"utterance": "hi"}) == {"utterance": "hi"}


class TestGradingKindDerivation:
    """Assembled line by line rather than with nested dedent: a helper whose
    own indentation is subtly wrong produces a YAML error, and a YAML error
    passes `pytest.raises(DatasetError)` while testing nothing."""

    def _entry(
        self, tmp_path: Path, properties: list[str], declared: str | None = None
    ) -> object:
        lines = [
            "dataset:",
            "  slug: lecturer_test",
            "  kind: agent_output",
            "  agent: lecturer",
            "  description: A test dataset.",
            "entries:",
            "  - id: entry_001",
            "    input: {concept: {title: T}}",
        ]
        if declared:
            lines.append(f"    grading_kind: {declared}")
        lines.append("    expected:")
        lines.append("      properties:")
        lines.extend(f"        {line}" for line in properties)

        path = tmp_path / "d.yaml"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return parse_dataset(path).entries[0]

    META = [
        "- name: uses_requested_stance",
        "  check: meta_graded",
        "  rubric: Formal language throughout.",
    ]
    DETERMINISTIC = [
        "- name: citations_resolve",
        "  check: citation_targets_valid",
    ]

    def test_deterministic_only(self, tmp_path: Path) -> None:
        assert self._entry(tmp_path, self.DETERMINISTIC).grading_kind == "deterministic"

    def test_meta_only(self, tmp_path: Path) -> None:
        assert self._entry(tmp_path, self.META).grading_kind == "meta_graded"

    def test_hybrid(self, tmp_path: Path) -> None:
        """§5's column comment allows two values; §7.2's own worked example
        declares a third. Three it is -- DIVERGENCES-EVALUATION (E2)."""
        entry = self._entry(tmp_path, self.DETERMINISTIC + self.META)
        assert entry.grading_kind == "hybrid"

    def test_declared_kind_contradicting_the_properties_is_refused(
        self, tmp_path: Path
    ) -> None:
        """A 'deterministic' entry carrying a meta_graded property would skip
        the Evaluator call and report a pass on a property nothing evaluated."""
        with pytest.raises(DatasetError, match="properties are what get graded"):
            self._entry(tmp_path, self.META, declared="deterministic")

    def test_declared_kind_matching_the_properties_is_accepted(
        self, tmp_path: Path
    ) -> None:
        assert self._entry(
            tmp_path, self.META, declared="meta_graded"
        ).grading_kind == "meta_graded"

    def test_meta_graded_without_a_rubric_is_refused(self, tmp_path: Path) -> None:
        """Without one the Evaluator judges against nothing and returns a
        confident number anyway."""
        with pytest.raises(DatasetError, match="needs a rubric"):
            self._entry(
                tmp_path,
                ["- name: uses_requested_stance", "  check: meta_graded"],
            )


class TestMalformedInput:
    def test_empty_file(self, tmp_path: Path) -> None:
        with pytest.raises(DatasetError, match="empty"):
            parse_dataset(write(tmp_path, ""))

    def test_invalid_yaml_names_the_file(self, tmp_path: Path) -> None:
        with pytest.raises(DatasetError, match="invalid YAML"):
            parse_dataset(write(tmp_path, "dataset: [unclosed"))

    def test_missing_dataset_header(self, tmp_path: Path) -> None:
        with pytest.raises(DatasetError, match="dataset:"):
            parse_dataset(write(tmp_path, "entries: []"))

    def test_unknown_check_name_is_caught_before_a_run(self, tmp_path: Path) -> None:
        """§17: "every check name in a YAML entry exists in
        studium.eval.checks". Caught at validate time, because a run that
        discovers it on entry 14 of 20 has already spent fourteen entries."""
        with pytest.raises(DatasetError, match="unknown check"):
            parse_dataset(
                write(
                    tmp_path,
                    """
                    dataset: {slug: fixture_ds, kind: agent_output, agent: lecturer, description: x}
                    entries:
                      - id: entry_001
                        input: {concept: {title: T}}
                        expected:
                          properties:
                            - name: p
                              check: at_least_n_citation
                              n: 2
                    """,
                )
            )

    def test_bad_check_parameters_are_caught(self, tmp_path: Path) -> None:
        with pytest.raises(DatasetError, match="requires parameter"):
            parse_dataset(
                write(
                    tmp_path,
                    """
                    dataset: {slug: fixture_ds, kind: agent_output, agent: lecturer, description: x}
                    entries:
                      - id: entry_001
                        input: {concept: {title: T}}
                        expected:
                          properties:
                            - name: p
                              check: word_count_between
                              min: 10
                    """,
                )
            )

    def test_every_problem_is_reported_not_just_the_first(self, tmp_path: Path) -> None:
        """An author fixing a 20-entry dataset wants the list, not twenty
        edit-run cycles."""
        with pytest.raises(DatasetError) as exc:
            parse_dataset(
                write(
                    tmp_path,
                    """
                    dataset: {slug: BAD SLUG, kind: nonsense, description: ""}
                    entries:
                      - id: "not a key"
                        input: {}
                    """,
                )
            )
        assert len(exc.value.problems) >= 3

    def test_entry_with_no_properties_is_refused(self, tmp_path: Path) -> None:
        """An entry that asserts nothing cannot fail, which makes it a
        permanent free pass in the aggregate."""
        with pytest.raises(DatasetError, match="at least one property"):
            parse_dataset(
                write(
                    tmp_path,
                    """
                    dataset: {slug: fixture_ds, kind: agent_output, agent: lecturer, description: x}
                    entries:
                      - id: entry_001
                        input: {concept: {title: T}}
                        expected: {properties: []}
                    """,
                )
            )


class TestKindAgentAgreement:
    def test_agent_output_needs_an_agent(self, tmp_path: Path) -> None:
        """A dataset with no agent is never selected by §13.4's affected-set
        lookup, so the prompt change it was meant to guard ships unevaluated."""
        with pytest.raises(DatasetError, match="agent is required"):
            parse_dataset(
                write(
                    tmp_path,
                    """
                    dataset: {slug: fixture_ds, kind: agent_output, description: x}
                    entries:
                      - id: e
                        input: {concept: {title: T}}
                        expected: {properties: [{name: p, check: citation_targets_valid}]}
                    """,
                )
            )

    def test_retrieval_quality_must_not_name_an_agent(self, tmp_path: Path) -> None:
        with pytest.raises(DatasetError, match="does not test"):
            parse_dataset(
                write(
                    tmp_path,
                    """
                    dataset: {slug: fixture_ds, kind: retrieval_quality, agent: lecturer, description: x}
                    entries:
                      - id: e
                        input: {concept_id: c}
                        expected: {relevant_chunks: [a]}
                    """,
                )
            )

    def test_grading_calibration_must_name_the_evaluator(self, tmp_path: Path) -> None:
        """§7.2: it is meta-evaluation of the grader, so it is the Evaluator or
        it is nothing."""
        with pytest.raises(DatasetError, match="evaluator"):
            parse_dataset(
                write(
                    tmp_path,
                    """
                    dataset: {slug: fixture_ds, kind: grading_calibration, agent: tutor, description: x}
                    entries:
                      - id: e
                        input: {answer: x}
                        expected: {properties: [{name: p, check: citation_targets_valid}]}
                    """,
                )
            )


class TestRetrievalEntries:
    def _parse(self, tmp_path: Path, expected: str) -> object:
        return parse_dataset(
            write(
                tmp_path,
                f"""
                dataset: {{slug: fixture_r, kind: retrieval_quality, description: x}}
                entries:
                  - id: retrieval_001
                    input: {{concept_id: c, k: 6}}
                    expected: {expected}
                """,
            )
        )

    def test_valid_entry(self, tmp_path: Path) -> None:
        dataset = self._parse(tmp_path, "{relevant_chunks: [a, b], misleading_chunks: [c]}")
        assert dataset.entries[0].grading_kind == "deterministic"

    def test_empty_relevant_set_is_refused(self, tmp_path: Path) -> None:
        """recall@k divides by it; an empty list makes every result score 1.0."""
        with pytest.raises(DatasetError, match="relevant_chunks"):
            self._parse(tmp_path, "{relevant_chunks: []}")

    def test_a_chunk_cannot_be_both_relevant_and_misleading(self, tmp_path: Path) -> None:
        """§9.1 is a two-tier judgment, not overlapping sets: such a chunk
        would score as a recall hit and a misleading hit at once, which is not
        a judgment anyone made."""
        with pytest.raises(DatasetError, match="both relevant and misleading"):
            self._parse(tmp_path, "{relevant_chunks: [a], misleading_chunks: [a]}")


class TestTolerance:
    def test_unknown_key_is_reported(self, tmp_path: Path) -> None:
        with pytest.raises(DatasetError, match="unknown key"):
            parse_dataset(
                write(
                    tmp_path,
                    VALID.replace(
                        "  version: 1",
                        "  regression_tolerance: {aggregate_score_drop_maximum: 0.05}",
                    ),
                )
            )

    def test_a_tolerance_above_one_can_never_block(self, tmp_path: Path) -> None:
        """Scores are 0..1, so this configures a gate that never fires -- which
        reads in review like a gate that is on."""
        with pytest.raises(DatasetError, match="can never block"):
            parse_dataset(
                write(
                    tmp_path,
                    VALID.replace(
                        "  version: 1",
                        "  regression_tolerance: {aggregate_score_drop_max: 2.0}",
                    ),
                )
            )


class TestStubs:
    def test_stub_entries_parse_but_are_not_runnable(self, tmp_path: Path) -> None:
        """`review flag --to-dataset` commits these. They must be legal in the
        tree and refused by a run -- otherwise the entry grades whatever the
        agent did against nothing and reports a pass for the defect it was
        created from."""
        dataset = parse_dataset(
            write(
                tmp_path,
                """
                dataset: {slug: fixture_ds, kind: agent_output, agent: lecturer, description: x}
                entries:
                  - id: defect_abc123
                    input: {concept: {title: T}}
                    expected: {stub: true}
                """,
            )
        )
        assert dataset.entries[0].is_stub
        assert dataset.stub_count == 1
        assert not dataset.runnable_entries


class TestDirectory:
    def test_duplicate_slugs_across_files_are_refused(self, tmp_path: Path) -> None:
        """golden_datasets.slug is UNIQUE; catching it here names both files."""
        write(tmp_path, VALID, "one.yaml")
        write(tmp_path, VALID, "two.yaml")
        with pytest.raises(DatasetError, match="already used by"):
            parse_directory(tmp_path)

    def test_problems_from_every_file_are_collected(self, tmp_path: Path) -> None:
        write(tmp_path, "entries: []", "a.yaml")
        write(tmp_path, "entries: []", "b.yaml")
        with pytest.raises(DatasetError) as exc:
            parse_directory(tmp_path)
        assert len(exc.value.problems) >= 2


class TestShippedDatasets:
    """The datasets under content/evaluation/ must parse.

    Without this, a dataset broken by an edit is discovered by the CI gate --
    after it has spent money getting there.
    """

    def test_content_root_exists(self) -> None:
        assert CONTENT_ROOT.is_dir(), f"{CONTENT_ROOT} is missing"

    def test_all_shipped_datasets_parse(self) -> None:
        datasets = parse_directory(CONTENT_ROOT)
        assert datasets, "no datasets shipped"

    def test_shipped_datasets_cover_their_blocking_metrics(self) -> None:
        """§8's thresholds are unenforceable on a dataset whose properties
        never produce them: compute() skips the metric, violations() finds
        nothing, and the gate reports green having measured nothing.
        DIVERGENCES-EVALUATION (E7)."""
        from studium.eval.metrics import coverage_gaps

        for dataset in parse_directory(CONTENT_ROOT):
            if not dataset.agent:
                continue
            names = [
                str(p.get("name", ""))
                for entry in dataset.entries
                for p in entry.properties
            ]
            assert not coverage_gaps(dataset.agent, names), (
                f"{dataset.slug} leaves a blocking metric unmeasured"
            )
