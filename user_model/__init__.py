"""Persistent, revisable, and auditable model of Hikari's primary user."""

from .extractor import (
    MAX_CANDIDATES_PER_TURN,
    ModelUserFactExtractor,
    UserFactExtractionError,
    parse_candidate_output,
)
from .models import (
    AssimilationDecision,
    AssimilationResult,
    UserFact,
    UserFactCandidate,
    UserFactCategory,
    UserFactEvidence,
    UserFactStatus,
)
from .service import UserModelService, make_evidence_key
from .store import UserModelStore
from .runtime import build_user_model_runtime

__all__ = [
    "MAX_CANDIDATES_PER_TURN",
    "AssimilationDecision",
    "AssimilationResult",
    "ModelUserFactExtractor",
    "UserFact",
    "UserFactCandidate",
    "UserFactCategory",
    "UserFactEvidence",
    "UserFactExtractionError",
    "UserFactStatus",
    "UserModelService",
    "UserModelStore",
    "make_evidence_key",
    "parse_candidate_output",
    "build_user_model_runtime",
]
