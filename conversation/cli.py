from __future__ import annotations

from resident.console import configure_utf8_output

import argparse
import getpass
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from copy import copy
import logging
from pathlib import Path
import threading
from uuid import uuid4

from awareness import (
    ChineseCalendarContextProvider,
    ContextCollector,
    ForegroundContextProvider,
    HostContextProvider,
    InputActivityContextProvider,
    TimeContextProvider,
)
from brain.model_reasoner import ChatProvider
from brain.providers import OpenAICompatibleProvider
from brain.providers.observed import ObservedChatProvider
from memory.store import MemoryStore
from personality import load_personality, load_voice
from resident.environment import load_runtime_environment, source_checkout_root
from resident.paths import default_state_dir
from user_model import build_user_model_runtime

from .bootstrap import build_private_task_router

from .engine import (
    ConversationEngine,
    INTERACTIVE_SYSTEM_INSTRUCTIONS,
    LEGACY_INTERACTIVE_SYSTEM_INSTRUCTIONS,
    THIN_HIKARI_SYSTEM_INSTRUCTIONS,
)
from .jarvis import JARVIS_SYSTEM_INSTRUCTIONS
from .jarvis_openjarvis import (
    HIKARI_OPENJARVIS_CHINESE_OUTPUT_SYSTEM_INSTRUCTIONS,
    JARVIS_PRODUCTION_SYSTEM_INSTRUCTIONS,
    OPENJARVIS_CHINESE_OUTPUT_SYSTEM_INSTRUCTIONS,
    OPENJARVIS_SYSTEM_INSTRUCTIONS,
)
from .models import UserTurn
from .whiteboard import (
    WHITEBOARD_1_RELATIONSHIP_CONTEXT,
    WHITEBOARD_2_RELEVANT_CONTEXT,
    WHITEBOARD_2C_RELATIONAL_STANCE,
    WHITEBOARD_HIKARI_SYSTEM_INSTRUCTIONS,
    WhiteboardConversationEngine,
)


DEFAULT_CHAT_TEMPERATURE = 0.65
USER_MODEL_DRAIN_INTERVAL_SECONDS = 0.5
USER_MODEL_DRAIN_TIMEOUT_SECONDS = 8.0
logger = logging.getLogger(__name__)
EXIT_COMMANDS = {"/exit", "/quit"}
PASTE_COMMAND = "/paste"
PASTE_SEND_COMMAND = "/send"
PASTE_CANCEL_COMMAND = "/cancel"
PROMPT_PROFILES = (
    "production",
    "whiteboard",
    "whiteboard0",
    "whiteboard1",
    "whiteboard2",
    "whiteboard2b",
    "whiteboard2c",
    "jarvis",
    "jarvis-production",
    "jarvis-openjarvis",
    "jarvis-openjarvis-zh",
    "hikari-openjarvis-zh",
    "thin",
    "legacy",
)


def build_chat_provider(environment: Mapping[str, str]) -> ChatProvider:
    base_url = environment.get("HIKARI_MODEL_BASE_URL", "").strip()
    model = environment.get("HIKARI_MODEL_NAME", "").strip()
    api_key = environment.get("HIKARI_MODEL_API_KEY")

    missing: list[str] = []
    if not base_url:
        missing.append("HIKARI_MODEL_BASE_URL")
    if not model:
        missing.append("HIKARI_MODEL_NAME")
    if missing:
        raise ValueError(
            "Hikari chat requires runtime environment variable(s): "
            + ", ".join(missing)
        )

    temperature_text = environment.get(
        "HIKARI_CHAT_TEMPERATURE",
        str(DEFAULT_CHAT_TEMPERATURE),
    ).strip()
    try:
        temperature = float(temperature_text)
    except ValueError as exc:
        raise ValueError("HIKARI_CHAT_TEMPERATURE must be numeric") from exc
    if not 0.0 <= temperature <= 2.0:
        raise ValueError("HIKARI_CHAT_TEMPERATURE must be between 0.0 and 2.0")

    return OpenAICompatibleProvider(
        base_url=base_url,
        model=model,
        api_key=api_key,
        temperature=temperature,
    )


def _entrypoint_paths(args, values: Mapping[str, str]) -> tuple[Path, Path, Path]:
    """Keep conversation memory, task state and the selected checkout explicit."""
    repository = Path(args.repository).expanduser().resolve() if args.repository else (source_checkout_root() or Path.cwd()).resolve()
    state_dir = (
        Path(args.state_dir).expanduser().resolve() if args.state_dir
        else Path(args.db).expanduser().resolve().parent if args.db
        else default_state_dir(values).resolve()
    )
    memory_path = Path(args.db).expanduser().resolve() if args.db else state_dir / "memory.db"
    return repository, state_dir, memory_path


def _entrypoint_engineering_bridge(*, repository: Path, state_dir: Path, enabled: bool):
    if not enabled:
        return None
    from engineering.bindings import EngineeringConversationBindingStore
    from engineering.session import EngineeringSessionStore
    from .engineering_bridge import ConversationEngineeringBridge

    # Standalone entrypoints use an independently managed Worker; intake never
    # launches another worker or adds authority because one is absent.
    return ConversationEngineeringBridge(
        EngineeringSessionStore(state_dir / "engineering"),
        EngineeringConversationBindingStore(state_dir / "engineering_bindings.json"),
        repository=repository,
    )


def _entrypoint_enabled(values: Mapping[str, str], name: str) -> bool:
    value = values.get(name, "false").strip().casefold() or "false"
    if value not in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
        raise ValueError(f"{name} must be true or false")
    return value in {"1", "true", "yes", "on"}


def _bounded_user_model_worker(worker):
    """Keep extraction shutdown bounded without changing conversation timeouts."""
    if worker is None:
        return None
    extractor = getattr(worker, "extractor", None)
    provider = getattr(extractor, "provider", None)
    if provider is None:
        return worker
    bounded = copy(worker)
    bounded.extractor = copy(extractor)
    bounded_provider = copy(provider)
    if isinstance(provider, ObservedChatProvider):
        bounded_provider.provider = copy(provider.provider)
        transport = bounded_provider.provider
    else:
        transport = bounded_provider
    if isinstance(transport, OpenAICompatibleProvider):
        transport.timeout = min(transport.timeout, USER_MODEL_DRAIN_TIMEOUT_SECONDS)
    bounded.extractor.provider = bounded_provider
    return bounded


@contextmanager
def _cli_user_model_drain(router):
    worker = _bounded_user_model_worker(getattr(router, "user_model_worker", None))
    if worker is None:
        yield
        return
    stop = threading.Event()

    def drain():
        # Waiting is interruptible, so an idle CLI exits immediately. Never drain
        # the backlog on shutdown; only finish the one bounded job already owned.
        while not stop.wait(USER_MODEL_DRAIN_INTERVAL_SECONDS):
            try:
                worker.drain_once()
            except Exception as exc:
                logger.warning("User Model background drain degraded: %s", type(exc).__name__)

    thread = threading.Thread(target=drain, name="hikari-cli-user-model", daemon=False)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()


def default_context_collector(*, include_desktop_activity: bool = False) -> ContextCollector:
    providers = [
        TimeContextProvider(),
        ChineseCalendarContextProvider(),
        HostContextProvider(),
    ]
    if include_desktop_activity:
        providers.extend(
            [
                InputActivityContextProvider(),
                ForegroundContextProvider(),
            ]
        )
    return ContextCollector(providers)


def collect_multiline_turn(
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> str | None:
    """Collect a pasted multiline block and return it as one user turn."""

    output_fn("多行粘贴模式：粘贴完成后单独输入 /send 发送，/cancel 取消。")
    lines: list[str] = []

    while True:
        line = input_fn("│ ")
        command = line.strip().lower()

        if command == PASTE_SEND_COMMAND:
            text = "\n".join(lines)
            if not text.strip():
                output_fn("没有可发送的内容，已退出多行粘贴模式。")
                return None
            return text

        if command == PASTE_CANCEL_COMMAND:
            output_fn("已取消多行粘贴。")
            return None

        lines.append(line)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hikari-chat",
        description="通过 Hikari 的持久直接对话核心进行交互。",
    )
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--db", default=None)
    parser.add_argument("--repository", default=None)
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--channel", default="cli")
    parser.add_argument("--conversation", default="local")
    parser.add_argument("--history-limit", type=int, default=12)
    parser.add_argument(
        "--prompt-profile",
        choices=PROMPT_PROFILES,
        default="jarvis-production",
        help=(
            "默认使用 jarvis-production（OpenJarvis 人格 + 中文输出 + Hikari factual boundary）；"
            "production 保留当前 grounded Hikari 基线用于回退；"
            "whiteboard/whiteboard0 是 Prompt + 最近真实对话的 Whiteboard 0；"
            "whiteboard1 只额外加入一段自然语言的长期关系背景；"
            "whiteboard2 不加关系背景，只把人工确认的当前相关事实作为 system 背景；"
            "whiteboard2b 使用同一批相关事实，但把它们放到当前消息附近；"
            "whiteboard2c 在 2B 基础上只增加一小段 relational stance 行为约束；"
            "jarvis 是我们从零写的极简 Jarvis 人格对照；"
            "jarvis-openjarvis 原样使用 OpenJarvis 的 Apache-2.0 Jarvis persona；"
            "jarvis-openjarvis-zh 保留同一份英文 persona，只额外要求输出简体中文；"
            "hikari-openjarvis-zh 只把同一份 persona 的身份名 Jarvis 换成 Hikari，并保持中文输出；"
            "thin 与 production 等价并保留给盲测脚本；"
            "legacy 显式启用旧版完整 voice/personality steering。"
        ),
    )
    parser.add_argument(
        "--desktop-context",
        action="store_true",
        help="显式允许 grounded 直接聊天读取当前前台窗口和输入活跃度。Whiteboard/Jarvis 对照会忽略该上下文。",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    configure_utf8_output()
    args = build_parser().parse_args(argv)
    jarvis_profiles = {
        "jarvis",
        "jarvis-production",
        "jarvis-openjarvis",
        "jarvis-openjarvis-zh",
    }
    identity_swap_profile = args.prompt_profile == "hikari-openjarvis-zh"
    assistant_name = "Jarvis" if args.prompt_profile in jarvis_profiles else "Hikari"

    try:
        runtime_environment = load_runtime_environment(env_file=args.env_file)
        values = runtime_environment.values
        repository, state_dir, memory_path = _entrypoint_paths(args, values)
        provider = ObservedChatProvider(build_chat_provider(values), state_dir,
                                        model=values.get("HIKARI_MODEL_NAME", "configured"))
        engineering_enabled = _entrypoint_enabled(values, "HIKARI_ENGINEERING_ENABLED")
        legacy_prompt = args.prompt_profile == "legacy"
        jarvis_prompt = args.prompt_profile in jarvis_profiles
        jarvis_production = args.prompt_profile == "jarvis-production"
        openjarvis_prompt = args.prompt_profile in {
            "jarvis-openjarvis",
            "jarvis-openjarvis-zh",
            "hikari-openjarvis-zh",
        }
        openjarvis_chinese_output = args.prompt_profile == "jarvis-openjarvis-zh"
        whiteboard_prompt = args.prompt_profile in {
            "whiteboard",
            "whiteboard0",
            "whiteboard1",
            "whiteboard2",
            "whiteboard2b",
            "whiteboard2c",
            "jarvis",
            "jarvis-production",
            "jarvis-openjarvis",
            "jarvis-openjarvis-zh",
            "hikari-openjarvis-zh",
        }
        whiteboard_relationship = args.prompt_profile == "whiteboard1"
        whiteboard_relational_stance = args.prompt_profile == "whiteboard2c"
        whiteboard_relevant = args.prompt_profile in {
            "whiteboard2",
            "whiteboard2b",
            "whiteboard2c",
        }
        whiteboard_relevant_placement = (
            "current_turn"
            if args.prompt_profile in {"whiteboard2b", "whiteboard2c"}
            else "system"
        )
        user_model_service, user_fact_extractor = build_user_model_runtime(
            provider,
            state_dir / "user_model.db",
        )

        shared_kwargs = {
            "history_limit": args.history_limit,
            "user_model_service": user_model_service,
            "user_fact_extractor": user_fact_extractor,
        }
        relationship_context = {
            "kind": "primary_local_user",
            "basis": "trusted_runtime_binding",
            "memory_claim": "continuity_without_implied_episode_recall",
            "continuity": (
                "This local CLI is an explicit trusted conversation with "
                "Hikari's primary local user. This is the person who has been "
                "building, testing, and talking with Hikari across the current "
                "development process. Specific personal facts remain unknown "
                "unless durable memory supplies them. This binding establishes "
                "the relationship but does not mean exact prior conversations "
                "or development episodes are independently remembered."
            ),
        }

        if whiteboard_prompt:
            if identity_swap_profile:
                comparison_system_instructions = (
                    HIKARI_OPENJARVIS_CHINESE_OUTPUT_SYSTEM_INSTRUCTIONS
                )
            elif jarvis_production:
                comparison_system_instructions = JARVIS_PRODUCTION_SYSTEM_INSTRUCTIONS
            elif openjarvis_chinese_output:
                comparison_system_instructions = (
                    OPENJARVIS_CHINESE_OUTPUT_SYSTEM_INSTRUCTIONS
                )
            elif openjarvis_prompt:
                comparison_system_instructions = OPENJARVIS_SYSTEM_INSTRUCTIONS
            elif jarvis_prompt:
                comparison_system_instructions = JARVIS_SYSTEM_INSTRUCTIONS
            else:
                comparison_system_instructions = WHITEBOARD_HIKARI_SYSTEM_INSTRUCTIONS

            engine = WhiteboardConversationEngine(
                provider,
                MemoryStore(memory_path),
                context_collector=None,
                personality_profile=None,
                voice_profile=None,
                relationship_context=relationship_context,
                system_instructions=comparison_system_instructions,
                relationship_context_text=(
                    WHITEBOARD_1_RELATIONSHIP_CONTEXT
                    if whiteboard_relationship
                    else None
                ),
                relational_stance_text=(
                    WHITEBOARD_2C_RELATIONAL_STANCE
                    if whiteboard_relational_stance
                    else None
                ),
                relevant_context_text=(
                    WHITEBOARD_2_RELEVANT_CONTEXT
                    if whiteboard_relevant
                    else None
                ),
                relevant_context_placement=whiteboard_relevant_placement,
                **shared_kwargs,
            )
        else:
            engine = ConversationEngine(
                provider,
                MemoryStore(memory_path),
                context_collector=default_context_collector(
                    include_desktop_activity=args.desktop_context,
                ),
                personality_profile=load_personality() if legacy_prompt else None,
                voice_profile=load_voice() if legacy_prompt else None,
                relationship_context=relationship_context,
                system_instructions=(
                    LEGACY_INTERACTIVE_SYSTEM_INSTRUCTIONS
                    if legacy_prompt
                    else (
                        THIN_HIKARI_SYSTEM_INSTRUCTIONS
                        if args.prompt_profile == "thin"
                        else INTERACTIVE_SYSTEM_INSTRUCTIONS
                    )
                ),
                **shared_kwargs,
            )
        router = build_private_task_router(
            engine, repository=repository, state_dir=state_dir, values=dict(values),
            engineering_bridge=_entrypoint_engineering_bridge(
                repository=repository, state_dir=state_dir, enabled=engineering_enabled,
            ),
        )
    except ValueError as exc:
        print(f"{assistant_name} 对话启动失败：{exc}")
        return 2

    print(f"{assistant_name} 对话已连接。输入 /exit 退出，/paste 粘贴多行内容。")
    if runtime_environment.env_file is not None:
        print(f"环境文件：{runtime_environment.env_file}")
    print(f"模型：{getattr(provider, 'model', type(provider).__name__)}")
    print(f"Prompt：{args.prompt_profile}")
    print(f"对话记忆：{memory_path}")

    with _cli_user_model_drain(router):
        return _chat_loop(engine, router, args, assistant_name)


def _chat_loop(engine, router, args, assistant_name) -> int:
    while True:
        try:
            text = input("你> ")
        except (EOFError, KeyboardInterrupt):
            print(f"\n{assistant_name} 对话已断开。")
            return 0

        command = text.strip().lower()
        if command in EXIT_COMMANDS:
            print(f"{assistant_name} 对话已断开。")
            return 0
        if command == PASTE_COMMAND:
            try:
                text = collect_multiline_turn(input_fn=input)
            except (EOFError, KeyboardInterrupt):
                print(f"\n{assistant_name} 对话已断开。")
                return 0
            if text is None:
                continue
        if not text.strip():
            continue

        try:
            reply = router.respond(
                engine,
                UserTurn(
                    channel=args.channel,
                    conversation_id=args.conversation,
                    text=text,
                    actor_id="local:" + getpass.getuser(),
                ),
                source_ref="cli:" + uuid4().hex,
            )
        except Exception as exc:
            print(f"{assistant_name}> 对话处理失败：{exc}")
            continue
        print(f"{assistant_name}> {reply.text}")


if __name__ == "__main__":
    raise SystemExit(main())
