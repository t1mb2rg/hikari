from __future__ import annotations

from pathlib import Path

from brain.model_reasoner import ChatMessage
from conversation.cli import build_parser
from conversation.models import UserTurn
from conversation.whiteboard import (
    WHITEBOARD_1_RELATIONSHIP_CONTEXT,
    WHITEBOARD_2_RELEVANT_CONTEXT,
    WHITEBOARD_2C_RELATIONAL_STANCE,
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
    relational_stance_text: str | None = None,
    relevant_context_text: str | None = None,
    relevant_context_placement: str = "system",
):
    return WhiteboardConversationEngine(
        provider,
        MemoryStore(path),
        relationship_context={
            "kind": "should_not_enter_whiteboard_prompt",
            "basis": "trusted_runtime_binding",
        },
        relationship_context_text=relationship_context_text,
        relational_stance_text=relational_stance_text,
        relevant_context_text=relevant_context_text,
        relevant_context_placement=relevant_context_placement,
        history_limit=history_limit,
        system_instructions=WHITEBOARD_HIKARI_SYSTEM_INSTRUCTIONS,
    )


def test_cli_accepts_whiteboard_prompt_profiles():
    for profile in (
        "whiteboard",
        "whiteboard0",
        "whiteboard1",
        "whiteboard2",
        "whiteboard2b",
        "whiteboard2c",
    ):
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


def test_whiteboard_two_b_places_same_context_next_to_current_turn(tmp_path: Path):
    provider = FakeProvider(
        ["<reaction>背景够用了。</reaction><reply>确实是保护层越叠越重了。</reply>"]
    )
    engine = _engine(
        tmp_path / "memory.db",
        provider,
        relevant_context_text=WHITEBOARD_2_RELEVANT_CONTEXT,
        relevant_context_placement="current_turn",
    )

    reply = engine.respond(UserTurn("qq", "main", "为什么现在M7越来越大了fk"))

    assert reply.text == "确实是保护层越叠越重了。"
    call = provider.calls[0]
    expected_current_turn = (
        f"{WHITEBOARD_2_RELEVANT_CONTEXT}\n\n"
        "【现在对你说】\n为什么现在M7越来越大了fk\n\n"
        "上面的背景只用于理解这句话，不需要单独回应背景；只回应【现在对你说】里的内容。"
    )
    assert [(message.role, message.content) for message in call] == [
        ("system", WHITEBOARD_HIKARI_SYSTEM_INSTRUCTIONS),
        ("user", expected_current_turn),
    ]
    assert all(
        message.content != WHITEBOARD_2_RELEVANT_CONTEXT
        for message in call
        if message.role == "system"
    )

    events = list(reversed(MemoryStore(tmp_path / "memory.db").recent_events(10)))
    assert [event.content for event in events] == [
        "为什么现在M7越来越大了fk",
        "确实是保护层越叠越重了。",
    ]
    assert "【现在对你说】" not in events[0].content
    assert "可参考的当前背景" not in events[0].content


def test_whiteboard_two_c_adds_only_relational_stance_to_two_b(tmp_path: Path):
    provider = FakeProvider(
        ["<reaction>这话听着确实磨人。</reaction><reply>嗯，这东西现在确实挺磨人的。</reply>"]
    )
    engine = _engine(
        tmp_path / "memory.db",
        provider,
        relational_stance_text=WHITEBOARD_2C_RELATIONAL_STANCE,
        relevant_context_text=WHITEBOARD_2_RELEVANT_CONTEXT,
        relevant_context_placement="current_turn",
    )

    reply = engine.respond(UserTurn("qq", "main", "让我有点累了"))

    assert reply.text == "嗯，这东西现在确实挺磨人的。"
    call = provider.calls[0]
    assert [(message.role, message.content) for message in call[:2]] == [
        ("system", WHITEBOARD_HIKARI_SYSTEM_INSTRUCTIONS),
        ("system", WHITEBOARD_2C_RELATIONAL_STANCE),
    ]
    assert call[-1].role == "user"
    assert WHITEBOARD_2_RELEVANT_CONTEXT in call[-1].content
    assert "【现在对你说】\n让我有点累了" in call[-1].content

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


def test_whiteboard_two_b_preserves_history_before_contextual_current_turn(tmp_path: Path):
    memory_path = tmp_path / "memory.db"
    first = FakeProvider(
        ["<reaction>接住。</reaction><reply>第一句回复。</reply>"]
    )
    _engine(
        memory_path,
        first,
        relevant_context_text=WHITEBOARD_2_RELEVANT_CONTEXT,
        relevant_context_placement="current_turn",
    ).respond(UserTurn("qq", "main", "第一句"))

    second = FakeProvider(
        ["<reaction>继续。</reaction><reply>第二句回复。</reply>"]
    )
    _engine(
        memory_path,
        second,
        relevant_context_text=WHITEBOARD_2_RELEVANT_CONTEXT,
        relevant_context_placement="current_turn",
    ).respond(UserTurn("qq", "main", "第二句"))

    call = second.calls[0]
    assert [(message.role, message.content) for message in call[:3]] == [
        ("system", WHITEBOARD_HIKARI_SYSTEM_INSTRUCTIONS),
        ("user", "第一句"),
        ("assistant", "第一句回复。"),
    ]
    assert call[-1].role == "user"
    assert "【现在对你说】\n第二句" in call[-1].content
    assert WHITEBOARD_2_RELEVANT_CONTEXT in call[-1].content


def test_whiteboard_two_c_preserves_history_between_stance_and_current_context(tmp_path: Path):
    memory_path = tmp_path / "memory.db"
    first = FakeProvider(
        ["<reaction>接住。</reaction><reply>第一句回复。</reply>"]
    )
    _engine(
        memory_path,
        first,
        relational_stance_text=WHITEBOARD_2C_RELATIONAL_STANCE,
        relevant_context_text=WHITEBOARD_2_RELEVANT_CONTEXT,
        relevant_context_placement="current_turn",
    ).respond(UserTurn("qq", "main", "第一句"))

    second = FakeProvider(
        ["<reaction>继续。</reaction><reply>第二句回复。</reply>"]
    )
    _engine(
        memory_path,
        second,
        relational_stance_text=WHITEBOARD_2C_RELATIONAL_STANCE,
        relevant_context_text=WHITEBOARD_2_RELEVANT_CONTEXT,
        relevant_context_placement="current_turn",
    ).respond(UserTurn("qq", "main", "第二句"))

    call = second.calls[0]
    assert [(message.role, message.content) for message in call[:4]] == [
        ("system", WHITEBOARD_HIKARI_SYSTEM_INSTRUCTIONS),
        ("system", WHITEBOARD_2C_RELATIONAL_STANCE),
        ("user", "第一句"),
        ("assistant", "第一句回复。"),
    ]
    assert call[-1].role == "user"
    assert "【现在对你说】\n第二句" in call[-1].content
    assert WHITEBOARD_2_RELEVANT_CONTEXT in call[-1].content


def test_whiteboard_rejects_unknown_relevant_context_placement(tmp_path: Path):
    provider = FakeProvider(["<reaction>不会调用。</reaction><reply>不会调用。</reply>"])

    try:
        _engine(
            tmp_path / "memory.db",
            provider,
            relevant_context_text=WHITEBOARD_2_RELEVANT_CONTEXT,
            relevant_context_placement="somewhere_else",
        )
    except ValueError as exc:
        assert "relevant_context_placement" in str(exc)
    else:
        raise AssertionError("unknown context placement must be rejected")


def test_whiteboard_plain_text_fallback_is_user_facing_reply():
    output = parse_whiteboard_output("格式偶尔没跟上，但这句仍然能正常发出去。")

    assert output.reaction == ""
    assert output.reply == "格式偶尔没跟上，但这句仍然能正常发出去。"
