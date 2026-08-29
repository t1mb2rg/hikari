from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path

from attention import AttentionPolicy
from brain.reasoner import Feedback
from core.delivery import DeliveryOutbox, DeliveryRouter
from core.presence import ConsoleFeedbackSink, PresencePipeline
from core.presence_policy import PresencePolicy, PresencePolicyConfig, PresencePolicyStore
from events.models import Event
from integrations.qq_bridge.config import QQBridgeConfig
from memory.store import MemoryStore

from .environment import load_runtime_environment
from .paths import default_state_dir
from .presence_delivery import RoutedPresenceDelivery, WindowsDeliverySink
from .unified import runtime_bool


class _GateReasoner:
    """Deterministic reasoner used only to prove the Presence policy plumbing."""

    def __init__(self, text: str) -> None:
        normalized = text.strip()
        if not normalized:
            raise ValueError("gate message must not be empty")
        self.text = normalized

    def reason(self, event: Event, decision) -> Feedback:
        return Feedback(self.text, event.event_type, decision.importance)


def _state_dir(value: str | None) -> Path:
    return (
        Path(value).expanduser().resolve()
        if value
        else default_state_dir().expanduser().resolve()
    )


def _parse_local_iso(value: str | None) -> datetime:
    if value is None:
        return datetime.now().astimezone()
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError as exc:
        raise ValueError("--local-iso must be ISO 8601") from exc
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed


def _event_context(
    local_time: datetime,
    *,
    foreground_title: str | None,
    schedule_current: bool,
) -> dict[str, object]:
    providers: dict[str, dict[str, object]] = {
        "time": {
            "local_iso": local_time.isoformat(),
            "hour": local_time.hour,
        },
        "input_activity": {"recent_input": True},
        "schedule": {
            "current": (
                [{"title": "M6-12 deterministic gate", "source": "manual_gate"}]
                if schedule_current
                else []
            )
        },
    }
    if foreground_title is None:
        providers["foreground"] = {"available": False}
    else:
        providers["foreground"] = {
            "available": True,
            "title": foreground_title,
        }
    return {
        "_hikari_context": {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "providers": providers,
        }
    }


def _build_gate_components(
    values: Mapping[str, str],
    *,
    state_dir: Path,
    policy_db: Path,
):
    config = PresencePolicyConfig.from_mapping(values, default_channel="windows")
    qq_recipient: str | None = None
    if config.channel == "qq":
        if not runtime_bool(values, "HIKARI_QQ_ENABLED", default=False):
            raise ValueError(
                "HIKARI_PRESENCE_CHANNEL=qq requires HIKARI_QQ_ENABLED=true"
            )
        qq_config = QQBridgeConfig.from_mapping(values, state_dir=state_dir)
        if qq_config.proactive_user_id is None:
            raise ValueError(
                "HIKARI_PRESENCE_CHANNEL=qq requires HIKARI_QQ_PROACTIVE_USER_ID"
            )
        qq_recipient = qq_config.proactive_user_id

    outbox = DeliveryOutbox(state_dir / "proactive_delivery.db")
    router = DeliveryRouter(
        outbox,
        sinks={"windows": WindowsDeliverySink()} if config.channel == "windows" else {},
    )
    policy = PresencePolicy(config, PresencePolicyStore(policy_db))
    delivery = RoutedPresenceDelivery(router, qq_recipient=qq_recipient)
    return config, policy, delivery, outbox


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hikari-presence",
        description=(
            "M6-12 deterministic Presence gate. It exercises the real policy and "
            "durable delivery boundary without asking the model to invent a test event."
        ),
    )
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--env-file", default=None)
    parser.add_argument(
        "--policy-db",
        default=None,
        help=(
            "Gate-only policy state DB. Defaults to <state-dir>/presence_gate_policy.db "
            "so production cooldown state is not modified."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    gate = subparsers.add_parser("gate", help="Evaluate one deterministic candidate and deliver only if allowed.")
    gate.add_argument("event_key", help="Stable semantic key used for duplicate suppression.")
    gate.add_argument("message", help="Exact test message used if policy allows delivery.")
    gate.add_argument("--importance", type=float, default=0.8)
    gate.add_argument("--local-iso", default=None)
    gate.add_argument("--foreground-title", default=None)
    gate.add_argument("--schedule-current", action="store_true")
    return parser


def _print_result(result, outbox: DeliveryOutbox) -> None:
    presence = result.presence_decision
    if presence is None:
        print("Presence decision：未进入（Attention 未放行）")
        return
    print(f"should_deliver：{'true' if presence.should_deliver else 'false'}")
    print(f"reason：{presence.reason}")
    print(f"channel：{presence.channel}")
    print(f"urgent：{'true' if presence.urgent else 'false'}")
    print(f"delivery_id：{presence.delivery_id}")
    if presence.user_state is not None:
        print(f"interruptibility：{presence.user_state.interruptibility}")
    record = outbox.get(presence.delivery_id)
    if record is not None:
        print(f"delivery_state：{record.state}")
        print(f"delivery_attempts：{record.attempts}")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = _state_dir(args.state_dir)
    policy_db = (
        Path(args.policy_db).expanduser().resolve()
        if args.policy_db
        else (root / "presence_gate_policy.db").resolve()
    )

    try:
        if not 0 <= args.importance <= 1:
            raise ValueError("--importance must be between 0 and 1")
        local_time = _parse_local_iso(args.local_iso)
        runtime = load_runtime_environment(env_file=args.env_file)
        config, policy, delivery, outbox = _build_gate_components(
            runtime.values,
            state_dir=root,
            policy_db=policy_db,
        )
        event = Event(
            event_type="presence.manual_gate",
            source="manual_gate",
            content=args.event_key,
            context=_event_context(
                local_time,
                foreground_title=args.foreground_title,
                schedule_current=bool(args.schedule_current),
            ),
            occurred_at=local_time.astimezone(timezone.utc),
        )
        pipeline = PresencePipeline(
            memory=MemoryStore(root / "presence_gate_memory.db"),
            attention=AttentionPolicy(
                threshold=0.0,
                event_importance={"presence.manual_gate": args.importance},
            ),
            reasoner=_GateReasoner(args.message),
            feedback_sink=ConsoleFeedbackSink(),
            presence_policy=policy,
            proactive_delivery_sink=delivery,
        )
        result = pipeline.handle(event)
    except (TypeError, ValueError) as exc:
        print(f"Hikari Presence Gate 失败：{exc}")
        return 2
    except Exception as exc:
        print(f"Hikari Presence Gate 失败：{type(exc).__name__}: {exc}")
        return 3

    print(f"Presence Gate channel config：{config.channel}")
    print(f"Presence Gate policy DB：{policy_db}")
    _print_result(result, outbox)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
