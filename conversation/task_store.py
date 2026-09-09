"""Source-request-linked task evidence across private conversation capabilities."""
from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import time

from .models import UserTurn


class ConversationTaskStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS task_requests(
                source_ref TEXT PRIMARY KEY, turn_json TEXT NOT NULL, intent_json TEXT NOT NULL,
                status TEXT NOT NULL, evidence_json TEXT NOT NULL DEFAULT '{}',
                reply_text TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL)""")

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def create(self, source_ref: str, turn: UserTurn, intent: dict) -> dict:
        if not isinstance(source_ref, str) or not source_ref.strip():
            raise ValueError("task requires source request identity")
        turn_data = asdict(turn)
        with self._connect() as connection:
            connection.execute("INSERT OR IGNORE INTO task_requests VALUES (?,?,?,'planned','{}',NULL,?,?)",
                               (source_ref, json.dumps(turn_data, sort_keys=True), json.dumps(intent, sort_keys=True), time.time(), time.time()))
        result = self.get(source_ref)
        if result["turn"] != turn_data:
            raise ValueError("source request was reused for a different private principal or message")
        return result

    def get(self, source_ref: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM task_requests WHERE source_ref=?", (source_ref,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        for key in ("turn", "intent", "evidence"):
            result[key] = json.loads(result.pop(key + "_json"))
        return result

    def finish(self, source_ref: str, *, status: str, evidence: dict, reply: str) -> dict:
        with self._connect() as connection:
            connection.execute("UPDATE task_requests SET status=?,evidence_json=?,reply_text=?,updated_at=? WHERE source_ref=?",
                               (status, json.dumps(evidence, ensure_ascii=False), reply, time.time(), source_ref))
        return self.get(source_ref)

    def recent(self, limit: int = 60) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute("SELECT source_ref FROM task_requests ORDER BY updated_at DESC LIMIT ?", (max(1, min(limit, 200)),)).fetchall()
        return [self.get(row["source_ref"]) for row in rows]

    def update_evidence(self, source_ref: str, *, status: str, evidence: dict) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE task_requests SET status=?,evidence_json=?,updated_at=? WHERE source_ref=?",
                               (status, json.dumps(evidence, ensure_ascii=False), time.time(), source_ref))

    def for_turn(self, turn: UserTurn, limit: int = 15) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute("""SELECT source_ref FROM task_requests
                WHERE json_extract(turn_json,'$.channel')=? AND json_extract(turn_json,'$.conversation_id')=?
                AND json_extract(turn_json,'$.actor_id') IS ? AND json_extract(turn_json,'$.scope')='private'
                ORDER BY created_at DESC LIMIT ?""", (turn.channel, turn.conversation_id, turn.actor_id, limit)).fetchall()
        return [self.get(row[0]) for row in rows]

    def unfinished(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute("""SELECT source_ref FROM task_requests
                WHERE status NOT IN ('completed','merged','resumed','failed','blocked','ok')
                AND json_extract(intent_json,'$.kind') IN ('engineering','capability','github')
                ORDER BY created_at""").fetchall()
        return [self.get(row[0]) for row in rows]
