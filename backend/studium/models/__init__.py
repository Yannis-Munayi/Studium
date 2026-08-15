"""SQLAlchemy models for the Studium data layer (spec §6).

Importing this package registers every table on ``Base.metadata``, which is
what Alembic's ``target_metadata`` and the §14 schema tests read.
"""

from __future__ import annotations

from .assessment import AssessmentAttempt, AssessmentResponse
from .base import (
    ALL_ENUMS,
    APPEND_ONLY_TABLES,
    SOFT_DELETE_TABLES,
    TRIGGERED_TABLES,
    Base,
)
from .content import ContentArtifact, ContentCitation, RubricCriterion
from .corpus import EMBEDDING_DIM, Source, SourceChunk, SourceChunkEmbedding
from .curriculum import (
    Concept,
    ConceptEdge,
    ConceptSource,
    Subject,
    SubjectMetadata,
)
from .identity import AuthSession, User, UserProfile
from .ingestion import ContentReviewQueueItem, IngestionJob
from .journal import JournalEntry, JournalEvent
from .learner import ConceptMastery, LearnerSubject, MasteryEvent
from .memory import RetrievalCheck, SessionSummary
from .operational import AuditLog, CostLedger, UserBudgetCap
from .portfolio import PortfolioItem
from .review import ReviewCard, ReviewEvent
from .sessions import AgentTrace, LearningSession, SessionTurn

__all__ = [
    "ALL_ENUMS",
    "APPEND_ONLY_TABLES",
    "EMBEDDING_DIM",
    "SOFT_DELETE_TABLES",
    "TRIGGERED_TABLES",
    "AgentTrace",
    "AssessmentAttempt",
    "AssessmentResponse",
    "AuditLog",
    "AuthSession",
    "Base",
    "Concept",
    "ConceptEdge",
    "ConceptMastery",
    "ConceptSource",
    "ContentArtifact",
    "ContentCitation",
    "ContentReviewQueueItem",
    "CostLedger",
    "IngestionJob",
    "JournalEntry",
    "JournalEvent",
    "LearnerSubject",
    "LearningSession",
    "MasteryEvent",
    "PortfolioItem",
    "RetrievalCheck",
    "ReviewCard",
    "ReviewEvent",
    "RubricCriterion",
    "SessionSummary",
    "SessionTurn",
    "Source",
    "SourceChunk",
    "SourceChunkEmbedding",
    "Subject",
    "SubjectMetadata",
    "User",
    "UserBudgetCap",
    "UserProfile",
]
