from __future__ import annotations

import argparse
import asyncio
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
import sys

from actions import ActionExecutor, ActionFeedbackSink, WindowsToastNotifyAdapter
from attention import AttentionPolicy
from awareness import (
    ChineseCalendarContextProvider,
    ContextCollector,
    ForegroundContextProvider,
    HostContextProvider,
    InputActivityContextProvider,
    TimeContextProvider,
)
from brain import ModelReasoner, Reasoner, SimpleReasoner
from brain.providers import OpenAICompatibleProvider
from conversation.cli import build_chat_provider, default_context_collector
from conversation.engine import ConversationEngine, INTERACTIVE_SYSTEM_INSTRUCTIONS
from conversation.receipts import ConversationReceiptStore
from conversation.remote import (
    DEFAULT_CONVERSATION_HOST,
    DEFAULT_CONVERSATION_PORT,
    PRIMARY_REMOTE_RELATIONSHIP_CONTEXT,
    ConversationRequestProcessor,
    ConversationWebSocketHost,
    _is_loopback_host,
)
from core.delivery import DeliveryOutbox, DeliveryRouter
from core.presence import (
    ConsoleFeedbackSink,
    FeedbackSink,
    PresencePipeline,
    ProactiveDeliverySink,
)
from core.presence_policy import PresencePolicy, PresencePolicyConfig, PresencePolicyStore
from core.runtime import ResidentPresenceRuntime
from events.sensors import GitSensor
from integrations.qq_bridge.config import QQBridgeConfig
from memory.store import MemoryStore
from personality import load_personality
from user_model import build_user_model_runtime

from .environment import load_runtime_environment
from .presence_delivery import RoutedPresenceDelivery, WindowsDeliverySink
from .unified import (
    QQBridgeProcessConfig,
    QQBridgeSupervisor,
    UnifiedResidentService,
    runtime_bool,
)


def feedback_sink(output: str) -> FeedbackSink:
    """Build one legacy/direct user-facing feedback channel."""

    if output == "console":
        return ConsoleFeedbackSink()
    if output == "windows":
        return ActionFeedbackSink(
            ActionExecutor([WindowsToastNotifyAdapter(app_name="Hikari")])
        )
    raise ValueError(f"unsupported feedback output: {output}")


def build_reasoner(
    mode: str,
    *,
    environment: Mapping[str, str] | None = None,
) -> Reasoner:
    """Build the selected cognition path from runtime-only configuration."""

    if mode == "simple":
        return SimpleReasoner()
    if mode != "model":
        raise ValueError(f"unsupported reasoner mode: {mode}")

    env = os.environ if environment is None else environment
    base_url = env.get("HIKARI_MODEL_BASE_URL", "").strip()
    model = env.get("HIKARI_MODEL_NAME", "").strip()
    api_key = env.get("HIKARI_MODEL_API_KEY")

    missing: list[str] = []
    if not base_url:
        missing.append("HIKARI_MODEL_BASE_URL")
    if not model:
        missing.append("HIKARI_MODEL_NAME")
    if missing:
        raise ValueError(
            "model reasoner requires runtime environment variable(s): "
            + ", ".join(missing)
        )

    provider = OpenAICompatibleProvider(
        base_url=base_url,
        model=model,
        api_key=api_key,
    )
    return ModelReasoner(provider)


def build_presence_components(
    values: Mapping[str, str],
    *,
    state_dir: Path,
    qq_enabled: bool,
    output: str,
) -> tuple[PresencePolicyConfig | None, PresencePolicy | None, ProactiveDeliverySink | None]:
    """Assemble governed Presence only for production-style or explicit use.

    `resident.app --output console` remains a lightweight developer path unless a
    Presence channel is explicitly configured. The detached Windows host uses
    `--output windows`, so the governed M6 path is active there by default.
    """

    explicit_channel = values.get("HIKARI_PRESENCE_CHANNEL", "").strip()
    if output == "console" and not explicit_channel:
        return None, None, None

    config = PresencePolicyConfig.from_mapping(values, default_channel="windows")
    qq_recipient: str | None = None
    if config.channel == "qq":
        if not qq_enabled:
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
    sinks = {"windows": WindowsDeliverySink()} if config.channel == "windows" else {}
    router = DeliveryRouter(outbox, sinks=sinks)
    policy = PresencePolicy(
        config,
        PresencePolicyStore(state_dir / "presence_policy.db"),
    )
    delivery = RoutedPresenceDelivery(router, qq_recipient=qq_recipient)
    return config, policy, delivery


def build_runtime(
    repository: Path,
    memory_path: Path,
    *,
    interval: float = 2.0,
    output: str = "console",
    reasoner: Reasoner | None = None,
    memory: MemoryStore | None = None,
    presence_policy: PresencePolicy | None = None,
    proactive_delivery_sink: ProactiveDeliverySink | None = None,
) -> ResidentPresenceRuntime:
    """Build Hikari's concrete Git-backed resident Presence runtime."""

    pipeline = PresencePipeline(
        memory=memory or MemoryStore(memory_path),
        attention=AttentionPolicy(
            threshold=0.7,
            event_importance={"git.commit": 0.8},
        ),
        reasoner=reasoner or SimpleReasoner(),
        feedback_sink=feedback_sink(output),
        context_collector=ContextCollector(
            [
                TimeContextProvider(),
                ChineseCalendarContextProvider(),
                HostContextProvider(),
                InputActivityContextProvider(),
                ForegroundContextProvider(),
            ]
        ),
        personality_profile=load_personality(),
        presence_policy=presence_policy,
        proactive_delivery_sink=proactive_delivery_sink,
    )
    return ResidentPresenceRuntime(
        [GitSensor(repository)],
        pipeline,
        poll_interval=max(0.1, interval),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="运行 Hikari 的常驻 Presence / Conversation 核心。",
    )
    parser.add_argument(
        "repository",
        nargs="?",
        default=".",
        help="要观察的 Git 仓库（默认：当前目录）",
    )
    parser.add_argument(
        "--db",
        default=".hikari/memory.db",
        help="SQLite memory 路径（默认：.hikari/memory.db）",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="轮询间隔秒数（默认：2.0）",
    )
    parser.add_argument(
        "--output",
        choices=("console", "windows"),
        default="console",
        help="旧版直接反馈通道；Windows host 会启用 governed Presence",
    )
    parser.add_argument(
        "--reasoner",
        choices=("simple", "model"),
        default="simple",
        help="认知模式；model 使用 HIKARI_MODEL_* 运行时配置",
    )
    parser.add_argument(
        "--env-file",
        default=None,
        help="可选 dotenv 文件；进程环境变量优先于文件中的同名值",
    )
    parser.add_argument(
        "--conversation",
        action=argparse.BooleanOptionalAction,
        default=None,
        help=(
            "启用统一 Conversation Host；默认读取 HIKARI_CONVERSATION_ENABLED，"
            "未配置时启用"
        ),
    )
    parser.add_argument(
        "--qq",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="由 resident 托管 Hikari QQ Bridge；默认读取 HIKARI_QQ_ENABLED，未配置时关闭",
    )
    return parser


def _runtime_value(values: Mapping[str, str], name: str, default: str) -> str:
    value = values.get(name, default).strip()
    return value or default


def _conversation_port(values: Mapping[str, str]) -> int:
    text = _runtime_value(
        values,
        "HIKARI_CONVERSATION_PORT",
        str(DEFAULT_CONVERSATION_PORT),
    )
    try:
        port = int(text)
    except ValueError as exc:
        raise ValueError("HIKARI_CONVERSATION_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError("HIKARI_CONVERSATION_PORT must be between 1 and 65535")
    return port


def _quiet_hours_description(config: PresencePolicyConfig) -> str:
    if not config.quiet_hours_enabled:
        return "关闭"
    return (
        f"{config.quiet_start.hour:02d}:{config.quiet_start.minute:02d}-"
        f"{config.quiet_end.hour:02d}:{config.quiet_end.minute:02d}"
    )


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)

    repository = Path(args.repository).resolve()
    memory_path = Path(args.db).resolve()
    try:
        runtime_environment = load_runtime_environment(env_file=args.env_file)
        values = runtime_environment.values
        reasoner = build_reasoner(
            args.reasoner,
            environment=values,
        )
        conversation_enabled = (
            bool(args.conversation)
            if args.conversation is not None
            else runtime_bool(
                values,
                "HIKARI_CONVERSATION_ENABLED",
                default=True,
            )
        )
        qq_enabled = (
            bool(args.qq)
            if args.qq is not None
            else runtime_bool(values, "HIKARI_QQ_ENABLED", default=False)
        )
        if qq_enabled and not conversation_enabled:
            raise ValueError("HIKARI_QQ_ENABLED requires Conversation Host to be enabled")
        presence_config, presence_policy, proactive_delivery_sink = build_presence_components(
            values,
            state_dir=memory_path.parent,
            qq_enabled=qq_enabled,
            output=args.output,
        )
    except (TypeError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc

    memory = MemoryStore(memory_path)
    runtime = build_runtime(
        repository,
        memory_path,
        interval=args.interval,
        output=args.output,
        reasoner=reasoner,
        memory=memory,
        presence_policy=presence_policy,
        proactive_delivery_sink=proactive_delivery_sink,
    )

    print(f"Hikari 正在观察：{repository}", flush=True)
    if presence_config is None:
        print(f"Hikari 主动反馈通道：{args.output}", flush=True)
    else:
        print(f"Hikari Presence 通道：{presence_config.channel}", flush=True)
        print(
            f"Hikari Presence 安静时段：{_quiet_hours_description(presence_config)}",
            flush=True,
        )
        print(
            "Hikari Presence 抑制："
            f"cooldown={presence_config.cooldown_seconds:g}s, "
            f"duplicate={presence_config.duplicate_window_seconds:g}s, "
            f"urgent>={presence_config.urgent_threshold:g}",
            flush=True,
        )
    print(f"Hikari 认知模式：{args.reasoner}", flush=True)
    if runtime_environment.env_file is not None:
        print(f"Hikari 环境文件：{runtime_environment.env_file}", flush=True)

    if not conversation_enabled:
        print("Hikari Conversation Host：关闭", flush=True)
        runtime.run_forever()
        return

    try:
        provider = build_chat_provider(values)
        bind_host = _runtime_value(
            values,
            "HIKARI_CONVERSATION_HOST",
            DEFAULT_CONVERSATION_HOST,
        )
        if not _is_loopback_host(bind_host):
            raise ValueError(
                "M6-09 resident Conversation Host is loopback-only; remote deployment requires secure WSS ingress"
            )
        bind_port = _conversation_port(values)
        shared_secret = values.get("HIKARI_CONVERSATION_SHARED_SECRET")
        shared_secret = shared_secret.strip() if shared_secret and shared_secret.strip() else None
        receipt_path = (memory_path.parent / "conversation_receipts.db").resolve()
        user_model_path = (memory_path.parent / "user_model.db").resolve()
        user_model_service, user_fact_extractor = build_user_model_runtime(
            provider,
            user_model_path,
        )

        engine = ConversationEngine(
            provider,
            memory,
            context_collector=default_context_collector(include_desktop_activity=False),
            personality_profile=None,
            voice_profile=None,
            relationship_context=PRIMARY_REMOTE_RELATIONSHIP_CONTEXT,
            history_limit=12,
            user_model_service=user_model_service,
            user_fact_extractor=user_fact_extractor,
            system_instructions=INTERACTIVE_SYSTEM_INSTRUCTIONS,
        )
        conversation_host = ConversationWebSocketHost(
            ConversationRequestProcessor(
                engine,
                ConversationReceiptStore(receipt_path),
            ),
            shared_secret=shared_secret,
        )

        qq_supervisor: QQBridgeSupervisor | None = None
        if qq_enabled:
            state_dir = memory_path.parent
            QQBridgeConfig.from_mapping(values, state_dir=state_dir)
            child_environment = dict(values)
            child_environment["HIKARI_CONVERSATION_URL"] = (
                f"ws://{bind_host}:{bind_port}"
            )
            qq_supervisor = QQBridgeSupervisor(
                QQBridgeProcessConfig(
                    repository=repository,
                    log_path=state_dir / "qq_bridge.log",
                    environment=child_environment,
                    env_file=runtime_environment.env_file,
                    python_executable=sys.executable,
                )
            )
    except (TypeError, ValueError) as exc:
        raise SystemExit(f"Hikari unified resident 启动失败：{exc}") from exc

    service = UnifiedResidentService(
        runtime,
        conversation_host,
        bind_host=bind_host,
        bind_port=bind_port,
        qq_supervisor=qq_supervisor,
    )
    print(f"Hikari Conversation Host：ws://{bind_host}:{bind_port}", flush=True)
    print(f"Hikari 对话模型：{getattr(provider, 'model', type(provider).__name__)}", flush=True)
    print(f"Hikari QQ Bridge：{'resident 托管' if qq_enabled else '关闭'}", flush=True)
    try:
        asyncio.run(service.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
