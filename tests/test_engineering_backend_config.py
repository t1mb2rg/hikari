from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from conversation.engineering_bridge import (
    engineering_requirements_for_intent,
    looks_like_engineering_status_query,
)
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
    assert seen["timeout"] == 300.0
    assert result.final_message == "done"


def test_engineering_backend_config_is_explicit_without_vendor_lock_in() -> None:
    config = EngineeringBackendConfig.from_mapping(
        {
            "HIKARI_ENGINEERING_CLAUDE_EXECUTABLE": "claude-custom",
            "HIKARI_ENGINEERING_MODEL": "deepseek-v4-pro[1m]",
            "HIKARI_ENGINEERING_BACKEND_TIMEOUT_SECONDS": "42",
            "HIKARI_ENGINEERING_MAX_TURNS": "7",
        }
    )

    assert config.executable == "claude-custom"
    assert config.model == "deepseek-v4-pro[1m]"
    assert config.timeout_seconds == 42
    assert config.max_turns == 7


def test_engineering_backend_uses_explicit_hikari_model(monkeypatch) -> None:
    monkeypatch.setenv("HIKARI_ENGINEERING_MODEL", "my-routed-engineering-model")

    backend = ClaudeEngineeringBackend()

    assert backend.model == "my-routed-engineering-model"


def test_engineering_backend_config_validates_deadline() -> None:
    with pytest.raises(ValueError, match="HIKARI_ENGINEERING_BACKEND_TIMEOUT_SECONDS"):
        EngineeringBackendConfig.from_mapping(
            {
                "HIKARI_ENGINEERING_MODEL": "sonnet",
                "HIKARI_ENGINEERING_BACKEND_TIMEOUT_SECONDS": "0",
            }
        )


def test_backend_reports_missing_cli_as_grounded_agent_failure(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("engineering.backend.shutil.which", lambda executable: None)
    backend = ClaudeEngineeringBackend(
        executable="definitely-not-a-real-claude-binary",
        model="owned-model",
        timeout_seconds=10,
    )

    result = backend.run(tmp_path, "inspect")

    assert result.returncode == 127
    assert "[claude-code:cli_not_found]" in result.stderr
    assert "RuntimeError" not in result.stderr


def test_backend_reports_deadline_as_grounded_agent_failure(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("engineering.backend.shutil.which", lambda executable: "claude")

    def fake_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="claude", timeout=3)

    monkeypatch.setattr("engineering.backend.subprocess.run", fake_timeout)
    backend = ClaudeEngineeringBackend(
        executable="claude",
        model="owned-model",
        timeout_seconds=3,
    )

    result = backend.run(tmp_path, "inspect")

    assert result.returncode == 124
    assert "[claude-code:timeout]" in result.stderr
    assert "3s deadline" in result.stderr


def test_status_words_inside_a_write_task_do_not_turn_it_into_status_query() -> None:
    text = (
        "Hikari，在 README 里修改一行，写成："
        "Engineering 现在状态以持久化结果为准，不由对话模型推测。"
    )

    assert looks_like_engineering_status_query(text) is False
    requirements = engineering_requirements_for_intent(text)
    assert requirements is not None
    assert "engineering.repository.write" in requirements
