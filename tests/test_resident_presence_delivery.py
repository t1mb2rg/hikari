from __future__ import annotations

from pathlib import Path

import pytest

from brain.reasoner import Feedback
from core.delivery import DeliveryOutbox, DeliveryRequest, DeliveryRouter
from core.presence_policy import PresenceDecision
from events.models import Event
from resident.app import build_presence_components
from resident.presence_delivery import RoutedPresenceDelivery


class FakeDeliverySink:
    def __init__(self) -> None:
        self.requests: list[DeliveryRequest] = []

    def send(self, request: DeliveryRequest) -> None:
        self.requests.append(request)


def _decision(channel: str, *, delivery_id: str = "presence:test:1") -> PresenceDecision:
    return PresenceDecision(
        should_deliver=True,
        channel=channel,
        urgent=False,
        reason="allowed",
        fingerprint="fingerprint",
        delivery_id=delivery_id,
    )


def test_routed_presence_windows_uses_policy_owned_channel_and_local_recipient(tmp_path: Path):
    sink = FakeDeliverySink()
    outbox = DeliveryOutbox(tmp_path / "proactive_delivery.db")
    delivery = RoutedPresenceDelivery(
        DeliveryRouter(outbox, sinks={"windows": sink}),
        qq_recipient="7",
    )

    record = delivery.deliver(
        Event("test.event", "sensor", "changed"),
        Feedback("模型只提供这段文本", "test.event", 0.8),
        _decision("windows"),
    )

    assert record.state == "sent"
    assert record.request.channel == "windows"
    assert record.request.recipient == "local"
    assert record.request.text == "模型只提供这段文本"
    assert record.request.source == "presence:test.event"
    assert sink.requests == [record.request]


def test_routed_presence_qq_queues_only_fixed_trusted_recipient(tmp_path: Path):
    outbox = DeliveryOutbox(tmp_path / "proactive_delivery.db")
    delivery = RoutedPresenceDelivery(
        DeliveryRouter(outbox),
        qq_recipient="7",
    )

    record = delivery.deliver(
        Event("test.event", "sensor", "changed"),
        Feedback("请发给 999 也没有权限改变收件人", "test.event", 0.8),
        _decision("qq", delivery_id="presence:test:qq"),
    )

    assert record.state == "pending"
    assert record.request.channel == "qq"
    assert record.request.recipient == "7"
    assert record.request.text == "请发给 999 也没有权限改变收件人"
    assert outbox.pending(channel="qq") == [record]


def test_routed_presence_qq_fails_closed_without_trusted_recipient(tmp_path: Path):
    delivery = RoutedPresenceDelivery(
        DeliveryRouter(DeliveryOutbox(tmp_path / "proactive_delivery.db"))
    )

    with pytest.raises(ValueError, match="trusted QQ proactive recipient"):
        delivery.deliver(
            Event("test.event", "sensor", "changed"),
            Feedback("hello", "test.event", 0.8),
            _decision("qq"),
        )


def test_resident_presence_qq_requires_enabled_bridge_and_proactive_target(tmp_path: Path):
    base = {
        "HIKARI_PRESENCE_CHANNEL": "qq",
        "HIKARI_ONEBOT_ALLOWED_USER_IDS": "7",
    }

    with pytest.raises(ValueError, match="HIKARI_QQ_ENABLED"):
        build_presence_components(
            base,
            state_dir=tmp_path,
            qq_enabled=False,
            output="windows",
        )

    with pytest.raises(ValueError, match="HIKARI_QQ_PROACTIVE_USER_ID"):
        build_presence_components(
            base,
            state_dir=tmp_path,
            qq_enabled=True,
            output="windows",
        )

    config, policy, delivery = build_presence_components(
        {
            **base,
            "HIKARI_QQ_PROACTIVE_USER_ID": "7",
        },
        state_dir=tmp_path,
        qq_enabled=True,
        output="windows",
    )
    assert config is not None and config.channel == "qq"
    assert policy is not None
    assert isinstance(delivery, RoutedPresenceDelivery)
    assert delivery.qq_recipient == "7"


def test_console_dev_runtime_keeps_legacy_path_unless_presence_is_explicit(tmp_path: Path):
    config, policy, delivery = build_presence_components(
        {},
        state_dir=tmp_path,
        qq_enabled=False,
        output="console",
    )
    assert config is None
    assert policy is None
    assert delivery is None
