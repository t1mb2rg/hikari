from dataclasses import replace
import json

from core.capabilities import describe_capabilities
from core.operational_state import OperationalStateConfig, OperationalStateService
from engineering.session import EngineeringAuthority, EngineeringSessionState
from resident.napcat_login_guard import NapCatLoginError, NapCatLoginStatus


class _EngineeringStore:
    def __init__(self, states=()):
        self.states = list(states)

    def list_states(self):
        return list(self.states)


def _service(tmp_path, *, napcat_probe, states=(), process_alive=True, onebot_open=True):
    state_dir = tmp_path / "resident"
    state_dir.mkdir(parents=True)
    (state_dir / "host.json").write_text(
        json.dumps({"pid": 4242, "started_at": "2026-09-01T00:00:00+00:00"}),
        encoding="utf-8",
    )
    return OperationalStateService(
        OperationalStateConfig(
            state_dir=state_dir,
            napcat_root=tmp_path / "napcat",
            cache_seconds=0,
        ),
        process_probe=lambda pid: process_alive,
        tcp_probe=lambda host, port, timeout: onebot_open,
        napcat_probe=napcat_probe,
        engineering_store=_EngineeringStore(states),
    )


def test_operational_snapshot_reports_observed_resident_qq_and_idle_engineering(tmp_path) -> None:
    service = _service(
        tmp_path,
        napcat_probe=lambda: NapCatLoginStatus(
            is_login=True,
            is_offline=False,
            qrcode_available=False,
        ),
    )

    snapshot = service.capture(force=True)

    assert snapshot["overall"] == "healthy"
    assert snapshot["components"]["resident"]["status"] == "healthy"
    assert snapshot["components"]["resident"]["observed"] is True
    assert snapshot["components"]["qq"]["status"] == "healthy"
    assert snapshot["components"]["qq"]["details"]["qq_logged_in"] is True
    engineering = snapshot["components"]["engineering"]
    assert engineering["status"] == "idle"
    assert engineering["details"]["worker_liveness"] == "unknown"
    assert engineering["details"]["worker_liveness_reason"] == "no_worker_heartbeat_probe_yet"


def test_operational_snapshot_keeps_failed_qq_probe_unknown(tmp_path) -> None:
    def fail_probe():
        raise NapCatLoginError("synthetic failure")

    service = _service(tmp_path, napcat_probe=fail_probe)
    snapshot = service.capture(force=True)

    qq = snapshot["components"]["qq"]
    assert qq["status"] == "unknown"
    assert qq["observed"] is False
    assert "synthetic failure" not in json.dumps(snapshot)


def test_operational_snapshot_reports_active_engineering_session_without_inventing_worker_liveness(tmp_path) -> None:
    state = EngineeringSessionState.create(
        project_id="hikari",
        repository=tmp_path,
        authority_ceiling=EngineeringAuthority.read_only(),
    )
    state = replace(state, status="running", current_turn_id="turn-1")
    service = _service(
        tmp_path,
        napcat_probe=lambda: NapCatLoginStatus(
            is_login=True,
            is_offline=False,
            qrcode_available=False,
        ),
        states=[state],
    )

    snapshot = service.capture(force=True)
    engineering = snapshot["components"]["engineering"]

    assert engineering["status"] == "running"
    assert engineering["details"]["active_session_count"] == 1
    assert engineering["details"]["latest_session_status"] == "running"
    assert engineering["details"]["worker_liveness"] == "unknown"


def test_capability_grounding_accepts_explicit_operational_snapshot_without_host_probe() -> None:
    operational = {
        "version": 1,
        "overall": "degraded",
        "components": {
            "qq": {
                "status": "waiting",
                "observed": True,
                "phase": "login_required",
                "message": "QQ login required",
                "details": {},
            }
        },
        "epistemic_rule": "unknown stays unknown",
    }

    capabilities = describe_capabilities(
        {"HIKARI_ENGINEERING_ENABLED": "true"},
        operational_state=operational,
    )

    assert capabilities["operational_state"]["overall"] == "degraded"
    assert capabilities["operational_state"]["components"]["qq"]["status"] == "waiting"
    assert capabilities["self_state"]["development"]["active_slice"] == "M7-06"
