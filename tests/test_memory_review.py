from memory import (
    MemoryCandidate,
    MemoryKind,
    MemoryReviewDecision,
    MemoryReviewPolicy,
    MemoryStore,
    apply_memory_review,
)


def candidate(*, kind=MemoryKind.EPISODIC, salience=0.95, confidence=0.9):
    return MemoryCandidate(
        kind=kind,
        content="A moment worth reviewing",
        context={"project": "hikari"},
        confidence=confidence,
        salience=salience,
        source_event_id=42,
        reason="candidate policy selected this event",
    )


def test_strong_candidate_can_be_accepted_and_persisted(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    policy = MemoryReviewPolicy(min_salience=0.9, min_confidence=0.8)

    review = policy.review(candidate())
    memory = apply_memory_review(store, review)

    assert review.decision is MemoryReviewDecision.ACCEPT
    assert memory is not None
    assert memory.source_event_id == 42
    assert memory.context["_hikari_memory_formation"]["reason"] == "candidate policy selected this event"
    assert memory.context["_hikari_memory_review"]["decision"] == "accept"
    assert len(store.recent_memories()) == 1


def test_unsupported_kind_is_rejected_without_persistence(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    policy = MemoryReviewPolicy(accepted_kinds=[MemoryKind.EPISODIC])

    review = policy.review(candidate(kind=MemoryKind.SEMANTIC))

    assert review.decision is MemoryReviewDecision.REJECT
    assert apply_memory_review(store, review) is None
    assert store.recent_memories() == []


def test_borderline_candidate_is_deferred_without_persistence(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    policy = MemoryReviewPolicy(min_salience=0.9, min_confidence=0.8)

    review = policy.review(candidate(salience=0.82))

    assert review.decision is MemoryReviewDecision.DEFER
    assert apply_memory_review(store, review) is None
    assert store.recent_memories() == []
