from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from enum import StrEnum
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import time
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4


MANUAL_VERIFICATION_URL = "http://127.0.0.1:6099/webui/"
DEFAULT_NAPCAT_ROOT = Path(r"D:\NapCat-Shell-v4.18.19")
DEFAULT_NAPCAT_TASK_NAME = "Hikari NapCat Shell"


class NapCatLoginError(RuntimeError):
    """A bounded, credential-safe NapCat WebUI probe failure."""


class NapCatAuthenticationError(NapCatLoginError):
    """The short-lived WebUI credential could not be obtained or used."""


class NapCatLoginKind(StrEnum):
    HEALTHY = "healthy"
    LOGIN_INVALID = "login_invalid"
    MANUAL_VERIFICATION_REQUIRED = "manual_verification_required"
    PENDING = "pending"
    UNKNOWN = "unknown"


_INVALID_LOGIN_MARKERS = (
    "kickedoffline",
    "kicked_offline",
    "kicked off",
    "登录已失效",
    "登陆已失效",
    "当前登录失效",
    "当前登陆失效",
    "重新登录",
    "重新登陆",
    "login expired",
    "login invalid",
)
_MANUAL_LOGIN_MARKERS = (
    "验证码",
    "扫码",
    "二维码",
    "新设备",
    "异常设备",
    "captcha",
    "qr code",
    "qrcode",
    "new device",
    "device verification",
)


@dataclass(frozen=True)
class NapCatLoginStatus:
    is_login: bool
    is_offline: bool
    qrcode_available: bool
    login_error: str | None = None

    @property
    def kind(self) -> NapCatLoginKind:
        if self.is_login:
            return NapCatLoginKind.HEALTHY
        normalized = (self.login_error or "").casefold()
        if self.is_offline or any(marker in normalized for marker in _INVALID_LOGIN_MARKERS):
            return NapCatLoginKind.LOGIN_INVALID
        if self.qrcode_available or any(
            marker in normalized for marker in _MANUAL_LOGIN_MARKERS
        ):
            return NapCatLoginKind.MANUAL_VERIFICATION_REQUIRED
        return NapCatLoginKind.PENDING


@dataclass(frozen=True)
class NapCatWebUIConfig:
    base_url: str
    token: str = field(repr=False)

    @classmethod
    def from_root(cls, root: str | Path) -> "NapCatWebUIConfig":
        path = Path(root).expanduser().resolve() / "config" / "webui.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise NapCatLoginError(
                f"NapCat WebUI configuration is unavailable ({type(exc).__name__})"
            ) from None
        if not isinstance(payload, dict):
            raise NapCatLoginError("NapCat WebUI configuration must be an object")
        if bool(payload.get("disableWebUI", False)):
            raise NapCatLoginError("NapCat WebUI is disabled")

        host = str(payload.get("host", "127.0.0.1")).strip() or "127.0.0.1"
        if host in {"0.0.0.0", "::", "*", "+"}:
            host = "127.0.0.1"
        if host.casefold() != "localhost":
            try:
                if not ipaddress.ip_address(host).is_loopback:
                    raise NapCatLoginError("NapCat Login Guard requires loopback WebUI")
            except ValueError:
                raise NapCatLoginError("NapCat Login Guard requires loopback WebUI") from None

        try:
            port = int(payload.get("port", 6099))
        except (TypeError, ValueError):
            raise NapCatLoginError("NapCat WebUI port is invalid") from None
        if not 1 <= port <= 65535:
            raise NapCatLoginError("NapCat WebUI port is invalid")
        token = payload.get("token")
        if not isinstance(token, str) or not token:
            raise NapCatLoginError("NapCat WebUI token is not configured")

        rendered_host = f"[{host}]" if ":" in host else host
        return cls(base_url=f"http://{rendered_host}:{port}", token=token)


UrlOpener = Callable[..., object]


class NapCatLoginClient:
    """Small authenticated client for only CheckLoginStatus.

    The configured WebUI token, its hash and the short-lived credential stay in
    memory and are never included in exceptions, logs or durable guard state.
    """

    def __init__(
        self,
        config: NapCatWebUIConfig,
        *,
        timeout_seconds: float = 5.0,
        max_response_bytes: int = 64 * 1024,
        opener: UrlOpener = urlopen,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        timeout = float(timeout_seconds)
        if timeout <= 0 or timeout > 10:
            raise ValueError("NapCat WebUI timeout must be > 0 and <= 10 seconds")
        if max_response_bytes < 1024:
            raise ValueError("NapCat max response size must be at least 1024 bytes")
        self.config = config
        self.timeout_seconds = timeout
        self.max_response_bytes = int(max_response_bytes)
        self._opener = opener
        self._clock = clock
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
        if credential is not None:
            headers["Authorization"] = f"Bearer {credential}"
        request = Request(
            f"{self.config.base_url}{path}",
            data=json.dumps(dict(body), separators=(",", ":")).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            response = self._opener(request, timeout=self.timeout_seconds)
            with response:  # type: ignore[attr-defined]
                raw = response.read(self.max_response_bytes + 1)  # type: ignore[attr-defined]
        except (HTTPError, URLError, OSError, TimeoutError) as exc:
            raise NapCatLoginError(
                f"NapCat WebUI request failed ({type(exc).__name__})"
            ) from None
        if len(raw) > self.max_response_bytes:
            raise NapCatLoginError("NapCat WebUI response exceeded size limit")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise NapCatLoginError("NapCat WebUI returned malformed JSON") from None
        if not isinstance(payload, dict):
            raise NapCatLoginError("NapCat WebUI returned a non-object response")
        if payload.get("code") != 0:
            message = str(payload.get("message", ""))
            if "unauthor" in message.casefold() or "token" in message.casefold():
                raise NapCatAuthenticationError("NapCat WebUI authorization failed")
            raise NapCatLoginError("NapCat WebUI API rejected the request")
        return payload.get("data")

    def _authenticate(self) -> str:
        token_hash = hashlib.sha256(
            f"{self.config.token}.napcat".encode("utf-8")
        ).hexdigest()
        data = self._post("/api/auth/login", {"hash": token_hash})
        if not isinstance(data, Mapping):
            raise NapCatAuthenticationError("NapCat WebUI credential was missing")
        credential = data.get("Credential")
        if not isinstance(credential, str) or not credential:
            raise NapCatAuthenticationError("NapCat WebUI credential was missing")
        self._credential = credential
        # NapCat credentials are valid for one hour. Refresh early without
        # persisting the credential or exposing it to callers.
        self._credential_expires_at = self._clock() + 50 * 60
        return credential

    def _active_credential(self) -> str:
        if (
            self._credential is not None
            and self._clock() < self._credential_expires_at
        ):
            return self._credential
        return self._authenticate()

    def check_login_status(self) -> NapCatLoginStatus:
        credential = self._active_credential()
        try:
            data = self._post(
                "/api/QQLogin/CheckLoginStatus",
                {},
                credential=credential,
            )
        except NapCatAuthenticationError:
            self._credential = None
            self._credential_expires_at = 0.0
            data = self._post(
                "/api/QQLogin/CheckLoginStatus",
                {},
                credential=self._active_credential(),
            )
        if not isinstance(data, Mapping):
            raise NapCatLoginError("NapCat login status payload was missing")
        error = data.get("loginError")
        return NapCatLoginStatus(
            is_login=data.get("isLogin") is True,
            is_offline=data.get("isOffline") is True,
            qrcode_available=bool(data.get("qrcodeurl")),
            login_error=str(error)[:500] if error else None,
        )


def check_napcat_login(root: str | Path) -> NapCatLoginStatus:
    return NapCatLoginClient(NapCatWebUIConfig.from_root(root)).check_login_status()


class NapCatLoginProbe:
    """Lazily bind the WebUI client so bad config cannot block Resident start."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self._client: NapCatLoginClient | None = None

    def __call__(self) -> NapCatLoginStatus:
        config = NapCatWebUIConfig.from_root(self.root)
        if self._client is None or self._client.config != config:
            self._client = NapCatLoginClient(config)
        return self._client.check_login_status()


@dataclass
class NapCatGuardState:
    version: int = 1
    phase: str = NapCatLoginKind.UNKNOWN.value
    outage_id: str | None = None
    outage_started_at: float | None = None
    restart_attempted: bool = False
    restart_attempted_at: float | None = None
    recovery_deadline: float | None = None
    manual_notification_attempted: bool = False
    last_checked_at: float | None = None
    last_login_kind: str = NapCatLoginKind.UNKNOWN.value
    last_error: str | None = None


class NapCatGuardStateStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()

    def load(self) -> NapCatGuardState:
        if not self.path.is_file():
            return NapCatGuardState()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("version") != 1:
                return NapCatGuardState()
            allowed = set(NapCatGuardState.__dataclass_fields__)
            selected = {
                key: value for key, value in payload.items() if key in allowed
            }
            return NapCatGuardState(**selected)
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return NapCatGuardState()

    def save(self, state: NapCatGuardState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary.write_text(
            json.dumps(asdict(state), ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, self.path)


_RESTART_TASK_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$taskName = $env:HIKARI_NAPCAT_GUARD_TASK_NAME
$task = Get-ScheduledTask -TaskName $taskName -ErrorAction Stop
if ([string]$task.State -eq 'Running') {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction Stop
    Start-Sleep -Milliseconds 750
}
Start-ScheduledTask -TaskName $taskName -ErrorAction Stop
"""


class WindowsScheduledTaskRestarter:
    """Restart only the configured NapCat task, never Hikari Resident."""

    def __init__(self, task_name: str, *, timeout_seconds: float = 15.0) -> None:
        normalized = str(task_name).strip()
        if not normalized:
            raise ValueError("NapCat task name must not be empty")
        self.task_name = normalized
        self.timeout_seconds = float(timeout_seconds)

    def __call__(self) -> None:
        if platform.system().casefold() != "windows":
            raise RuntimeError("NapCat scheduled-task restart is Windows-only")
        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        if powershell is None:
            raise RuntimeError("PowerShell is unavailable")
        environment = os.environ.copy()
        environment["HIKARI_NAPCAT_GUARD_TASK_NAME"] = self.task_name
        result = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                _RESTART_TASK_SCRIPT,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.timeout_seconds,
            check=False,
            env=environment,
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
        )
        if result.returncode != 0:
            raise RuntimeError("NapCat scheduled-task restart failed")


class ManualVerificationNotifier(Protocol):
    def __call__(self, message: str) -> None: ...


def send_windows_manual_verification_notification(message: str) -> None:
    if platform.system().casefold() != "windows":
        raise RuntimeError("Windows notifications are unavailable")
    try:
        from windows_toasts import Toast, WindowsToaster
    except ImportError:
        raise RuntimeError("Windows notification support is unavailable") from None
    toast = Toast()
    toast.text_fields = ["Hikari", message]
    WindowsToaster("Hikari").show_toast(toast)


class NapCatLoginGuard:
    """One-restart-per-outage QQ login recovery with a durable circuit breaker."""

    def __init__(
        self,
        status_probe: Callable[[], NapCatLoginStatus],
        restarter: Callable[[], None],
        notifier: ManualVerificationNotifier,
        state_store: NapCatGuardStateStore,
        *,
        interval_seconds: float = 45.0,
        recovery_window_seconds: float = 90.0,
        clock: Callable[[], float] = time.time,
        logger: Callable[[str], None] = print,
    ) -> None:
        interval = float(interval_seconds)
        recovery = float(recovery_window_seconds)
        if not 30 <= interval <= 60:
            raise ValueError("NapCat Login Guard interval must be between 30 and 60 seconds")
        if not 30 <= recovery <= 300:
            raise ValueError("NapCat recovery window must be between 30 and 300 seconds")
        self.status_probe = status_probe
        self.restarter = restarter
        self.notifier = notifier
        self.state_store = state_store
        self.interval_seconds = interval
        self.recovery_window_seconds = recovery
        self.clock = clock
        self.logger = logger
        self.state = state_store.load()

    def _save(self) -> None:
        try:
            self.state_store.save(self.state)
        except OSError as exc:
            self.logger(
                "Hikari NapCat Login Guard state write failed: "
                f"{type(exc).__name__}"
            )

    def _begin_outage(self, now: float) -> None:
        if self.state.outage_id is None:
            self.state.outage_id = uuid4().hex
            self.state.outage_started_at = now
            self.state.manual_notification_attempted = False

    def _mark_healthy(self, now: float) -> None:
        recovered = self.state.outage_id is not None
        if (
            not recovered
            and self.state.phase == NapCatLoginKind.HEALTHY.value
            and self.state.last_login_kind == NapCatLoginKind.HEALTHY.value
        ):
            return
        self.state = NapCatGuardState(
            phase=NapCatLoginKind.HEALTHY.value,
            last_checked_at=now,
            last_login_kind=NapCatLoginKind.HEALTHY.value,
        )
        self._save()
        if recovered:
            self.logger("Hikari NapCat Login Guard recovered QQ login")

    def _notify_manual(self, now: float, *, reason: str) -> None:
        self._begin_outage(now)
        self.state.phase = NapCatLoginKind.MANUAL_VERIFICATION_REQUIRED.value
        self.state.recovery_deadline = None
        self.state.last_error = reason
        should_notify = not self.state.manual_notification_attempted
        if should_notify:
            # Persist before the side effect so a notifier crash cannot create a
            # notification storm after Resident restarts.
            self.state.manual_notification_attempted = True
        self._save()
        if not should_notify:
            return
        message = (
            "NapCat 无法自动恢复 QQ 登录，需要人工验证。请打开 "
            f"{MANUAL_VERIFICATION_URL}"
        )
        try:
            self.notifier(message)
            self.logger("Hikari NapCat Login Guard requires manual verification")
        except Exception as exc:
            self.logger(
                "Hikari NapCat Login Guard notification failed: "
                f"{type(exc).__name__}"
            )

    def _record_unknown(self, now: float, exc: Exception) -> None:
        error = type(exc).__name__
        changed = (
            self.state.last_login_kind != NapCatLoginKind.UNKNOWN.value
            or self.state.last_error != error
        )
        self.state.last_checked_at = now
        self.state.last_login_kind = NapCatLoginKind.UNKNOWN.value
        self.state.last_error = error
        if self.state.phase == NapCatLoginKind.HEALTHY.value:
            self.state.phase = NapCatLoginKind.UNKNOWN.value
        if (
            self.state.restart_attempted
            and self.state.recovery_deadline is not None
            and now >= self.state.recovery_deadline
        ):
            self._notify_manual(now, reason="login_status_unavailable_after_restart")
            return
        self._save()
        if changed:
            self.logger(f"Hikari NapCat Login Guard probe unavailable: {error}")

    def cycle_once(self) -> NapCatGuardState:
        now = self.clock()
        try:
            status = self.status_probe()
        except Exception as exc:
            self._record_unknown(now, exc)
            return self.state

        kind = status.kind
        self.state.last_checked_at = now
        self.state.last_login_kind = kind.value
        # Store only the classified condition. NapCat response text is useful to
        # the live doctor but never belongs in this durable circuit-breaker file.
        self.state.last_error = kind.value if status.login_error else None

        if kind is NapCatLoginKind.HEALTHY:
            self._mark_healthy(now)
            return self.state

        if self.state.phase == NapCatLoginKind.MANUAL_VERIFICATION_REQUIRED.value:
            # Manual state is terminal for this outage. Only a subsequently
            # observed healthy login opens the breaker for a future outage.
            self._save()
            return self.state

        if self.state.restart_attempted:
            deadline = self.state.recovery_deadline or now
            if now < deadline:
                self.state.phase = "recovering"
                self._save()
                return self.state
            self._notify_manual(now, reason=kind.value)
            return self.state

        if kind is NapCatLoginKind.LOGIN_INVALID:
            self._begin_outage(now)
            # The durable breaker is closed before restarting. A Resident crash
            # between here and task completion still cannot trigger a second
            # restart for this outage.
            self.state.restart_attempted = True
            self.state.restart_attempted_at = now
            self.state.recovery_deadline = now + self.recovery_window_seconds
            self.state.phase = "recovering"
            self._save()
            try:
                self.restarter()
                self.logger("Hikari NapCat Login Guard restarted NapCat once")
            except Exception as exc:
                self._notify_manual(
                    now,
                    reason=f"restart_failed:{type(exc).__name__}",
                )
            return self.state

        if kind is NapCatLoginKind.MANUAL_VERIFICATION_REQUIRED:
            self._notify_manual(now, reason=kind.value)
            return self.state

        if self.state.outage_id is None:
            self.state.phase = NapCatLoginKind.PENDING.value
        self._save()
        return self.state

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await asyncio.to_thread(self.cycle_once)
            except Exception as exc:
                # This is the final isolation boundary: Guard failure must never
                # tear down Resident or Conversation.
                self.logger(
                    "Hikari NapCat Login Guard cycle failed: "
                    f"{type(exc).__name__}"
                )
            try:
                await asyncio.wait_for(
                    stop_event.wait(), timeout=self.interval_seconds
                )
            except TimeoutError:
                pass


def _bounded_float(
    values: Mapping[str, str],
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    raw = values.get(name)
    try:
        value = default if raw is None or not raw.strip() else float(raw)
    except ValueError:
        raise ValueError(f"{name} must be a number") from None
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum:g} and {maximum:g}")
    return value


def build_napcat_login_guard(
    values: Mapping[str, str],
    *,
    state_dir: str | Path,
) -> NapCatLoginGuard:
    root_text = values.get("HIKARI_NAPCAT_ROOT", "").strip()
    root = Path(root_text).expanduser().resolve() if root_text else DEFAULT_NAPCAT_ROOT
    task_name = values.get(
        "HIKARI_NAPCAT_TASK_NAME", DEFAULT_NAPCAT_TASK_NAME
    ).strip()
    if not task_name:
        raise ValueError("HIKARI_NAPCAT_TASK_NAME must not be empty")
    interval = _bounded_float(
        values,
        "HIKARI_NAPCAT_LOGIN_CHECK_SECONDS",
        45.0,
        minimum=30.0,
        maximum=60.0,
    )
    recovery = _bounded_float(
        values,
        "HIKARI_NAPCAT_LOGIN_RECOVERY_SECONDS",
        90.0,
        minimum=30.0,
        maximum=300.0,
    )
    store = NapCatGuardStateStore(
        Path(state_dir).expanduser().resolve() / "napcat_login_guard.json"
    )
    return NapCatLoginGuard(
        NapCatLoginProbe(root),
        WindowsScheduledTaskRestarter(task_name),
        send_windows_manual_verification_notification,
        store,
        interval_seconds=interval,
        recovery_window_seconds=recovery,
    )
