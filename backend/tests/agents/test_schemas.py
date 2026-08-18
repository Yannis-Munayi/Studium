"""Structured-output schema round-trip (agent runtime §23 Tier 1, §3).

§23: "Structured output schema round-trip: every ``AgentOutput.structured``
model serializes to JSON and back losslessly."

The round-trip is what guarantees a schema change cannot quietly break the
JSONB column it lands in -- ``mastery_events.evidence``,
``journal_entries.hypothesis``, ``session_summaries.key_points`` are all fed
from these models.
"""

from __future__ import annotations

import json
import uuid

import pytest
from pydantic import BaseModel, ValidationError

from studium.agents.schemas import (
    ALL_SCHEMAS,
    PRIMITIVE_NAMES,
    ComprehensionCheck,
    CriterionGrade,
    GradeReport,
    Intent,
    IntentClassification,
    PartialCheck,
    RetrievalPrompt,
    TrackerDecision,
)


def _example(model: type[BaseModel]) -> BaseModel:
    """A populated instance of each schema, for the round-trip sweep."""
    examples: dict[str, BaseModel] = {
        "IntentClassification": IntentClassification(
            intent="primitive:im_lost", confidence=0.91, reasoning="says they are lost"
        ),
        "RetrievalPrompt": RetrievalPrompt(
            prompt="State the beta rule.",
            kind="definition",
            model_answer="(\\x. M) N reduces to M[x := N].",
            expected_key_points=["substitution"],
        ),
        "CriterionGrade": CriterionGrade(
            criterion_id=uuid.uuid4(), score=1, feedback="partial", missing_points=["scope"]
        ),
        "TrackerDecision": TrackerDecision(
            action="create_entry",
            summary="Treats reduction order as significant.",
            hypothesis="Has not internalised confluence.",
            reasoning="third occurrence",
        ),
        "PartialCheck": PartialCheck(
            verdict="partially_correct", confident=False, missing_points=["the side condition"]
        ),
        "ComprehensionCheck": ComprehensionCheck(
            question="Why is the order irrelevant?",
            expected_key_points=["confluence"],
            hint="Think about the diamond property.",
            model_answer="Because reduction is confluent.",
        ),
    }
    if model.__name__ in examples:
        return examples[model.__name__]

    # Everything else is constructible from its required fields alone.
    fields: dict[str, object] = {}
    for name, info in model.model_fields.items():
        if not info.is_required():
            continue
        annotation = str(info.annotation)
        if "UUID" in annotation:
            fields[name] = uuid.uuid4()
        elif "Literal" in annotation:
            fields[name] = info.annotation.__args__[0]  # type: ignore[union-attr]
        elif "int" in annotation:
            fields[name] = 1
        elif "float" in annotation:
            fields[name] = 0.5
        else:
            fields[name] = "x"
    return model(**fields)


class TestRoundTrip:
    @pytest.mark.parametrize("model", ALL_SCHEMAS, ids=lambda m: m.__name__)
    def test_serialises_to_json_and_back_losslessly(self, model):
        """The §23 requirement."""
        original = _example(model)
        rebuilt = model.model_validate(json.loads(original.model_dump_json()))
        assert rebuilt == original

    @pytest.mark.parametrize("model", ALL_SCHEMAS, ids=lambda m: m.__name__)
    def test_json_schema_is_generatable(self, model):
        """Structured output needs a JSON Schema; a model that cannot emit one fails at call time."""
        schema = model.model_json_schema()
        assert schema["type"] == "object"

    @pytest.mark.parametrize("model", ALL_SCHEMAS, ids=lambda m: m.__name__)
    def test_no_schema_declares_unsupported_numeric_constraints(self, model):
        """The API does not enforce minimum/maximum or minLength/maxLength.

        Declaring them would read as a stronger guarantee than the API gives --
        they would be validated client-side only, after the model had already
        produced an out-of-range value.
        """
        blob = json.dumps(model.model_json_schema())
        for unsupported in ("minimum", "maximum", "multipleOf", "minLength", "maxLength"):
            assert unsupported not in blob, f"{model.__name__} declares {unsupported}"


class TestIntentSet:
    def test_covers_every_intent_in_section_8(self):
        base = {"question", "answer", "comment", "interrupt", "next", "back", "end_session"}
        declared = set(Intent.__args__)  # type: ignore[attr-defined]
        assert base <= declared

    def test_primitives_are_enumerated_not_free_form(self):
        """A free-form `primitive:{name}` would let the model invent a handler-less one."""
        declared = set(Intent.__args__)  # type: ignore[attr-defined]
        for name in PRIMITIVE_NAMES:
            assert f"primitive:{name}" in declared

    def test_primitive_accessor_extracts_the_name(self):
        c = IntentClassification(
            intent="primitive:vocabulary_check", confidence=0.9, reasoning="asks about a word"
        )
        assert c.primitive == "vocabulary_check"

    def test_non_primitive_intent_has_no_primitive(self):
        c = IntentClassification(intent="question", confidence=0.9, reasoning="has a ?")
        assert c.primitive is None

    def test_an_invented_intent_is_rejected(self):
        with pytest.raises(ValidationError):
            IntentClassification(intent="primitive:teleport", confidence=0.9, reasoning="")


class TestGradingSchemas:
    def test_score_outside_zero_to_two_is_rejected_by_the_schema(self):
        """§12's first failure mode; an enum is the one bound the API enforces."""
        with pytest.raises(ValidationError):
            CriterionGrade(criterion_id=uuid.uuid4(), score=3)

    def test_grade_report_accepts_multiple_criteria(self):
        report = GradeReport(
            criterion_grades=[
                CriterionGrade(criterion_id=uuid.uuid4(), score=2),
                CriterionGrade(criterion_id=uuid.uuid4(), score=0),
            ],
            overall_feedback="mixed",
        )
        assert len(report.criterion_grades) == 2


class TestTrackerValidity:
    @pytest.mark.parametrize(
        ("decision", "valid"),
        [
            (TrackerDecision(action="none"), True),
            (TrackerDecision(action="create_entry", summary="s", hypothesis="h"), True),
            (TrackerDecision(action="create_entry", summary="s"), False),
            (TrackerDecision(action="create_entry"), False),
            (TrackerDecision(action="revise_entry", entry_id=uuid.uuid4(), hypothesis="h"), True),
            (TrackerDecision(action="revise_entry", hypothesis="h"), False),
            (TrackerDecision(action="flag_resolved", entry_id=uuid.uuid4()), True),
            (TrackerDecision(action="flag_resolved"), False),
        ],
    )
    def test_conditional_required_fields_are_checked_in_code(self, decision, valid):
        """§13 states per-action requirements a JSON Schema cannot express.

        An entry with a null summary would show the learner a blank journal
        card, so the check lives in application code and the tracker drops an
        invalid decision rather than writing one.
        """
        assert decision.is_valid() is valid

    def test_origin_is_pinned_to_tracker_inferred(self):
        """journal_entries.origin CHECKs the allowed set."""
        assert TrackerDecision(action="none").origin == "tracker_inferred"
