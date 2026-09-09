from __future__ import annotations

from pathlib import Path
import sqlite3

from brain.model_reasoner import ChatMessage
from conversation.models import AssistantReply, UserTurn
from conversation.natural import NaturalConversationEngine
from conversation.protocol import decode_envelope, encode_envelope, parse_turn, turn_envelope
from conversation.receipts import ConversationReceiptStore
from conversation.remote import ConversationRequestProcessor
from integrations.qq_bridge.config import QQBridgeConfig
from integrations.qq_bridge.mapper import normalize_group_message, normalize_private_message
from integrations.qq_bridge.spool import BridgeSpool
from memory.store import MemoryStore


class RecordingProvider:
    def __init__(self, reply: str = "知道了。") -> None:
        self.reply = reply
        self.calls: list[list[ChatMessage]] = []

    def complete(self, messages) -> str:
        self.calls.append(list(messages))
        return self.reply


def _at_self(bot_id: int, text: str) -> list[dict[str, object]]:
    return [
        {"type": "at", "data": {"qq": bot_id}},
        {"type": "text", "data": {"text": text}},
    ]


def test_group_participant_allowlist_does_not_grant_private_chat(tmp_path: Path):
    config = QQBridgeConfig.from_mapping(
        {
            "HIKARI_ONEBOT_ALLOWED_USER_IDS": "7",
            "HIKARI_ONEBOT_ALLOWED_GROUP_IDS": "10",
            "HIKARI_ONEBOT_ALLOWED_GROUP_USER_IDS": "8",
        },
        state_dir=tmp_path,
    )

    assert config.allowed_user_ids == frozenset({"7"})
    assert config.allowed_group_user_ids == frozenset({"8"})

    group = normalize_group_message(
        bot_self_id=100,
        group_id=10,
        user_id=8,
        message_id=1,
        message=_at_self(100, "你好"),
        allowed_group_ids=config.allowed_group_ids,
        allowed_group_user_ids=config.allowed_group_user_ids,
    )
    assert group is not None
    assert group[1].actor_id == "8"
    assert group[1].scope == "shared"

    private = normalize_private_message(
        bot_self_id=100,
        user_id=8,
        message_id=2,
        message="你好",
        allowed_user_ids=config.allowed_user_ids,
    )
    assert private is None


def test_shared_actor_and_scope_survive_wire_spool_and_receipt(tmp_path: Path):
    turn = UserTurn(
        "qq",
        "group:10",
        "在吗",
        actor_id="8",
        scope="shared",
    )
    request_id, parsed = parse_turn(
        decode_envelope(
            encode_envelope(turn_envelope(request_id="qq:100:g:10:3", turn=turn))
        )
    )
    assert request_id == "qq:100:g:10:3"
    assert parsed.actor_id == "8"
    assert parsed.scope == "shared"
    assert parsed.same_wire_turn(turn)

    spool = BridgeSpool(tmp_path / "spool.db")
    stored = spool.record_turn(request_id, turn)
    assert stored.turn.actor_id == "8"
    assert stored.turn.scope == "shared"

    receipts = ConversationReceiptStore(tmp_path / "receipts.db")
    reply = AssistantReply("qq", "group:10", "在。")
    receipt = receipts.save(request_id, turn, reply)
    assert receipt.turn.actor_id == "8"
    assert receipt.turn.scope == "shared"


def test_legacy_group_rows_migrate_to_shared_scope(tmp_path: Path):
    spool_path = tmp_path / "legacy-spool.db"
    with sqlite3.connect(spool_path) as connection:
        connection.execute(
            """
            CREATE TABLE qq_bridge_spool (
                request_id TEXT PRIMARY KEY,
                channel TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                user_text TEXT NOT NULL,
                reply_text TEXT,
                state TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            INSERT INTO qq_bridge_spool (
                request_id, channel, conversation_id, user_text, state
            ) VALUES ('qq:100:g:10:44', 'qq', 'group:10', '旧群消息', 'pending')
            """
        )

    migrated = BridgeSpool(spool_path).get("qq:100:g:10:44")
    assert migrated is not None
    assert migrated.turn.scope == "shared"
    assert migrated.turn.actor_id is None

    receipt_path = tmp_path / "legacy-receipts.db"
    with sqlite3.connect(receipt_path) as connection:
        connection.execute(
            """
            CREATE TABLE conversation_receipts (
                request_id TEXT PRIMARY KEY,
                channel TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                user_text TEXT NOT NULL,
                reply_text TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            INSERT INTO conversation_receipts (
                request_id, channel, conversation_id, user_text, reply_text
            ) VALUES ('qq:100:g:10:45', 'qq', 'group:10', '旧群消息', '旧回复')
            """
        )

    receipt = ConversationReceiptStore(receipt_path).get("qq:100:g:10:45")
    assert receipt is not None
    assert receipt.turn.scope == "shared"
    assert receipt.turn.actor_id is None


def test_legacy_private_rows_recover_actor_from_private_route(tmp_path: Path):
    spool_path = tmp_path / "legacy-private-spool.db"
    with sqlite3.connect(spool_path) as connection:
        connection.execute(
            """
            CREATE TABLE qq_bridge_spool (
                request_id TEXT PRIMARY KEY,
                channel TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                user_text TEXT NOT NULL,
                reply_text TEXT,
                state TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            INSERT INTO qq_bridge_spool (
                request_id, channel, conversation_id, user_text, state
            ) VALUES ('qq:100:46', 'qq', 'private:7', '旧私聊', 'pending')
            """
        )

    migrated = BridgeSpool(spool_path).get("qq:100:46")
    assert migrated is not None
    assert migrated.turn.scope == "private"
    assert migrated.turn.actor_id == "7"


def test_shared_conversation_hides_private_context_and_labels_actor(tmp_path: Path):
    memory = MemoryStore(tmp_path / "memory.db")
    memory.remember_event(
        "conversation.user",
        "我的私人代号是蓝鲸。",
        context={"channel": "qq", "conversation_id": "private:7", "role": "user"},
        importance=1.0,
    )
    calls = {"context": 0}

    def private_context() -> str:
        calls["context"] += 1
        return "PRIVATE-CONTEXT-SHOULD-NOT-LEAK"

    provider = RecordingProvider("你好。")
    engine = NaturalConversationEngine(
        provider,
        memory,
        relevant_context_provider=private_context,
        relevant_context_placement="current_turn",
    )

    engine.respond(
        UserTurn(
            "qq",
            "group:10",
            "你知道他的私人代号吗",
            actor_id="8",
            scope="shared",
        )
    )

    assert calls["context"] == 0
    flattened = "\n".join(message.content for message in provider.calls[0])
    assert "PRIVATE-CONTEXT-SHOULD-NOT-LEAK" not in flattened
    assert "蓝鲸" not in flattened
    assert "【群成员 8】" in flattened

    shared_user = [
        event
        for event in memory.recent_events(10)
        if event.event_type == "conversation.user"
        and event.context.get("conversation_id") == "group:10"
    ][0]
    assert shared_user.context.get("scope") == "shared"
    assert shared_user.context.get("actor_id") == "8"


def test_shared_events_are_not_recalled_into_private_turns(tmp_path: Path):
    memory = MemoryStore(tmp_path / "memory.db")
    provider = RecordingProvider("收到。")
    engine = NaturalConversationEngine(
        provider,
        memory,
        relevant_context_provider=lambda: "当前可用的系统事实：\n- Resident 正在运行。",
        relevant_context_placement="current_turn",
    )

    engine.respond(
        UserTurn(
            "qq",
            "group:10",
            "我的群聊暗号是火箭企鹅。",
            actor_id="8",
            scope="shared",
        )
    )
    engine.respond(UserTurn("qq", "private:7", "之前有人说过什么暗号吗"))

    private_messages = "\n".join(message.content for message in provider.calls[1])
    assert "火箭企鹅" not in private_messages


def test_pre_scope_group_events_are_not_recalled_into_private_turns(tmp_path: Path):
    memory = MemoryStore(tmp_path / "memory.db")
    memory.remember_event(
        "conversation.user",
        "旧版群聊暗号是海盐卫星。",
        context={"channel": "qq", "conversation_id": "group:10", "role": "user"},
        importance=1.0,
    )
    provider = RecordingProvider("收到。")
    engine = NaturalConversationEngine(
        provider,
        memory,
        relevant_context_provider=lambda: "当前可用的系统事实：\n- Resident 正在运行。",
        relevant_context_placement="current_turn",
    )

    engine.respond(UserTurn("qq", "private:7", "之前有人说过什么暗号吗"))

    private_messages = "\n".join(message.content for message in provider.calls[0])
    assert "海盐卫星" not in private_messages


class RecordingActionBridge:
    def __init__(self) -> None:
        self.calls: list[UserTurn] = []

    def respond(self, engine, turn, *, source_ref=None) -> AssistantReply:
        self.calls.append(turn)
        return AssistantReply(turn.channel, turn.conversation_id, "engineering accepted")


def test_shared_turn_never_enters_private_action_bridge(tmp_path: Path):
    provider = RecordingProvider("共享群聊不执行私人系统动作。")
    engine = NaturalConversationEngine(provider, MemoryStore(tmp_path / "memory.db"))
    bridge = RecordingActionBridge()
    processor = ConversationRequestProcessor(
        engine,
        ConversationReceiptStore(tmp_path / "receipts.db"),
        action_bridge=bridge,
    )

    reply, duplicate = processor.process(
        "qq:100:g:10:9",
        UserTurn(
            "qq",
            "group:10",
            "帮我修改 Hikari 仓库并开 PR",
            actor_id="8",
            scope="shared",
        ),
    )

    assert duplicate is False
    assert bridge.calls == []
    assert reply.text == "共享群聊不执行私人系统动作。"


def test_private_turn_still_reaches_action_bridge(tmp_path: Path):
    provider = RecordingProvider()
    engine = NaturalConversationEngine(provider, MemoryStore(tmp_path / "memory.db"))
    bridge = RecordingActionBridge()
    processor = ConversationRequestProcessor(
        engine,
        ConversationReceiptStore(tmp_path / "receipts.db"),
        action_bridge=bridge,
    )

    reply, _ = processor.process(
        "qq:100:10",
        UserTurn("qq", "private:7", "帮我修改 Hikari 仓库"),
    )

    assert len(bridge.calls) == 1
    assert reply.text == "engineering accepted"
