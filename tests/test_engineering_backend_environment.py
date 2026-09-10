from __future__ import annotations

from io import StringIO
from pathlib import Path
import json

from engineering.backend import ClaudeEngineeringBackend, _claude_child_environment


def test_claude_child_environment_removes_only_model_overrides() -> None:
    cleaned = _claude_child_environment(
        {
            "PATH": "test-path",
            "ANTHROPIC_BASE_URL": "https://example.invalid",
            "ANTHROPIC_AUTH_TOKEN": "auth-token",
            "HIKARI_ENGINEERING_MODEL": "sonnet",
            "ANTHROPIC_MODEL": "deepseek-v4-pro[1m]",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "deepseek-v4-flash",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "deepseek-v4-pro[1m]",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "deepseek-v4-pro[1m]",
            "CLAUDE_CODE_SUBAGENT_MODEL": "deepseek-v4-flash",
        }
    )

    assert cleaned == {
        "PATH": "test-path",
        "ANTHROPIC_BASE_URL": "https://example.invalid",
        "ANTHROPIC_AUTH_TOKEN": "auth-token",
        "HIKARI_ENGINEERING_MODEL": "sonnet",
    }


def test_backend_spawns_claude_with_isolated_model_environment(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr("engineering.backend.shutil.which", lambda executable: "claude")
    monkeypatch.setenv("HIKARI_ENGINEERING_MODEL", "sonnet")
    monkeypatch.setenv("ANTHROPIC_MODEL", "deepseek-v4-pro[1m]")
    monkeypatch.setenv("ANTHROPIC_DEFAULT_HAIKU_MODEL", "deepseek-v4-flash")
    monkeypatch.setenv("ANTHROPIC_DEFAULT_OPUS_MODEL", "deepseek-v4-pro[1m]")
    monkeypatch.setenv("ANTHROPIC_DEFAULT_SONNET_MODEL", "deepseek-v4-pro[1m]")
    monkeypatch.setenv("CLAUDE_CODE_SUBAGENT_MODEL", "deepseek-v4-flash")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://example.invalid")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "keep-me")
    captured: dict[str, object] = {}

    class FakePopen:
        def __init__(self, argv, **kwargs) -> None:
            captured["argv"] = list(argv)
            captured["env"] = dict(kwargs["env"])
            self.stdin = StringIO()
            self.stdout = StringIO(
                json.dumps(
                    {
                        "type": "result",
                        "subtype": "success",
                        "session_id": "isolated-model-session",
                        "is_error": False,
                        "result": "done",
                        "structured_output": {"status": "completed", "summary": "done", "validation": []},
                    }
                )
                + "\n"
            )
            self.stderr = StringIO("")

        def wait(self, timeout=None):
            return 0

        def kill(self):
            return None

    monkeypatch.setattr("engineering.backend.subprocess.Popen", FakePopen)

    result = ClaudeEngineeringBackend().run(tmp_path, "inspect")

    assert result.returncode == 0
    argv = captured["argv"]
    env = captured["env"]
    assert isinstance(argv, list)
    assert isinstance(env, dict)
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert "ANTHROPIC_MODEL" not in env
    assert "ANTHROPIC_DEFAULT_HAIKU_MODEL" not in env
    assert "ANTHROPIC_DEFAULT_OPUS_MODEL" not in env
    assert "ANTHROPIC_DEFAULT_SONNET_MODEL" not in env
    assert "CLAUDE_CODE_SUBAGENT_MODEL" not in env
    assert env["ANTHROPIC_BASE_URL"] == "https://example.invalid"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "keep-me"
