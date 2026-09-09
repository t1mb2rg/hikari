from dataclasses import replace
import json
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from conversation.receipts import ConversationReceiptStore
from conversation.models import UserTurn, AssistantReply
from dashboard.app import create_app
from dashboard.operations import DashboardOperations
from dashboard.probes import DashboardProbeConfig
from dashboard.settings import DashboardSettings, SettingsConflict
from engineering.goal import EngineeringGoalStore, EngineeringGoalState, EngineeringGoalStep


def test_settings_secrets_are_write_only_and_conflicting_writes_fail(tmp_path: Path):
    path = tmp_path / ".env"
    path.write_text('# keep me\nHIKARI_MODEL_API_KEY="old-secret"\nUNKNOWN_FIELD=keep\n', encoding="utf-8")
    settings = DashboardSettings(path)
    initial = settings.snapshot()
    assert "old-secret" not in json.dumps(initial)
    response = settings.save({"HIKARI_MODEL_API_KEY": "new-secret", "HIKARI_MODEL_NAME": "new-model"}, initial["revision"])
    assert "new-secret" not in json.dumps(response)
    assert settings.values()["HIKARI_MODEL_API_KEY"] == "new-secret"
    assert '# keep me' in path.read_text() and 'UNKNOWN_FIELD=keep' in path.read_text()
    assert response["restart_required"] and not response["runtime_applied"]
    with pytest.raises(SettingsConflict):
        settings.save({"HIKARI_MODEL_NAME": "stale"}, initial["revision"])


@pytest.mark.parametrize("changes", [
    {"HIKARI_MODEL_NAME": "injected\nEVIL=true"},
    {"PATH": "untrusted"}, {"HIKARI_QQ_ENABLED": "maybe"},
    {"HIKARI_MODEL_BASE_URL": "https://user:password@example.com"},
    {"HIKARI_ENGINEERING_MAX_TURNS": "NaN"},
])
def test_invalid_settings_do_not_write(tmp_path: Path, changes):
    settings = DashboardSettings(tmp_path / ".env")
    with pytest.raises(ValueError):
        settings.save(changes, settings.snapshot()["revision"])
    assert not settings.path.exists()


def test_settings_api_requires_local_origin_and_custom_header(tmp_path: Path):
    client = TestClient(create_app(DashboardProbeConfig(tmp_path, tmp_path / "state")), base_url="http://127.0.0.1")
    data = {"revision": client.get("/api/settings").json()["revision"], "changes": {"HIKARI_MODEL_NAME": "configured"}}
    assert client.put("/api/settings", json=data).status_code == 403
    assert client.put("/api/settings", json=data, headers={"X-Hikari-Action": "dashboard", "Origin": "https://evil.invalid"}).status_code == 403
    assert client.put("/api/settings", json=data, headers={"X-Hikari-Action": "dashboard"}).status_code == 200
    assert client.get("/api/settings", headers={"Host": "evil.invalid"}).status_code == 400


def test_dashboard_read_does_not_create_or_migrate_state(tmp_path: Path):
    state = tmp_path / "state"
    ops = DashboardOperations(state, DashboardSettings(tmp_path / ".env"))
    snapshot = ops.snapshot()
    assert snapshot["worker"]["status"] == "unknown"
    assert snapshot["model"]["status"] == "unknown"
    assert not state.exists()
    state.mkdir()
    # Legacy receipt DB lacks the new claim table. Snapshot must remain read-only.
    import sqlite3
    path = state / "conversation_receipts.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE conversation_receipts(request_id,channel,conversation_id,user_text,reply_text,created_at)")
    before = path.read_bytes()
    ops.snapshot()
    assert path.read_bytes() == before


def test_dashboard_exposes_durable_goal_and_receipt_without_hiding_corruption(tmp_path: Path):
    state = tmp_path / "state"
    store = EngineeringGoalStore(state / "engineering_goals")
    goal = EngineeringGoalState.create(project_id="hikari", session_id="s", goal="real goal",
        steps=(EngineeringGoalStep.create(effect="inspect_project", instruction="inspect"),))
    store.create(goal)
    receipts = ConversationReceiptStore(state / "conversation_receipts.db")
    turn = UserTurn("qq", "private:42", "request")
    receipts.claim("r", turn)
    receipts.save("r", turn, AssistantReply("qq", "private:42", "accepted"))
    ops = DashboardOperations(state, DashboardSettings(tmp_path / ".env"))
    data = ops.snapshot()
    assert data["goals"][0]["goal"] == "real goal"
    assert data["claims"][0]["state"] == "completed"
    assert data["receipts"][0]["request_id"] == "r"
    (store.root / "broken.json").write_text("{broken", encoding="utf-8")
    data = ops.snapshot()
    assert data["complete"] is False
    assert any(error["source"] == "goals" for error in data["errors"])
