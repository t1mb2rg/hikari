from __future__ import annotations

import json
from pathlib import Path

from resident.doctor_cli import (
    _console_safe,
    _redact_log_line,
    collect_doctor_report,
    main,
)


def _prepare_napcat(root: Path, *, host: str = "127.0.0.1", file_log: bool = True) -> None:
    config = root / "config"
    config.mkdir(parents=True)
    (root / "cache").mkdir()
    (root / "logs").mkdir()
    if file_log:
        (root / "logs" / "napcat-test.log").write_text("ready\n", encoding="utf-8")
    (config / "webui.json").write_text(
        json.dumps(
            {
                "host": host,
                "port": 6099,
                "token": "super-secret-token",
                "disableWebUI": False,
            }
        ),
        encoding="utf-8",
    )
    (config / "napcat.json").write_text(
        json.dumps({"fileLog": file_log}),
        encoding="utf-8",
    )
    (config / "onebot11_1.json").write_text(
        json.dumps(
            {
                "network": {
                    "websocketClients": [
                        {
                            "enable": True,
                            "url": "ws://user:url-password@127.0.0.1:8081/onebot/v11/ws?access_token=url-secret",
                            "token": "must-not-leak",
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )


def _healthy_probe(_: str):
    return {
        "task": {
            "present": True,
            "state": "Running",
            "last_result": 267009,
            "error": None,
        },
        "ports": {
            "8081": {"listeners": 1, "established": 1},
            "6099": {"listeners": 1, "established": 0},
        },
        "qq_pids": [1234],
    }


def test_doctor_reports_healthy_napcat_without_leaking_tokens(tmp_path: Path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    napcat = tmp_path / "napcat"
    _prepare_napcat(napcat)

    report = collect_doctor_report(
        state_dir,
        napcat,
        windows_probe=_healthy_probe,
        system_name="Windows",
    )

    # An empty state directory means Resident is down, but the NapCat section is healthy.
    assert report.status == "error"
    assert report.napcat.task_state == "Running"
    assert report.napcat.qq_pids == (1234,)
    assert report.napcat.bridge_established_count == 1
    assert report.napcat.webui_url == "http://127.0.0.1:6099"
    assert report.napcat.webui_token_configured is True
    assert report.napcat.log_file_count == 1
    assert report.napcat.latest_log_path is not None
    payload = json.dumps(report.as_dict(), ensure_ascii=False)
    assert "super-secret-token" not in payload
    assert "must-not-leak" not in payload
    assert "url-secret" not in payload
    assert "url-password" not in payload
    assert report.napcat.onebot_websocket_urls == (
        "ws://127.0.0.1:8081/onebot/v11/ws",
    )


def test_doctor_warns_for_wide_webui_and_disabled_file_logging(tmp_path: Path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    napcat = tmp_path / "napcat"
    _prepare_napcat(napcat, host="::", file_log=False)

    report = collect_doctor_report(
        state_dir,
        napcat,
        windows_probe=_healthy_probe,
        system_name="Windows",
    )

    codes = {finding.code for finding in report.findings}
    assert "webui.wide_bind" in codes
    assert "napcat.file_log_disabled" in codes


def test_doctor_warns_when_file_logging_is_enabled_but_no_file_exists(tmp_path: Path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    napcat = tmp_path / "napcat"
    _prepare_napcat(napcat)
    (napcat / "logs" / "napcat-test.log").unlink()

    report = collect_doctor_report(
        state_dir,
        napcat,
        windows_probe=_healthy_probe,
        system_name="Windows",
    )

    assert "napcat.file_log_pending" in {
        finding.code for finding in report.findings
    }


def test_doctor_surfaces_stopped_task_missing_qq_and_disconnected_bridge(tmp_path: Path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    napcat = tmp_path / "napcat"
    _prepare_napcat(napcat)

    report = collect_doctor_report(
        state_dir,
        napcat,
        windows_probe=lambda _: {
            "task": {"present": True, "state": "Ready", "last_result": 1},
            "ports": {
                "8081": {"listeners": 1, "established": 0},
                "6099": {"listeners": 0, "established": 0},
            },
            "qq_pids": [],
        },
        system_name="Windows",
    )

    codes = {finding.code for finding in report.findings}
    assert "napcat.task_stopped" in codes
    assert "qq.down" in codes
    assert "bridge.disconnected" in codes
    assert "webui.not_listening" in codes


def test_doctor_handles_probe_failure_and_malformed_config(tmp_path: Path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    napcat = tmp_path / "napcat"
    (napcat / "config").mkdir(parents=True)
    (napcat / "config" / "webui.json").write_text("not json", encoding="utf-8")

    def fail(_: str):
        raise RuntimeError("probe failed")

    report = collect_doctor_report(
        state_dir,
        napcat,
        windows_probe=fail,
        system_name="Windows",
    )

    codes = {finding.code for finding in report.findings}
    assert "napcat.probe" in codes
    assert "napcat.config" in codes
    assert "probe failed" in (report.napcat.probe_error or "")


def test_doctor_cli_json_is_machine_readable_and_returns_error(tmp_path: Path, capsys):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    napcat = tmp_path / "missing-napcat"

    exit_code = main(
        [
            "--state-dir",
            str(state_dir),
            "--napcat-root",
            str(napcat),
            "--recent-errors",
            "0",
            "--json",
        ]
    )

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "error"
    assert payload["napcat"]["webui_token_configured"] is False


def test_console_safe_escapes_characters_missing_from_windows_code_page():
    rendered = _console_safe("bridge clue Ⱥ", encoding="gbk")

    assert rendered == r"bridge clue \u023a"


def test_log_clues_redact_common_secret_shapes():
    rendered = _redact_log_line(
        "WARNING provider failed token=abc123 api_key:xyz789 password=hunter2 "
        "Authorization: Bearer auth456 standalone Bearer bearer789"
    )

    assert "abc123" not in rendered
    assert "xyz789" not in rendered
    assert "hunter2" not in rendered
    assert "auth456" not in rendered
    assert "bearer789" not in rendered
    assert rendered.count("<redacted>") == 5
