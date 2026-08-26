from memory import MemoryKind, MemoryRecallPolicy, MemoryStore


def test_recall_is_bounded_and_filtered_by_configured_kinds(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    store.remember_memory(MemoryKind.EPISODIC, "first episode")
    store.remember_memory(MemoryKind.SEMANTIC, "one stable fact")
    store.remember_memory(MemoryKind.EPISODIC, "second episode")

    policy = MemoryRecallPolicy(
        {"test.event": [MemoryKind.EPISODIC, MemoryKind.SEMANTIC]},
        limit=2,
    )

    recalled = policy.recall(store, "test.event")

    assert [memory.content for memory in recalled] == [
        "second episode",
        "one stable fact",
    ]


def test_unknown_event_type_recalls_nothing(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    store.remember_memory(MemoryKind.EPISODIC, "should stay asleep")
    policy = MemoryRecallPolicy({"known.event": [MemoryKind.EPISODIC]})

    assert policy.recall(store, "unknown.event") == []
