from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from resident.napcat_login_guard import (
    DEFAULT_NAPCAT_ROOT,
    DEFAULT_NAPCAT_TASK_NAME,
    NapCatLoginError,
    NapCatWebUIConfig,
    WindowsScheduledTaskRestarter,
)
from resident.windows_host import _default_process_probe, default_state_dir

from .models import ComponentSnapshot, ComponentStatus


@dataclass(frozen=True)
class DashboardProbeConfig:
    repository: Path
    state_dir: Path
    napcat_root: Path = DEFAULT_NAPCAT_ROOT
    napcat_task_name: str = DEFAULT_NAPCAT_TASK_NAME
    onebot_host: str = "127.0.0.1"
    onebot_port: int = 8081
    forge_stale_seconds: float = 180.0

    def __post_init__(self) -> None:
        repository = Path(self.repository).expanduser().resolve()
        state_dir = Path(self.state_dir).expanduser().resolve()
        napcat_root = Path(self.napcat_root).expanduser().resolve()
        if not repository.is_dir():
            raise ValueError(f"dashboard repository must exist: {repository}")
        if not str(self.napcat_task_name).strip():
            raise ValueError("napcat_task_name must not be empty")
        if not 1 <= int(self.onebot_port) <= 65535:
            raise ValueError("onebot_port must be between 1 and 65535")
        if float(self.forge_stale_seconds) <= 0:
            raise ValueError("forge_stale_seconds must be > 0")
        object.__setattr__(self, "repository", repository)
        object.__setattr__(self, "state_dir", state_dir)
        object.__setattr__(self, "napcat_root", napcat_root)
        object.__setattr__(self, "napcat_task_name", str(self.napcat_task_name).strip())
        object.__setattr__(self, "onebot_port", int(self.onebot_port))
        object.__setattr__(self, "forge_stale_seconds", float(self.forge_stale_seconds))

    @classmethod
    def local_default(cls, repository: str | Path = ".") -> "DashboardProbeConfig":
        return cls(
            repository=Path(repository),
            state_dir=default_state_dir(),
        )


@dataclass(frozen=True)
class NapCatDashboardStatus:
    is_login: bool
    is_offline: bool
    qrcode_url: str | None
    login_error: str | None


class _NapCatDashboardClient:
    """Credential-safe WebUI client for the small status surface used by the dashboard."""

    def __init__(self, config: NapCatWebUIConfig, *, timeout_seconds: float = 2.0) -> None:
        self.config = config
        self.timeout_seconds = float(timeout_seconds)
        self._credential: str | None = None
        self._credential_expires_at = 0.0

    def _post(
        self,
        path: str,
        body: Mapping[str, object],
        *,
        credential: str | None = None,
    ) -> object:
        headers = {"Content-Type": "application/json"}
        if credential:
            headers["Authorization"] = f"Bearer {credential}"
        request = Request(
            f"{self.config.base_url}{path}",
            data=json.dumps(dict(body), separators=(",", ":")).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            response = urlopen(request, timeout=self.timeout_seconds)
            with response:
                raw = response.read(64 * 1024 + 1)
        except (HTTPError, URLError, OSError, TimeoutError):
            raise NapCatLoginError("NapCat WebUI request failed") from None
        if len(raw) > 64 * 1024:
            raise NapCatLoginError("NapCat WebUI response exceeded size limit")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise NapCatLoginError("NapCat WebUI returned malformed JSON") from None
        if not isinstance(payload, dict) or payload.get("code") != 0:
            raise NapCatLoginError("NapCat WebUI API rejected the request")
        return payload.get("data")

    def _authenticate(self) -> str:
        token_hash = hashlib.sha256(
            f"{self.config.token}.napcat".encode("utf-8")
        ).hexdigest()
        data = self._post("/api/auth/login", {"hash": token_hash})
        if not isinstance(data, Mapping):
            raise NapCatLoginError("NapCat WebUI credential was missing")
        credential = data.get("Credential")
        if not isinstance(credential, str) or not credential:
            raise NapCatLoginError("NapCat WebUI credential was missing")
        self._credential = credential
        self._credential_expires_at = time.monotonic() + 50 * 60
        return credential

    def _active_credential(self) -> str:
        if self._credential and time.monotonic() < self._credential_expires_at:
            return self._credential
        return self._authenticate()

    def check(self) -> NapCatDashboardStatus:
        try:
            data = self._post(
                "/api/QQLogin/CheckLoginStatus",
                {},
                credential=self._active_credential(),
            )
        except NapCatLoginError:
            # NapCat may invalidate the short-lived WebUI credential after
            # login-state changes. Status probing is read-only, so one bounded
            # re-authentication retry is safe.
            self._credential = None
            self._credential_expires_at = 0.0
            data = self._post(
                "/api/QQLogin/CheckLoginStatus",
                {},
                credential=self._active_credential(),
            )
        if not isinstance(data, Mapping):
            raise NapCatLoginError("NapCat login status payload was missing")
        raw_qr = data.get("qrcodeurl")
        qrcode_url = raw_qr.strip() if isinstance(raw_qr, str) and raw_qr.strip() else None
        error = data.get("loginError")
        return NapCatDashboardStatus(
            is_login=data.get("isLogin") is True,
            is_offline=data.get("isOffline") is True,
            qrcode_url=qrcode_url,
            login_error=str(error)[:500] if error else None,
        )


_SECRET_PATTERN = re.compile(
    r"(?i)(authorization\s*[:=]\s*bearer\s+|api[_-]?key\s*[:=]\s*|token\s*[:=]\s*)([^\s,;]+)"
)


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_from_epoch(value: float | int | None) -> str | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value), timezone.utc).isoformat()
    except (OSError, OverflowError, TypeError, ValueError):
        return None


def _mtime_iso(path: Path) -> str | None:
    try:
        return _iso_from_epoch(path.stat().st_mtime)
    except OSError:
        return None


def _safe_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            return _default_process_probe(pid)
        except OSError:
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


def _tail_lines(path: Path, limit: int) -> list[str]:
    if limit <= 0 or not path.is_file():
        return []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            return list(deque((line.rstrip() for line in handle), maxlen=limit))
    except OSError:
        return []


def _sanitize_line(line: str) -> str:
    return _SECRET_PATTERN.sub(lambda match: f"{match.group(1)}<redacted>", line)


class DashboardProbeService:
    """Read-only probes plus a tiny fixed NapCat control surface for the local dashboard."""

    def __init__(self, config: DashboardProbeConfig) -> None:
        if not isinstance(config, DashboardProbeConfig):
            raise TypeError("DashboardProbeService requires DashboardProbeConfig")
        self.config = config
        self._napcat_client: _NapCatDashboardClient | None = None
        self._napcat_client_config: NapCatWebUIConfig | None = None

    @property
    def forge_run_root(self) -> Path:
        return self.config.repository.parent / ".forge-runs"

    def _get_napcat_client(self) -> _NapCatDashboardClient:
        config = NapCatWebUIConfig.from_root(self.config.napcat_root)
        if self._napcat_client is None or self._napcat_client_config != config:
            self._napcat_client = _NapCatDashboardClient(config)
            self._napcat_client_config = config
        return self._napcat_client

    def probe_resident(self) -> ComponentSnapshot:
        host_file = self.config.state_dir / "host.json"
        payload = _safe_json(host_file)
        if payload is None:
            return ComponentSnapshot(
                component_id="resident",
                label="Resident",
                status=ComponentStatus.OFFLINE,
                phase="已停止",
                message="没有发现 Resident 运行状态",
                updated_at=_mtime_iso(host_file),
            )

        pid = payload.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool):
            return ComponentSnapshot(
                component_id="resident",
                label="Resident",
                status=ComponentStatus.ERROR,
                phase="状态异常",
                message="Resident 状态文件无法识别",
                updated_at=_mtime_iso(host_file),
                last_error="host.json 中缺少有效 PID",
            )

        running = _process_alive(pid)
        log_file = self.config.state_dir / "resident.log"
        updated_at = _mtime_iso(log_file) or _mtime_iso(host_file)
        if not running:
            return ComponentSnapshot(
                component_id="resident",
                label="Resident",
                status=ComponentStatus.OFFLINE,
                phase="进程已退出",
                message="Resident 状态仍存在，但进程已经不在运行",
                updated_at=updated_at,
                last_error="stale host state",
                details={"pid": pid},
            )

        return ComponentSnapshot(
            component_id="resident",
            label="Resident",
            status=ComponentStatus.HEALTHY,
            phase="常驻监听",
            message="Resident 正在运行",
            updated_at=updated_at,
            details={
                "pid": pid,
                "started_at": payload.get("started_at"),
                "repository": payload.get("repository"),
            },
        )

    def probe_napcat(self) -> ComponentSnapshot:
        onebot_connected = _tcp_open(self.config.onebot_host, self.config.onebot_port)
        try:
            status = self._get_napcat_client().check()
        except NapCatLoginError as exc:
            return ComponentSnapshot(
                component_id="napcat",
                label="QQ / NapCat",
                status=ComponentStatus.OFFLINE,
                phase="WebUI 不可用",
                message="无法读取 NapCat 登录状态",
                updated_at=_iso_now(),
                last_error=str(exc),
                details={"onebot_connected": onebot_connected},
            )

        details = {
            "qq_logged_in": status.is_login,
            "qq_offline": status.is_offline,
            "onebot_connected": onebot_connected,
            "qrcode_url": status.qrcode_url,
            "login_error": status.login_error,
        }
        if status.is_login and onebot_connected:
            return ComponentSnapshot(
                component_id="napcat",
                label="QQ / NapCat",
                status=ComponentStatus.HEALTHY,
                phase="QQ 已登录",
                message="NapCat 与 OneBot 通道可用",
                updated_at=_iso_now(),
                details=details,
            )
        if status.is_login:
            return ComponentSnapshot(
                component_id="napcat",
                label="QQ / NapCat",
                status=ComponentStatus.WARNING,
                phase="QQ 已登录",
                message="QQ 已登录，但 OneBot 端口没有响应",
                updated_at=_iso_now(),
                blocking_on="OneBot 连接",
                details=details,
            )
        if status.qrcode_url:
            return ComponentSnapshot(
                component_id="napcat",
                label="QQ / NapCat",
                status=ComponentStatus.WAITING,
                phase="等待扫码",
                message="请使用手机 QQ 扫描登录二维码",
                updated_at=_iso_now(),
                blocking_on="QQ 扫码确认",
                last_error=status.login_error,
                details=details,
            )
        return ComponentSnapshot(
            component_id="napcat",
            label="QQ / NapCat",
            status=ComponentStatus.WARNING,
            phase="QQ 未登录",
            message="NapCat 正在等待 QQ 登录",
            updated_at=_iso_now(),
            blocking_on="QQ 登录",
            last_error=status.login_error,
            details=details,
        )

    def _latest_forge_run(self) -> Path | None:
        root = self.forge_run_root
        if not root.is_dir():
            return None
        try:
            runs = [path for path in root.iterdir() if path.is_dir()]
        except OSError:
            return None
        if not runs:
            return None
        try:
            return max(runs, key=lambda path: path.stat().st_mtime)
        except OSError:
            return None

    def probe_forge(self) -> ComponentSnapshot:
        run_dir = self._latest_forge_run()
        if run_dir is None:
            return ComponentSnapshot(
                component_id="forge",
                label="Forge",
                status=ComponentStatus.IDLE,
                phase="空闲",
                message="当前没有 Forge 运行记录",
            )

        report = _safe_json(run_dir / "report.json")
        state = _safe_json(run_dir / "control" / "state.json")
        if state is None and report is None:
            return ComponentSnapshot(
                component_id="forge",
                label="Forge",
                status=ComponentStatus.WARNING,
                phase="状态未知",
                message="发现 Forge 运行目录，但没有可读取的状态",
                updated_at=_mtime_iso(run_dir),
                details={"run": run_dir.name},
            )

        if state is None:
            state = {}
        phase = str(state.get("phase") or "").upper()
        outcome = state.get("outcome")
        if outcome is None and report is not None:
            outcome = report.get("outcome") or report.get("status")
        updated_epoch = state.get("updated_at")
        updated_at = _iso_from_epoch(updated_epoch) or _mtime_iso(run_dir)
        details = {
            "run": run_dir.name,
            "attempt": state.get("attempt"),
            "max_attempts": state.get("max_attempts"),
            "branch": state.get("branch"),
            "worktree": state.get("worktree"),
        }

        if outcome is not None:
            normalized = str(outcome).casefold()
            successful = "complete" in normalized or "success" in normalized or normalized == "passed"
            return ComponentSnapshot(
                component_id="forge",
                label="Forge",
                status=ComponentStatus.HEALTHY if successful else ComponentStatus.ERROR,
                phase="已完成" if successful else "执行失败",
                message=(
                    "最近一次 Forge 任务已完成"
                    if successful
                    else "最近一次 Forge 任务没有通过"
                ),
                updated_at=updated_at,
                last_error=None if successful else str(outcome),
                details=details,
            )

        phase_map: dict[str, tuple[str, str, str | None]] = {
            "PLANNING": ("规划中", "正在准备工程任务", "任务规划"),
            "WORKING": ("执行中", "Forge 正在修改代码", "工程执行器"),
            "IMPLEMENTING": ("执行中", "Forge 正在修改代码", "工程执行器"),
            "REVIEWING": ("审查中", "正在审查工程结果", "语义审查"),
            "VERIFYING": ("验证中", "正在验证代码变更", "项目测试"),
            "DELIVERING": ("交付中", "正在整理本地工程结果", "本地交付"),
        }
        localized_phase, message, blocking_on = phase_map.get(
            phase,
            (phase or "运行中", "Forge 任务正在运行", None),
        )

        stale = False
        try:
            stale = updated_epoch is not None and time.time() - float(updated_epoch) > self.config.forge_stale_seconds
        except (TypeError, ValueError):
            stale = False
        if stale:
            return ComponentSnapshot(
                component_id="forge",
                label="Forge",
                status=ComponentStatus.IDLE,
                phase="上次任务未收尾",
                message=(
                    f"最近一次 Forge 记录停在{localized_phase}，但已长时间没有更新；"
                    "当前不视为活动任务"
                ),
                updated_at=updated_at,
                blocking_on=None,
                last_error="历史运行状态未收尾",
                details={**details, "stale_phase": phase or None},
            )

        return ComponentSnapshot(
            component_id="forge",
            label="Forge",
            status=ComponentStatus.RUNNING,
            phase=localized_phase,
            message=message,
            updated_at=updated_at,
            blocking_on=blocking_on,
            details=details,
        )

    def recent_events(self, *, limit: int = 60) -> list[dict[str, str]]:
        limit = max(1, min(int(limit), 200))
        sources = [
            ("Resident", self.config.state_dir / "resident.log"),
            ("QQ", self.config.state_dir / "qq_bridge.log"),
        ]
        events: list[dict[str, str]] = []
        per_source = max(10, limit)
        for source, path in sources:
            for line in _tail_lines(path, per_source):
                stripped = line.strip()
                if not stripped:
                    continue
                events.append(
                    {
                        "source": source,
                        "summary": _sanitize_line(stripped)[:1000],
                    }
                )
        return events[-limit:][::-1]

    def recent_errors(self, *, limit: int = 8) -> list[dict[str, str]]:
        markers = ("error", "warning", "failed", "timeout", "degraded", "traceback")
        selected = [
            event
            for event in self.recent_events(limit=200)
            if any(marker in event["summary"].casefold() for marker in markers)
        ]
        return selected[: max(1, min(int(limit), 20))]

    def snapshot(self) -> dict[str, Any]:
        components = [
            self.probe_resident(),
            self.probe_napcat(),
            self.probe_forge(),
        ]
        severe = [
            component
            for component in components
            if component.status in {ComponentStatus.ERROR, ComponentStatus.OFFLINE}
        ]
        warnings = [
            component
            for component in components
            if component.status in {ComponentStatus.WARNING, ComponentStatus.WAITING}
        ]
        if severe:
            overall = "offline" if any(item.component_id == "resident" for item in severe) else "degraded"
        elif warnings:
            overall = "degraded"
        else:
            overall = "healthy"

        blocker = next(
            (
                {
                    "component": item.label,
                    "phase": item.phase,
                    "blocking_on": item.blocking_on,
                    "message": item.message,
                }
                for item in components
                if item.blocking_on
            ),
            None,
        )
        return {
            "generated_at": _iso_now(),
            "overall": overall,
            "components": [item.to_mapping() for item in components],
            "current_blocker": blocker,
            "recent_errors": self.recent_errors(),
        }

    def restart_napcat(self) -> None:
        launcher = self.config.napcat_root / "NapCatWinBootMain.exe"
        WindowsScheduledTaskRestarter(
            self.config.napcat_task_name,
            launcher,
        )()
