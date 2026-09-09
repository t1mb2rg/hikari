"""Piped CLI output has the same UTF-8 contract on every Windows code page."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

from core.delivery import DeliveryOutbox


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
