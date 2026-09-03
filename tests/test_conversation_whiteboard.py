from __future__ import annotations

from pathlib import Path

from brain.model_reasoner import ChatMessage
from conversation.cli import build_parser
from conversation.models import UserTurn
from conversation.whiteboard import (
    WHITEBOARD_HIKARI_SYSTEM_INSTRUCTIONS,
    WhiteboardConversationEngine,
    parse_whiteboard_output,
)
from memory.store import MemoryStore


class FakeProvider:
    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[list[ChatMessage]] = []

    def complete(self, messages: list[ChatMessage]) -> str:
        self.calls.append(list(messages))
        return self.replies.pop(0)


def _engine(path: Path, provider: FakeProvider, *, history_limit: int = 12):
    return WhiteboardConversationEngine(
        provider,
        MemoryStore(path),
        relationship_context={
            "kind": "should_not_enter_whiteboard_prompt",
            "basis": "trusted_runtime_binding",
        },
        history_limit=history_limit,
        system_instructions=WHITEBOARD_HIKARI_SYSTEM_INSTRUCTIONS,
    )


def test_cli_accepts_whiteboard_prompt_profile():
    args = build_parser().parse_args(["--prompt-profile", "whiteboard"])

    assert args.prompt_profile == "whiteboard"


def test_whiteboard_parser_separates_reaction_from_reply():
    output = parse_whiteboard_output(
        "<reaction>他又来叫我了。</reaction><reply>在呢，怎么了？</reply>"
    )

    assert output.reaction == "他又来叫我了。"
    assert output.reply == "在呢，怎么了？"


def test_whiteboard_sends_only_prompt_history_and_current_turn(tmp_path: Path):
    provider = FakeProvider(
        ["<reaction>先接住这句话。</reaction><reply>嗯，我在。</reply>"]
    )
    engine = _engine(tmp_path / "memory.db", provider)

    reply = engine.respond(UserTurn("qq", "main", "hikari"))

    assert reply.text == "嗯，我在。"
    call = provider.calls[0]
    assert [(message.role, message.content) for message in call] == [
        ("system", WHITEBOARD_HIKARI_SYSTEM_INSTRUCTIONS),
        ("user", "hikari"),
    ]


def test_whiteboard_rehydrates_real_history_without_reaction(tmp_path: Path):
    memory_path = tmp_path / "memory.db"
    first = FakeProvider(
        ["<reaction>先回一句。</reaction><reply>第一句回复。</reply>"]
    )
    _engine(memory_path, first).respond(UserTurn("qq", "main", "第一句"))

    second = FakeProvider(
        ["<reaction>接着聊。</reaction><reply>第二句回复。</reply>"]
    )
    reply = _engine(memory_path, second).respond(UserTurn("qq", "main", "第二句"))

    assert reply.text == "第二句回复。"
    assert [(message.role, message.content) for message in second.calls[0]] == [
        ("system", WHITEBOARD_HIKARI_SYSTEM_INSTRUCTIONS),
        ("user", "第一句"),
        ("assistant", "第一句回复。"),
        ("user", "第二句"),
    ]

    events = list(reversed(MemoryStore(memory_path).recent_events(10)))
    assert [event.content for event in events] == [
        "第一句",
        "第一句回复。",
        "第二句",
        "第二句回复。",
    ]
    assert all("reaction" not in event.content for event in events)
    assert all("先回一句" not in event.content for event in events)


def test_whiteboard_plain_text_fallback_is_user_facing_reply():
    output = parse_whiteboard_output("格式偶尔没跟上，但这句仍然能正常发出去。")

    assert output.reaction == ""
    assert output.reply == "格式偶尔没跟上，但这句仍然能正常发出去。"
