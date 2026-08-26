from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any

from .models import DurableMemory, MemoryKind, parse_memory_kind


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

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Open one short-lived SQLite connection and always close it.

        ``sqlite3.Connection`` is a transaction context manager, but leaving
        ``with connection`` does not close the underlying database handle.
        Explicit close is required for reliable temporary-file cleanup on
        Windows, where an open SQLite handle prevents unlinking the DB file.
        """
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

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
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    context_json TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    source_event_id INTEGER,
                    created_at TEXT NOT NULL
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

    def remember_memory(
        self,
        kind: MemoryKind | str,
        content: str,
        *,
        context: dict[str, Any] | None = None,
        confidence: float = 1.0,
        source_event_id: int | None = None,
        created_at: datetime | None = None,
    ) -> DurableMemory:
        memory_kind = parse_memory_kind(kind)
        if not content.strip():
            raise ValueError("memory content must not be empty")

        confidence_value = float(confidence)
        if not 0.0 <= confidence_value <= 1.0:
            raise ValueError("memory confidence must be between 0.0 and 1.0")

        timestamp = created_at or datetime.now(timezone.utc)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        timestamp_text = timestamp.astimezone(timezone.utc).isoformat()
        context_data = context or {}

        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO memories (
                    kind,
                    content,
                    context_json,
                    confidence,
                    source_event_id,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    memory_kind.value,
                    content,
                    json.dumps(context_data, ensure_ascii=False, sort_keys=True),
                    confidence_value,
                    source_event_id,
                    timestamp_text,
                ),
            )
            memory_id = int(cursor.lastrowid)

        return DurableMemory(
            id=memory_id,
            kind=memory_kind,
            content=content,
            context=context_data,
            confidence=confidence_value,
            source_event_id=source_event_id,
            created_at=timestamp_text,
        )

    def update_importance(self, event_id: int, importance: float) -> MemoryEvent:
        """Attach Attention's score to an already remembered event."""
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE events SET importance = ? WHERE id = ?",
                (float(importance), event_id),
            )
            rowcount = cursor.rowcount

        if rowcount == 0:
            raise KeyError(f"Memory event does not exist: {event_id}")

        event = self.get_event(event_id)
        if event is None:
            raise RuntimeError(f"Memory event disappeared after update: {event_id}")
        return event

    def get_event(self, event_id: int) -> MemoryEvent | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM events WHERE id = ?",
                (event_id,),
            ).fetchone()

        if row is None:
            return None

        return self._row_to_event(row)

    def get_memory(self, memory_id: int) -> DurableMemory | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM memories WHERE id = ?",
                (memory_id,),
            ).fetchone()

        if row is None:
            return None

        return self._row_to_memory(row)

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

    def recent_memories(
        self,
        limit: int = 50,
        *,
        kind: MemoryKind | str | None = None,
    ) -> list[DurableMemory]:
        if limit <= 0:
            return []

        if kind is None:
            query = "SELECT * FROM memories ORDER BY id DESC LIMIT ?"
            params: tuple[object, ...] = (limit,)
        else:
            memory_kind = parse_memory_kind(kind)
            query = "SELECT * FROM memories WHERE kind = ? ORDER BY id DESC LIMIT ?"
            params = (memory_kind.value, limit)

        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()

        return [self._row_to_memory(row) for row in rows]

    def memories_after(
        self,
        after_id: int,
        *,
        kinds: Iterable[MemoryKind | str] | None = None,
        limit: int = 50,
    ) -> list[DurableMemory]:
        """Return durable memories newer than a caller-owned watermark.

        Results are oldest-first so a bounded consumer can advance through new
        experience without skipping earlier memories when more than ``limit``
        items are waiting.
        """

        watermark = int(after_id)
        if watermark < 0:
            raise ValueError("after_id must be non-negative")
        if limit <= 0:
            return []

        params: list[object] = [watermark]
        clauses = ["id > ?"]

        if kinds is not None:
            parsed_kinds = tuple(parse_memory_kind(kind) for kind in kinds)
            if not parsed_kinds:
                return []
            placeholders = ", ".join("?" for _ in parsed_kinds)
            clauses.append(f"kind IN ({placeholders})")
            params.extend(kind.value for kind in parsed_kinds)

        params.append(int(limit))
        query = (
            "SELECT * FROM memories WHERE "
            + " AND ".join(clauses)
            + " ORDER BY id ASC LIMIT ?"
        )

        with self._connect() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()

        return [self._row_to_memory(row) for row in rows]

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

    @staticmethod
    def _row_to_memory(row: sqlite3.Row) -> DurableMemory:
        source_event_id = row["source_event_id"]
        return DurableMemory(
            id=int(row["id"]),
            kind=parse_memory_kind(str(row["kind"])),
            content=str(row["content"]),
            context=json.loads(row["context_json"]),
            confidence=float(row["confidence"]),
            source_event_id=int(source_event_id) if source_event_id is not None else None,
            created_at=str(row["created_at"]),
        )
