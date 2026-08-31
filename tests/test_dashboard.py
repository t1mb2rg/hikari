from __future__ import annotations

import json
from pathlib import Path
import time

import pytest

from dashboard.app import DEFAULT_DASHBOARD_PORT, _require_loopback, build_parser
from dashboard.models import ComponentStatus
from dashboard.probes import DashboardProbeConfig, DashboardProbeService


def _service(tmp_path: Path, *, stale_seconds: float = 180.0) -> DashboardProbeService:
    repository = tmp_path / "hikari"
    repository.mkdir()
    return DashboardProbeService(
        DashboardProbeConfig(
            repository=repository,
            state_dir=tmp_path / "resident-state",
            napcat_root=tmp_path / "napcat",
            forge_stale_seconds=stale_seconds,
        )
    )


def _forge_state(service: DashboardProbeService, payload: dict[str, object]) -> Path:
    state_file = service.forge_run_root / "task-1" / "control" / "state.json"
    state_file.parent.mkdir(parents=True)
    state_file.write_text(json.dumps(payload), encoding="utf-8")
    return state_file


def test_forge_probe_surfaces_current_verification_blocker(tmp_path: Path):
    service = _service(tmp_path)
    _forge_state(
        service,
        {
            "phase": "VERIFYING",
            "outcome": None,
            "attempt": 1,
            "max_attempts": 3,
            "worktree": str(tmp_path / "worktree"),
            "updated_at": time.time(),
        },
    )

    snapshot = service.probe_forge()

    assert snapshot.status is ComponentStatus.RUNNING
    assert snapshot.phase == "验证中"
    assert snapshot.blocking_on == "项目测试"
    assert snapshot.details["attempt"] == 1


def test_forge_probe_marks_stale_running_state_as_warning(tmp_path: Path):
    service = _service(tmp_path, stale_seconds=10)
    _forge_state(
        service,
        {
            "phase": "VERIFYING",
            "outcome": None,
            "updated_at": time.time() - 30,
        },
    )

    snapshot = service.probe_forge()

    assert snapshot.status is ComponentStatus.WARNING
    assert snapshot.last_error == "可能停滞"
    assert snapshot.blocking_on == "项目测试"


def test_resident_probe_is_offline_without_host_state(tmp_path: Path):
    service = _service(tmp_path)

    snapshot = service.probe_resident()

    assert snapshot.status is ComponentStatus.OFFLINE
    assert snapshot.phase == "已停止"


def test_recent_events_redact_token_like_values(tmp_path: Path):
    service = _service(tmp_path)
    service.config.state_dir.mkdir(parents=True)
    (service.config.state_dir / "resident.log").write_text(
        "normal line\nAuthorization: Bearer top-secret\ntoken=another-secret failed\n",
        encoding="utf-8",
    )

    events = service.recent_events(limit=10)
    rendered = "\n".join(event["summary"] for event in events)

    assert "top-secret" not in rendered
    assert "another-secret" not in rendered
    assert "<redacted>" in rendered


def test_dashboard_v01_rejects_non_loopback_bind():
    _require_loopback("127.0.0.1")
    _require_loopback("localhost")
    with pytest.raises(ValueError, match="loopback"):
        _require_loopback("0.0.0.0")


def test_dashboard_default_port_does_not_overlap_resident_port():
    args = build_parser().parse_args([])

    assert args.port == DEFAULT_DASHBOARD_PORT == 8787
    assert args.port != 8765
