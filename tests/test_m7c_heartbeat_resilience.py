from __future__ import annotations

from pathlib import Path
import threading

from engineering.heartbeat import (
    EngineeringWorkerHeartbeat,
    EngineeringWorkerHeartbeatEmitter,
    EngineeringWorkerHeartbeatStore,
)


def test_heartbeat_store_atomic_write_leaves_no_temporary_files(tmp_path: Path) -> None:
    path = tmp_path / "engineering_worker.json"
    store = EngineeringWorkerHeartbeatStore(path)

    for updated_at in (1.0, 2.0, 3.0):
        store.write(
            EngineeringWorkerHeartbeat(
                pid=5151,
                owner="resident",
                started_at=1.0,
                updated_at=updated_at,
            )
        )

    current = store.load()
    assert current is not None
    assert current.updated_at == 3.0
    assert list(tmp_path.glob("*.tmp")) == []
    assert list(tmp_path.glob(".*.tmp")) == []


def test_heartbeat_emitter_survives_one_transient_background_write_error() -> None:
    recovered = threading.Event()

    class FlakyStore:
        def __init__(self) -> None:
            self.calls = 0
            self.removed_pid: int | None = None

        def write(self, heartbeat: EngineeringWorkerHeartbeat) -> None:
            self.calls += 1
            if self.calls == 2:
                raise PermissionError("transient Windows sharing violation")
            if self.calls >= 3:
                recovered.set()

        def remove_if_owned_by(self, pid: int) -> None:
            self.removed_pid = pid

    store = FlakyStore()
    emitter = EngineeringWorkerHeartbeatEmitter(
        store,  # type: ignore[arg-type]
        owner="resident",
        interval_seconds=0.01,
        pid=6262,
    )

    emitter.start()
    try:
        assert recovered.wait(timeout=1.0)
        assert store.calls >= 3
    finally:
        emitter.stop()

    assert store.removed_pid == 6262
