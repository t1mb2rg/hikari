from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from attention import AttentionPolicy
from attention.policy import AttentionDecision
from awareness.context import HIKARI_CONTEXT_KEY
from brain.reasoner import Feedback
from core.presence import ConsoleFeedbackSink, PresencePipeline
from core.presence_policy import (
    PresencePolicy,
    PresencePolicyConfig,
    PresencePolicyStore,
)
from events.models import Event
from memory.store import MemoryStore


TZ8 = timezone(timedelta(hours=8))


def _event(
    *,
    content: str = "something changed",
    local_iso: str = "2026-08-29T12:00:00+08:00",
    recent_input: object = True,
    foreground_title: str | None = "Editor",
    current_schedule: list[object] | None = None,
    occurred_at: datetime | None = None,
) -> Event:
    providers: dict[str, dict[str, object]] = {
        "time": {"local_iso": local_iso, "hour": int(local_iso[11:13])},
        "input_activity": {"recent_input": recent_input},
        "schedule": {"current": list(current_schedule or [])},
    }
    if foreground_title is None:
        providers["foreground"] = {"available": False}
    else:
        providers["foreground"] = {
            "available": True,
            "title": foreground_title,
        }
    return Event(
        "test.event",
        "test",
        content,
        context={
            HIKARI_CONTEXT_KEY: {
                "captured_at": "2026-08-29T04:00:00+00:00",
                "providers": providers,
            }
        },
        occurred_at=occurred_at or datetime(2026, 8, 29, 4, 0, tzinfo=timezone.utc),
    )


def _attention(importance: float = 0.8) -> AttentionDecision:
    return AttentionDecision(True, importance, "test")


def _policy(
    tmp_path: Path,
    *,
    config: PresencePolicyConfig | None = None,
    now: datetime | None = None,
) -> PresencePolicy:
    instant = now or datetime(2026, 8, 29, 12, 0, tzinfo=TZ8)
    return PresencePolicy(
        config or PresencePolicyConfig(cooldown_seconds=0, duplicate_window_seconds=0),
        PresencePolicyStore(tmp_path / "presence_policy.db"),
        clock=lambda: instant,
    )


def test_presence_config_parses_strict_runtime_values():
    config = PresencePolicyConfig.from_mapping(
        {
            "HIKARI_PRESENCE_CHANNEL": "QQ",
            "HIKARI_PRESENCE_QUIET_HOURS_ENABLED": "yes",
            "HIKARI_PRESENCE_QUIET_START": "22:30",
            "HIKARI_PRESENCE_QUIET_END": "06:15",
            "HIKARI_PRESENCE_COOLDOWN_SECONDS": "90",
            "HIKARI_PRESENCE_DUPLICATE_WINDOW_SECONDS": "600",
            "HIKARI_PRESENCE_URGENT_THRESHOLD": "0.97",
            "HIKARI_PRESENCE_BUSY_FOREGROUND_PATTERNS": " meeting , Game ",
        }
    )

    assert config.channel == "qq"
    assert config.quiet_hours_enabled is True
    assert (config.quiet_start.hour, config.quiet_start.minute) == (22, 30)
    assert (config.quiet_end.hour, config.quiet_end.minute) == (6, 15)
    assert config.cooldown_seconds == 90
    assert config.duplicate_window_seconds == 600
    assert config.urgent_threshold == 0.97
    assert config.busy_foreground_patterns == ("meeting", "game")

    with pytest.raises(ValueError, match="QUIET_START"):
        PresencePolicyConfig.from_mapping({"HIKARI_PRESENCE_QUIET_START": "25:00"})
    with pytest.raises(ValueError, match="boolean"):
        PresencePolicyConfig.from_mapping(
            {"HIKARI_PRESENCE_QUIET_HOURS_ENABLED": "sometimes"}
        )


def test_quiet_hours_support_overnight_range_and_urgent_bypass(tmp_path: Path):
    config = PresencePolicyConfig(
        channel="qq",
        quiet_hours_enabled=True,
        cooldown_seconds=0,
        duplicate_window_seconds=0,
        urgent_threshold=0.95,
    )
    policy = _policy(
        tmp_path,
        config=config,
        now=datetime(2026, 8, 29, 23, 30, tzinfo=TZ8),
    )
    event = _event(local_iso="2026-08-29T23:30:00+08:00")

    ordinary = policy.evaluate(event, _attention(0.8))
    assert ordinary.should_deliver is False
    assert ordinary.reason == "quiet hours"

    urgent = policy.evaluate(event, _attention(0.98))
    assert urgent.should_deliver is True
    assert urgent.urgent is True
    assert urgent.reason == "urgent threshold bypass"


def test_duplicate_suppression_survives_restart_even_for_urgent(tmp_path: Path):
    config = PresencePolicyConfig(
        cooldown_seconds=0,
        duplicate_window_seconds=600,
        urgent_threshold=0.95,
    )
    instant = datetime(2026, 8, 29, 12, 0, tzinfo=TZ8)
    first = _policy(tmp_path, config=config, now=instant)
    event = _event(content="same event")
    accepted = first.evaluate(event, _attention(0.99))
    assert accepted.should_deliver is True
    first.mark_accepted(accepted)

    restarted = _policy(
        tmp_path,
        config=config,
        now=instant + timedelta(seconds=30),
    )
    duplicate = restarted.evaluate(event, _attention(0.99))
    assert duplicate.should_deliver is False
    assert duplicate.reason == "duplicate suppression window"


def test_global_cooldown_survives_policy_recreation(tmp_path: Path):
    config = PresencePolicyConfig(
        cooldown_seconds=300,
        duplicate_window_seconds=0,
    )
    instant = datetime(2026, 8, 29, 12, 0, tzinfo=TZ8)
    first = _policy(tmp_path, config=config, now=instant)
    accepted = first.evaluate(_event(content="first"), _attention())
    first.mark_accepted(accepted)

    restarted = _policy(
        tmp_path,
        config=config,
        now=instant + timedelta(seconds=60),
    )
    blocked = restarted.evaluate(_event(content="second"), _attention())
    assert blocked.should_deliver is False
    assert blocked.reason == "global cooldown active"

    urgent = restarted.evaluate(_event(content="urgent"), _attention(0.99))
    assert urgent.should_deliver is True
    assert urgent.reason == "urgent threshold bypass"


def test_active_schedule_and_foreground_patterns_suppress_nonurgent(tmp_path: Path):
    config = PresencePolicyConfig(
        cooldown_seconds=0,
        duplicate_window_seconds=0,
        busy_foreground_patterns=("counter-strike", "meeting"),
    )
    policy = _policy(tmp_path, config=config)

    scheduled = policy.evaluate(
        _event(current_schedule=[{"title": "meeting"}]),
        _attention(),
    )
    assert scheduled.should_deliver is False
    assert scheduled.user_state is not None
    assert scheduled.user_state.interruptibility == "likely_busy"
    assert scheduled.reason == "active schedule suggests low interruptibility"

    gaming = policy.evaluate(
        _event(foreground_title="Counter-Strike 2"),
        _attention(),
    )
    assert gaming.should_deliver is False
    assert gaming.reason == "foreground matches busy pattern: counter-strike"


def test_unknown_context_does_not_invent_busy_state(tmp_path: Path):
    policy = _policy(tmp_path)
    event = Event("test.event", "test", "unknown context")

    decision = policy.evaluate(event, _attention())

    assert decision.should_deliver is True
    assert decision.user_state is None
    assert decision.reason == "allowed"


class CountingReasoner:
    def __init__(self) -> None:
        self.calls = 0

    def reason(self, event, decision):
        self.calls += 1
        return Feedback("hello", event.event_type, decision.importance)


class CapturingDelivery:
    def __init__(self) -> None:
        self.items: list[tuple[Event, Feedback, object]] = []

    def deliver(self, event, feedback, decision):
        self.items.append((event, feedback, decision))
        return None


def test_presence_pipeline_suppression_skips_reasoner(tmp_path: Path):
    reasoner = CountingReasoner()
    policy = _policy(
        tmp_path,
        config=PresencePolicyConfig(
            quiet_hours_enabled=True,
            cooldown_seconds=0,
            duplicate_window_seconds=0,
        ),
        now=datetime(2026, 8, 29, 23, 30, tzinfo=TZ8),
    )
    pipeline = PresencePipeline(
        memory=MemoryStore(tmp_path / "memory.db"),
        attention=AttentionPolicy(threshold=0.7, event_importance={"test.event": 0.8}),
        reasoner=reasoner,
        feedback_sink=ConsoleFeedbackSink(),
        presence_policy=policy,
    )

    result = pipeline.handle(_event(local_iso="2026-08-29T23:30:00+08:00"))

    assert result.feedback is None
    assert result.presence_decision is not None
    assert result.presence_decision.reason == "quiet hours"
    assert reasoner.calls == 0


def test_presence_pipeline_approved_path_uses_policy_delivery_and_records_state(tmp_path: Path):
    reasoner = CountingReasoner()
    delivery = CapturingDelivery()
    instant = datetime(2026, 8, 29, 12, 0, tzinfo=TZ8)
    config = PresencePolicyConfig(cooldown_seconds=300, duplicate_window_seconds=600)
    policy = _policy(tmp_path, config=config, now=instant)
    pipeline = PresencePipeline(
        memory=MemoryStore(tmp_path / "memory.db"),
        attention=AttentionPolicy(threshold=0.7, event_importance={"test.event": 0.8}),
        reasoner=reasoner,
        feedback_sink=ConsoleFeedbackSink(),
        presence_policy=policy,
        proactive_delivery_sink=delivery,
    )
    event = _event(content="approved")

    result = pipeline.handle(event)

    assert result.feedback is not None
    assert result.presence_decision is not None
    assert result.presence_decision.should_deliver is True
    assert reasoner.calls == 1
    assert len(delivery.items) == 1
    assert policy.store.accepted_at(result.presence_decision.fingerprint) is not None

    duplicate = policy.evaluate(event, _attention())
    assert duplicate.should_deliver is False
    assert duplicate.reason == "duplicate suppression window"
