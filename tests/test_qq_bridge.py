from __future__ import annotations

import asyncio
import os
from pathlib import Path
import subprocess
import sys

import pytest

from conversation.models import AssistantReply, UserTurn
from integrations.qq_bridge.config import QQBridgeConfig
from integrations.qq_bridge.health import OneBotLinkHealth
from integrations.qq_bridge.mapper import (
    extract_text_message,
    normalize_group_message,
    normalize_private_message,
)
from integrations.qq_bridge.runtime import QQBridgeRuntime
from integrations.qq_bridge.spool import BridgeSpool


def test_config_fails_closed_without_allowlist(tmp_path: Path):
    with pytest.raises(ValueError, match="ALLOWED_USER_IDS"):
        QQBridgeConfig.from_mapping({}, state_dir=tmp_path)


def test_config_builds_reverse_websocket_defaults(tmp_path: Path):
    config = QQBridgeConfig.from_mapping(
        {"HIKARI_ONEBOT_ALLOWED_USER_IDS": " 7,8,7 "},
        state_dir=tmp_path,
    )

    assert config.onebot_host == "127.0.0.1"
    assert config.onebot_port == 8081
    assert config.core_url == "ws://127.0.0.1:8765"
    assert config.allowed_user_ids == frozenset({"7", "8"})
    assert config.spool_path == (tmp_path / "qq_bridge.db").resolve()
    assert config.conversation_retry_initial_seconds == 5
    assert config.conversation_retry_max_seconds == 60


def test_config_group_chat_disabled_by_default(tmp_path: Path):
    config = QQBridgeConfig.from_mapping(
        {"HIKARI_ONEBOT_ALLOWED_USER_IDS": "7"},
        state_dir=tmp_path,
    )

    assert config.allowed_group_ids == frozenset()


def test_config_parses_group_allowlist(tmp_path: Path):
    config = QQBridgeConfig.from_mapping(
        {
            "HIKARI_ONEBOT_ALLOWED_USER_IDS": "7",
            "HIKARI_ONEBOT_ALLOWED_GROUP_IDS": " 10, 11,10 ",
        },
        state_dir=tmp_path,
    )

    assert config.allowed_group_ids == frozenset({"10", "11"})


def test_config_rejects_conversation_retry_max_below_initial(tmp_path: Path):
    with pytest.raises(ValueError, match="max interval"):
        QQBridgeConfig.from_mapping(
            {
                "HIKARI_ONEBOT_ALLOWED_USER_IDS": "7",
                "HIKARI_QQ_CONVERSATION_RETRY_INITIAL_SECONDS": "10",
                "HIKARI_QQ_CONVERSATION_RETRY_MAX_SECONDS": "5",
            },
            state_dir=tmp_path,
        )


def test_non_loopback_onebot_listener_is_rejected_even_with_token(tmp_path: Path):
    with pytest.raises(ValueError, match="loopback-only"):
        QQBridgeConfig.from_mapping(
            {
                "HIKARI_ONEBOT_ALLOWED_USER_IDS": "7",
                "HIKARI_ONEBOT_HOST": "0.0.0.0",
                "HIKARI_ONEBOT_ACCESS_TOKEN": "still-not-a-tls-layer",
            },
            state_dir=tmp_path,
        )


def test_mapper_accepts_allowlisted_private_pure_text():
    result = normalize_private_message(
        bot_self_id=100,
        user_id=7,
        message_id=99,
        message=[
            {"type": "text", "data": {"text": "你"}},
            {"type": "text", "data": {"text": "好"}},
        ],
        allowed_user_ids=frozenset({"7"}),
    )

    assert result is not None
    request_id, turn = result
    assert request_id == "qq:100:99"
    assert turn == UserTurn("qq", "private:7", "你好")


def test_mapper_ignores_unapproved_or_non_text_payload():
    assert normalize_private_message(
        bot_self_id=100,
        user_id=8,
        message_id=1,
        message="hello",
        allowed_user_ids=frozenset({"7"}),
    ) is None
    assert extract_text_message(
        [{"type": "image", "data": {"file": "x"}}]
    ) is None
    assert extract_text_message("[CQ:image,file=x]") is None


def at_message(target: object, *texts: str) -> list[dict[str, object]]:
    segments: list[dict[str, object]] = [{"type": "at", "data": {"qq": target}}]
    segments.extend({"type": "text", "data": {"text": text}} for text in texts)
    return segments


def test_group_mapper_accepts_allowlisted_user_addressing_hikari():
    result = normalize_group_message(
        bot_self_id=100,
        group_id=10,
        user_id=7,
        message_id=301,
        message=at_message(100, "在", "吗"),
        allowed_user_ids=frozenset({"7"}),
        allowed_group_ids=frozenset({"10"}),
    )

    assert result == ("qq:100:g:10:301", UserTurn("qq", "group:10", "在吗"))


def test_group_mapper_accepts_int_at_target_matching_self_id():
    result = normalize_group_message(
        bot_self_id="100",
        group_id="10",
        user_id="7",
        message_id=302,
        message=at_message(100, "你好"),
        allowed_user_ids=frozenset({"7"}),
        allowed_group_ids=frozenset({"10"}),
    )

    assert result is not None
    assert result[0] == "qq:100:g:10:302"
    assert result[1].conversation_id == "group:10"
    assert result[1].text == "你好"


def test_group_mapper_strips_at_self_anywhere_in_segments():
    result = normalize_group_message(
        bot_self_id=100,
        group_id=10,
        user_id=7,
        message_id=303,
        message=[
            {"type": "text", "data": {"text": "请"}},
            {"type": "at", "data": {"qq": 100}},
            {"type": "text", "data": {"text": "看下"}},
        ],
        allowed_user_ids=frozenset({"7"}),
        allowed_group_ids=frozenset({"10"}),
    )

    assert result is not None
    assert result[1].text == "请看下"


def test_group_mapper_rejects_messages_not_addressing_hikari():
    assert normalize_group_message(
        bot_self_id=100,
        group_id=10,
        user_id=7,
        message_id=304,
        message="hello",
        allowed_user_ids=frozenset({"7"}),
        allowed_group_ids=frozenset({"10"}),
    ) is None
    assert normalize_group_message(
        bot_self_id=100,
        group_id=10,
        user_id=7,
        message_id=305,
        message=[{"type": "text", "data": {"text": "hello"}}],
        allowed_user_ids=frozenset({"7"}),
        allowed_group_ids=frozenset({"10"}),
    ) is None
    assert normalize_group_message(
        bot_self_id=100,
        group_id=10,
        user_id=7,
        message_id=306,
        message=at_message(101, "hello"),
        allowed_user_ids=frozenset({"7"}),
        allowed_group_ids=frozenset({"10"}),
    ) is None


def test_group_mapper_rejects_unapproved_sender_group_or_rich_message():
    assert normalize_group_message(
        bot_self_id=100,
        group_id=10,
        user_id=8,
        message_id=307,
        message=at_message(100, "hello"),
        allowed_user_ids=frozenset({"7"}),
        allowed_group_ids=frozenset({"10"}),
    ) is None
    assert normalize_group_message(
        bot_self_id=100,
        group_id=20,
        user_id=7,
        message_id=308,
        message=at_message(100, "hello"),
        allowed_user_ids=frozenset({"7"}),
        allowed_group_ids=frozenset({"10"}),
    ) is None
    assert normalize_group_message(
        bot_self_id=100,
        group_id=10,
        user_id=7,
        message_id=309,
        message=at_message(100, "hello"),
        allowed_user_ids=frozenset({"7"}),
        allowed_group_ids=frozenset(),
    ) is None
    assert normalize_group_message(
        bot_self_id=100,
        group_id=10,
        user_id=7,
        message_id=310,
        message=at_message(100) + [{"type": "image", "data": {"file": "x"}}],
        allowed_user_ids=frozenset({"7"}),
        allowed_group_ids=frozenset({"10"}),
    ) is None


def test_spool_survives_restart_and_tracks_delivery(tmp_path: Path):
    path = tmp_path / "qq.db"
    turn = UserTurn("qq", "private:7", "hello")
    first = BridgeSpool(path)
    item = first.record_turn("qq:100:1", turn)
    assert item.state == "pending"

    first.set_reply(
        "qq:100:1",
        AssistantReply("qq", "private:7", "reply"),
    )
    restarted = BridgeSpool(path)
    unsent = restarted.unsent()
    assert len(unsent) == 1
    assert unsent[0].reply_text == "reply"
    assert unsent[0].state == "replied"

    restarted.mark_sent("qq:100:1")
    assert restarted.unsent() == []


def test_link_health_probes_quiet_connections_without_declaring_disconnect():
    now = [0.0]
    health = OneBotLinkHealth(timeout_seconds=10, clock=lambda: now[0])
    health.mark_connected(100)
    assert health.snapshot().healthy is True

    now[0] = 11.0
    assert health.needs_probe() is True
    health.mark_probe(True)
    snapshot = health.snapshot()
    assert snapshot.connected is True
    assert snapshot.last_probe_ok is True
    assert snapshot.healthy is True

    health.mark_disconnected()
    assert health.snapshot().healthy is False


class FakeCore:
    def __init__(self) -> None:
        self.requests: list[tuple[str, UserTurn]] = []
        self.closed = False

    async def request(self, request_id: str, turn: UserTurn) -> AssistantReply:
        self.requests.append((request_id, turn))
        return AssistantReply(turn.channel, turn.conversation_id, "在呢。")

    async def close(self) -> None:
        self.closed = True


class RecoveringCore(FakeCore):
    def __init__(self, *, failures: int) -> None:
        super().__init__()
        self.failures = failures

    async def request(self, request_id: str, turn: UserTurn) -> AssistantReply:
        self.requests.append((request_id, turn))
        if self.failures > 0:
            self.failures -= 1
            raise ConnectionError("model unavailable")
        return AssistantReply(turn.channel, turn.conversation_id, "恢复了。")


class BlockingCore(FakeCore):
    def __init__(self, started: asyncio.Event, release: asyncio.Event) -> None:
        super().__init__()
        self.started = started
        self.release = release

    async def request(self, request_id: str, turn: UserTurn) -> AssistantReply:
        self.requests.append((request_id, turn))
        self.started.set()
        await self.release.wait()
        return AssistantReply(turn.channel, turn.conversation_id, "只回复一次。")


class FakeBot:
    self_id = "100"

    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []
        self.probes = 0

    async def send_private_msg(self, **kwargs):
        self.sent.append(dict(kwargs))
        return {"message_id": 1}

    async def send_group_msg(self, **kwargs):
        self.sent.append(dict(kwargs))
        return {"message_id": 1}

    async def get_status(self):
        self.probes += 1
        return {"online": True}


class FakePrivateEvent:
    def __init__(self, *, user_id: int, message_id: int, message: object) -> None:
        self.user_id = user_id
        self.message_id = message_id
        self.message = message


class FakeGroupEvent:
    def __init__(
        self,
        *,
        group_id: int,
        user_id: int,
        message_id: int,
        message: object,
    ) -> None:
        self.group_id = group_id
        self.user_id = user_id
        self.message_id = message_id
        self.message = message
        self.original_message = message


def test_runtime_delivers_each_onebot_message_once(tmp_path: Path):
    config = QQBridgeConfig.from_mapping(
        {"HIKARI_ONEBOT_ALLOWED_USER_IDS": "7"},
        state_dir=tmp_path,
    )
    core = FakeCore()
    bot = FakeBot()
    runtime = QQBridgeRuntime(
        config,
        core,  # type: ignore[arg-type]
        BridgeSpool(tmp_path / "spool.db"),
        OneBotLinkHealth(timeout_seconds=10),
    )
    event = FakePrivateEvent(user_id=7, message_id=55, message="hikari")

    asyncio.run(runtime.handle_private_message(bot, event))  # type: ignore[arg-type]
    asyncio.run(runtime.handle_private_message(bot, event))  # type: ignore[arg-type]

    assert len(core.requests) == 1
    assert core.requests[0][0] == "qq:100:55"
    assert len(bot.sent) == 1
    assert bot.sent[0]["user_id"] == 7
    assert bot.sent[0]["message"] == "在呢。"
    assert bot.sent[0]["auto_escape"] is True


def test_runtime_delivers_allowlisted_group_mention_once_to_the_group(
    tmp_path: Path,
):
    config = QQBridgeConfig.from_mapping(
        {
            "HIKARI_ONEBOT_ALLOWED_USER_IDS": "7",
            "HIKARI_ONEBOT_ALLOWED_GROUP_IDS": "10",
        },
        state_dir=tmp_path,
    )
    core = FakeCore()
    bot = FakeBot()
    runtime = QQBridgeRuntime(
        config,
        core,  # type: ignore[arg-type]
        BridgeSpool(tmp_path / "spool.db"),
        OneBotLinkHealth(timeout_seconds=10),
    )
    event = FakeGroupEvent(
        group_id=10,
        user_id=7,
        message_id=55,
        message=at_message(100, "大家好"),
    )

    asyncio.run(runtime.handle_group_message(bot, event))  # type: ignore[arg-type]
    asyncio.run(runtime.handle_group_message(bot, event))  # type: ignore[arg-type]

    assert len(core.requests) == 1
    assert core.requests[0][0] == "qq:100:g:10:55"
    assert core.requests[0][1] == UserTurn("qq", "group:10", "大家好")
    assert bot.sent == [
        {"group_id": 10, "message": "在呢。", "auto_escape": True}
    ]


def test_runtime_ignores_group_outside_allowlist_without_model_call(tmp_path: Path):
    config = QQBridgeConfig.from_mapping(
        {
            "HIKARI_ONEBOT_ALLOWED_USER_IDS": "7",
            "HIKARI_ONEBOT_ALLOWED_GROUP_IDS": "10",
        },
        state_dir=tmp_path,
    )
    core = FakeCore()
    bot = FakeBot()
    runtime = QQBridgeRuntime(
        config,
        core,  # type: ignore[arg-type]
        BridgeSpool(tmp_path / "spool.db"),
        OneBotLinkHealth(timeout_seconds=10),
    )
    event = FakeGroupEvent(
        group_id=99,
        user_id=7,
        message_id=56,
        message=at_message(100, "在吗"),
    )

    asyncio.run(runtime.handle_group_message(bot, event))  # type: ignore[arg-type]

    assert core.requests == []
    assert bot.sent == []


def test_outbound_validation_requires_approved_group_target(tmp_path: Path):
    config = QQBridgeConfig.from_mapping(
        {"HIKARI_ONEBOT_ALLOWED_USER_IDS": "7"},
        state_dir=tmp_path,
    )
    runtime = QQBridgeRuntime(
        config,
        FakeCore(),  # type: ignore[arg-type]
        BridgeSpool(tmp_path / "spool.db"),
        OneBotLinkHealth(timeout_seconds=10),
    )
    with pytest.raises(ValueError, match="unapproved group"):
        runtime._validate_outbound(AssistantReply("qq", "group:10", "hi"))

    approved = QQBridgeConfig.from_mapping(
        {
            "HIKARI_ONEBOT_ALLOWED_USER_IDS": "7",
            "HIKARI_ONEBOT_ALLOWED_GROUP_IDS": "10",
        },
        state_dir=tmp_path,
    )
    runtime = QQBridgeRuntime(
        approved,
        FakeCore(),  # type: ignore[arg-type]
        BridgeSpool(tmp_path / "spool2.db"),
        OneBotLinkHealth(timeout_seconds=10),
    )
    runtime._validate_outbound(AssistantReply("qq", "group:10", "hi"))


def test_runtime_automatically_retries_pending_turn_after_model_recovery(
    tmp_path: Path,
):
    config = QQBridgeConfig.from_mapping(
        {
            "HIKARI_ONEBOT_ALLOWED_USER_IDS": "7",
            "HIKARI_QQ_CONVERSATION_RETRY_INITIAL_SECONDS": "0.01",
            "HIKARI_QQ_CONVERSATION_RETRY_MAX_SECONDS": "0.02",
        },
        state_dir=tmp_path,
    )
    core = RecoveringCore(failures=1)
    bot = FakeBot()
    spool = BridgeSpool(tmp_path / "spool.db")
    runtime = QQBridgeRuntime(
        config,
        core,  # type: ignore[arg-type]
        spool,
        OneBotLinkHealth(timeout_seconds=10),
    )
    event = FakePrivateEvent(user_id=7, message_id=56, message="回来了吗")

    async def scenario() -> None:
        await runtime.start()
        await runtime.on_bot_connect(bot)  # type: ignore[arg-type]
        with pytest.raises(ConnectionError, match="model unavailable"):
            await runtime.handle_private_message(bot, event)  # type: ignore[arg-type]
        assert spool.get("qq:100:56").state == "pending"  # type: ignore[union-attr]
        for _ in range(100):
            if spool.get("qq:100:56").state == "sent":  # type: ignore[union-attr]
                break
            await asyncio.sleep(0.01)
        await runtime.close()

    asyncio.run(scenario())

    assert spool.get("qq:100:56").state == "sent"  # type: ignore[union-attr]
    assert len(core.requests) == 2
    assert len(bot.sent) == 1
    assert bot.sent[0]["message"] == "恢复了。"


def test_live_and_recovery_paths_serialize_same_turn(tmp_path: Path):
    config = QQBridgeConfig.from_mapping(
        {"HIKARI_ONEBOT_ALLOWED_USER_IDS": "7"},
        state_dir=tmp_path,
    )
    bot = FakeBot()
    spool = BridgeSpool(tmp_path / "spool.db")
    event = FakePrivateEvent(user_id=7, message_id=57, message="别重复")

    async def scenario() -> tuple[BlockingCore, QQBridgeRuntime]:
        started = asyncio.Event()
        release = asyncio.Event()
        core = BlockingCore(started, release)
        runtime = QQBridgeRuntime(
            config,
            core,  # type: ignore[arg-type]
            spool,
            OneBotLinkHealth(timeout_seconds=10),
        )
        spool.record_turn("qq:100:57", UserTurn("qq", "private:7", "别重复"))
        recovery = asyncio.create_task(runtime.drain_unsent(bot))  # type: ignore[arg-type]
        await started.wait()
        live = asyncio.create_task(
            runtime.handle_private_message(bot, event)  # type: ignore[arg-type]
        )
        await asyncio.sleep(0)
        assert len(core.requests) == 1
        release.set()
        await asyncio.gather(recovery, live)
        return core, runtime

    core, _ = asyncio.run(scenario())

    assert len(core.requests) == 1
    assert len(bot.sent) == 1
    assert spool.get("qq:100:57").state == "sent"  # type: ignore[union-attr]


def test_standalone_cli_check_assembles_nonebot_runtime(tmp_path: Path):
    repository = Path(__file__).resolve().parents[1]
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("HIKARI_")
    }
    environment["HIKARI_ONEBOT_ALLOWED_USER_IDS"] = "7"
    environment["HIKARI_ONEBOT_HOST"] = "127.0.0.1"
    environment["HIKARI_ONEBOT_PORT"] = "18081"
    environment["HIKARI_CONVERSATION_URL"] = "ws://127.0.0.1:18765"
    environment["XDG_STATE_HOME"] = str(tmp_path / "state")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "integrations.qq_bridge.app",
            "--check",
        ],
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Hikari QQ Bridge check：PASS" in result.stdout
    assert "ws://127.0.0.1:18081/onebot/v11/ws" in result.stdout
