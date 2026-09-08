from __future__ import annotations

import json
from pathlib import Path

import pytest

from awareness import ContextCollector
from brain.model_reasoner import ChatMessage
from conversation import ConversationEngine, ConversationGateway, UserTurn
from memory.models import MemoryKind
from memory.store import MemoryStore
from personality import PersonalityProfile, load_voice


class FakeProvider:
    def __init__(self, replies: list[str] | None = None) -> None:
        self.replies = list(replies or ["收到。"])
        self.calls: list[list[ChatMessage]] = []

    def complete(self, messages: list[ChatMessage]) -> str:
        self.calls.append(list(messages))
        if self.replies:
            return self.replies.pop(0)
        return "收到。"


class StaticContextProvider:
    name = "test_context"

    def capture(self) -> dict[str, object]:
        return {"foreground": "editor", "idle": False}


def _personality() -> PersonalityProfile:
    return PersonalityProfile(
        version="test",
        traits={
            "warmth": 0.85,
            "directness": 0.8,
            "curiosity": 0.9,
            "assertiveness": 0.65,
            "patience": 0.8,
        },
    )


def _engine(
    path: Path,
    provider: FakeProvider,
    *,
    history_limit: int = 12,
) -> ConversationEngine:
    return ConversationEngine(
        provider,
        MemoryStore(path),
        context_collector=ContextCollector([StaticContextProvider()]),
        personality_profile=_personality(),
        voice_profile=load_voice(),
        relationship_context={
            "kind": "primary_local_user",
            "basis": "trusted_runtime_binding",
            "memory_claim": "continuity_without_implied_episode_recall",
            "continuity": "trusted local continuity",
        },
        history_limit=history_limit,
    )


def test_direct_turn_calls_model_and_persists_both_sides(tmp_path: Path):
    provider = FakeProvider(["你好，我在。"])
    memory_path = tmp_path / "memory.db"
    engine = _engine(memory_path, provider)

    reply = engine.respond(UserTurn("cli", "main", "你好"))

    assert reply.text == "你好，我在。"
    assert len(provider.calls) == 1
    events = MemoryStore(memory_path).recent_events(10)
    assert [event.event_type for event in reversed(events)] == [
        "conversation.user",
        "conversation.assistant",
    ]
    assert all(event.context["channel"] == "cli" for event in events)
    assert all(event.context["conversation_id"] == "main" for event in events)


def test_fresh_engine_rehydrates_same_session_history(tmp_path: Path):
    memory_path = tmp_path / "memory.db"
    first_provider = FakeProvider(["第一句回复"])
    _engine(memory_path, first_provider).respond(
        UserTurn("qq", "friend-1", "第一句")
    )

    second_provider = FakeProvider(["第二句回复"])
    _engine(memory_path, second_provider).respond(
        UserTurn("qq", "friend-1", "第二句")
    )

    call = second_provider.calls[0]
    conversational = [(message.role, message.content) for message in call[2:]]
    assert conversational == [
        ("user", "第一句"),
        ("assistant", "第一句回复"),
        ("user", "第二句"),
    ]
    metadata = json.loads(call[1].content)
    assert metadata["memory_provenance"]["recent_history"]["count"] == 2
    assert metadata["memory_provenance"]["recent_history"]["recalled"] is True


def test_history_is_isolated_by_channel_and_conversation(tmp_path: Path):
    memory_path = tmp_path / "memory.db"
    _engine(memory_path, FakeProvider(["A回复"])).respond(
        UserTurn("qq", "A", "只属于A")
    )

    provider = FakeProvider(["B回复"])
    _engine(memory_path, provider).respond(
        UserTurn("telegram", "B", "只属于B")
    )

    serialized = "\n".join(message.content for message in provider.calls[0])
    assert "只属于A" not in serialized
    assert "A回复" not in serialized
    assert "只属于B" in serialized
    metadata = json.loads(provider.calls[0][1].content)
    assert metadata["memory_provenance"]["recent_history"]["count"] == 0


def test_prompt_attaches_identity_relationship_capabilities_personality_voice_and_context(
    tmp_path: Path,
):
    provider = FakeProvider(["好"])
    _engine(tmp_path / "memory.db", provider).respond(
        UserTurn("cli", "main", "看看上下文")
    )

    system = provider.calls[0][0].content
    metadata = json.loads(provider.calls[0][1].content)
    assert metadata["identity"]["name"] == "Hikari"
    assert metadata["relationship"]["kind"] == "primary_local_user"
    assert metadata["relationship"]["basis"] == "trusted_runtime_binding"
    assert metadata["capabilities"]["memory"]["available"] is True
    assert metadata["capabilities"]["current_chat_authority"]["direct_filesystem"] is False
    assert metadata["ambient_context"]["providers"]["test_context"] == {
        "foreground": "editor",
        "idle": False,
    }
    assert metadata["personality"]["traits"]["curiosity"] == 0.9
    assert metadata["voice"]["stance"]["relation"] == "familiar"
    assert metadata["voice"]["cadence"]["headings_in_casual_chat"] is False
    provenance = metadata["memory_provenance"]
    assert provenance["current_user_turn"]["source"] == "user_supplied_current_turn"
    assert provenance["current_user_turn"]["recalled"] is False
    assert provenance["relationship"]["source"] == "trusted_runtime_binding"
    assert provenance["relationship"]["recalled"] is False
    assert "你是 Hikari（光 / ひかり）" in system
    assert "聊天首先是聊天，不是答题" in system
    assert "当前用户消息里的粘贴记录" in system
    assert "不等于你自己记得" in system
    assert "没有真实记忆支持的事情不要伪装成回忆" in system
    assert "直接聊天本身不会自动授予 shell" in system
    assert "保留必要的不确定性" in system
    assert "customer-service chatbot" not in system
    assert "therapist, life coach" not in system


def test_prompt_attaches_bounded_durable_user_and_relationship_memory(tmp_path: Path):
    memory_path = tmp_path / "memory.db"
    memory = MemoryStore(memory_path)
    user_memory = memory.remember_memory(
        MemoryKind.USER_MODEL,
        "用户偏好自然、直接的中文交流。",
        confidence=0.9,
    )
    episode = memory.remember_memory(
        MemoryKind.EPISODIC,
        "Hikari 和用户一起完成了一次后台驻留验收。",
        confidence=0.8,
    )

    provider = FakeProvider(["知道了"])
    _engine(memory_path, provider).respond(UserTurn("cli", "main", "你了解我吗"))

    metadata = json.loads(provider.calls[0][1].content)
    assert metadata["known_user"][0]["id"] == user_memory.id
    assert metadata["known_user"][0]["content"] == "用户偏好自然、直接的中文交流。"
    assert metadata["known_user"][0]["confidence"] == 0.9
    assert metadata["known_user"][0]["provenance"] == "durable_memory"
    assert metadata["relationship_memories"][0]["id"] == episode.id
    assert metadata["relationship_memories"][0]["content"] == (
        "Hikari 和用户一起完成了一次后台驻留验收。"
    )
    assert metadata["relationship_memories"][0]["provenance"] == "durable_memory"
    assert metadata["memory_provenance"]["known_user"]["count"] == 1
    assert metadata["memory_provenance"]["relationship_memories"]["count"] == 1
    assert "elapsed gaps" in metadata["memory_provenance"]["relationship"]["note"]


def test_pasted_transcript_is_marked_user_supplied_not_recalled(tmp_path: Path):
    provider = FakeProvider(["从你贴的记录看，这句确实很像早期版本。"])
    _engine(tmp_path / "memory.db", provider).respond(
        UserTurn(
            "cli",
            "fresh",
            "Hikari> 嗨，你好呀！😊 我是 Hikari，很高兴见到你。",
        )
    )

    call = provider.calls[0]
    metadata = json.loads(call[1].content)
    provenance = metadata["memory_provenance"]
    assert provenance["current_user_turn"]["recalled"] is False
    assert provenance["recent_history"]["count"] == 0
    assert provenance["relationship"]["recalled"] is False
    assert "Hikari> 嗨，你好呀" in call[-1].content


def test_empty_message_rejected_before_model_call(tmp_path: Path):
    provider = FakeProvider()
    engine = _engine(tmp_path / "memory.db", provider)

    with pytest.raises(ValueError, match="text must not be empty"):
        engine.respond(UserTurn("cli", "main", "   "))

    assert provider.calls == []


def test_history_limit_is_bounded(tmp_path: Path):
    memory_path = tmp_path / "memory.db"
    engine = _engine(memory_path, FakeProvider(["r1", "r2", "r3"]), history_limit=2)
    engine.respond(UserTurn("cli", "main", "u1"))
    engine.respond(UserTurn("cli", "main", "u2"))
    engine.respond(UserTurn("cli", "main", "u3"))

    last_call = engine.provider.calls[-1]
    conversational = [(message.role, message.content) for message in last_call[2:]]
    assert conversational == [
        ("user", "u2"),
        ("assistant", "r2"),
        ("user", "u3"),
    ]


class QueueTransport:
    def __init__(self, turn: UserTurn | None) -> None:
        self.turn = turn
        self.sent = []

    def receive(self) -> UserTurn | None:
        turn, self.turn = self.turn, None
        return turn

    def send(self, reply) -> None:
        self.sent.append(reply)


def test_gateway_routes_one_transport_turn_through_shared_engine(tmp_path: Path):
    provider = FakeProvider(["网关回复"])
    engine = _engine(tmp_path / "memory.db", provider)
    transport = QueueTransport(UserTurn("onebot", "42", "在吗"))
    gateway = ConversationGateway(engine, transport)

    reply = gateway.cycle_once()

    assert reply is not None
    assert reply.text == "网关回复"
    assert transport.sent == [reply]
    assert gateway.cycle_once() is None
