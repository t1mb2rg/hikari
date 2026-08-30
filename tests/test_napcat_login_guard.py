from __future__ import annotations

from collections.abc import Callable
import hashlib
import io
import json
from pathlib import Path
import subprocess
from urllib.request import Request

import pytest

from resident.napcat_login_guard import (
    _RESTART_TASK_SCRIPT,
    NapCatGuardStateStore,
    NapCatLoginClient,
    NapCatLoginGuard,
    NapCatLoginKind,
    NapCatLoginStatus,
    NapCatWebUIConfig,
    WindowsScheduledTaskRestarter,
    build_napcat_login_guard,
)


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class _Opener:
    def __init__(self, payloads: list[dict[str, object]]) -> None:
        self.payloads = list(payloads)
        self.requests: list[Request] = []

    def __call__(self, request: Request, *, timeout: float):
        assert timeout <= 10
        self.requests.append(request)
        return _Response(json.dumps(self.payloads.pop(0)).encode("utf-8"))


def _status(
    kind: NapCatLoginKind,
) -> NapCatLoginStatus:
    if kind is NapCatLoginKind.HEALTHY:
        return NapCatLoginStatus(True, False, False)
    if kind is NapCatLoginKind.LOGIN_INVALID:
        return NapCatLoginStatus(
            False,
            False,
            False,
            "[KickedOffLine] 当前登录已失效，请重新登录",
        )
    if kind is NapCatLoginKind.MANUAL_VERIFICATION_REQUIRED:
        return NapCatLoginStatus(False, False, True, "需要新设备扫码验证")
    return NapCatLoginStatus(False, False, False)


def _guard(
    tmp_path: Path,
    probe: Callable[[], NapCatLoginStatus],
    restarts: list[str],
    notifications: list[str],
    now: list[float],
) -> NapCatLoginGuard:
    return NapCatLoginGuard(
        probe,
        lambda: restarts.append("restart"),
        notifications.append,
        NapCatGuardStateStore(tmp_path / "guard.json"),
        interval_seconds=45,
        recovery_window_seconds=90,
        clock=lambda: now[0],
        logger=lambda _: None,
    )


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (NapCatLoginStatus(True, False, True, "旧错误"), NapCatLoginKind.HEALTHY),
        (
            NapCatLoginStatus(False, False, True, "KickedOffLine 登录已失效"),
            NapCatLoginKind.LOGIN_INVALID,
        ),
        (
            NapCatLoginStatus(False, False, True, "需要新设备扫码验证"),
            NapCatLoginKind.MANUAL_VERIFICATION_REQUIRED,
        ),
        (NapCatLoginStatus(False, False, False), NapCatLoginKind.PENDING),
    ],
)
def test_login_status_classification(
    status: NapCatLoginStatus,
    expected: NapCatLoginKind,
):
    assert status.kind is expected


def test_client_authenticates_and_checks_real_login_without_persisting_token():
    opener = _Opener(
        [
            {"code": 0, "data": {"Credential": "short-lived"}, "message": "success"},
            {
                "code": 0,
                "data": {
                    "isLogin": True,
                    "isOffline": False,
                    "qrcodeurl": "",
                    "loginError": "",
                },
                "message": "success",
            },
        ]
    )
    config = NapCatWebUIConfig("http://127.0.0.1:6099", "webui-secret")
    client = NapCatLoginClient(config, opener=opener)

    status = client.check_login_status()

    assert status.kind is NapCatLoginKind.HEALTHY
    assert len(opener.requests) == 2
    login_body = json.loads(opener.requests[0].data or b"{}")
    assert login_body == {
        "hash": hashlib.sha256(b"webui-secret.napcat").hexdigest()
    }
    assert opener.requests[1].headers["Authorization"] == "Bearer short-lived"
    assert "webui-secret" not in repr(config)


def test_same_outage_restarts_once_even_across_guard_restart(tmp_path: Path):
    restarts: list[str] = []
    notifications: list[str] = []
    now = [1000.0]
    probe = lambda: _status(NapCatLoginKind.LOGIN_INVALID)
    guard = _guard(tmp_path, probe, restarts, notifications, now)

    first = guard.cycle_once()
    guard.cycle_once()
    replacement = _guard(tmp_path, probe, restarts, notifications, now)
    replacement.cycle_once()

    assert first.phase == "recovering"
    assert first.restart_attempted is True
    assert restarts == ["restart"]
    assert notifications == []


def test_failed_quick_login_becomes_manual_once_after_recovery_window(
    tmp_path: Path,
):
    restarts: list[str] = []
    notifications: list[str] = []
    now = [1000.0]
    current = [NapCatLoginKind.LOGIN_INVALID]
    guard = _guard(
        tmp_path,
        lambda: _status(current[0]),
        restarts,
        notifications,
        now,
    )

    guard.cycle_once()
    current[0] = NapCatLoginKind.MANUAL_VERIFICATION_REQUIRED
    now[0] = 1089.0
    assert guard.cycle_once().phase == "recovering"
    now[0] = 1090.0
    state = guard.cycle_once()
    guard.cycle_once()

    assert state.phase == NapCatLoginKind.MANUAL_VERIFICATION_REQUIRED.value
    assert restarts == ["restart"]
    assert len(notifications) == 1
    assert "http://127.0.0.1:6099/webui/" in notifications[0]


def test_recovery_resets_breaker_for_a_later_outage(tmp_path: Path):
    restarts: list[str] = []
    notifications: list[str] = []
    now = [1000.0]
    current = [NapCatLoginKind.LOGIN_INVALID]
    guard = _guard(
        tmp_path,
        lambda: _status(current[0]),
        restarts,
        notifications,
        now,
    )

    guard.cycle_once()
    current[0] = NapCatLoginKind.HEALTHY
    now[0] = 1010.0
    healthy_phase = guard.cycle_once().phase
    current[0] = NapCatLoginKind.LOGIN_INVALID
    now[0] = 2000.0
    guard.cycle_once()

    assert healthy_phase == NapCatLoginKind.HEALTHY.value
    assert restarts == ["restart", "restart"]


def test_probe_restart_and_notification_failures_are_bounded(tmp_path: Path):
    now = [1000.0]
    logs: list[str] = []
    attempts = {"restart": 0, "notify": 0}

    def restart() -> None:
        attempts["restart"] += 1
        raise RuntimeError("must not escape")

    def notify(_: str) -> None:
        attempts["notify"] += 1
        raise RuntimeError("must not escape")

    guard = NapCatLoginGuard(
        lambda: _status(NapCatLoginKind.LOGIN_INVALID),
        restart,
        notify,
        NapCatGuardStateStore(tmp_path / "guard.json"),
        clock=lambda: now[0],
        logger=logs.append,
    )

    state = guard.cycle_once()
    guard.cycle_once()

    assert state.phase == NapCatLoginKind.MANUAL_VERIFICATION_REQUIRED.value
    assert attempts == {"restart": 1, "notify": 1}
    assert "must not escape" not in json.dumps(state.__dict__)


def test_unknown_probe_does_not_restart_healthy_napcat(tmp_path: Path):
    restarts: list[str] = []
    notifications: list[str] = []

    def fail() -> NapCatLoginStatus:
        raise TimeoutError("transport only")

    guard = _guard(tmp_path, fail, restarts, notifications, [1000.0])

    state = guard.cycle_once()

    assert state.last_login_kind == NapCatLoginKind.UNKNOWN.value
    assert restarts == []
    assert notifications == []


def test_healthy_and_pending_checks_have_no_recovery_side_effects(tmp_path: Path):
    for kind in (NapCatLoginKind.HEALTHY, NapCatLoginKind.PENDING):
        restarts: list[str] = []
        notifications: list[str] = []
        guard = _guard(
            tmp_path / kind.value,
            lambda selected=kind: _status(selected),
            restarts,
            notifications,
            [1000.0],
        )

        guard.cycle_once()

        assert restarts == []
        assert notifications == []


def test_manual_state_never_restarts_until_login_is_healthy(tmp_path: Path):
    restarts: list[str] = []
    notifications: list[str] = []
    now = [1000.0]
    current = [NapCatLoginKind.MANUAL_VERIFICATION_REQUIRED]
    guard = _guard(
        tmp_path,
        lambda: _status(current[0]),
        restarts,
        notifications,
        now,
    )

    guard.cycle_once()
    current[0] = NapCatLoginKind.LOGIN_INVALID
    now[0] = 2000.0
    guard.cycle_once()

    assert restarts == []
    assert len(notifications) == 1


def test_guard_build_is_lazy_when_napcat_config_is_unavailable(tmp_path: Path):
    guard = build_napcat_login_guard(
        {"HIKARI_NAPCAT_ROOT": str(tmp_path / "missing")},
        state_dir=tmp_path / "state",
    )

    state = guard.cycle_once()

    assert state.last_login_kind == NapCatLoginKind.UNKNOWN.value
    assert state.restart_attempted is False


def test_guard_state_never_persists_napcat_response_secrets(tmp_path: Path):
    guard = NapCatLoginGuard(
        lambda: NapCatLoginStatus(
            False,
            False,
            True,
            "captcha required webui_token=must-not-leak",
        ),
        lambda: None,
        lambda _: None,
        NapCatGuardStateStore(tmp_path / "guard.json"),
        logger=lambda _: None,
    )

    guard.cycle_once()

    assert "must-not-leak" not in (tmp_path / "guard.json").read_text("utf-8")


def test_task_restarter_targets_only_exact_napcat_launcher_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[tuple[list[str], dict[str, object]]] = []

    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((list(argv), dict(kwargs)))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr("resident.napcat_login_guard.platform.system", lambda: "Windows")
    monkeypatch.setattr(
        "resident.napcat_login_guard.shutil.which",
        lambda _: "powershell.exe",
    )
    launcher = tmp_path / "NapCatWinBootMain.exe"
    restarter = WindowsScheduledTaskRestarter(
        "Hikari NapCat Shell",
        launcher,
        runner=run,
    )

    restarter()

    assert len(calls) == 1
    environment = calls[0][1]["env"]
    assert isinstance(environment, dict)
    assert environment["HIKARI_NAPCAT_GUARD_TASK_NAME"] == "Hikari NapCat Shell"
    assert environment["HIKARI_NAPCAT_GUARD_LAUNCHER_PATH"] == str(
        launcher.resolve()
    )
    assert "D:\\QQ.exe" not in _RESTART_TASK_SCRIPT
