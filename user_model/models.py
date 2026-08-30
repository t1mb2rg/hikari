from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Mapping


class UserFactCategory(str, Enum):
    PREFERENCE = "preference"
    GOAL = "goal"
    PROJECT = "project"
    HABIT = "habit"
    CAPABILITY = "capability"
    RELATIONSHIP = "relationship"


class UserFactStatus(str, Enum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    DISPUTED = "disputed"


class AssimilationDecision(str, Enum):
    CREATED = "created"
    CONFIRMED = "confirmed"
    SUPERSEDED = "superseded"
    DISPUTED = "disputed"
    DUPLICATE = "duplicate"


@dataclass(frozen=True)
class UserFactCandidate:
    """A model-proposed durable fact that has not yet been trusted or stored."""

    category: UserFactCategory
    key: str
    value: str
    statement: str
    confidence: float
    source_ref: str
    evidence_key: str
    provenance: Mapping[str, str]


@dataclass(frozen=True)
class UserFact:
    """One immutable-content revision in Hikari's auditable user model."""

    id: int
    category: UserFactCategory
    key: str
    value: str
    statement: str
    status: UserFactStatus
    confidence: float
    revision: int
    first_seen_at: str
    last_confirmed_at: str
    supersedes_id: int | None
    evidence_key: str
    source_ref: str
    provenance: Mapping[str, str]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class UserFactEvidence:
    evidence_key: str
    source_ref: str
    fact_id: int
    decision: AssimilationDecision
    candidate_json: str
    observed_at: str


@dataclass(frozen=True)
class AssimilationResult:
    decision: AssimilationDecision
    fact: UserFact
    evidence_key: str
