from __future__ import annotations

from pathlib import Path

from brain.model_reasoner import ChatMessage
from conversation.cli import build_parser
from conversation.jarvis import JARVIS_SYSTEM_INSTRUCTIONS
from conversation.models import UserTurn
from conversation.whiteboard import WhiteboardConversationEngine
from memory.store import MemoryStore


class FakeProvider:
    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[list[ChatMessage]] = []

    def complete(self, messages: list[ChatMessage]) -> str:
        self.calls.append(list(messages))
        return self.replies.pop(0)


def _engine(path: Path, provider: FakeProvider) -> WhiteboardConversationEngine:
    return WhiteboardConversationEngine(
        provider,
        MemoryStore(path),
        relationship_context={
            "kind": "should_not_enter_jarvis_prompt",
            "basis": "trusted_runtime_binding",
        },
        history_limit=12,
        system_instructions=JARVIS_SYSTEM_INSTRUCTIONS,
    )


def test_cli_accepts_jarvis_prompt_profile():
    args = build_parser().parse_args(["--prompt-profile", "jarvis"])

    assert args.prompt_profile == "jarvis"


def test_jarvis_sends_only_jarvis_prompt_and_current_turn(tmp_path: Path):
    provider = FakeProvider(["在。有什么事？"])
    engine = _engine(tmp_path / "memory.db", provider)

    reply = engine.respond(UserTurn("cli", "jarvis-0", "jarvis"))

    assert reply.text == "在。有什么事？"
    assert [(message.role, message.content) for message in provider.calls[0]] == [
        ("system", JARVIS_SYSTEM_INSTRUCTIONS),
        ("user", "jarvis"),
    ]
    serialized = "\n".join(message.content for message in provider.calls[0])
    assert "should_not_enter_jarvis_prompt" not in serialized
    assert "可参考的当前背景" not in serialized
    assert "关系姿态" not in serialized


def test_jarvis_rehydrates_only_real_conversation_history(tmp_path: Path):
    memory_path = tmp_path / "memory.db"
    first = FakeProvider(["第一句回复。"])
    _engine(memory_path, first).respond(UserTurn("cli", "jarvis-0", "第一句"))

    second = FakeProvider(["第二句回复。"])
    reply = _engine(memory_path, second).respond(
        UserTurn("cli", "jarvis-0", "第二句")
    )

    assert reply.text == "第二句回复。"
    assert [(message.role, message.content) for message in second.calls[0]] == [
        ("system", JARVIS_SYSTEM_INSTRUCTIONS),
        ("user", "第一句"),
        ("assistant", "第一句回复。"),
        ("user", "第二句"),
    ]
