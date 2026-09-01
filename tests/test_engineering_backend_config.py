from pathlib import Path
import subprocess

import pytest

from engineering.backend import ClaudeEngineeringBackend
from engineering.config import EngineeringBackendConfig


def test_engineering_backend_ignores_unrelated_ambient_model(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("HIKARI_ENGINEERING_MODEL", raising=False)
    monkeypatch.setenv("ANTHROPIC_MODEL", "deepseek-v4-pro[1m]")
    monkeypatch.setattr("engineering.backend.shutil.which", lambda executable: "claude")
    seen: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = list(argv)
        seen["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout='{"session_id":"s1","result":"done"}',
            stderr="",
        )

    monkeypatch.setattr("engineering.backend.subprocess.run", fake_run)
    result = ClaudeEngineeringBackend().run(tmp_path, "inspect")

    argv = seen["argv"]
    assert isinstance(argv, list)
    index = argv.index("--model")
    assert argv[index + 1] == "sonnet"
    assert "deepseek-v4-pro[1m]" not in argv
    assert seen["timeout"] == 600.0
    assert result.final_message == "done"


def test_engineering_backend_uses_explicit_hikari_model(monkeypatch) -> None:
    monkeypatch.setenv("HIKARI_ENGINEERING_MODEL", "claude-sonnet-4-5")

    backend = ClaudeEngineeringBackend()

    assert backend.model == "claude-sonnet-4-5"


def test_invalid_engineering_model_fails_before_claude_process(monkeypatch) -> None:
    monkeypatch.setenv("HIKARI_ENGINEERING_MODEL", "deepseek-v4-pro[1m]")

    with pytest.raises(ValueError, match="HIKARI_ENGINEERING_MODEL"):
        ClaudeEngineeringBackend()


def test_engineering_backend_config_validates_deadline() -> None:
    with pytest.raises(ValueError, match="HIKARI_ENGINEERING_BACKEND_TIMEOUT_SECONDS"):
        EngineeringBackendConfig.from_mapping(
            {
                "HIKARI_ENGINEERING_MODEL": "sonnet",
                "HIKARI_ENGINEERING_BACKEND_TIMEOUT_SECONDS": "0",
            }
        )
