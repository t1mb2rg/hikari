from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import time
from typing import Any, Callable

from engineering.heartbeat import EngineeringWorkerHeartbeatStore
from engineering.session import EngineeringSessionState, EngineeringSessionStore
from resident.napcat_login_guard import (
    DEFAULT_NAPCAT_ROOT,
    NapCatLoginError,
    NapCatLoginProbe,
)
from resident.paths import default_state_dir


@dataclass(frozen=True)
class OperationalStateConfig:
    state_dir: Path
    napcat_root: Path = DEFAULT_NAPCAT_ROOT
    onebot_host: str = "127.0.0.1"
    onebot_port: int = 8081
    cache_seconds: float = 5.0
    engineering_worker_stale_seconds: float = 5.0

    def __post_init__(self) -> None:
        state_dir = Path(self.state_dir).expanduser().resolve()
        napcat_root = Path(self.napcat_root).expanduser().resolve()
        if not 1 <= int(self.onebot_port) <= 65535:
            raise ValueError("onebot_port must be between 1 and 65535")
        if float(self.cache_seconds) < 0:
            raise ValueError("cache_seconds must be non-negative")
        if float(self.engineering_worker_stale_seconds) <= 0:
            raise ValueError("engineering_worker_stale_seconds must be > 0")
        object.__setattr__(self, "state_dir", state_dir)
        object.__setattr__(self, "napcat_root", napcat_root)
        object.__setattr__(self, "onebot_port", int(self.onebot_port))
        object.__setattr__(self, "cache_seconds", float(self.cache_seconds))
        object.__setattr__(
            self,
            "engineering_worker_stale_seconds",
            float(self.engineering_worker_stale_seconds),
        )

    @classmethod
    def local_default(cls) -> "OperationalStateConfig":
        return cls(state_dir=default_state_dir())


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_from_epoch(value: float | int | None) -> str | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value), timezone.utc).isoformat()
    except (OSError, OverflowError, TypeError, ValueError):
        return None


def _safe_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            process_query_limited_information = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                process_query_limited_information,
                False,
                int(pid),
            )
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


def _tcp_open(host: str, port: int, timeout: float = 0.2) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _component(
    status: str,
    *,
    observed: bool,
    message: str,
    phase: str,
    updated_at: str | None = None,
    details: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "status": status,
        "observed": observed,
        "phase": phase,
        "message": message,
        "updated_at": updated_at,
        "details": dict(details or {}),
    }


class OperationalStateService:
    """Read-only point-in-time status for Hikari's own runtime.

    The snapshot is intentionally small and secret-safe. An unavailable probe
    becomes ``unknown`` instead of being inferred as healthy or running.
    """

    def __init__(
        self,
        config: OperationalStateConfig,
        *,
        process_probe: Callable[[int], bool] = _process_alive,
        tcp_probe: Callable[[str, int, float], bool] = _tcp_open,
        napcat_probe: Callable[[], object] | None = None,
        engineering_store: EngineeringSessionStore | None = None,
        heartbeat_store: EngineeringWorkerHeartbeatStore | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self._process_probe = process_probe
        self._tcp_probe = tcp_probe
        self._napcat_probe = napcat_probe or NapCatLoginProbe(config.napcat_root)
        self._engineering_store = engineering_store or EngineeringSessionStore(
            config.state_dir / "engineering"
        )
        self._heartbeat_store = heartbeat_store or EngineeringWorkerHeartbeatStore(
            config.state_dir / "engineering_worker.json"
        )
        self._clock = clock
        self._wall_clock = wall_clock
        self._cached_at = float("-inf")
        self._cached_snapshot: dict[str, object] | None = None

    def capture(self, *, force: bool = False) -> dict[str, object]:
        now = self._clock()
        if (
            not force
            and self._cached_snapshot is not None
            and now - self._cached_at < self.config.cache_seconds
        ):
            return json.loads(json.dumps(self._cached_snapshot, ensure_ascii=False))

        components = {
            "resident": self._probe_resident(),
            "qq": self._probe_qq(),
            "engineering": self._probe_engineering(),
        }
        overall = self._overall_status(components)
        snapshot: dict[str, object] = {
            "version": 1,
            "captured_at": _iso_now(),
            "overall": overall,
            "components": components,
            "epistemic_rule": (
                "Only fields with observed=true are current observations. unknown means the "
                "runtime has no trustworthy observation and must not be rewritten as healthy, "
                "running, stopped, or failed."
            ),
        }
        self._cached_snapshot = snapshot
        self._cached_at = now
        return json.loads(json.dumps(snapshot, ensure_ascii=False))

    def _probe_resident(self) -> dict[str, object]:
        host_path = self.config.state_dir / "host.json"
        payload = _safe_json(host_path)
        if payload is None:
            return _component(
                "offline",
                observed=True,
                phase="no_host_state",
                message="No Resident host state is present.",
            )
        pid = payload.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool):
            return _component(
                "unknown",
                observed=False,
                phase="invalid_host_state",
                message="Resident host state exists but does not contain a trustworthy PID.",
            )
        alive = self._process_probe(pid)
        return _component(
            "healthy" if alive else "offline",
            observed=True,
            phase="running" if alive else "stale_host_state",
            message=(
                "Resident process is running."
                if alive
                else "Resident host state remains, but the recorded process is not running."
            ),
            details={
                "pid": pid,
                "started_at": payload.get("started_at"),
            },
        )

    def _probe_qq(self) -> dict[str, object]:
        onebot_open = self._tcp_probe(
            self.config.onebot_host,
            self.config.onebot_port,
            0.2,
        )
        try:
            login = self._napcat_probe()
        except NapCatLoginError:
            return _component(
                "unknown",
                observed=False,
                phase="napcat_probe_unavailable",
                message="NapCat login state could not be observed safely.",
                details={"onebot_port_open": onebot_open},
            )
        except Exception:
            return _component(
                "unknown",
                observed=False,
                phase="napcat_probe_failed",
                message="QQ runtime status probe failed without a trustworthy login observation.",
                details={"onebot_port_open": onebot_open},
            )

        is_login = getattr(login, "is_login", None)
        is_offline = getattr(login, "is_offline", None)
        qrcode_available = getattr(login, "qrcode_available", None)
        if is_login is True and onebot_open:
            status = "healthy"
            phase = "logged_in"
            message = "QQ is logged in and the OneBot endpoint is reachable."
        elif is_login is True:
            status = "warning"
            phase = "logged_in_onebot_unreachable"
            message = "QQ is logged in, but the OneBot endpoint is not reachable."
        elif qrcode_available is True:
            status = "waiting"
            phase = "login_required"
            message = "NapCat is waiting for QQ login confirmation."
        elif is_offline is True:
            status = "warning"
            phase = "login_invalid"
            message = "NapCat reports the QQ session as offline or invalid."
        else:
            status = "waiting"
            phase = "not_logged_in"
            message = "QQ is not currently observed as logged in."
        return _component(
            status,
            observed=True,
            phase=phase,
            message=message,
            details={
                "qq_logged_in": is_login is True,
                "qq_offline": is_offline is True,
                "onebot_port_open": onebot_open,
            },
        )

    def _probe_engineering_worker(self) -> dict[str, object]:
        heartbeat = self._heartbeat_store.load()
        if heartbeat is None:
            return {
                "status": "unknown",
                "observed": False,
                "reason": "no_worker_heartbeat",
            }

        alive = self._process_probe(heartbeat.pid)
        age = max(0.0, float(self._wall_clock()) - heartbeat.updated_at)
        details: dict[str, object] = {
            "pid": heartbeat.pid,
            "owner": heartbeat.owner,
            "started_at": _iso_from_epoch(heartbeat.started_at),
            "updated_at": _iso_from_epoch(heartbeat.updated_at),
            "heartbeat_age_seconds": round(age, 3),
        }
        if not alive:
            return {
                "status": "offline",
                "observed": True,
                "reason": "heartbeat_pid_not_running",
                **details,
            }
        if age > self.config.engineering_worker_stale_seconds:
            return {
                "status": "warning",
                "observed": True,
                "reason": "heartbeat_stale",
                **details,
            }
        return {
            "status": "healthy",
            "observed": True,
            "reason": "fresh_heartbeat_and_live_pid",
            **details,
        }

    def _probe_engineering(self) -> dict[str, object]:
        worker = self._probe_engineering_worker()
        try:
            states = self._engineering_store.list_states()
        except Exception:
            return _component(
                "unknown",
                observed=False,
                phase="session_store_unreadable",
                message="EngineeringSession state could not be read safely.",
                details={"worker": worker},
            )

        latest: EngineeringSessionState | None = (
            max(states, key=lambda state: state.updated_at) if states else None
        )
        active = [state for state in states if state.status in {"pending", "running"}]
        if active:
            current = max(active, key=lambda state: state.updated_at)
            status = "running" if current.status == "running" else "waiting"
            phase = current.status
            message = (
                "An EngineeringSession is currently running."
                if current.status == "running"
                else "An EngineeringSession is pending worker execution."
            )
        elif latest is None:
            status = "idle"
            phase = "no_sessions"
            message = "Engineering has no current session work."
        else:
            status = "idle"
            phase = "idle"
            message = "Engineering has no active session work."

        worker_status = str(worker.get("status", "unknown"))
        if worker_status in {"offline", "warning"}:
            status = "warning"
            phase = "worker_unhealthy"
            message = "Engineering session state is readable, but the Engineering Worker is not healthy."

        details: dict[str, object] = {
            "active_session_count": len(active),
            "session_count": len(states),
            "worker": worker,
            "worker_liveness": worker_status,
        }
        if latest is not None:
            details.update(
                {
                    "latest_session_status": latest.status,
                    "latest_session_updated_at": _iso_from_epoch(latest.updated_at),
                }
            )
        return _component(
            status,
            observed=True,
            phase=phase,
            message=message,
            details=details,
        )

    @staticmethod
    def _overall_status(components: dict[str, dict[str, object]]) -> str:
        statuses = {str(component.get("status", "unknown")) for component in components.values()}
        if "offline" in statuses or "error" in statuses:
            return "degraded"
        if "warning" in statuses or "waiting" in statuses:
            return "degraded"
        if statuses == {"unknown"}:
            return "unknown"
        if "unknown" in statuses:
            return "partial"
        return "healthy"


_DEFAULT_SERVICE: OperationalStateService | None = None


def capture_operational_state(*, force: bool = False) -> dict[str, object]:
    """Capture Hikari's local operational state for Conversation grounding."""

    global _DEFAULT_SERVICE
    if _DEFAULT_SERVICE is None:
        _DEFAULT_SERVICE = OperationalStateService(OperationalStateConfig.local_default())
    try:
        return _DEFAULT_SERVICE.capture(force=force)
    except Exception:
        return {
            "version": 1,
            "captured_at": _iso_now(),
            "overall": "unknown",
            "components": {},
            "epistemic_rule": (
                "Operational status capture failed. Do not infer current component health or "
                "liveness from static capability descriptions or conversation history."
            ),
        }