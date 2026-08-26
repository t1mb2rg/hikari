from memory import MemoryKind, MemoryStore
from memory.candidates import MemoryCandidatePolicy, promote_candidate


def _remember_event(store, *, event_type="git.commit", importance=0.9):
    event = store.remember_event(
        event_type,
        "Add memory candidate boundary",
        context={"repository": "hikari"},
    )
    return store.update_importance(event.id, importance)


def test_low_importance_event_produces_no_candidate(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    event = _remember_event(store, importance=0.4)
    policy = MemoryCandidatePolicy(
        {"git.commit": MemoryKind.EPISODIC},
        min_importance=0.8,
    )

    assert policy.propose(event) is None


def test_unconfigured_event_type_produces_no_candidate(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    event = _remember_event(store, event_type="device.changed", importance=1.0)
    policy = MemoryCandidatePolicy(
        {"git.commit": MemoryKind.EPISODIC},
        min_importance=0.8,
    )

    assert policy.propose(event) is None


def test_configured_important_event_becomes_reviewable_candidate(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    event = _remember_event(store, importance=0.9)
    policy = MemoryCandidatePolicy(
        {"git.commit": MemoryKind.EPISODIC},
        min_importance=0.8,
    )

    candidate = policy.propose(event)

    assert candidate is not None
    assert candidate.kind is MemoryKind.EPISODIC
    assert candidate.content == event.content
    assert candidate.context == event.context
    assert candidate.source_event_id == event.id
    assert candidate.salience == 0.9
    assert "importance 0.90 >= 0.80" in candidate.reason


def test_proposing_candidate_does_not_write_durable_memory(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    event = _remember_event(store)
    policy = MemoryCandidatePolicy({"git.commit": "episodic"})

    candidate = policy.propose(event)

    assert candidate is not None
    assert store.recent_memories() == []


def test_explicit_promotion_creates_linked_durable_memory(tmp_path):
    path = tmp_path / "memory.db"
    store = MemoryStore(path)
    event = _remember_event(store)
    policy = MemoryCandidatePolicy({"git.commit": "episodic"})
    candidate = policy.propose(event)
    assert candidate is not None

    durable = promote_candidate(store, candidate)
    reopened = MemoryStore(path)
    loaded = reopened.get_memory(durable.id)

    assert loaded is not None
    assert loaded.kind is MemoryKind.EPISODIC
    assert loaded.source_event_id == event.id
    assert loaded.context["repository"] == "hikari"
    assert loaded.context["_hikari_memory_formation"]["salience"] == 0.9
    assert "configured for episodic memory" in loaded.context["_hikari_memory_formation"]["reason"]
