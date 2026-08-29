from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from actions import ActionExecutor, ActionFeedbackSink, WindowsToastNotifyAdapter
from brain.reasoner import Feedback
from core.delivery import DeliveryOutbox, DeliveryRequest, DeliveryRouter
from integrations.qq_bridge.config import QQBridgeConfig

from .environment import load_runtime_environment
from .paths import default_state_dir


class _WindowsDeliverySink:
    def __init__(self) -> None:
        self.feedback = ActionFeedbackSink(
            ActionExecutor([WindowsToastNotifyAdapter(app_name="Hikari")])
        )

    def send(self, request: DeliveryRequest) -> None:
        self.feedback.deliver(
            Feedback(
                text=request.text,
                event_type="presence.delivery",
                importance=1.0,
            )
        )


def _state_dir(value: str | None) -> Path:
    if value:
        return Path(value).expanduser().resolve()
    return default_state_dir().expanduser().resolve()


def _outbox(root: Path) -> DeliveryOutbox:
    return DeliveryOutbox(root / "proactive_delivery.db")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hikari-deliver",
        description="触发或检查 Hikari 的主动 Presence 投递边界。",
    )
    parser.add_argument("--state-dir", default=None)
    subparsers = parser.add_subparsers(dest="command", required=True)

    send = subparsers.add_parser(
        "send",
        help="提交一个显式主动投递；QQ 收件人只能来自可信运行时配置。",
    )
    send.add_argument("channel", choices=("qq", "windows"))
    send.add_argument("delivery_id")
    send.add_argument("text")
    send.add_argument("--env-file", default=None)

    status = subparsers.add_parser("status", help="查看一个主动投递的持久化状态。")
    status.add_argument("delivery_id")

    retry = subparsers.add_parser(
        "retry-uncertain",
        help="显式允许重试一个结果不确定的投递；不会自动执行。",
    )
    retry.add_argument("delivery_id")
    return parser


def _print_record(record) -> None:
    print(f"delivery_id：{record.request.delivery_id}")
    print(f"channel：{record.request.channel}")
    print(f"recipient：{record.request.recipient}")
    print(f"state：{record.state}")
    print(f"attempts：{record.attempts}")
    if record.last_error:
        print(f"last_error：{record.last_error}")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = _state_dir(args.state_dir)
    outbox = _outbox(root)

    if args.command == "status":
        record = outbox.get(args.delivery_id)
        if record is None:
            print("Hikari 主动投递不存在。")
            return 1
        _print_record(record)
        return 0

    if args.command == "retry-uncertain":
        try:
            record = outbox.retry_uncertain(args.delivery_id)
        except (KeyError, ValueError) as exc:
            print(f"Hikari 主动投递无法重试：{exc}")
            return 2
        _print_record(record)
        return 0

    if args.command != "send":
        raise RuntimeError(f"unsupported command: {args.command}")

    try:
        if args.channel == "qq":
            runtime = load_runtime_environment(env_file=args.env_file)
            config = QQBridgeConfig.from_mapping(runtime.values, state_dir=root)
            if config.proactive_user_id is None:
                raise ValueError(
                    "HIKARI_QQ_PROACTIVE_USER_ID must be configured for proactive QQ delivery"
                )
            request = DeliveryRequest(
                delivery_id=args.delivery_id,
                channel="qq",
                recipient=config.proactive_user_id,
                text=args.text,
                source="presence.manual_gate",
            )
            record = DeliveryRouter(outbox).submit(request)
        else:
            request = DeliveryRequest(
                delivery_id=args.delivery_id,
                channel="windows",
                recipient="local",
                text=args.text,
                source="presence.manual_gate",
            )
            record = DeliveryRouter(
                outbox,
                sinks={"windows": _WindowsDeliverySink()},
            ).submit(request)
    except (TypeError, ValueError) as exc:
        print(f"Hikari 主动投递失败：{exc}")
        return 2
    except Exception as exc:
        print(f"Hikari 主动投递失败：{type(exc).__name__}: {exc}")
        return 3

    _print_record(record)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
