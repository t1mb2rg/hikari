from __future__ import annotations

from pathlib import Path

from brain.model_reasoner import ChatMessage
from conversation.cli import build_parser
from conversation.models import UserTurn
from conversation.whiteboard import (
    WHITEBOARD_1_RELATIONSHIP_CONTEXT,
    WHITEBOARD_2_RELEVANT_CONTEXT,
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


def _engine(
    path: Path,
    provider: FakeProvider,
    *,
    history_limit: int = 12,
    relationship_context_text: str | None = None,
    relevant_context_text: str | None = None,
):
    return WhiteboardConversationEngine(
        provider,
        MemoryStore(path),
        relationship_context={
            "kind": "should_not_enter_whiteboard_prompt",
            "basis": "trusted_runtime_binding",
        },
        relationship_context_text=relationship_context_text,
        relevant_context_text=relevant_context_text,
        history_limit=history_limit,
        system_instructions=WHITEBOARD_HIKARI_SYSTEM_INSTRUCTIONS,
    )


def test_cli_accepts_whiteboard_prompt_profiles():
    for profile in ("whiteboard", "whiteboard0", "whiteboard1", "whiteboard2"):
        args = build_parser().parse_args(["--prompt-profile", profile])
        assert args.prompt_profile == profile


def test_whiteboard_parser_separates_reaction_from_reply():
    output = parse_whiteboard_output(
        "<reaction>他又来叫我了。</reaction><reply>在呢，怎么了？</reply>"
    )

    assert output.reaction == "他又来叫我了。"
    assert output.reply == "在呢，怎么了？"


def test_whiteboard_zero_sends_only_prompt_history_and_current_turn(tmp_path: Path):
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


def test_whiteboard_one_adds_only_natural_relationship_context(tmp_path: Path):
    provider = FakeProvider(
        ["<reaction>熟悉的人又来找我了。</reaction><reply>在呢。</reply>"]
    )
    engine = _engine(
        tmp_path / "memory.db",
        provider,
        relationship_context_text=WHITEBOARD_1_RELATIONSHIP_CONTEXT,
    )

    reply = engine.respond(UserTurn("qq", "main", "hikari"))

    assert reply.text == "在呢。"
    call = provider.calls[0]
    assert [(message.role, message.content) for message in call] == [
        ("system", WHITEBOARD_HIKARI_SYSTEM_INSTRUCTIONS),
        ("system", WHITEBOARD_1_RELATIONSHIP_CONTEXT),
        ("user", "hikari"),
    ]
    serialized = "\n".join(message.content for message in call)
    assert "should_not_enter_whiteboard_prompt" not in serialized
    assert "capabilities" not in serialized
    assert "memory_provenance" not in serialized
    assert "known_user" not in serialized


def test_whiteboard_two_adds_only_manually_confirmed_relevant_context(tmp_path: Path):
    provider = FakeProvider(
        ["<reaction>这下有具体背景了。</reaction><reply>确实已经做得有点重了。</reply>"]
    )
    engine = _engine(
        tmp_path / "memory.db",
        provider,
        relevant_context_text=WHITEBOARD_2_RELEVANT_CONTEXT,
    )

    reply = engine.respond(UserTurn("qq", "main", "为什么现在M7越来越大了fk"))

    assert reply.text == "确实已经做得有点重了。"
    call = provider.calls[0]
    assert [(message.role, message.content) for message in call] == [
        ("system", WHITEBOARD_HIKARI_SYSTEM_INSTRUCTIONS),
        ("system", WHITEBOARD_2_RELEVANT_CONTEXT),
        ("user", "为什么现在M7越来越大了fk"),
    ]
    serialized = "\n".join(message.content for message in call)
    assert "关系背景：" not in serialized
    assert "should_not_enter_whiteboard_prompt" not in serialized
    assert "capabilities" not in serialized
    assert "memory_provenance" not in serialized
    assert "known_user" not in serialized


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


def test_whiteboard_one_preserves_history_after_relationship_section(tmp_path: Path):
    memory_path = tmp_path / "memory.db"
    first = FakeProvider(
        ["<reaction>接住。</reaction><reply>第一句回复。</reply>"]
    )
    _engine(
        memory_path,
        first,
        relationship_context_text=WHITEBOARD_1_RELATIONSHIP_CONTEXT,
    ).respond(UserTurn("qq", "main", "第一句"))

    second = FakeProvider(
        ["<reaction>继续。</reaction><reply>第二句回复。</reply>"]
    )
    _engine(
        memory_path,
        second,
        relationship_context_text=WHITEBOARD_1_RELATIONSHIP_CONTEXT,
    ).respond(UserTurn("qq", "main", "第二句"))

    assert [(message.role, message.content) for message in second.calls[0]] == [
        ("system", WHITEBOARD_HIKARI_SYSTEM_INSTRUCTIONS),
        ("system", WHITEBOARD_1_RELATIONSHIP_CONTEXT),
        ("user", "第一句"),
        ("assistant", "第一句回复。"),
        ("user", "第二句"),
    ]


def test_whiteboard_two_preserves_history_after_relevant_context(tmp_path: Path):
    memory_path = tmp_path / "memory.db"
    first = FakeProvider(
        ["<reaction>接住。</reaction><reply>第一句回复。</reply>"]
    )
    _engine(
        memory_path,
        first,
        relevant_context_text=WHITEBOARD_2_RELEVANT_CONTEXT,
    ).respond(UserTurn("qq", "main", "第一句"))

    second = FakeProvider(
        ["<reaction>继续。</reaction><reply>第二句回复。</reply>"]
    )
    _engine(
        memory_path,
        second,
        relevant_context_text=WHITEBOARD_2_RELEVANT_CONTEXT,
    ).respond(UserTurn("qq", "main", "第二句"))

    assert [(message.role, message.content) for message in second.calls[0]] == [
        ("system", WHITEBOARD_HIKARI_SYSTEM_INSTRUCTIONS),
        ("system", WHITEBOARD_2_RELEVANT_CONTEXT),
        ("user", "第一句"),
        ("assistant", "第一句回复。"),
        ("user", "第二句"),
    ]


def test_whiteboard_plain_text_fallback_is_user_facing_reply():
    output = parse_whiteboard_output("格式偶尔没跟上，但这句仍然能正常发出去。")

    assert output.reaction == ""
    assert output.reply == "格式偶尔没跟上，但这句仍然能正常发出去。"
