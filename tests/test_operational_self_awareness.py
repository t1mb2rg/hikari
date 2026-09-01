from dataclasses import replace
import json

from core.capabilities import describe_capabilities
from core.delivery import DeliveryOutbox, DeliveryRequest
from core.operational_state import OperationalStateConfig, OperationalStateService
from engineering.heartbeat import (
    EngineeringWorkerHeartbeat,
    EngineeringWorkerHeartbeatStore,
)
from engineering.session import EngineeringAuthority, EngineeringSessionState
from resident.napcat_login_guard import NapCatLoginError, NapCatLoginStatus


class _EngineeringStore:
    def __init__(self, states=()):
        self.states = list(states)

    def list_states(self):
        return list(self.states)


def _service(
    tmp_path,
    *,
    napcat_probe,
    states=(),
    process_probe=None,
    onebot_open=True,
    worker_heartbeat=None,
    delivery_outbox=None,
    wall_time=1000.0,
):
    state_dir = tmp_path / "resident"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "host.json").write_text(
        json.dumps({"pid": 4242, "started_at": "2026-09-01T00:00:00+00:00"}),
        encoding="utf-8",
    )
    heartbeat_store = EngineeringWorkerHeartbeatStore(
        state_dir / "engineering_worker.json"
    )
    if worker_heartbeat is not None:
        heartbeat_store.write(worker_heartbeat)
    return OperationalStateService(
        OperationalStateConfig(
            state_dir=state_dir,
            napcat_root=tmp_path / "napcat",
            cache_seconds=0,
            engineering_worker_stale_seconds=5.0,
        ),
        process_probe=process_probe or (lambda pid: True),
        tcp_probe=lambda host, port, timeout: onebot_open,
        napcat_probe=napcat_probe,
        engineering_store=_EngineeringStore(states),
        heartbeat_store=heartbeat_store,
        delivery_outbox=delivery_outbox,
        wall_clock=lambda: wall_time,
    )


def _healthy_napcat():
    return NapCatLoginStatus(
        is_login=True,
        is_offline=False,
        qrcode_available=False,
    )


def test_operational_snapshot_reports_observed_resident_qq_and_idle_engineering(tmp_path) -> None:
    service = _service(tmp_path, napcat_probe=_healthy_napcat)

    snapshot = service.capture(force=True)

    assert snapshot["overall"] == "healthy"
    assert snapshot["components"]["resident"]["status"] == "healthy"
    assert snapshot["components"]["resident"]["observed"] is True
    assert snapshot["components"]["qq"]["status"] == "healthy"
    assert snapshot["components"]["qq"]["details"]["qq_logged_in"] is True
    engineering = snapshot["components"]["engineering"]
    assert engineering["status"] == "idle"
    assert engineering["details"]["worker_liveness"] == "unknown"
    assert engineering["details"]["worker"]["reason"] == "no_worker_heartbeat"


def test_operational_snapshot_observes_fresh_resident_owned_worker(tmp_path) -> None:
    service = _service(
        tmp_path,
        napcat_probe=_healthy_napcat,
        worker_heartbeat=EngineeringWorkerHeartbeat(
            pid=5151,
            owner="resident",
            started_at=900.0,
            updated_at=999.0,
        ),
    )

    snapshot = service.capture(force=True)
    engineering = snapshot["components"]["engineering"]
    worker = engineering["details"]["worker"]

    assert engineering["status"] == "idle"
    assert engineering["details"]["worker_liveness"] == "healthy"
    assert worker["observed"] is True
    assert worker["owner"] == "resident"
    assert worker["reason"] == "fresh_heartbeat_and_live_pid"


def test_operational_snapshot_degrades_when_live_worker_heartbeat_is_stale(tmp_path) -> None:
    service = _service(
        tmp_path,
        napcat_probe=_healthy_napcat,
        worker_heartbeat=EngineeringWorkerHeartbeat(
            pid=5151,
            owner="resident",
            started_at=900.0,
            updated_at=990.0,
        ),
    )

    snapshot = service.capture(force=True)
    engineering = snapshot["components"]["engineering"]

    assert snapshot["overall"] == "degraded"
    assert engineering["status"] == "warning"
    assert engineering["details"]["worker_liveness"] == "warning"
    assert engineering["details"]["worker"]["reason"] == "heartbeat_stale"


def test_operational_snapshot_reports_dead_worker_pid_offline(tmp_path) -> None:
    service = _service(
        tmp_path,
        napcat_probe=_healthy_napcat,
        process_probe=lambda pid: pid == 4242,
        worker_heartbeat=EngineeringWorkerHeartbeat(
            pid=5151,
            owner="resident",
            started_at=900.0,
            updated_at=999.0,
        ),
    )

    snapshot = service.capture(force=True)
    engineering = snapshot["components"]["engineering"]

    assert snapshot["overall"] == "degraded"
    assert engineering["status"] == "warning"
    assert engineering["details"]["worker_liveness"] == "offline"
    assert engineering["details"]["worker"]["reason"] == "heartbeat_pid_not_running"


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
    state = replace(
        state,
        status="running",
        current_turn_id="turn-1",
        latest_summary="正在运行项目测试",
        updated_at=995.0,
    )
    service = _service(
        tmp_path,
        napcat_probe=_healthy_napcat,
        states=[state],
    )

    snapshot = service.capture(force=True)
    engineering = snapshot["components"]["engineering"]

    assert engineering["status"] == "running"
    assert engineering["phase"] == "testing"
    assert engineering["details"]["active_session_count"] == 1
    assert engineering["details"]["latest_session_status"] == "running"
    assert engineering["details"]["latest_session_phase"] == "testing"
    assert engineering["details"]["latest_progress_at"] is not None
    assert engineering["details"]["worker_liveness"] == "unknown"


def test_operational_snapshot_exposes_terminal_delivery_state_without_message_body(tmp_path) -> None:
    state = EngineeringSessionState.create(
        project_id="hikari",
        repository=tmp_path,
        authority_ceiling=EngineeringAuthority.read_only(),
        session_id="done-session",
    )
    state = replace(
        state,
        status="completed",
        current_turn_id="turn-1",
        latest_summary="sensitive terminal text must not be copied into operational snapshot",
        updated_at=995.0,
    )
    outbox = DeliveryOutbox(tmp_path / "resident" / "proactive_delivery.db")
    outbox.enqueue(
        DeliveryRequest(
            delivery_id="engineering:done-session:turn-1",
            channel="qq",
            recipient="42",
            text="terminal body",
            source="engineering",
        )
    )
    service = _service(
        tmp_path,
        napcat_probe=_healthy_napcat,
        states=[state],
        delivery_outbox=outbox,
    )

    snapshot = service.capture(force=True)
    engineering = snapshot["components"]["engineering"]

    assert engineering["details"]["latest_session_phase"] == "completed"
    assert engineering["details"]["latest_delivery_state"] == "pending"
    encoded = json.dumps(snapshot)
    assert "terminal body" not in encoded
    assert "sensitive terminal text" not in encoded


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
    assert capabilities["self_state"]["development"]["active_slice"] == "M7-07"
