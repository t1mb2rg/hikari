import json
from pathlib import Path

import pytest

from brain.providers.observed import ObservedChatProvider
from resident.telemetry import read_observation
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
