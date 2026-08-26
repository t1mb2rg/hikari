from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any


@dataclass(frozen=True)
class MemoryEvent:
    id: int
    event_type: str
    content: str
    context: dict[str, Any]
    importance: float
    occurred_at: str


class MemoryStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    context_json TEXT NOT NULL,
                    importance REAL NOT NULL,
                    occurred_at TEXT NOT NULL
                )
                """
            )

    def remember_event(
        self,
        event_type: str,
        content: str,
        *,
        context: dict[str, Any] | None = None,
        importance: float = 0.0,
        occurred_at: datetime | None = None,
    ) -> MemoryEvent:
        timestamp = occurred_at or datetime.now(timezone.utc)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)

        timestamp_text = timestamp.astimezone(timezone.utc).isoformat()
        context_data = context or {}

        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO events (
                    event_type,
                    content,
                    context_json,
                    importance,
                    occurred_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    event_type,
                    content,
                    json.dumps(context_data, ensure_ascii=False, sort_keys=True),
                    float(importance),
                    timestamp_text,
                ),
            )
            event_id = int(cursor.lastrowid)

        return MemoryEvent(
            id=event_id,
            event_type=event_type,
            content=content,
            context=context_data,
            importance=float(importance),
            occurred_at=timestamp_text,
        )

    def get_event(self, event_id: int) -> MemoryEvent | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM events WHERE id = ?",
                (event_id,),
            ).fetchone()

        if row is None:
            return None

        return self._row_to_event(row)

    def recent_events(self, limit: int = 50) -> list[MemoryEvent]:
        if limit <= 0:
            return []

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM events
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        return [self._row_to_event(row) for row in rows]

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> MemoryEvent:
        return MemoryEvent(
            id=int(row["id"]),
            event_type=str(row["event_type"]),
            content=str(row["content"]),
            context=json.loads(row["context_json"]),
            importance=float(row["importance"]),
            occurred_at=str(row["occurred_at"]),
        )
