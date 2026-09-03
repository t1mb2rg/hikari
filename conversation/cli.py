from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

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
from memory.store import MemoryStore
from personality import load_personality, load_voice
from resident.environment import load_runtime_environment
from resident.paths import default_state_dir
from user_model import build_user_model_runtime

from .engine import (
    ConversationEngine,
    INTERACTIVE_SYSTEM_INSTRUCTIONS,
    LEGACY_INTERACTIVE_SYSTEM_INSTRUCTIONS,
    THIN_HIKARI_SYSTEM_INSTRUCTIONS,
)
from .jarvis import JARVIS_SYSTEM_INSTRUCTIONS
from .models import UserTurn
from .whiteboard import (
    WHITEBOARD_1_RELATIONSHIP_CONTEXT,
    WHITEBOARD_2_RELEVANT_CONTEXT,
    WHITEBOARD_2C_RELATIONAL_STANCE,
    WHITEBOARD_HIKARI_SYSTEM_INSTRUCTIONS,
    WhiteboardConversationEngine,
)


DEFAULT_CHAT_TEMPERATURE = 0.65
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
    parser.add_argument("--channel", default="cli")
    parser.add_argument("--conversation", default="local")
    parser.add_argument("--history-limit", type=int, default=12)
    parser.add_argument(
        "--prompt-profile",
        choices=PROMPT_PROFILES,
        default="production",
        help=(
            "production 使用当前 grounded Hikari 基线；"
            "whiteboard/whiteboard0 是 Prompt + 最近真实对话的 Whiteboard 0；"
            "whiteboard1 只额外加入一段自然语言的长期关系背景；"
            "whiteboard2 不加关系背景，只把人工确认的当前相关事实作为 system 背景；"
            "whiteboard2b 使用同一批相关事实，但把它们放到当前消息附近；"
            "whiteboard2c 在 2B 基础上只增加一小段 relational stance 行为约束；"
            "jarvis 是独立的极简 Jarvis 人格对照，只看最近真实对话和当前消息；"
            "thin 与 production 等价并保留给盲测脚本；"
            "legacy 显式启用旧版完整 voice/personality steering。"
        ),
    )
    parser.add_argument(
        "--desktop-context",
        action="store_true",
        help="显式允许 grounded 直接聊天读取当前前台窗口和输入活跃度。Whiteboard/Jarvis 会忽略该上下文。",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    assistant_name = "Jarvis" if args.prompt_profile == "jarvis" else "Hikari"

    try:
        runtime_environment = load_runtime_environment(env_file=args.env_file)
        provider = build_chat_provider(runtime_environment.values)
        memory_path = (
            Path(args.db).expanduser().resolve()
            if args.db
            else (default_state_dir() / "memory.db").resolve()
        )
        legacy_prompt = args.prompt_profile == "legacy"
        jarvis_prompt = args.prompt_profile == "jarvis"
        whiteboard_prompt = args.prompt_profile in {
            "whiteboard",
            "whiteboard0",
            "whiteboard1",
            "whiteboard2",
            "whiteboard2b",
            "whiteboard2c",
            "jarvis",
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
            memory_path.parent / "user_model.db",
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
            engine = WhiteboardConversationEngine(
                provider,
                MemoryStore(memory_path),
                context_collector=None,
                personality_profile=None,
                voice_profile=None,
                relationship_context=relationship_context,
                system_instructions=(
                    JARVIS_SYSTEM_INSTRUCTIONS
                    if jarvis_prompt
                    else WHITEBOARD_HIKARI_SYSTEM_INSTRUCTIONS
                ),
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
    except ValueError as exc:
        print(f"{assistant_name} 对话启动失败：{exc}")
        return 2

    print(f"{assistant_name} 对话已连接。输入 /exit 退出，/paste 粘贴多行内容。")
    if runtime_environment.env_file is not None:
        print(f"环境文件：{runtime_environment.env_file}")
    print(f"模型：{getattr(provider, 'model', type(provider).__name__)}")
    print(f"Prompt：{args.prompt_profile}")
    print(f"对话记忆：{memory_path}")

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
                text = collect_multiline_turn()
            except (EOFError, KeyboardInterrupt):
                print(f"\n{assistant_name} 对话已断开。")
                return 0
            if text is None:
                continue
        if not text.strip():
            continue

        try:
            reply = engine.respond(
                UserTurn(
                    channel=args.channel,
                    conversation_id=args.conversation,
                    text=text,
                )
            )
        except Exception as exc:
            print(f"{assistant_name}> 对话处理失败：{exc}")
            continue
        print(f"{assistant_name}> {reply.text}")


if __name__ == "__main__":
    raise SystemExit(main())
