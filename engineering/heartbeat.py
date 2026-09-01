from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import threading
import time
from typing import Callable


HEARTBEAT_VERSION = 1


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            query = 0x1000  # PROCESS_QUERY_LIMITED_INFORMATION
            handle = ctypes.windll.kernel32.OpenProcess(query, False, int(pid))
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        except (AttributeError, OSError, ValueError):
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


@dataclass(frozen=True, slots=True)
class EngineeringWorkerHeartbeat:
    pid: int
    owner: str
    started_at: float
    updated_at: float

    def to_mapping(self) -> dict[str, object]:
        return {
            "version": HEARTBEAT_VERSION,
            "pid": int(self.pid),
            "owner": self.owner,
            "started_at": float(self.started_at),
            "updated_at": float(self.updated_at),
        }

    @classmethod
    def from_mapping(cls, payload: object) -> "EngineeringWorkerHeartbeat":
        if not isinstance(payload, dict) or payload.get("version") != HEARTBEAT_VERSION:
            raise ValueError("unsupported engineering worker heartbeat")
        pid = payload.get("pid")
        owner = payload.get("owner")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            raise ValueError("engineering worker heartbeat PID is invalid")
        if not isinstance(owner, str) or not owner.strip():
            raise ValueError("engineering worker heartbeat owner is invalid")
        return cls(
            pid=pid,
            owner=owner.strip(),
            started_at=float(payload.get("started_at", 0.0)),
            updated_at=float(payload.get("updated_at", 0.0)),
        )


class EngineeringWorkerHeartbeatStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()

    def write(self, heartbeat: EngineeringWorkerHeartbeat) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary.write_text(
            json.dumps(heartbeat.to_mapping(), ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)

    def load(self) -> EngineeringWorkerHeartbeat | None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return EngineeringWorkerHeartbeat.from_mapping(payload)
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
            return None

    def remove_if_owned_by(self, pid: int) -> None:
        current = self.load()
        if current is None or current.pid != int(pid):
            return
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


class EngineeringWorkerLease:
    """One-process lease preventing two Engineering Workers from consuming one store."""

    def __init__(
        self,
        path: str | Path,
        heartbeat_store: EngineeringWorkerHeartbeatStore,
        *,
        process_probe: Callable[[int], bool] = _process_alive,
    ) -> None:
        self.path = Path(path).expanduser().resolve()
        self.heartbeat_store = heartbeat_store
        self._process_probe = process_probe
        self._pid: int | None = None

    def _live_existing_owner(self) -> tuple[int, str] | None:
        # The lease itself is authoritative during the tiny startup window before
        # the heartbeat thread has written its first sample.
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict):
            pid = payload.get("pid")
            owner = payload.get("owner")
            if (
                isinstance(pid, int)
                and not isinstance(pid, bool)
                and pid > 0
                and self._process_probe(pid)
            ):
                return pid, str(owner or "unknown")

        # A valid heartbeat is a second line of defence if the lease file was
        # malformed or left by an older implementation.
        heartbeat = self.heartbeat_store.load()
        if heartbeat is not None and self._process_probe(heartbeat.pid):
            return heartbeat.pid, heartbeat.owner
        return None

    def acquire(self, *, pid: int, owner: str, started_at: float) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {
                "version": 1,
                "pid": int(pid),
                "owner": str(owner),
                "started_at": float(started_at),
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")

        for attempt in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                existing = self._live_existing_owner()
                if existing is not None:
                    existing_pid, existing_owner = existing
                    raise RuntimeError(
                        f"engineering worker already active pid={existing_pid} owner={existing_owner}"
                    ) from None
                if attempt == 0:
                    try:
                        self.path.unlink()
                    except FileNotFoundError:
                        pass
                    continue
                raise RuntimeError("engineering worker lease is already held") from None
            else:
                try:
                    os.write(fd, payload)
                finally:
                    os.close(fd)
                self._pid = int(pid)
                return

        raise RuntimeError("engineering worker lease could not be acquired")

    def release(self) -> None:
        if self._pid is None:
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict) and payload.get("pid") == self._pid:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        self._pid = None


class EngineeringWorkerHeartbeatEmitter:
    """Keep worker liveness fresh even while the engineering backend blocks."""

    def __init__(
        self,
        store: EngineeringWorkerHeartbeatStore,
        *,
        owner: str,
        interval_seconds: float = 1.0,
        pid: int | None = None,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        interval = float(interval_seconds)
        if interval <= 0:
            raise ValueError("engineering heartbeat interval must be > 0")
        self.store = store
        self.owner = str(owner).strip() or "manual"
        self.interval_seconds = interval
        self.pid = int(pid or os.getpid())
        self._wall_clock = wall_clock
        self.started_at = float(self._wall_clock())
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _write(self) -> None:
        self.store.write(
            EngineeringWorkerHeartbeat(
                pid=self.pid,
                owner=self.owner,
                started_at=self.started_at,
                updated_at=float(self._wall_clock()),
            )
        )

    def start(self) -> None:
        if self._thread is not None:
            return
        self._write()
        self._thread = threading.Thread(
            target=self._run,
            name="hikari-engineering-heartbeat",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            self._write()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(1.0, self.interval_seconds * 2.0))
        self.store.remove_if_owned_by(self.pid)
        self._thread = None

    def __enter__(self) -> "EngineeringWorkerHeartbeatEmitter":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()
