from __future__ import annotations

import asyncio
from pathlib import Path

from conversation.models import AssistantReply
from integrations.qq_bridge.config import QQBridgeConfig
from integrations.qq_bridge.health import OneBotLinkHealth
from integrations.qq_bridge.runtime import QQBridgeRuntime
from integrations.qq_bridge.spool import BridgeSpool
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment


class RecordingCore:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def request(self, request_id: str, turn):
        self.calls.append((request_id, turn))
        return AssistantReply(
            channel="qq",
            conversation_id=turn.conversation_id,
            text="收到",
        )

    async def close(self) -> None:
        return None


class RecordingBot:
    self_id = 100

    def __init__(self) -> None:
        self.group_sends: list[dict[str, object]] = []

    async def send_group_msg(self, **kwargs):
        self.group_sends.append(dict(kwargs))


def _group_event(*, message: Message | None = None) -> GroupMessageEvent:
    return GroupMessageEvent(
        time=1,
        self_id=100,
        post_type="message",
        sub_type="normal",
        user_id=7,
        message_type="group",
        message_id=501,
        message=(
            message
            if message is not None
            else Message(
                [
                    MessageSegment.at(100),
                    MessageSegment.text(" 你好"),
                ]
            )
        ),
        raw_message="[CQ:at,qq=100] 你好",
        font=0,
        sender={"user_id": 7, "nickname": "tester"},
        group_id=10,
        anonymous=None,
    )


def _runtime(tmp_path: Path) -> tuple[QQBridgeRuntime, RecordingCore, RecordingBot]:
    config = QQBridgeConfig.from_mapping(
        {
            "HIKARI_ONEBOT_ALLOWED_USER_IDS": "7",
            "HIKARI_ONEBOT_ALLOWED_GROUP_IDS": "10",
        },
        state_dir=tmp_path,
    )
    core = RecordingCore()
    bot = RecordingBot()
    runtime = QQBridgeRuntime(
        config,
        core,  # type: ignore[arg-type]
        BridgeSpool(tmp_path / "qq_bridge.db"),
        OneBotLinkHealth(timeout_seconds=config.link_timeout_seconds),
    )
    return runtime, core, bot


def test_group_runtime_uses_original_message_after_nonebot_strips_at_self(
    tmp_path: Path,
):
    event = _group_event()

    # nonebot-adapter-onebot v11 _check_at_me() runs before matchers and removes
    # the at-self segment from event.message while keeping original_message intact.
    event.to_me = True
    event.message.pop(0)
    event.message[0].data["text"] = event.message[0].data["text"].lstrip()

    assert [segment.type for segment in event.message] == ["text"]
    assert [segment.type for segment in event.original_message] == ["at", "text"]

    runtime, core, bot = _runtime(tmp_path)

    asyncio.run(runtime.handle_group_message(bot, event))  # type: ignore[arg-type]

    assert len(core.calls) == 1
    request_id, turn = core.calls[0]
    assert request_id == "qq:100:g:10:501"
    assert turn.conversation_id == "group:10"
    assert turn.text == "你好"
    assert turn.actor_id == "7"
    assert turn.scope == "shared"
    assert bot.group_sends == [
        {
            "group_id": 10,
            "message": "收到",
            "auto_escape": True,
        }
    ]


def test_group_runtime_does_not_trust_to_me_without_original_at_self(
    tmp_path: Path,
):
    event = _group_event(message=Message([MessageSegment.text("你好")]))

    # `to_me` may be set by adapter behavior other than an explicit at-self
    # (for example reply/nickname handling). Hikari's shared ingress contract is
    # intentionally stricter: only a real @Hikari present in original_message passes.
    event.to_me = True

    assert [segment.type for segment in event.original_message] == ["text"]

    runtime, core, bot = _runtime(tmp_path)

    asyncio.run(runtime.handle_group_message(bot, event))  # type: ignore[arg-type]

    assert core.calls == []
    assert bot.group_sends == []
