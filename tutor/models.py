"""Pydantic models shared across ingestion, lessons, and assessment.

These double as the JSON schemas for Claude's structured outputs
(`client.messages.parse`), so every response is guaranteed to validate.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Unit pack — produced once per unit by the ingestion pipeline
# ---------------------------------------------------------------------------

class Definition(BaseModel):
    term: str
    definition: str


class PracticeQuestion(BaseModel):
    question: str
    hint: str
    model_answer: str


class Segment(BaseModel):
    title: str
    content: str
    practice: Optional[PracticeQuestion] = None


class RubricCriterion(BaseModel):
    id: str
    concept: str
    key_points: list[str]
    weight: Literal[1, 2, 3]


class UnitPack(BaseModel):
    overview: str
    learning_objectives: list[str]
    key_definitions: list[Definition]
    segments: list[Segment]
    rubric: list[RubricCriterion]


# ---------------------------------------------------------------------------
# Lesson-time practice checking
# ---------------------------------------------------------------------------

class AnswerCheck(BaseModel):
    verdict: Literal["correct", "partially_correct", "incorrect"]
    feedback: str


# ---------------------------------------------------------------------------
# Assessment grading
# ---------------------------------------------------------------------------

class CriterionGrade(BaseModel):
    criterion_id: str
    score: Literal[0, 1, 2]
    feedback: str
    missing_points: list[str]


class GradeReport(BaseModel):
    criterion_grades: list[CriterionGrade]
    overall_feedback: str
