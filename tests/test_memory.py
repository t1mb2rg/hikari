from datetime import datetime, timezone

from memory import MemoryStore


def test_memory_store_can_remember_and_retrieve_event(tmp_path):
    store = MemoryStore(tmp_path / "hikari.db")
    occurred_at = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)

    remembered = store.remember_event(
        "git.commit",
        "Forge received a new commit.",
        context={"repository": "forge", "branch": "main"},
        importance=0.8,
        occurred_at=occurred_at,
    )

    loaded = store.get_event(remembered.id)

    assert loaded is not None
    assert loaded.event_type == "git.commit"
    assert loaded.content == "Forge received a new commit."
    assert loaded.context == {"repository": "forge", "branch": "main"}
    assert loaded.importance == 0.8
    assert loaded.occurred_at == "2026-08-26T12:00:00+00:00"


def test_memory_store_persists_between_instances(tmp_path):
    database_path = tmp_path / "hikari.db"
    first_store = MemoryStore(database_path)
    remembered = first_store.remember_event(
        "system.heartbeat",
        "Hikari is awake.",
        importance=0.2,
    )

    second_store = MemoryStore(database_path)
    loaded = second_store.get_event(remembered.id)

    assert loaded is not None
    assert loaded.content == "Hikari is awake."


def test_recent_events_returns_newest_first(tmp_path):
    store = MemoryStore(tmp_path / "hikari.db")
    store.remember_event("event", "first")
    store.remember_event("event", "second")
    store.remember_event("event", "third")

    events = store.recent_events(limit=2)

    assert [event.content for event in events] == ["third", "second"]
