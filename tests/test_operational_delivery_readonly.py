from dataclasses import replace

from core.operational_state import OperationalStateConfig, OperationalStateService
from engineering.session import EngineeringAuthority, EngineeringSessionState
from resident.napcat_login_guard import NapCatLoginStatus


class _EngineeringStore:
    def __init__(self, state):
        self.state = state

    def list_states(self):
        return [self.state]


def test_terminal_delivery_probe_does_not_create_outbox_database(tmp_path) -> None:
    state_dir = tmp_path / "resident"
    state_dir.mkdir()
    (state_dir / "host.json").write_text('{"pid": 1}', encoding="utf-8")
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
        updated_at=1000.0,
    )
    service = OperationalStateService(
        OperationalStateConfig(state_dir=state_dir, cache_seconds=0),
        process_probe=lambda pid: True,
        tcp_probe=lambda host, port, timeout: True,
        napcat_probe=lambda: NapCatLoginStatus(
            is_login=True,
            is_offline=False,
            qrcode_available=False,
        ),
        engineering_store=_EngineeringStore(state),
    )

    database = state_dir / "proactive_delivery.db"
    assert not database.exists()

    snapshot = service.capture(force=True)

    assert snapshot["components"]["engineering"]["details"]["latest_delivery_state"] == "not_enqueued"
    assert not database.exists()
