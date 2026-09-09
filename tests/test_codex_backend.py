import json
import os
from pathlib import Path
import sys

import pytest

from engineering.codex_backend import CodexEngineeringBackend, codex_provider_configuration
from engineering.config import EngineeringBackendConfig


def test_existing_provider_token_is_transferred_without_command_line_exposure(tmp_path: Path):
    (tmp_path / "config.toml").write_text('''model = "configured-model"
model_provider = "custom"
[model_providers.custom]
name = "Custom"
base_url = "https://provider.example/v1"
wire_api = "responses"
experimental_bearer_token = "test-only-token"
requires_openai_auth = false
''', encoding="utf-8")
    args, environment, model = codex_provider_configuration({"CODEX_HOME": str(tmp_path)})
    assert model == "configured-model"
    assert "test-only-token" not in " ".join(args)
    assert environment["HIKARI_CODEX_RUNTIME_TOKEN"] == "test-only-token"
    assert 'model_providers.hikari_engineering.env_key="HIKARI_CODEX_RUNTIME_TOKEN"' in args


def test_codex_selection_does_not_inherit_claude_model(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setenv("HIKARI_ENGINEERING_BACKEND", "codex")
    monkeypatch.setenv("HIKARI_ENGINEERING_MODEL", "sonnet")
    config = EngineeringBackendConfig.from_mapping(dict(os.environ))
    assert config.backend == "codex" and config.executable == "codex"
    backend = CodexEngineeringBackend(executable=sys.executable, writable=True)
    argv, _ = backend.build_invocation(tmp_path)
    assert "sonnet" not in argv
    assert 'default_permissions=":workspace"' in argv
    assert "--sandbox" not in argv
    assert "--ignore-user-config" in argv and "--ignore-rules" in argv
    assert 'approval_policy="never"' in argv
    assert "--dangerously-bypass-approvals-and-sandbox" not in argv


@pytest.mark.parametrize("completed,failed,expected", [(True, False, 0), (False, False, 1), (True, True, 1)])
def test_real_subprocess_jsonl_requires_successful_completion(tmp_path: Path, monkeypatch, completed, failed, expected):
    trace = [
        {"type": "thread.started", "thread_id": "01234567-1234-1234-1234-012345678901"},
        {"type": "item.completed", "item": {"type": "command_execution", "command": "python -V", "status": "completed", "exit_code": 0}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps({"status": "completed", "summary": "verified result", "validation": ["python -V passed"]})}},
    ]
    if completed:
        trace.append({"type": "turn.completed", "usage": {}})
    if failed:
        trace.append({"type": "turn.failed", "error": {"message": "test failure"}})
    command = tmp_path / "recorded_cli.py"
    command.write_text("import sys\nsys.stdin.read()\n" + "\n".join(f"print({json.dumps(json.dumps(item))})" for item in trace), encoding="utf-8")
    backend = CodexEngineeringBackend()
    monkeypatch.setattr(backend, "build_invocation", lambda _: ([sys.executable, str(command)], dict(os.environ)))
    observed = []
    backend.set_event_sink(observed.append)
    result = backend.run(tmp_path, "inspect")
    assert result.returncode == expected
    assert result.session_id == "codex:01234567-1234-1234-1234-012345678901"
    if expected == 0:
        assert result.final_message == "verified result"
    assert any("python -V" in event.summary for event in observed)


def test_resume_uses_only_explicit_codex_identity(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    backend = CodexEngineeringBackend(executable=sys.executable, session_id="claude-session")
    argv, _ = backend.build_invocation(tmp_path)
    assert "resume" not in argv
    backend = CodexEngineeringBackend(executable=sys.executable, session_id="codex:01234567-1234-1234-1234-012345678901")
    argv, _ = backend.build_invocation(tmp_path)
    assert argv[-3:] == ["resume", "01234567-1234-1234-1234-012345678901", "-"]


def test_completed_codex_process_can_still_report_blocked_task(tmp_path: Path, monkeypatch):
    trace = [
        {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps({
            "status": "blocked", "summary": "Workspace was read-only; no requested file exists", "validation": [],
        })}},
        {"type": "turn.completed"},
    ]
    command = tmp_path / "blocked_cli.py"
    command.write_text("import sys\nsys.stdin.read()\n" + "\n".join(f"print({json.dumps(json.dumps(item))})" for item in trace), encoding="utf-8")
    backend = CodexEngineeringBackend()
    monkeypatch.setattr(backend, "build_invocation", lambda _: ([sys.executable, str(command)], dict(os.environ)))
    result = backend.run(tmp_path, "create a file")
    assert result.returncode == 77
    assert "[codex:blocked]" in result.stderr


def test_desktop_session_permissions_are_not_inherited(tmp_path: Path):
    _, environment, _ = codex_provider_configuration({
        "CODEX_HOME": str(tmp_path), "CODEX_PERMISSION_PROFILE": ":danger-full-access",
        "CODEX_THREAD_ID": "parent", "CODEX_APP_TOOLS_PIPE_PATH": "parent-pipe",
    })
    assert environment == {"CODEX_HOME": str(tmp_path)}
