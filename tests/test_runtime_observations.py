import json
from pathlib import Path

import pytest

from brain.providers.observed import ObservedChatProvider
from resident.telemetry import read_observation
from resident.telemetry import record_observation
from dashboard.operations import DashboardOperations
from dashboard.settings import DashboardSettings


class Provider:
    def complete(self, messages):
        return "private reply"


def test_model_observation_records_results_without_conversation_content(tmp_path: Path):
    provider = ObservedChatProvider(Provider(), tmp_path, model="configured-model")
    assert provider.complete(["private prompt"]) == "private reply"
    observed = read_observation(tmp_path, "model")
    assert observed["status"] == "healthy"
    assert observed["details"]["last_success_at"] > 0
    assert "private prompt" not in json.dumps(observed)
    assert "private reply" not in json.dumps(observed)
    data = DashboardOperations(tmp_path, DashboardSettings(tmp_path / ".env")).snapshot()
    assert data["model"]["status"] == "healthy"


def test_model_failure_preserves_exception_and_records_no_secret_text(tmp_path: Path):
    class Broken:
        def complete(self, messages):
            raise RuntimeError("secret-bearing-provider-error")
    provider = ObservedChatProvider(Broken(), tmp_path, model="configured-model")
    with pytest.raises(RuntimeError, match="secret-bearing"):
        provider.complete([])
    observed = read_observation(tmp_path, "model")
    assert observed["status"] == "error"
    assert observed["details"]["error_type"] == "RuntimeError"
    assert "secret-bearing" not in json.dumps(observed)


def test_quiet_qq_does_not_expire_before_configured_observation_interval(tmp_path: Path):
    import time
    record_observation(tmp_path, "qq", "healthy", connected=True, observation_ttl_seconds=150)
    path = tmp_path / "observations" / "qq.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["observed_at"] = time.time() - 65
    path.write_text(json.dumps(data), encoding="utf-8")
    result = DashboardOperations(tmp_path, DashboardSettings(tmp_path / ".env")).snapshot()
    assert result["qq"]["status"] == "healthy"
    data["observed_at"] = time.time() - 160
    path.write_text(json.dumps(data), encoding="utf-8")
    assert DashboardOperations(tmp_path, DashboardSettings(tmp_path / ".env")).snapshot()["qq"]["status"] == "unknown"
