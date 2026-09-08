from __future__ import annotations

from io import StringIO
from pathlib import Path
import json
import subprocess

import pytest

from conversation.engineering_bridge import (
    engineering_requirements_for_intent,
    looks_like_engineering_status_query,
)
from engineering.backend import ClaudeEngineeringBackend
from engineering.config import EngineeringBackendConfig


class _FakePopen:
    def __init__(
        self,
        argv,
        *,
        stdout_lines: list[str],
        stderr_text: str = "",
        returncode: int = 0,
        wait_timeout: bool = False,
        **_kwargs,
    ) -> None:
        self.argv = list(argv)
        self.stdin = StringIO()
        self.stdout = StringIO("".join(stdout_lines))
        self.stderr = StringIO(stderr_text)
        self._returncode = returncode
        self._wait_timeout = wait_timeout
        self.killed = False

    def wait(self, timeout=None):
        if self._wait_timeout and not self.killed:
            raise subprocess.TimeoutExpired(cmd=self.argv, timeout=timeout)
        return self._returncode if not self.killed else -9

    def kill(self):
        self.killed = True


def _stream(*payloads: dict[str, object]) -> list[str]:
    return [json.dumps(payload) + "\n" for payload in payloads]


def test_engineering_backend_ignores_unrelated_ambient_model(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("HIKARI_ENGINEERING_MODEL", raising=False)
    monkeypatch.setenv("ANTHROPIC_MODEL", "deepseek-v4-pro[1m]")
    monkeypatch.setattr("engineering.backend.shutil.which", lambda executable: "claude")
    seen: dict[str, object] = {}

    def fake_popen(argv, **kwargs):
        seen["argv"] = list(argv)
        proc = _FakePopen(
            argv,
            stdout_lines=_stream(
                {"type": "system", "subtype": "init", "session_id": "s1", "model": "sonnet"},
                {"type": "result", "subtype": "success", "session_id": "s1", "is_error": False, "result": "done"},
            ),
            **kwargs,
        )
        original_wait = proc.wait

        def wait(timeout=None):
            seen["timeout"] = timeout
            return original_wait(timeout)

        proc.wait = wait  # type: ignore[method-assign]
        return proc

    monkeypatch.setattr("engineering.backend.subprocess.Popen", fake_popen)
    result = ClaudeEngineeringBackend().run(tmp_path, "inspect")

    argv = seen["argv"]
    assert isinstance(argv, list)
    index = argv.index("--model")
    assert argv[index + 1] == "sonnet"
    assert "deepseek-v4-pro[1m]" not in argv
    assert argv[argv.index("--output-format") + 1] == "stream-json"
    assert "--verbose" in argv
    assert seen["timeout"] == 300.0
    assert result.final_message == "done"
    assert result.session_id == "s1"


def test_engineering_backend_streams_grounded_activity(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr("engineering.backend.shutil.which", lambda executable: "claude")
    observed = []

    stdout = _stream(
        {"type": "system", "subtype": "init", "session_id": "stream-1", "model": "sonnet", "permissionMode": "acceptEdits"},
        {
            "type": "assistant",
            "session_id": "stream-1",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": "Bash",
                        "input": {"command": "python -m pytest tests/test_x.py -q"},
                    }
                ]
            },
        },
        {
            "type": "user",
            "session_id": "stream-1",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "tool-1",
                        "content": "1 passed",
                    }
                ]
            },
        },
        {"type": "result", "subtype": "success", "session_id": "stream-1", "is_error": False, "result": "implemented and validated"},
    )

    monkeypatch.setattr(
        "engineering.backend.subprocess.Popen",
        lambda argv, **kwargs: _FakePopen(argv, stdout_lines=stdout, **kwargs),
    )
    backend = ClaudeEngineeringBackend(event_sink=observed.append)
    result = backend.run(tmp_path, "maintain")

    assert result.returncode == 0
    assert result.final_message == "implemented and validated"
    assert any(event.kind == "tool" and "pytest" in event.summary for event in result.events)
    assert any(event.kind == "tool_result" and "completed" in event.summary for event in result.events)
    assert observed == list(result.events)


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


def test_backend_reports_deadline_with_partial_activity(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("engineering.backend.shutil.which", lambda executable: "claude")
    stdout = _stream(
        {"type": "system", "subtype": "init", "session_id": "timeout-session", "model": "sonnet"},
        {
            "type": "assistant",
            "session_id": "timeout-session",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tool-1",
                        "name": "Read",
                        "input": {"file_path": "README.md"},
                    }
                ]
            },
        },
    )
    monkeypatch.setattr(
        "engineering.backend.subprocess.Popen",
        lambda argv, **kwargs: _FakePopen(
            argv,
            stdout_lines=stdout,
            wait_timeout=True,
            **kwargs,
        ),
    )
    backend = ClaudeEngineeringBackend(
        executable="claude",
        model="owned-model",
        timeout_seconds=3,
    )

    result = backend.run(tmp_path, "inspect")

    assert result.returncode == 124
    assert "[claude-code:timeout]" in result.stderr
    assert "3s deadline" in result.stderr
    assert result.session_id == "timeout-session"
    assert any("README.md" in event.summary for event in result.events)


def test_status_words_inside_a_write_task_do_not_turn_it_into_status_query() -> None:
    text = (
        "Hikari，在 README 里修改一行，写成："
        "Engineering 现在状态以持久化结果为准，不由对话模型推测。"
    )

    assert looks_like_engineering_status_query(text) is False
    requirements = engineering_requirements_for_intent(text)
    assert requirements is not None
    assert "engineering.repository.write" in requirements
