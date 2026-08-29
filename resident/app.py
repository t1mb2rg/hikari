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
from core.presence import ConsoleFeedbackSink, FeedbackSink, PresencePipeline
from core.runtime import ResidentPresenceRuntime
from events.sensors import GitSensor
from integrations.qq_bridge.config import QQBridgeConfig
from memory.store import MemoryStore
from personality import load_personality

from .environment import load_runtime_environment
from .unified import (
    QQBridgeProcessConfig,
    QQBridgeSupervisor,
    UnifiedResidentService,
    runtime_bool,
)


def feedback_sink(output: str) -> FeedbackSink:
    """Build one user-facing feedback channel without widening Presence authority."""

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


def build_runtime(
    repository: Path,
    memory_path: Path,
    *,
    interval: float = 2.0,
    output: str = "console",
    reasoner: Reasoner | None = None,
    memory: MemoryStore | None = None,
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
        help="主动反馈通道（默认：console）",
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
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    memory = MemoryStore(memory_path)
    runtime = build_runtime(
        repository,
        memory_path,
        interval=args.interval,
        output=args.output,
        reasoner=reasoner,
        memory=memory,
    )

    print(f"Hikari 正在观察：{repository}", flush=True)
    print(f"Hikari 主动反馈通道：{args.output}", flush=True)
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

        engine = ConversationEngine(
            provider,
            memory,
            context_collector=default_context_collector(include_desktop_activity=False),
            personality_profile=None,
            voice_profile=None,
            relationship_context=PRIMARY_REMOTE_RELATIONSHIP_CONTEXT,
            history_limit=12,
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
