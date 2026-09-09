"""Piped CLI output has the same UTF-8 contract on every Windows code page."""
from __future__ import annotations

import os
import json
from pathlib import Path
import subprocess
import sys
import tomllib

import pytest

from core.delivery import DeliveryOutbox


_PROJECT_ROOT = Path(__file__).parents[1]
_SCRIPTS = tomllib.loads((_PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["scripts"]


def _legacy_output_environment(tmp_path):
    environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("HIKARI_")}
    environment.update(PYTHONIOENCODING="cp1252:strict", PYTHONUTF8="0", PYTHONDONTWRITEBYTECODE="1",
                       LOCALAPPDATA=str(tmp_path / "local"), XDG_STATE_HOME=str(tmp_path / "state"))
    return environment


@pytest.mark.parametrize("module,arguments,expected", [
    ("resident.presence_cli", ["gate", "utf8-presence", "中文测试", "--importance", "0.8", "--local-iso", "2026-08-29T12:00:00+08:00"], "should_deliver：true"),
    ("resident.delivery_cli", ["send", "qq", "utf8-delivery", "中文测试"], "recipient：7"),
    ("integrations.qq_bridge.app", ["--check"], "Hikari QQ Bridge 运行时装配完成。"),
])
def test_cli_output_is_utf8_even_when_python_starts_with_cp1252(tmp_path: Path, module, arguments, expected):
    environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("HIKARI_")}
    environment.update({
        "PYTHONIOENCODING": "cp1252:strict",
        "PYTHONUTF8": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        "HIKARI_ONEBOT_ALLOWED_USER_IDS": "7",
        "HIKARI_QQ_PROACTIVE_USER_ID": "7",
        "HIKARI_PRESENCE_CHANNEL": "qq",
        "HIKARI_PRESENCE_QUIET_HOURS_ENABLED": "false",
        "HIKARI_QQ_ENABLED": "true",
        "XDG_STATE_HOME": str(tmp_path / "state"),
        "LOCALAPPDATA": str(tmp_path / "local"),
    })
    argv = [sys.executable, "-B", "-m", module]
    if module.startswith("resident."):
        argv += ["--state-dir", str(tmp_path)]
    result = subprocess.run(argv + arguments, cwd=Path(__file__).parents[1], env=environment,
                            capture_output=True, encoding="utf-8", errors="strict", timeout=20, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert expected in result.stdout
    assert "UnicodeEncodeError" not in result.stderr
    if module.startswith("resident."):
        queued = DeliveryOutbox(tmp_path / "proactive_delivery.db").pending(channel="qq")
        assert len(queued) == 1
        assert queued[0].request.text == "中文测试"


def test_cli_argument_errors_are_utf8_before_runtime_start(tmp_path: Path):
    environment = {**os.environ, "PYTHONIOENCODING": "cp1252:strict", "PYTHONUTF8": "0"}
    result = subprocess.run([sys.executable, "-B", "-m", "resident.delivery_cli", "--unknown"],
                            cwd=Path(__file__).parents[1], env=environment, capture_output=True,
                            encoding="utf-8", errors="strict", timeout=20, check=False)
    assert result.returncode == 2
    assert "UnicodeEncodeError" not in result.stderr


@pytest.mark.parametrize("name,target", sorted(_SCRIPTS.items()), ids=sorted(_SCRIPTS))
def test_every_registered_entrypoint_help_is_printable_on_legacy_windows_codepages(tmp_path, name, target):
    module, function = target.split(":")
    # Match the installed console-script dispatch, while --help guarantees no
    # runtime configuration is loaded and no service/action is started.
    code = "import importlib,sys;entry=getattr(importlib.import_module(sys.argv[2]),sys.argv[3]);sys.argv=[sys.argv[1],'--help'];raise SystemExit(entry())"
    result = subprocess.run([sys.executable, "-B", "-c", code, name, module, function], cwd=_PROJECT_ROOT,
        env=_legacy_output_environment(tmp_path), capture_output=True, encoding="utf-8", errors="strict", timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "usage:" in result.stdout
    assert "--help" in result.stdout
    assert "UnicodeEncodeError" not in result.stderr


@pytest.mark.parametrize("command", ["status", "result", "list"])
def test_engineering_read_commands_preserve_chinese_durable_json(tmp_path, command):
    from engineering.session import EngineeringAuthority, EngineeringResult, EngineeringSessionState, EngineeringSessionStore, EngineeringTurn
    store = EngineeringSessionStore(tmp_path / "engineering")
    session = store.create(EngineeringSessionState.create(project_id="中文项目", repository=tmp_path,
        authority_ceiling=EngineeringAuthority.read_only()))
    turn = EngineeringTurn.create(intent="只读检查", authority=EngineeringAuthority.read_only())
    store.enqueue_turn(session.session_id, turn)
    store.save_result(session.session_id, EngineeringResult(turn.turn_id, "completed", "实际中文结果已保存"))
    before = {str(path.relative_to(tmp_path)): path.read_bytes() for path in tmp_path.rglob("*.json")}
    argv = [sys.executable, "-B", "-m", "engineering.cli", "--state-dir", str(tmp_path), command]
    if command != "list":
        argv.append(session.session_id)
    result = subprocess.run(argv, cwd=_PROJECT_ROOT, env=_legacy_output_environment(tmp_path),
                            capture_output=True, encoding="utf-8", errors="strict", timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert (payload[0] if command == "list" else payload)["status"] == "completed"
    if command == "result":
        assert payload["message"] == "实际中文结果已保存"
    else:
        assert (payload[0] if command == "list" else payload)["project_id"] == "中文项目"
    assert before == {str(path.relative_to(tmp_path)): path.read_bytes() for path in tmp_path.rglob("*.json")}


def test_environment_status_preserves_unicode_source_path_without_mutating_pointer(tmp_path):
    repository = tmp_path / "中文源码"
    repository.mkdir()
    (repository / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
    managed = tmp_path / "state" / "environments"
    managed.mkdir(parents=True)
    identity = "a" * 20
    pointer = {"version": 1, "environment_id": identity, "path": str(managed / identity), "source_path": str(repository)}
    path = managed / "current.json"
    path.write_text(json.dumps(pointer, ensure_ascii=False), encoding="utf-8")
    before = path.read_bytes()
    result = subprocess.run([sys.executable, "-B", "-m", "resident.environment_manager", "--repo", str(repository),
                            "--state-dir", str(managed.parent), "status"], cwd=_PROJECT_ROOT,
        env=_legacy_output_environment(tmp_path), capture_output=True, encoding="utf-8", errors="strict", timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == pointer
    assert path.read_bytes() == before
