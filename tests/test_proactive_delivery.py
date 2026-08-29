from __future__ import annotations

import asyncio
import os
from pathlib import Path
import subprocess
import sys

import pytest

from core.delivery import DeliveryOutbox, DeliveryRequest, DeliveryRouter
from integrations.qq_bridge.config import QQBridgeConfig
from integrations.qq_bridge.health import OneBotLinkHealth
from integrations.qq_bridge.runtime import QQBridgeRuntime
from integrations.qq_bridge.spool import BridgeSpool


class FakeSink:
    def __init__(self) -> None:
        self.requests: list[DeliveryRequest] = []

    def send(self, request: DeliveryRequest) -> None:
        self.requests.append(request)


class FakeCore:
    def __init__(self) -> None:
        self.requests: list[object] = []
        self.closed = False

    async def request(self, request_id, turn):
        self.requests.append((request_id, turn))
        raise AssertionError("proactive delivery must not create an inbound Conversation turn")

    async def close(self) -> None:
        self.closed = True


class FakeBot:
    self_id = "100"

    def __init__(self, *, failures: int = 0) -> None:
        self.sent: list[dict[str, object]] = []
        self.failures = failures

    async def send_private_msg(self, **kwargs):
        if self.failures > 0:
            self.failures -= 1
            raise ConnectionError("transport offline")
        self.sent.append(dict(kwargs))
        return {"message_id": len(self.sent)}

    async def get_status(self):
        return {"online": True}


def _config(tmp_path: Path, **extra: str) -> QQBridgeConfig:
    values = {
        "HIKARI_ONEBOT_ALLOWED_USER_IDS": "7",
        "HIKARI_QQ_PROACTIVE_USER_ID": "7",
        "HIKARI_QQ_DELIVERY_POLL_SECONDS": "0.01",
    }
    values.update(extra)
    return QQBridgeConfig.from_mapping(values, state_dir=tmp_path)


def _runtime(tmp_path: Path, outbox: DeliveryOutbox) -> tuple[QQBridgeRuntime, FakeCore]:
    core = FakeCore()
    runtime = QQBridgeRuntime(
        _config(tmp_path),
        core,  # type: ignore[arg-type]
        BridgeSpool(tmp_path / "qq_bridge.db"),
        OneBotLinkHealth(timeout_seconds=10),
        outbox,
    )
    return runtime, core


def test_proactive_recipient_must_be_allowlisted(tmp_path: Path):
    with pytest.raises(ValueError, match="PROACTIVE_USER_ID"):
        QQBridgeConfig.from_mapping(
            {
                "HIKARI_ONEBOT_ALLOWED_USER_IDS": "7",
                "HIKARI_QQ_PROACTIVE_USER_ID": "8",
            },
            state_dir=tmp_path,
        )

    config = QQBridgeConfig.from_mapping(
        {"HIKARI_ONEBOT_ALLOWED_USER_IDS": "7"},
        state_dir=tmp_path,
    )
    assert config.proactive_user_id is None
    assert config.delivery_outbox_path == (tmp_path / "proactive_delivery.db").resolve()


def test_delivery_outbox_is_idempotent_and_rejects_id_reuse(tmp_path: Path):
    outbox = DeliveryOutbox(tmp_path / "delivery.db")
    request = DeliveryRequest("presence:1", "qq", "7", "先开口。")

    first = outbox.enqueue(request)
    second = outbox.enqueue(request)

    assert first == second
    assert first.state == "pending"
    assert len(outbox.pending(channel="qq")) == 1

    with pytest.raises(ValueError, match="reused"):
        outbox.enqueue(DeliveryRequest("presence:1", "qq", "7", "另一条消息"))


def test_delivery_router_supports_immediate_windows_boundary(tmp_path: Path):
    outbox = DeliveryOutbox(tmp_path / "delivery.db")
    sink = FakeSink()
    router = DeliveryRouter(outbox, sinks={"windows": sink})
    request = DeliveryRequest("presence:windows:1", "windows", "local", "看这里。")

    first = router.submit(request)
    second = router.submit(request)

    assert first.state == "sent"
    assert second.state == "sent"
    assert first.attempts == 1
    assert sink.requests == [request]


def test_qq_runtime_sends_proactive_without_inbound_conversation(tmp_path: Path):
    outbox = DeliveryOutbox(tmp_path / "delivery.db")
    request = DeliveryRequest("presence:qq:1", "qq", "7", "我先来找你。")
    outbox.enqueue(request)
    runtime, core = _runtime(tmp_path, outbox)
    bot = FakeBot()

    async def scenario() -> None:
        await runtime.on_bot_connect(bot)  # type: ignore[arg-type]
        await runtime.on_bot_disconnect(bot)  # type: ignore[arg-type]
        await runtime.on_bot_connect(bot)  # type: ignore[arg-type]

    asyncio.run(scenario())

    assert core.requests == []
    assert len(bot.sent) == 1
    assert bot.sent[0] == {
        "user_id": 7,
        "message": "我先来找你。",
        "auto_escape": True,
    }
    record = outbox.get(request.delivery_id)
    assert record is not None
    assert record.state == "sent"
    assert record.attempts == 1


def test_qq_runtime_retries_pending_after_transport_recovery(tmp_path: Path):
    outbox = DeliveryOutbox(tmp_path / "delivery.db")
    request = DeliveryRequest("presence:qq:2", "qq", "7", "等连接回来。")
    outbox.enqueue(request)
    runtime, _ = _runtime(tmp_path, outbox)
    bot = FakeBot(failures=1)

    asyncio.run(runtime.on_bot_connect(bot))  # type: ignore[arg-type]
    failed = outbox.get(request.delivery_id)
    assert failed is not None
    assert failed.state == "pending"
    assert failed.attempts == 1
    assert bot.sent == []

    asyncio.run(runtime.on_bot_connect(bot))  # type: ignore[arg-type]
    delivered = outbox.get(request.delivery_id)
    assert delivered is not None
    assert delivered.state == "sent"
    assert delivered.attempts == 2
    assert len(bot.sent) == 1


def test_qq_runtime_refuses_outbox_record_for_other_recipient(tmp_path: Path):
    outbox = DeliveryOutbox(tmp_path / "delivery.db")
    request = DeliveryRequest("presence:qq:bad", "qq", "8", "不能发。")
    outbox.enqueue(request)
    runtime, _ = _runtime(tmp_path, outbox)
    bot = FakeBot()

    asyncio.run(runtime.on_bot_connect(bot))  # type: ignore[arg-type]

    assert bot.sent == []
    record = outbox.get(request.delivery_id)
    assert record is not None
    assert record.state == "pending"
    assert record.attempts == 0


def test_crash_interrupted_send_becomes_uncertain_not_auto_retried(tmp_path: Path):
    outbox = DeliveryOutbox(tmp_path / "delivery.db")
    request = DeliveryRequest("presence:qq:uncertain", "qq", "7", "不要重复。")
    outbox.enqueue(request)
    claimed = outbox.claim(request.delivery_id)
    assert claimed.state == "sending"

    restarted = DeliveryOutbox(tmp_path / "delivery.db")
    assert restarted.recover_inflight() == 1
    uncertain = restarted.get(request.delivery_id)
    assert uncertain is not None
    assert uncertain.state == "uncertain"
    assert restarted.pending(channel="qq") == []

    retried = restarted.retry_uncertain(request.delivery_id)
    assert retried.state == "pending"


def test_delivery_cli_queues_only_configured_qq_recipient(tmp_path: Path):
    repository = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    environment["HIKARI_ONEBOT_ALLOWED_USER_IDS"] = "7"
    environment["HIKARI_QQ_PROACTIVE_USER_ID"] = "7"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "resident.delivery_cli",
            "--state-dir",
            str(tmp_path),
            "send",
            "qq",
            "presence:cli:1",
            "主动测试",
        ],
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "recipient：7" in result.stdout
    assert "state：pending" in result.stdout
    record = DeliveryOutbox(tmp_path / "proactive_delivery.db").get("presence:cli:1")
    assert record is not None
    assert record.request.recipient == "7"
