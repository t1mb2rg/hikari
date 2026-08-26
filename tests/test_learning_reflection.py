from __future__ import annotations

import json

import pytest

from learning import LEARNING_CONTEXT_KEY, LearningReflectionError, LearningReflector
from memory import (
    MemoryKind,
    MemoryReviewDecision,
    MemoryReviewPolicy,
    MemoryStore,
    apply_memory_review,
)


class FakeProvider:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.calls = []

    def complete(self, messages):
        self.calls.append(tuple(messages))
        return json.dumps(self.response, ensure_ascii=False)


def _seed_memories(store: MemoryStore):
    first = store.remember_memory(
        MemoryKind.EPISODIC,
        "The user chose a concise implementation path instead of extending the milestone.",
        confidence=0.95,
    )
    second = store.remember_memory(
        MemoryKind.EPISODIC,
        "The user again preferred closing a green milestone and moving to the next capability.",
        confidence=0.95,
    )
    return first, second


def test_reflection_proposes_reviewable_user_model_without_writing_memory(tmp_path):
    store = MemoryStore(tmp_path / "learning.db")
    first, second = _seed_memories(store)
    provider = FakeProvider(
        {
            "decision": "propose",
            "kind": "user_model",
            "content": "The user generally prefers short milestone loops and rapid forward progress.",
            "confidence": 0.95,
            "evidence_memory_ids": [first.id, second.id],
            "reason": "Two separate project decisions show the same preference.",
        }
    )

    candidate = LearningReflector(provider).reflect([first, second])

    assert candidate is not None
    assert candidate.kind is MemoryKind.USER_MODEL
    assert candidate.source_event_id is None
    assert candidate.context[LEARNING_CONTEXT_KEY]["evidence_memory_ids"] == [
        first.id,
        second.id,
    ]
    assert len(store.recent_memories()) == 2
    assert len(provider.calls) == 1

    review = MemoryReviewPolicy().review(candidate)
    assert review.decision is MemoryReviewDecision.ACCEPT
    learned = apply_memory_review(store, review)

    assert learned is not None
    assert learned.kind is MemoryKind.USER_MODEL
    assert learned.source_event_id is None
    assert learned.context[LEARNING_CONTEXT_KEY]["method"] == "model_reflection"
    assert learned.context["_hikari_memory_review"]["decision"] == "accept"
    assert len(store.recent_memories()) == 3


def test_reflection_can_decline_to_learn(tmp_path):
    store = MemoryStore(tmp_path / "none.db")
    first, second = _seed_memories(store)
    provider = FakeProvider(
        {
            "decision": "none",
            "reason": "The evidence is not stable enough for a reusable learning.",
        }
    )

    candidate = LearningReflector(provider).reflect([first, second])

    assert candidate is None
    assert len(store.recent_memories()) == 2


def test_user_model_rejects_single_memory_overgeneralization(tmp_path):
    store = MemoryStore(tmp_path / "single.db")
    first, _ = _seed_memories(store)
    provider = FakeProvider(
        {
            "decision": "propose",
            "kind": "user_model",
            "content": "The user always wants concise output.",
            "confidence": 0.99,
            "evidence_memory_ids": [first.id],
            "reason": "One memory suggests it.",
        }
    )

    with pytest.raises(LearningReflectionError, match="at least two distinct"):
        LearningReflector(provider).reflect([first])


def test_reflection_rejects_evidence_outside_supplied_memories(tmp_path):
    store = MemoryStore(tmp_path / "unknown.db")
    first, second = _seed_memories(store)
    provider = FakeProvider(
        {
            "decision": "propose",
            "kind": "semantic",
            "content": "A reusable observation.",
            "confidence": 0.9,
            "evidence_memory_ids": [9999],
            "reason": "Claims evidence that was not supplied.",
        }
    )

    with pytest.raises(LearningReflectionError, match="unknown memory IDs"):
        LearningReflector(provider).reflect([first, second])
