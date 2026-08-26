from __future__ import annotations

from learning import LearningSession, LearningSessionState, ReflectionTriggerPolicy
from memory import MemoryCandidate, MemoryKind, MemoryStore


class FakeReflector:
    def __init__(self, result: MemoryCandidate | None) -> None:
        self.result = result
        self.calls = 0
        self.seen_ids: list[tuple[int, ...]] = []

    def reflect(self, memories):
        self.calls += 1
        self.seen_ids.append(tuple(memory.id for memory in memories))
        return self.result


def _candidate() -> MemoryCandidate:
    return MemoryCandidate(
        kind=MemoryKind.USER_MODEL,
        content="The user prefers rapid milestone loops.",
        context={"_hikari_learning": {"evidence_memory_ids": [1, 2, 3]}},
        confidence=0.9,
        salience=0.9,
        source_event_id=None,
        reason="bounded test learning",
    )


def test_below_threshold_does_not_call_reflector(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    store.remember_memory(MemoryKind.EPISODIC, "experience one")
    store.remember_memory(MemoryKind.EXPERIENCE, "experience two")
    reflector = FakeReflector(_candidate())
    session = LearningSession(store=store, reflector=reflector)

    result = session.run()

    assert result.reflected is False
    assert result.candidate is None
    assert result.state == LearningSessionState()
    assert reflector.calls == 0


def test_threshold_triggers_once_and_advances_watermark_without_auto_write(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    first = store.remember_memory(MemoryKind.EPISODIC, "experience one")
    second = store.remember_memory(MemoryKind.EXPERIENCE, "experience two")
    third = store.remember_memory(MemoryKind.EPISODIC, "experience three")
    reflector = FakeReflector(_candidate())
    session = LearningSession(store=store, reflector=reflector)

    result = session.run()

    assert result.reflected is True
    assert result.candidate == reflector.result
    assert result.considered_memory_ids == (first.id, second.id, third.id)
    assert result.state.last_reflected_memory_id == third.id
    assert reflector.calls == 1
    assert len(store.recent_memories()) == 3

    repeated = session.run(result.state)
    assert repeated.reflected is False
    assert reflector.calls == 1


def test_none_result_still_advances_watermark(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    memories = [
        store.remember_memory(MemoryKind.EXPERIENCE, f"experience {index}")
        for index in range(3)
    ]
    reflector = FakeReflector(None)
    session = LearningSession(store=store, reflector=reflector)

    result = session.run()

    assert result.reflected is True
    assert result.candidate is None
    assert result.state.last_reflected_memory_id == memories[-1].id
    assert reflector.calls == 1

    repeated = session.run(result.state)
    assert repeated.reflected is False
    assert reflector.calls == 1


def test_derived_learning_memories_do_not_trigger_reflection_by_default(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    for index in range(4):
        kind = MemoryKind.USER_MODEL if index % 2 == 0 else MemoryKind.SEMANTIC
        store.remember_memory(kind, f"derived learning {index}")

    reflector = FakeReflector(_candidate())
    session = LearningSession(store=store, reflector=reflector)

    result = session.run()

    assert result.reflected is False
    assert result.considered_memory_ids == ()
    assert reflector.calls == 0


def test_session_accumulates_subthreshold_new_experience(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    reflector = FakeReflector(_candidate())
    trigger = ReflectionTriggerPolicy(min_new_memories=3, max_memories=5)
    session = LearningSession(store=store, reflector=reflector, trigger=trigger)
    state = LearningSessionState()

    first = store.remember_memory(MemoryKind.EPISODIC, "one")
    second = store.remember_memory(MemoryKind.EXPERIENCE, "two")
    waiting = session.run(state)

    assert waiting.reflected is False
    assert waiting.state == state
    assert waiting.considered_memory_ids == (first.id, second.id)

    third = store.remember_memory(MemoryKind.EPISODIC, "three")
    ready = session.run(waiting.state)

    assert ready.reflected is True
    assert ready.considered_memory_ids == (first.id, second.id, third.id)
    assert ready.state.last_reflected_memory_id == third.id
    assert reflector.calls == 1
