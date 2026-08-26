from memory import MemoryKind, MemoryStore


def test_durable_memory_survives_store_reopen(tmp_path):
    path = tmp_path / "memory.db"
    store = MemoryStore(path)

    remembered = store.remember_memory(
        MemoryKind.EPISODIC,
        "M0 physical gate passed",
        context={"project": "hikari"},
        confidence=0.95,
        source_event_id=7,
    )

    reopened = MemoryStore(path)
    loaded = reopened.get_memory(remembered.id)

    assert loaded is not None
    assert loaded.kind is MemoryKind.EPISODIC
    assert loaded.content == "M0 physical gate passed"
    assert loaded.context == {"project": "hikari"}
    assert loaded.confidence == 0.95
    assert loaded.source_event_id == 7


def test_recent_memories_can_filter_by_kind(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    store.remember_memory("episodic", "first shared event")
    store.remember_memory("semantic", "Git sensors normalize into Event")
    store.remember_memory("episodic", "second shared event")

    episodic = store.recent_memories(kind=MemoryKind.EPISODIC)

    assert [memory.content for memory in episodic] == [
        "second shared event",
        "first shared event",
    ]


def test_invalid_memory_kind_is_rejected(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")

    try:
        store.remember_memory("everything", "too vague")
    except ValueError as exc:
        assert "Unknown memory kind" in str(exc)
    else:
        raise AssertionError("Unknown memory kind should be rejected")


def test_invalid_memory_confidence_is_rejected(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")

    try:
        store.remember_memory("semantic", "invalid confidence", confidence=1.5)
    except ValueError as exc:
        assert "between 0.0 and 1.0" in str(exc)
    else:
        raise AssertionError("Out-of-range confidence should be rejected")
