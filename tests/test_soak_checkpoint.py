from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from resident.soak_cli import build_checkpoint, main


def _write_host_state(root: Path, *, pid: int = 123) -> None:
    (root / "host.json").write_text(
        json.dumps(
            {
                "pid": pid,
                "started_at": "2026-08-29T10:00:00+00:00",
                "repository": "G:/work/LAB/code/hikari",
                "log_path": str(root / "resident.log"),
            }
        ),
        encoding="utf-8",
    )


def _prepare_sqlite_state(root: Path) -> None:
    with sqlite3.connect(root / "proactive_delivery.db") as connection:
        connection.execute(
            "CREATE TABLE proactive_delivery_outbox (delivery_id TEXT PRIMARY KEY, state TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO proactive_delivery_outbox (delivery_id, state) VALUES (?, ?)",
            [
                ("a", "pending"),
                ("b", "sent"),
                ("c", "sent"),
                ("d", "uncertain"),
            ],
        )

    with sqlite3.connect(root / "qq_bridge.db") as connection:
        connection.execute(
            "CREATE TABLE qq_bridge_spool (request_id TEXT PRIMARY KEY, state TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO qq_bridge_spool (request_id, state) VALUES (?, ?)",
            [("a", "pending"), ("b", "sent")],
        )

    with sqlite3.connect(root / "conversation_receipts.db") as connection:
        connection.execute(
            "CREATE TABLE conversation_receipts (request_id TEXT PRIMARY KEY)"
        )
        connection.executemany(
            "INSERT INTO conversation_receipts (request_id) VALUES (?)",
            [("a",), ("b",), ("c",)],
        )

    with sqlite3.connect(root / "presence_policy.db") as connection:
        connection.execute(
            "CREATE TABLE presence_acceptance (fingerprint TEXT PRIMARY KEY, accepted_at REAL NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE presence_meta (key TEXT PRIMARY KEY, value REAL NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO presence_acceptance (fingerprint, accepted_at) VALUES (?, ?)",
            [("one", 100.0), ("two", 200.0)],
        )
        connection.execute(
            "INSERT INTO presence_meta (key, value) VALUES ('last_accepted_at', 200.0)"
        )


def test_checkpoint_reports_process_tree_and_durable_state_without_mutating(tmp_path: Path):
    _write_host_state(tmp_path)
    _prepare_sqlite_state(tmp_path)
    (tmp_path / "resident.log").write_text("resident log\n", encoding="utf-8")
    (tmp_path / "qq_bridge.log").write_text("bridge log\n", encoding="utf-8")

    checkpoint = build_checkpoint(
        tmp_path,
        process_probe=lambda pid: pid == 123,
        process_tree_resolver=lambda pid: [pid, 456, 456],
        now=datetime(2026, 8, 29, 15, 30, tzinfo=timezone.utc),
    )

    assert checkpoint.captured_at == "2026-08-29T15:30:00+00:00"
    assert checkpoint.host_state_present is True
    assert checkpoint.host_state_error is None
    assert checkpoint.resident_running is True
    assert checkpoint.resident_pid == 123
    assert checkpoint.process_tree == (123, 456)
    assert checkpoint.process_count == 2
    assert checkpoint.process_error is None
    assert checkpoint.delivery_states == {
        "pending": 1,
        "sending": 0,
        "sent": 2,
        "uncertain": 1,
    }
    assert checkpoint.qq_spool_states == {
        "pending": 1,
        "replied": 0,
        "sent": 1,
    }
    assert checkpoint.conversation_receipts == 3
    assert checkpoint.presence_acceptance_count == 2
    assert checkpoint.presence_last_accepted_at == 200.0
    assert checkpoint.sqlite_errors == {}
    assert checkpoint.file_sizes["resident.log"] > 0
    assert checkpoint.file_sizes["qq_bridge.log"] > 0
    assert (tmp_path / "host.json").is_file()


def test_checkpoint_preserves_stale_host_evidence_instead_of_cleaning_it(tmp_path: Path):
    _write_host_state(tmp_path, pid=777)

    checkpoint = build_checkpoint(
        tmp_path,
        process_probe=lambda pid: False,
        process_tree_resolver=lambda pid: [pid, 888],
    )

    assert checkpoint.host_state_present is True
    assert checkpoint.resident_running is False
    assert checkpoint.resident_pid == 777
    assert checkpoint.process_tree == ()
    assert checkpoint.process_count == 0
    assert (tmp_path / "host.json").is_file()


def test_checkpoint_surfaces_invalid_host_state_and_sqlite_errors(tmp_path: Path):
    (tmp_path / "host.json").write_text("not json", encoding="utf-8")
    (tmp_path / "proactive_delivery.db").write_bytes(b"not sqlite")

    checkpoint = build_checkpoint(tmp_path)

    assert checkpoint.host_state_present is True
    assert checkpoint.host_state_error is not None
    assert checkpoint.resident_pid is None
    assert checkpoint.resident_running is False
    assert "proactive_delivery.db" in checkpoint.sqlite_errors


def test_checkpoint_cli_json_works_without_a_running_resident(tmp_path: Path, capsys):
    exit_code = main(["checkpoint", "--state-dir", str(tmp_path), "--json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["state_dir"] == str(tmp_path.resolve())
    assert payload["host_state_present"] is False
    assert payload["resident_running"] is False
    assert payload["process_tree"] == []
    assert payload["delivery_states"] == {
        "pending": 0,
        "sending": 0,
        "sent": 0,
        "uncertain": 0,
    }
