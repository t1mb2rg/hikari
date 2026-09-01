from __future__ import annotations

import asyncio
from pathlib import Path
import time

import pytest

from engineering.heartbeat import (
    EngineeringWorkerHeartbeat,
    EngineeringWorkerHeartbeatEmitter,
    EngineeringWorkerHeartbeatStore,
    EngineeringWorkerLease,
)
from resident.unified import (
    EngineeringWorkerProcessConfig,
    EngineeringWorkerSupervisor,
)


def test_heartbeat_emitter_stays_fresh_and_cleans_up_on_normal_stop(tmp_path: Path) -> None:
    store = EngineeringWorkerHeartbeatStore(tmp_path / "engineering_worker.json")
    emitter = EngineeringWorkerHeartbeatEmitter(
        store,
        owner="resident",
        interval_seconds=0.02,
        pid=5151,
    )

    emitter.start()
    first = store.load()
    assert first is not None
    assert first.pid == 5151
    assert first.owner == "resident"

    time.sleep(0.05)
    second = store.load()
    assert second is not None
    assert second.updated_at >= first.updated_at

    emitter.stop()
    assert store.load() is None


def test_worker_lease_rejects_second_live_worker(tmp_path: Path) -> None:
    heartbeat_store = EngineeringWorkerHeartbeatStore(
        tmp_path / "engineering_worker.json"
    )
    heartbeat_store.write(
        EngineeringWorkerHeartbeat(
            pid=5151,
            owner="resident",
            started_at=900.0,
            updated_at=999.0,
        )
    )
    lease_path = tmp_path / "engineering_worker.lock"
    lease_path.write_text('{"version":1,"pid":5151}', encoding="utf-8")
    lease = EngineeringWorkerLease(
        lease_path,
        heartbeat_store,
        process_probe=lambda pid: pid == 5151,
    )

    with pytest.raises(RuntimeError, match="already active"):
        lease.acquire(pid=6262, owner="manual", started_at=1000.0)


def test_worker_lease_recovers_stale_lock(tmp_path: Path) -> None:
    heartbeat_store = EngineeringWorkerHeartbeatStore(
        tmp_path / "engineering_worker.json"
    )
    heartbeat_store.write(
        EngineeringWorkerHeartbeat(
            pid=5151,
            owner="resident",
            started_at=900.0,
            updated_at=950.0,
        )
    )
    lease_path = tmp_path / "engineering_worker.lock"
    lease_path.write_text('{"version":1,"pid":5151}', encoding="utf-8")
    lease = EngineeringWorkerLease(
        lease_path,
        heartbeat_store,
        process_probe=lambda pid: False,
    )

    lease.acquire(pid=6262, owner="resident", started_at=1000.0)
    assert lease_path.is_file()
    lease.release()
    assert not lease_path.exists()


def test_engineering_worker_supervisor_starts_resident_owned_child(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    state_dir = tmp_path / "state"
    captured: list[tuple[list[str], dict[str, object]]] = []

    class FakeProcess:
        pid = 6060

        def poll(self):
            return None

        def terminate(self):
            return None

        def kill(self):
            return None

        def wait(self):
            return 0

    def factory(argv, **kwargs):
        captured.append((list(argv), dict(kwargs)))
        return FakeProcess()

    supervisor = EngineeringWorkerSupervisor(
        EngineeringWorkerProcessConfig(
            repository=repository,
            state_dir=state_dir,
            log_path=state_dir / "engineering_worker.log",
            environment={"SAFE": "1"},
            python_executable="python-test",
        ),
        process_factory=factory,  # type: ignore[arg-type]
    )

    process = supervisor.start_once()

    assert process.pid == 6060
    assert captured[0][0] == [
        "python-test",
        "-m",
        "engineering.worker",
        "--state-dir",
        str(state_dir.resolve()),
        "--owner",
        "resident",
    ]
    assert captured[0][1]["cwd"] == repository.resolve()
    assert captured[0][1]["env"]["SAFE"] == "1"
    assert captured[0][1]["env"]["PYTHONUTF8"] == "1"


def test_engineering_worker_supervisor_restarts_crashed_child(tmp_path: Path) -> None:
    async def scenario() -> None:
        repository = tmp_path / "repo"
        repository.mkdir()
        created: list[FakeProcess] = []

        class FakeProcess:
            def __init__(self, pid: int, *, exited: bool) -> None:
                self.pid = pid
                self.returncode: int | None = 1 if exited else None
                self.terminated = False

            def poll(self):
                return self.returncode

            def terminate(self):
                self.terminated = True
                self.returncode = 0

            def kill(self):
                self.returncode = -9

            def wait(self):
                if self.returncode is None:
                    self.returncode = 0
                return self.returncode

        def factory(argv, **kwargs):
            process = FakeProcess(
                7000 + len(created),
                exited=len(created) == 0,
            )
            created.append(process)
            return process

        supervisor = EngineeringWorkerSupervisor(
            EngineeringWorkerProcessConfig(
                repository=repository,
                state_dir=tmp_path / "state",
                log_path=tmp_path / "worker.log",
                environment={},
                python_executable="python-test",
                restart_initial_seconds=0.01,
                restart_max_seconds=0.02,
                stable_reset_seconds=0.02,
            ),
            process_factory=factory,  # type: ignore[arg-type]
        )
        stop = asyncio.Event()
        task = asyncio.create_task(supervisor.run(stop))

        for _ in range(100):
            if len(created) >= 2:
                break
            await asyncio.sleep(0.01)

        assert len(created) >= 2
        assert supervisor.restart_count >= 1
        assert created[0].returncode == 1
        assert created[1].poll() is None

        stop.set()
        await asyncio.wait_for(task, timeout=2.0)
        assert created[1].terminated is True

    asyncio.run(scenario())
