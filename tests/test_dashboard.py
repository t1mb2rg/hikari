from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from dashboard.app import DEFAULT_DASHBOARD_PORT, _require_loopback, build_parser
from dashboard.models import ComponentStatus
from dashboard.probes import DashboardProbeConfig, DashboardProbeService
from engineering.session import (
    EngineeringAuthority,
    EngineeringSessionState,
    EngineeringSessionStore,
)


def _service(tmp_path: Path) -> DashboardProbeService:
    repository = tmp_path / "hikari"
    repository.mkdir()
    return DashboardProbeService(
        DashboardProbeConfig(
            repository=repository,
            state_dir=tmp_path / "resident-state",
            napcat_root=tmp_path / "napcat",
        )
    )


def test_engineering_probe_is_idle_without_session(tmp_path: Path):
    service = _service(tmp_path)

    snapshot = service.probe_engineering()

    assert snapshot.status is ComponentStatus.IDLE
    assert snapshot.label == "Engineering Runtime"
    assert snapshot.phase == "空闲"


def test_engineering_probe_surfaces_running_test_phase(tmp_path: Path):
    service = _service(tmp_path)
    store = EngineeringSessionStore(service.config.state_dir / "engineering")
    state = EngineeringSessionState.create(
        project_id="hikari",
        repository=service.config.repository,
        authority_ceiling=EngineeringAuthority.read_only(),
    )
    state = replace(
        state,
        status="running",
        latest_summary="正在运行项目测试。",
    )
    store.create(state)

    snapshot = service.probe_engineering()

    assert snapshot.status is ComponentStatus.RUNNING
    assert snapshot.label == "Engineering Runtime"
    assert snapshot.phase == "测试中"
    assert snapshot.details["session_id"] == state.session_id


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
