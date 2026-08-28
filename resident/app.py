from __future__ import annotations

import argparse
import os
from collections.abc import Mapping, Sequence
from pathlib import Path

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
from core.presence import ConsoleFeedbackSink, FeedbackSink, PresencePipeline
from core.runtime import ResidentPresenceRuntime
from events.sensors import GitSensor
from memory.store import MemoryStore
from personality import load_personality


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
) -> ResidentPresenceRuntime:
    """Build Hikari's concrete Git-backed resident Presence runtime."""

    pipeline = PresencePipeline(
        memory=MemoryStore(memory_path),
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
        description="运行 Hikari 的常驻主动 Presence 循环。",
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
        help="认知模式；model 使用 HIKARI_MODEL_* 运行时环境变量",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = build_parser().parse_args(argv)

    repository = Path(args.repository).resolve()
    memory_path = Path(args.db).resolve()
    try:
        reasoner = build_reasoner(args.reasoner)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    runtime = build_runtime(
        repository,
        memory_path,
        interval=args.interval,
        output=args.output,
        reasoner=reasoner,
    )

    print(f"Hikari 正在观察：{repository}", flush=True)
    print(f"Hikari 主动反馈通道：{args.output}", flush=True)
    print(f"Hikari 认知模式：{args.reasoner}", flush=True)
    runtime.run_forever()


if __name__ == "__main__":
    main()
