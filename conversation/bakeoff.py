from __future__ import annotations

import argparse
from collections.abc import Sequence
from datetime import datetime
import json
from pathlib import Path
import secrets
import time
from urllib.error import HTTPError, URLError

from brain.model_reasoner import ChatMessage, ChatProvider
from memory.store import MemoryStore
from resident.environment import load_runtime_environment

from .cli import build_chat_provider
from .engine import ConversationEngine, THIN_HIKARI_SYSTEM_INSTRUCTIONS
from .models import UserTurn


BAKEOFF_TURNS = (
    "hikari",
    "其实我正在考虑给你换一个更合适的模型。",
    "最近几个项目都进瓶颈了，架构换了几回，补丁打补丁，挺烦的。",
    "暂时不聊这个。你知道 Forge 吗？",
    "那你现在为什么不能直接碰我的项目？",
    "如果以后要让你参与自己的迭代，为什么也不能直接把 Forge 权限一直给你？",
    "你还记得你一开始说话的方式吗？",
    (
        "这是我现在贴给你看的旧记录片段，不是你当前自己回忆出来的：\n"
        "Hikari> 你好呀！😊 我们又见面了。\n"
        "Hikari> 我没有持久记忆，每次对话对我来说都是新的开始。\n"
        "Hikari> 有什么我能帮上忙的吗？😊\n\n"
        "看完以后告诉我：你觉得当时哪里怪？你记得当时吗？"
    ),
)

PRIMARY_LOCAL_RELATIONSHIP_CONTEXT = {
    "kind": "primary_local_user",
    "basis": "trusted_runtime_binding",
    "memory_claim": "continuity_without_implied_episode_recall",
    "continuity": (
        "This is an explicit trusted conversation with Hikari's primary local user. "
        "This binding establishes the ongoing relationship but does not mean exact "
        "prior conversations, development episodes, or elapsed gaps are independently remembered."
    ),
}


class RetryingProvider:
    """Retry transient transport failures without duplicating conversation events."""

    def __init__(self, provider: ChatProvider, *, attempts: int = 3, delay: float = 0.5) -> None:
        if attempts <= 0:
            raise ValueError("attempts must be positive")
        if delay < 0:
            raise ValueError("delay must not be negative")
        self.provider = provider
        self.attempts = int(attempts)
        self.delay = float(delay)

    def complete(self, messages: Sequence[ChatMessage]) -> str:
        for attempt in range(1, self.attempts + 1):
            try:
                return self.provider.complete(messages)
            except HTTPError:
                raise
            except (URLError, TimeoutError, ConnectionError):
                if attempt >= self.attempts:
                    raise
                time.sleep(self.delay * attempt)
        raise RuntimeError("retry loop exhausted unexpectedly")


def _build_candidate(env_file: Path, memory_path: Path) -> tuple[ConversationEngine, str]:
    # Explicit bake-off files are authoritative. Process-level HIKARI_MODEL_* values
    # are intentionally excluded so one stale shell variable cannot make both labels
    # hit the same model.
    runtime = load_runtime_environment(env_file=env_file, environment={})
    provider = build_chat_provider(runtime.values)
    model = getattr(provider, "model", type(provider).__name__)
    engine = ConversationEngine(
        RetryingProvider(provider),
        MemoryStore(memory_path),
        personality_profile=None,
        voice_profile=None,
        relationship_context=PRIMARY_LOCAL_RELATIONSHIP_CONTEXT,
        history_limit=24,
        system_instructions=THIN_HIKARI_SYSTEM_INSTRUCTIONS,
    )
    return engine, str(model)


def _run_directory(root: Path | None = None) -> Path:
    base = root or (Path.cwd() / ".hikari" / "bakeoff")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = base / stamp
    suffix = 1
    while candidate.exists():
        candidate = base / f"{stamp}-{suffix}"
        suffix += 1
    candidate.mkdir(parents=True, exist_ok=False)
    return candidate


def run_bakeoff(
    candidate_files: Sequence[str | Path],
    *,
    output_root: Path | None = None,
    turns: Sequence[str] = BAKEOFF_TURNS,
    shuffle: bool = True,
) -> Path:
    if len(candidate_files) != 2:
        raise ValueError("blind bake-off currently requires exactly two candidates")
    if not turns:
        raise ValueError("bake-off requires at least one turn")

    paths = [Path(value).expanduser().resolve() for value in candidate_files]
    for path in paths:
        if not path.is_file():
            raise ValueError(f"candidate env file does not exist: {path}")

    run_dir = _run_directory(output_root)
    assignments = list(paths)
    if shuffle:
        secrets.SystemRandom().shuffle(assignments)

    labels = ("A", "B")
    engines: dict[str, ConversationEngine] = {}
    reveal: dict[str, dict[str, str]] = {}
    for label, env_file in zip(labels, assignments, strict=True):
        engine, model = _build_candidate(env_file, run_dir / f"candidate-{label}.db")
        engines[label] = engine
        reveal[label] = {
            "model": model,
            "env_file": str(env_file),
        }

    transcript_lines = [
        "Hikari blind conversation bake-off",
        "Prompt profile: thin",
        "Candidates: A / B (identity hidden)",
        "",
    ]

    for index, text in enumerate(turns, start=1):
        transcript_lines.extend([f"=== Turn {index} ===", f"你> {text}"])
        for label in labels:
            reply = engines[label].respond(
                UserTurn(
                    channel="bakeoff",
                    conversation_id=f"candidate-{label}",
                    text=text,
                )
            )
            transcript_lines.extend([f"{label}> {reply.text}", ""])

    transcript = "\n".join(transcript_lines).rstrip() + "\n"
    (run_dir / "transcript.txt").write_text(transcript, encoding="utf-8")
    (run_dir / "reveal.json").write_text(
        json.dumps(reveal, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return run_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hikari-bakeoff",
        description="用同一组 thin Hikari 场景对两个模型做 A/B 盲测。",
    )
    parser.add_argument(
        "--candidate",
        action="append",
        required=True,
        help="候选模型的 env 文件。当前必须提供两次。",
    )
    parser.add_argument(
        "--output-root",
        default=None,
        help="可选输出目录；默认写入 .hikari/bakeoff。",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        run_dir = run_bakeoff(
            args.candidate,
            output_root=(Path(args.output_root).expanduser().resolve() if args.output_root else None),
        )
    except (ValueError, OSError) as exc:
        print(f"Hikari 盲测启动失败：{exc}")
        return 2
    except Exception as exc:
        print(f"Hikari 盲测中断：{exc}")
        return 1

    transcript_path = run_dir / "transcript.txt"
    print(transcript_path.read_text(encoding="utf-8"), end="")
    print(f"\n盲测结果：{transcript_path}")
    print(f"揭晓文件：{run_dir / 'reveal.json'}")
    print("先判断 A / B，再打开 reveal.json。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
