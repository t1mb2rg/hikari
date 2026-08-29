from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

from core.delivery import DeliveryOutbox


def _environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "HIKARI_PRESENCE_CHANNEL": "qq",
            "HIKARI_PRESENCE_QUIET_HOURS_ENABLED": "true",
            "HIKARI_PRESENCE_QUIET_START": "23:00",
            "HIKARI_PRESENCE_QUIET_END": "07:00",
            "HIKARI_PRESENCE_COOLDOWN_SECONDS": "300",
            "HIKARI_PRESENCE_DUPLICATE_WINDOW_SECONDS": "3600",
            "HIKARI_PRESENCE_URGENT_THRESHOLD": "0.95",
            "HIKARI_QQ_ENABLED": "true",
            "HIKARI_ONEBOT_ALLOWED_USER_IDS": "7",
            "HIKARI_QQ_PROACTIVE_USER_ID": "7",
        }
    )
    return environment


def _run(tmp_path: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    repository = Path(__file__).resolve().parents[1]
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "resident.presence_cli",
            "--state-dir",
            str(tmp_path),
            *arguments,
        ],
        cwd=repository,
        env=_environment(),
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def test_presence_gate_allowed_candidate_queues_durable_qq_delivery(tmp_path: Path):
    result = _run(
        tmp_path,
        "gate",
        "allowed-1",
        "deterministic hello",
        "--importance",
        "0.8",
        "--local-iso",
        "2026-08-29T12:00:00+08:00",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "should_deliver：true" in result.stdout
    assert "reason：allowed" in result.stdout
    assert "channel：qq" in result.stdout
    assert "delivery_state：pending" in result.stdout
    pending = DeliveryOutbox(tmp_path / "proactive_delivery.db").pending(channel="qq")
    assert len(pending) == 1
    assert pending[0].request.recipient == "7"
    assert pending[0].request.text == "deterministic hello"


def test_presence_gate_persists_cooldown_across_processes(tmp_path: Path):
    first = _run(
        tmp_path,
        "gate",
        "cooldown-first",
        "first",
        "--local-iso",
        "2026-08-29T12:00:00+08:00",
    )
    assert first.returncode == 0, first.stdout + first.stderr
    assert "should_deliver：true" in first.stdout

    second = _run(
        tmp_path,
        "gate",
        "cooldown-second",
        "second",
        "--local-iso",
        "2026-08-29T12:01:00+08:00",
    )
    assert second.returncode == 0, second.stdout + second.stderr
    assert "should_deliver：false" in second.stdout
    assert "reason：global cooldown active" in second.stdout


def test_presence_gate_quiet_hours_suppress_but_urgent_can_bypass(tmp_path: Path):
    quiet_db = tmp_path / "quiet.db"
    quiet = _run(
        tmp_path,
        "--policy-db",
        str(quiet_db),
        "gate",
        "quiet-ordinary",
        "ordinary",
        "--importance",
        "0.8",
        "--local-iso",
        "2026-08-29T23:30:00+08:00",
    )
    assert quiet.returncode == 0, quiet.stdout + quiet.stderr
    assert "should_deliver：false" in quiet.stdout
    assert "reason：quiet hours" in quiet.stdout

    urgent = _run(
        tmp_path,
        "--policy-db",
        str(tmp_path / "urgent.db"),
        "gate",
        "quiet-urgent",
        "urgent",
        "--importance",
        "0.99",
        "--local-iso",
        "2026-08-29T23:30:00+08:00",
    )
    assert urgent.returncode == 0, urgent.stdout + urgent.stderr
    assert "should_deliver：true" in urgent.stdout
    assert "reason：urgent threshold bypass" in urgent.stdout
    assert "urgent：true" in urgent.stdout


def test_presence_gate_busy_foreground_suppresses_ordinary(tmp_path: Path):
    environment = _environment()
    environment["HIKARI_PRESENCE_BUSY_FOREGROUND_PATTERNS"] = "counter-strike"
    repository = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "resident.presence_cli",
            "--state-dir",
            str(tmp_path),
            "--policy-db",
            str(tmp_path / "busy.db"),
            "gate",
            "busy-game",
            "ordinary",
            "--importance",
            "0.8",
            "--local-iso",
            "2026-08-29T12:00:00+08:00",
            "--foreground-title",
            "Counter-Strike 2",
        ],
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "should_deliver：false" in result.stdout
    assert "reason：foreground matches busy pattern: counter-strike" in result.stdout
