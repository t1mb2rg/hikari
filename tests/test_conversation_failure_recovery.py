from __future__ import annotations

from pathlib import Path

import pytest

from brain.model_reasoner import ChatMessage
from conversation import ConversationEngine, UserTurn
from memory.store import MemoryStore


class FailOnceProvider:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, messages: list[ChatMessage]) -> str:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("temporary model outage")
        return "恢复了。"


def test_model_failure_leaves_no_partial_conversation_memory_and_next_turn_recovers(
    tmp_path: Path,
):
    memory_path = tmp_path / "memory.db"
    provider = FailOnceProvider()
    engine = ConversationEngine(provider, MemoryStore(memory_path))

    with pytest.raises(RuntimeError, match="temporary model outage"):
        engine.respond(UserTurn("qq", "private:test", "失败期间的消息"))

    assert MemoryStore(memory_path).recent_events(10) == []

    reply = engine.respond(UserTurn("qq", "private:test", "恢复后的消息"))

    assert reply.text == "恢复了。"
    events = list(reversed(MemoryStore(memory_path).recent_events(10)))
    assert [(event.event_type, event.content) for event in events] == [
        ("conversation.user", "恢复后的消息"),
        ("conversation.assistant", "恢复了。"),
    ]
