"""Deterministic check correctness (evaluation §17 Tier 1).

§17 asks for two things this file provides:

* "Deterministic check correctness: for each check kind, fixtures with known
  inputs produce expected results."
* "Coverage guard on the check-registry test: fails if fewer than N check kinds
  are exercised."

The coverage guard is the load-bearing one and it is not a formality. The
registry is populated by import side effects, so a refactor that stopped
importing the module would leave every parametrised test collecting zero cases
and reporting green having verified nothing -- the same shape of vacuous-test
failure that ingestion §16's provenance guard exists to catch.
"""

from __future__ import annotations

import pytest

from studium.eval import checks
from studium.eval.checks import REGISTRY, CheckError, CheckOutcome

#: §17's N. Every check §7.2 enumerates, counted: at_least_n_citations,
#: word_count_between, contains_all_of, contains_none_of,
#: structured_output_matches_schema, numeric_answer_within_tolerance,
#: citation_targets_valid.
MINIMUM_CHECK_KINDS = 7


def _entry_input(*chunk_ids: str) -> dict:
    """An entry input whose passages are already in citation order.

    Order matters and is decided once, by ``datasets.order_passages``. These
    fixtures pass ids already sorted so the ordinal mapping here is the one a
    parsed dataset would produce.
    """
    return {
        "retrieved_passages": [{"id": cid, "text": f"text of {cid}"} for cid in sorted(chunk_ids)]
    }


class TestRegistry:
    def test_registry_is_populated(self) -> None:
        """§17's coverage guard. A registry emptied by a refactor makes every
        parametrised test below collect nothing and report green."""
        assert len(REGISTRY) >= MINIMUM_CHECK_KINDS, (
            f"only {len(REGISTRY)} check kinds registered; §7.2 enumerates "
            f"{MINIMUM_CHECK_KINDS}. A check that stopped being imported is "
            f"invisible to every test in this file."
        )

    def test_every_spec_named_check_exists(self) -> None:
        for name in (
            "at_least_n_citations",
            "word_count_between",
            "contains_all_of",
            "contains_none_of",
            "structured_output_matches_schema",
            "numeric_answer_within_tolerance",
            "citation_targets_valid",
        ):
            assert name in REGISTRY, f"§7.2 enumerates {name!r}; it is not registered"

    def test_meta_graded_is_known_but_not_a_check(self) -> None:
        """It is a routing marker, not a function. Both facts matter: YAML may
        name it, and nothing may call it as a deterministic check."""
        assert checks.META_GRADED in checks.known()
        assert checks.META_GRADED not in REGISTRY

    def test_unknown_check_names_the_alternatives(self) -> None:
        with pytest.raises(CheckError, match="at_least_n_citations"):
            checks.get("at_least_n_citation")

    @pytest.mark.parametrize("name", sorted(REGISTRY))
    def test_every_check_has_a_docstring(self, name: str) -> None:
        """The docstring is what an author reads to know what a check does;
        `studium eval validate` has no other description to offer."""
        assert REGISTRY[name].doc.strip(), f"{name} has no docstring"


class TestOutcome:
    def test_score_outside_the_range_is_refused(self) -> None:
        """evaluation_results.score has a CHECK on 0..1. Catching it here names
        the check that produced it; catching it at INSERT names a constraint."""
        with pytest.raises(CheckError, match="0.0-1.0"):
            CheckOutcome(True, 1.5, "note")


class TestAtLeastNCitations:
    def test_counts_distinct_markers(self) -> None:
        outcome = checks.at_least_n_citations(
            "First claim [P1] and second [P2].", {"n": 2}, _entry_input("a", "b")
        )
        assert outcome.passed
        assert outcome.score == 1.0

    def test_partial_credit_is_the_fraction_of_n(self) -> None:
        """Not binary: §13.2 gates on aggregate drift, and a segment that went
        from citing two passages to one must move the number."""
        outcome = checks.at_least_n_citations(
            "Only one [P1].", {"n": 2}, _entry_input("a", "b")
        )
        assert not outcome.passed
        assert outcome.score == 0.5

    def test_repeated_marker_counts_once(self) -> None:
        outcome = checks.at_least_n_citations(
            "[P1] and again [P1] and again [P1].", {"n": 2}, _entry_input("a", "b")
        )
        assert not outcome.passed, "three markers naming one passage is one citation"
        assert outcome.score == 0.5

    def test_from_restricts_by_chunk_id_through_the_ordinal(self) -> None:
        """`from` names chunk ids; the text carries ordinals. Getting the
        mapping backwards makes a check pass on the wrong evidence."""
        entry = _entry_input("chunk_a", "chunk_b")
        # sorted -> chunk_a is [P1], chunk_b is [P2].
        assert checks.at_least_n_citations(
            "[P1]", {"n": 1, "from": ["chunk_a"]}, entry
        ).passed
        assert not checks.at_least_n_citations(
            "[P1]", {"n": 1, "from": ["chunk_b"]}, entry
        ).passed

    def test_n_below_one_is_an_authoring_error(self) -> None:
        with pytest.raises(CheckError):
            checks.at_least_n_citations("x", {"n": 0}, _entry_input("a"))


class TestWordCountBetween:
    def test_inside_the_band(self) -> None:
        outcome = checks.word_count_between("word " * 250, {"min": 200, "max": 400}, {})
        assert outcome.passed and outcome.score == 1.0

    def test_citation_markers_are_not_words(self) -> None:
        """A segment with twelve citations would otherwise be twelve words
        closer to the ceiling than the same prose with two -- penalising the
        grounding §8.1's other metric rewards."""
        body = ("word " * 10) + ("[P1] " * 10)
        bare = checks.word_count_between(body, {"min": 10, "max": 10}, {})
        assert bare.passed, bare.note

    def test_score_decays_rather_than_dropping_to_zero(self) -> None:
        """410 words against a 400 ceiling is a near miss; 4000 is not, and the
        aggregate has to be able to tell them apart."""
        near = checks.word_count_between("w " * 410, {"min": 200, "max": 400}, {})
        far = checks.word_count_between("w " * 4000, {"min": 200, "max": 400}, {})
        assert not near.passed and not far.passed
        assert near.score > far.score
        assert far.score == 0.0

    def test_inverted_band_is_an_authoring_error(self) -> None:
        with pytest.raises(CheckError, match="exceeds"):
            checks.word_count_between("x", {"min": 400, "max": 200}, {})


class TestContains:
    def test_all_of_scores_the_fraction_present(self) -> None:
        outcome = checks.contains_all_of(
            "alpha and beta", {"strings": ["alpha", "beta", "gamma"]}, {}
        )
        assert not outcome.passed
        assert outcome.score == pytest.approx(2 / 3)
        assert "gamma" in outcome.note

    def test_none_of_is_binary(self) -> None:
        """One forbidden phrase is a failure and two are not twice the failure."""
        one = checks.contains_none_of("has eta here", {"strings": ["eta", "zeta"]}, {})
        two = checks.contains_none_of("eta and zeta", {"strings": ["eta", "zeta"]}, {})
        assert one.score == two.score == 0.0

    def test_case_insensitive_by_default(self) -> None:
        assert not checks.contains_none_of("Extensionality", {"strings": ["extensionality"]}, {}).passed
        assert checks.contains_none_of(
            "Extensionality", {"strings": ["extensionality"], "case_sensitive": True}, {}
        ).passed

    def test_empty_string_list_is_an_authoring_error(self) -> None:
        with pytest.raises(CheckError, match="non-empty"):
            checks.contains_all_of("x", {"strings": []}, {})


class TestCitationTargetsValid:
    def test_valid_markers_pass(self) -> None:
        outcome = checks.citation_targets_valid("[P1] and [P2]", {}, _entry_input("a", "b"))
        assert outcome.passed and outcome.score == 1.0

    def test_invented_citation_fails_and_is_named(self) -> None:
        """§8.1: an invalid citation is a hallucinated reference and blocks
        deploy. The reviewer's first question is which one."""
        outcome = checks.citation_targets_valid("[P1] and [P9]", {}, _entry_input("a", "b"))
        assert not outcome.passed
        assert outcome.score == 0.0
        assert "[P9]" in outcome.note

    def test_binary_because_the_threshold_is_100_percent(self) -> None:
        one_bad = checks.citation_targets_valid("[P9]", {}, _entry_input("a", "b"))
        two_bad = checks.citation_targets_valid("[P9] [P8]", {}, _entry_input("a", "b"))
        assert one_bad.score == two_bad.score == 0.0

    def test_no_citations_at_all_is_valid(self) -> None:
        """Ungrounded, but not *invented*. §8.1 keeps grounding rate and
        citation validity as separate metrics for this reason."""
        assert checks.citation_targets_valid("no markers", {}, _entry_input("a")).passed


class TestNumericAnswerWithinTolerance:
    def test_exact_match(self) -> None:
        outcome = checks.numeric_answer_within_tolerance("the grade is 2", {"expected": 2}, {})
        assert outcome.passed and outcome.score == 1.0

    def test_default_tolerance_is_exact(self) -> None:
        """§8.3's scoring accuracy is exact match. Grades are IN (0,1,2) and
        'close' is meaningless there."""
        assert not checks.numeric_answer_within_tolerance("1", {"expected": 2}, {}).passed

    def test_within_tolerance_scores_partial(self) -> None:
        outcome = checks.numeric_answer_within_tolerance(
            "0.84", {"expected": 0.85, "tolerance": 0.05}, {}
        )
        assert outcome.passed
        assert 0.0 < outcome.score < 1.0

    def test_no_number_is_a_failure_not_a_crash(self) -> None:
        outcome = checks.numeric_answer_within_tolerance("no digits", {"expected": 2}, {})
        assert not outcome.passed and outcome.score == 0.0

    def test_negative_tolerance_is_an_authoring_error(self) -> None:
        with pytest.raises(CheckError):
            checks.numeric_answer_within_tolerance("1", {"expected": 1, "tolerance": -1}, {})


class TestStructuredOutputMatchesSchema:
    def test_valid_payload_passes(self) -> None:
        outcome = checks.structured_output_matches_schema(
            '{"score": 0.8, "verdict": "good"}', {"schema": "MetaGrade"}, {}
        )
        assert outcome.passed

    def test_invalid_payload_fails_with_a_count(self) -> None:
        outcome = checks.structured_output_matches_schema(
            '{"verdict": "no score"}', {"schema": "MetaGrade"}, {}
        )
        assert not outcome.passed
        assert "error" in outcome.note

    def test_non_json_fails_rather_than_raising(self) -> None:
        outcome = checks.structured_output_matches_schema(
            "not json at all", {"schema": "MetaGrade"}, {}
        )
        assert not outcome.passed

    def test_unknown_schema_is_an_authoring_error(self) -> None:
        with pytest.raises(CheckError, match="studium.agents.schemas"):
            checks.structured_output_matches_schema("{}", {"schema": "NoSuchModel"}, {})

    def test_arbitrary_dotted_paths_are_refused(self) -> None:
        """An allow-list, not an import. A YAML file naming any dotted path
        would be an import-anything primitive in a directory a reviewer edits
        -- the same reasoning that makes ingestion use yaml.safe_load."""
        with pytest.raises(CheckError):
            checks.structured_output_matches_schema(
                "{}", {"schema": "os.system"}, {}
            )


class TestParameterValidation:
    def test_missing_required_parameter_is_reported(self) -> None:
        problems = checks.get("word_count_between").validate_params(
            {"name": "x", "check": "word_count_between", "min": 10}
        )
        assert any("max" in p for p in problems)

    def test_unknown_parameter_is_reported_with_the_accepted_set(self) -> None:
        problems = checks.get("word_count_between").validate_params(
            {"name": "x", "check": "word_count_between", "min": 1, "max": 2, "maxx": 3}
        )
        assert any("maxx" in p and "accepts" in p for p in problems)

    def test_name_and_check_are_not_parameters(self) -> None:
        assert not checks.get("citation_targets_valid").validate_params(
            {"name": "citations_resolve", "check": "citation_targets_valid"}
        )
