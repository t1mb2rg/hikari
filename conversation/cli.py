from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
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
from resident.windows_host import default_state_dir

from .engine import ConversationEngine
from .models import UserTurn


DEFAULT_CHAT_TEMPERATURE = 0.65


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
        "--desktop-context",
        action="store_true",
        help="显式允许直接聊天读取当前前台窗口和输入活跃度。默认关闭。",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        runtime_environment = load_runtime_environment(env_file=args.env_file)
        provider = build_chat_provider(runtime_environment.values)
        memory_path = (
            Path(args.db).expanduser().resolve()
            if args.db
            else (default_state_dir() / "memory.db").resolve()
        )
        engine = ConversationEngine(
            provider,
            MemoryStore(memory_path),
            context_collector=default_context_collector(
                include_desktop_activity=args.desktop_context,
            ),
            personality_profile=load_personality(),
            voice_profile=load_voice(),
            relationship_context={
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
            },
            history_limit=args.history_limit,
        )
    except ValueError as exc:
        print(f"Hikari 对话启动失败：{exc}")
        return 2

    print("Hikari 对话已连接。输入 /exit 退出。")
    if runtime_environment.env_file is not None:
        print(f"环境文件：{runtime_environment.env_file}")
    print(f"对话记忆：{memory_path}")

    while True:
        try:
            text = input("你> ")
        except (EOFError, KeyboardInterrupt):
            print("\nHikari 对话已断开。")
            return 0

        if text.strip().lower() in {"/exit", "/quit"}:
            print("Hikari 对话已断开。")
            return 0
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
            print(f"Hikari> 对话处理失败：{exc}")
            continue
        print(f"Hikari> {reply.text}")


if __name__ == "__main__":
    raise SystemExit(main())
