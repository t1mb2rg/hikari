from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from dashboard.app import create_app
from dashboard.models import ComponentSnapshot, ComponentStatus
from dashboard.napcat_control import NapCatDashboardControl
from dashboard.probes import DashboardProbeConfig, DashboardProbeService


def _config(tmp_path: Path) -> DashboardProbeConfig:
    repository = tmp_path / "hikari"
    repository.mkdir()
    return DashboardProbeConfig(
        repository=repository,
        state_dir=tmp_path / "state",
        napcat_root=tmp_path / "napcat",
    )


def test_qrcode_endpoint_renders_napcat_login_url_as_png(tmp_path: Path, monkeypatch):
    def fake_probe(self):
        return ComponentSnapshot(
            component_id="napcat",
            label="QQ / NapCat",
            status=ComponentStatus.WAITING,
            phase="等待扫码",
            message="等待扫码",
            details={
                "qq_logged_in": False,
                "qrcode_url": "https://example.invalid/qq-login?ticket=test",
            },
        )

    monkeypatch.setattr(DashboardProbeService, "probe_napcat", fake_probe)
    client = TestClient(create_app(_config(tmp_path)))

    response = client.get("/api/napcat/qrcode")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content.startswith(b"\x89PNG\r\n\x1a\n")
    assert "no-store" in response.headers["cache-control"]


def test_qrcode_refresh_calls_only_bounded_napcat_action(tmp_path: Path, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(
        NapCatDashboardControl,
        "refresh_qrcode",
        lambda self: calls.append("refresh"),
    )
    client = TestClient(create_app(_config(tmp_path)))

    response = client.post("/api/napcat/qrcode/refresh")

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert calls == ["refresh"]
