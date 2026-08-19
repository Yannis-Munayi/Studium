"""Tier 1: fusion, stance weighting, and the numbering contract (§9, §11, §17).

Fusion arithmetic is checked against hand-computed values rather than against
whatever the implementation currently returns -- a test that asserts the code's
own output cannot catch the code being wrong.
"""

from __future__ import annotations

import uuid

import pytest

from studium.retrieval.search import Candidate, fuse
from studium.retrieval.types import (
    CURATED_BOOST,
    RRF_K,
    STANCE_BOOST,
    STANCE_ROLES,
    Passage,
    clamp_k,
    sort_for_citation,
    stance_roles,
)

# Fixed ids, ascending as written, so "chunk_id order" is checkable by eye.
IDS = [uuid.UUID(f"00000000-0000-7000-8000-00000000000{i}") for i in range(1, 7)]


def candidate(
    index: int,
    *,
    reason: str = "vector",
    role: str | None = None,
    ranks: dict[str, int] | None = None,
) -> Candidate:
    return Candidate(
        chunk_id=IDS[index],
        text=f"passage {index}",
        source_id=IDS[0],
        source_title="A Source",
        source_authors=["Author, A."],
        page_start=1,
        page_end=1,
        section_path=["Ch 1"],
        chunk_type="body",
        chunk_index=index,
        reason=reason,
        role=role,
        ranks=dict(ranks or {}),
    )


class TestFusionArithmetic:
    """§9 "Fusion", verified against the spec's own worked numbers."""

    def test_rank_one_in_both_modalities(self):
        """§9: "1/61 + 1/61 = 0.0328"."""
        [fused] = fuse([[candidate(0, ranks={"vector": 1, "keyword": 1})]])
        assert fused.fused_score == pytest.approx(2 / 61, rel=1e-9)
        assert fused.fused_score == pytest.approx(0.0328, abs=1e-4)

    def test_rank_one_in_one_modality_only(self):
        """§9: "1/61 = 0.0164"."""
        [fused] = fuse([[candidate(0, ranks={"vector": 1})]])
        assert fused.fused_score == pytest.approx(1 / 61, rel=1e-9)
        assert fused.fused_score == pytest.approx(0.0164, abs=1e-4)

    def test_present_in_both_beats_rank_one_in_one(self):
        """The property RRF exists for: no invented score for the missing side."""
        both = candidate(0, ranks={"vector": 3, "keyword": 3})
        one = candidate(1, ranks={"vector": 1})
        ranked = fuse([[both, one]])
        assert ranked[0].chunk_id == both.chunk_id
        assert ranked[0].fused_score == pytest.approx(2 / (RRF_K + 3))
        assert ranked[1].fused_score == pytest.approx(1 / (RRF_K + 1))

    def test_the_constant_is_sixty(self):
        assert RRF_K == 60


class TestBoosts:
    """§10's curated boost and §11's stance boost, in that order."""

    def test_a_curated_chunk_is_boosted(self):
        plain = candidate(0, ranks={"vector": 1})
        curated = candidate(1, reason="curated", ranks={"vector": 1})
        ranked = fuse([[plain, curated]])
        assert ranked[0].chunk_id == curated.chunk_id
        assert ranked[0].fused_score == pytest.approx((1 / 61) * CURATED_BOOST)

    def test_a_curated_chunk_no_modality_returned_still_scores(self):
        """Otherwise phase 1 contributes nothing exactly when curation matters
        most -- the sparse corpus where search found nothing."""
        [fused] = fuse([[candidate(0, reason="curated")]])
        assert fused.fused_score > 0

    def test_stance_boost_applies_after_the_curated_boost(self):
        curated = candidate(
            0, reason="curated", role="canonical_definition", ranks={"vector": 1}
        )
        [fused] = fuse([[curated]], stance_preferred_roles=STANCE_ROLES["formal"])
        assert fused.fused_score == pytest.approx(
            (1 / 61) * CURATED_BOOST * STANCE_BOOST
        )

    def test_an_unclassified_chunk_gets_no_stance_boost(self):
        """§11: "the stance signal is only meaningful for chunks the curator
        has classified"."""
        plain = candidate(0, ranks={"vector": 1})
        [fused] = fuse([[plain]], stance_preferred_roles=STANCE_ROLES["formal"])
        assert fused.fused_score == pytest.approx(1 / 61)

    def test_stance_is_not_a_hard_filter(self):
        """§11: a formal retrieval still returns a worked example that scores
        highest overall."""
        example = candidate(0, reason="curated", role="worked_example", ranks={"vector": 1, "keyword": 1})
        definition = candidate(1, reason="curated", role="canonical_definition", ranks={"vector": 18})
        ranked = fuse([[example, definition]], stance_preferred_roles=STANCE_ROLES["formal"])
        assert ranked[0].chunk_id == example.chunk_id
        assert len(ranked) == 2, "the unboosted role is still present"


class TestDeduplication:
    def test_the_same_chunk_from_two_modalities_merges(self):
        ranked = fuse(
            [
                [candidate(0, reason="vector", ranks={"vector": 2})],
                [candidate(0, reason="keyword", ranks={"keyword": 5})],
            ]
        )
        assert len(ranked) == 1
        assert ranked[0].ranks == {"vector": 2, "keyword": 5}
        assert ranked[0].fused_score == pytest.approx(1 / 62 + 1 / 65)

    def test_curated_wins_the_reason_when_search_also_found_it(self):
        """§10's audit trail should say an expert chose it, not that a cosine did."""
        ranked = fuse(
            [
                [candidate(0, reason="curated", role="primary_exposition")],
                [candidate(0, reason="vector", ranks={"vector": 1})],
            ]
        )
        assert ranked[0].reason == "curated"
        assert ranked[0].role == "primary_exposition"


class TestDeterministicOrdering:
    def test_tied_scores_break_on_chunk_id(self):
        """Fusion ties are common, and an arbitrary tiebreak would make the
        reranker's input vary between two identical calls."""
        tied = [candidate(i, ranks={"vector": 1}) for i in (3, 1, 2, 0)]
        first = [c.chunk_id for c in fuse([tied])]
        for _ in range(5):
            assert [c.chunk_id for c in fuse([list(reversed(tied))])] == first
        assert first == sorted(first, key=str)


class TestNumberingContract:
    """§3, §17: passages are numbered in chunk_id order, whatever order they
    arrived in."""

    def test_passages_sort_by_chunk_id_regardless_of_input_order(self):
        made = [Passage(chunk_id=IDS[i], text=f"p{i}") for i in range(3)]
        expected = [p.chunk_id for p in sort_for_citation(made)]

        for arrangement in ([2, 0, 1], [1, 2, 0], [2, 1, 0]):
            shuffled = [made[i] for i in arrangement]
            assert [p.chunk_id for p in sort_for_citation(shuffled)] == expected

    def test_the_order_is_ascending_by_string_form(self):
        made = [Passage(chunk_id=IDS[i], text="x") for i in (2, 0, 1)]
        ordered = sort_for_citation(made)
        assert [str(p.chunk_id) for p in ordered] == sorted(str(p.chunk_id) for p in made)


class TestStanceMapping:
    """§17: "Every stance value maps to a defined set of preferred roles"."""

    @pytest.mark.parametrize("stance", ["formal", "intuitive", "applied", "historical", "default"])
    def test_every_stance_maps(self, stance):
        roles = stance_roles(stance)
        assert roles, stance
        assert all(isinstance(r, str) for r in roles)

    def test_the_mapping_matches_the_table_in_section_11(self):
        assert STANCE_ROLES == {
            "formal": ("canonical_definition", "primary_exposition"),
            "intuitive": ("worked_example", "primary_exposition"),
            "applied": ("worked_example", "exercise"),
            "historical": ("historical", "primary_exposition"),
            "default": ("primary_exposition", "canonical_definition"),
        }

    def test_every_preferred_role_is_a_real_enum_value(self):
        """A typo here would silently disable the boost for that stance."""
        from studium.models.base import concept_source_role

        valid = set(concept_source_role.enums)
        for stance, roles in STANCE_ROLES.items():
            assert set(roles) <= valid, f"{stance} names a role the schema lacks"

    def test_the_stance_values_match_the_artifact_stance_enum(self):
        from studium.models.base import artifact_stance

        assert set(STANCE_ROLES) == set(artifact_stance.enums)

    def test_an_unknown_stance_falls_back_rather_than_raising(self):
        """A model returning an unmapped stance should cost a preference, not
        the whole retrieval."""
        assert stance_roles("nonsense") == STANCE_ROLES["default"]


class TestKClamping:
    def test_k_is_bounded_at_twenty(self):
        """§6: "Maximum enforced at 20 to bound reranking cost"."""
        assert clamp_k(500) == 20
        assert clamp_k(6) == 6

    def test_k_is_at_least_one(self):
        assert clamp_k(0) == 1
        assert clamp_k(-3) == 1
