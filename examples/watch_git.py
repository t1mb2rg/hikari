from __future__ import annotations

import argparse
from pathlib import Path
import time

from attention import AttentionPolicy
from awareness import (
    ContextCollector,
    HostContextProvider,
    InputActivityContextProvider,
    TimeContextProvider,
)
from brain import SimpleReasoner
from core.presence import ConsoleFeedbackSink, PresencePipeline
from events.runner import SensorRunner
from events.sensors import GitSensor
from memory.store import MemoryStore


def build_runner(repository: Path, memory_path: Path) -> SensorRunner:
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
                HostContextProvider(),
                InputActivityContextProvider(),
            ]
        ),
    )
    return SensorRunner(
        [GitSensor(repository)],
        on_event=pipeline.handle,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run Hikari's proactive loop against a Git repository.",
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
    runner = build_runner(repository, memory_path)

    # Establish sensor baselines before announcing that observation is active.
    runner.poll_once()
    print(f"Hikari is watching {repository}")

    try:
        while True:
            time.sleep(max(0.1, args.interval))
            runner.poll_once()
    except KeyboardInterrupt:
        print("Hikari stopped watching.")


if __name__ == "__main__":
    main()
