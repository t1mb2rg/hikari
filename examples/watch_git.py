from __future__ import annotations

import argparse
from pathlib import Path

from attention import AttentionPolicy
from awareness import (
    ChineseCalendarContextProvider,
    ContextCollector,
    ForegroundContextProvider,
    HostContextProvider,
    InputActivityContextProvider,
    TimeContextProvider,
)
from brain import SimpleReasoner
from core.presence import ConsoleFeedbackSink, PresencePipeline
from core.runtime import ResidentPresenceRuntime
from events.sensors import GitSensor
from memory.store import MemoryStore


def build_runtime(
    repository: Path,
    memory_path: Path,
    *,
    interval: float = 2.0,
) -> ResidentPresenceRuntime:
    """Build the concrete Git-backed resident Presence runtime used by this gate."""

    pipeline = PresencePipeline(
        memory=MemoryStore(memory_path),
        attention=AttentionPolicy(
            threshold=0.7,
            event_importance={"git.commit": 0.8},
        ),
        reasoner=SimpleReasoner(),
        feedback_sink=ConsoleFeedbackSink(),
        context_collector=ContextCollector(
            [
                TimeContextProvider(),
                ChineseCalendarContextProvider(),
                HostContextProvider(),
                InputActivityContextProvider(),
                ForegroundContextProvider(),
            ]
        ),
    )
    return ResidentPresenceRuntime(
        [GitSensor(repository)],
        pipeline,
        poll_interval=max(0.1, interval),
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run Hikari's resident proactive loop against a Git repository.",
    )
    parser.add_argument(
        "repository",
        nargs="?",
        default=".",
        help="Git repository to observe (default: current directory)",
    )
    parser.add_argument(
        "--db",
        default=".hikari/memory.db",
        help="SQLite memory path (default: .hikari/memory.db)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="Polling interval in seconds (default: 2.0)",
    )
    args = parser.parse_args()

    repository = Path(args.repository).resolve()
    memory_path = Path(args.db).resolve()
    runtime = build_runtime(repository, memory_path, interval=args.interval)

    print(f"Hikari is watching {repository}")
    runtime.run_forever()


if __name__ == "__main__":
    main()
