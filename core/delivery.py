from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
import sqlite3
from typing import Protocol, runtime_checkable


VALID_DELIVERY_CHANNELS = frozenset({"windows", "qq"})
VALID_DELIVERY_STATES = frozenset({"pending", "sending", "sent", "uncertain"})


def _required_text(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


@dataclass(frozen=True)
class DeliveryRequest:
    """One explicit proactive message request, independent from Conversation turns."""

    delivery_id: str
    channel: str
    recipient: str
    text: str
    source: str = "presence"

    def __post_init__(self) -> None:
        delivery_id = _required_text(self.delivery_id, name="delivery_id")
        channel = _required_text(self.channel, name="channel")
        recipient = _required_text(self.recipient, name="recipient")
        text = _required_text(self.text, name="text")
        source = _required_text(self.source, name="source")
        if channel not in VALID_DELIVERY_CHANNELS:
            raise ValueError(f"unsupported proactive delivery channel: {channel}")
        object.__setattr__(self, "delivery_id", delivery_id)
        object.__setattr__(self, "channel", channel)
        object.__setattr__(self, "recipient", recipient)
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "source", source)


@dataclass(frozen=True)
class DeliveryRecord:
    request: DeliveryRequest
    state: str
    attempts: int
    last_error: str | None = None


@runtime_checkable
class DeliverySink(Protocol):
    """Immediate transport for a delivery channel owned by the current process."""

    def send(self, request: DeliveryRequest) -> None:
        ...


class DeliveryOutbox:
    """Durable proactive delivery state shared by Resident and transport edges.

    `sending` is deliberately not auto-retried after a process restart. A hard
    crash can happen after an external transport accepted the message but before
    Hikari persisted `sent`. Such records become `uncertain` on recovery so the
    safe default is no duplicate user-visible message.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS proactive_delivery_outbox (
                    delivery_id TEXT PRIMARY KEY,
                    channel TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    message_text TEXT NOT NULL,
                    source TEXT NOT NULL,
                    state TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> DeliveryRecord:
        state = str(row["state"])
        if state not in VALID_DELIVERY_STATES:
            raise RuntimeError(f"invalid proactive delivery state in database: {state}")
        return DeliveryRecord(
            request=DeliveryRequest(
                delivery_id=row["delivery_id"],
                channel=row["channel"],
                recipient=row["recipient"],
                text=row["message_text"],
                source=row["source"],
            ),
            state=state,
            attempts=int(row["attempts"]),
            last_error=row["last_error"],
        )

    def get(self, delivery_id: str) -> DeliveryRecord | None:
        delivery_id = _required_text(delivery_id, name="delivery_id")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT delivery_id, channel, recipient, message_text, source,
                       state, attempts, last_error
                FROM proactive_delivery_outbox
                WHERE delivery_id = ?
                """,
                (delivery_id,),
            ).fetchone()
        return None if row is None else self._row_to_record(row)

    def enqueue(self, request: DeliveryRequest) -> DeliveryRecord:
        if not isinstance(request, DeliveryRequest):
            raise TypeError("request must be DeliveryRequest")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO proactive_delivery_outbox (
                    delivery_id, channel, recipient, message_text, source, state
                ) VALUES (?, ?, ?, ?, ?, 'pending')
                """,
                (
                    request.delivery_id,
                    request.channel,
                    request.recipient,
                    request.text,
                    request.source,
                ),
            )
        record = self.get(request.delivery_id)
        if record is None:
            raise RuntimeError("failed to persist proactive delivery request")
        if record.request != request:
            raise ValueError("delivery_id was reused for a different proactive delivery")
        return record

    def pending(
        self,
        *,
        channel: str | None = None,
        limit: int = 100,
    ) -> list[DeliveryRecord]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        parameters: list[object] = []
        where = "state = 'pending'"
        if channel is not None:
            normalized = _required_text(channel, name="channel")
            where += " AND channel = ?"
            parameters.append(normalized)
        parameters.append(int(limit))
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT delivery_id, channel, recipient, message_text, source,
                       state, attempts, last_error
                FROM proactive_delivery_outbox
                WHERE {where}
                ORDER BY created_at ASC, delivery_id ASC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def claim(self, delivery_id: str) -> DeliveryRecord:
        delivery_id = _required_text(delivery_id, name="delivery_id")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT delivery_id, channel, recipient, message_text, source,
                       state, attempts, last_error
                FROM proactive_delivery_outbox
                WHERE delivery_id = ?
                """,
                (delivery_id,),
            ).fetchone()
            if row is None:
                raise KeyError(delivery_id)
            record = self._row_to_record(row)
            if record.state == "pending":
                connection.execute(
                    """
                    UPDATE proactive_delivery_outbox
                    SET state = 'sending', attempts = attempts + 1,
                        last_error = NULL, updated_at = CURRENT_TIMESTAMP
                    WHERE delivery_id = ? AND state = 'pending'
                    """,
                    (delivery_id,),
                )
        updated = self.get(delivery_id)
        if updated is None:
            raise RuntimeError("failed to claim proactive delivery")
        return updated

    def release_pending(self, delivery_id: str, error: str) -> DeliveryRecord:
        delivery_id = _required_text(delivery_id, name="delivery_id")
        message = _required_text(error, name="error")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE proactive_delivery_outbox
                SET state = 'pending', last_error = ?, updated_at = CURRENT_TIMESTAMP
                WHERE delivery_id = ? AND state = 'sending'
                """,
                (message, delivery_id),
            )
        if cursor.rowcount == 0:
            record = self.get(delivery_id)
            if record is None:
                raise KeyError(delivery_id)
            if record.state != "pending":
                raise ValueError("only a sending delivery can be released for retry")
            return record
        record = self.get(delivery_id)
        if record is None:
            raise RuntimeError("failed to release proactive delivery")
        return record

    def mark_sent(self, delivery_id: str) -> DeliveryRecord:
        delivery_id = _required_text(delivery_id, name="delivery_id")
        record = self.get(delivery_id)
        if record is None:
            raise KeyError(delivery_id)
        if record.state == "sent":
            return record
        if record.state != "sending":
            raise ValueError("only a sending delivery can be marked sent")
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE proactive_delivery_outbox
                SET state = 'sent', last_error = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE delivery_id = ? AND state = 'sending'
                """,
                (delivery_id,),
            )
        updated = self.get(delivery_id)
        if updated is None:
            raise RuntimeError("failed to mark proactive delivery sent")
        return updated

    def recover_inflight(self) -> int:
        """Quarantine crash-interrupted sends instead of risking duplicate delivery."""

        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE proactive_delivery_outbox
                SET state = 'uncertain',
                    last_error = COALESCE(
                        last_error,
                        'delivery outcome uncertain after transport process restart'
                    ),
                    updated_at = CURRENT_TIMESTAMP
                WHERE state = 'sending'
                """
            )
        return int(cursor.rowcount)

    def retry_uncertain(self, delivery_id: str) -> DeliveryRecord:
        """Explicitly authorize retry of an uncertain record; never automatic."""

        delivery_id = _required_text(delivery_id, name="delivery_id")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE proactive_delivery_outbox
                SET state = 'pending', updated_at = CURRENT_TIMESTAMP
                WHERE delivery_id = ? AND state = 'uncertain'
                """,
                (delivery_id,),
            )
        if cursor.rowcount == 0:
            record = self.get(delivery_id)
            if record is None:
                raise KeyError(delivery_id)
            if record.state != "pending":
                raise ValueError("delivery is not uncertain")
            return record
        record = self.get(delivery_id)
        if record is None:
            raise RuntimeError("failed to retry uncertain proactive delivery")
        return record


class DeliveryRouter:
    """First-class proactive boundary for immediate sinks and durable transports."""

    def __init__(
        self,
        outbox: DeliveryOutbox,
        *,
        sinks: Mapping[str, DeliverySink] | None = None,
    ) -> None:
        if not isinstance(outbox, DeliveryOutbox):
            raise TypeError("DeliveryRouter requires DeliveryOutbox")
        self.outbox = outbox
        self.sinks = dict(sinks or {})

    def submit(self, request: DeliveryRequest) -> DeliveryRecord:
        record = self.outbox.enqueue(request)
        if record.state != "pending":
            return record
        sink = self.sinks.get(request.channel)
        if sink is None:
            return record

        record = self.outbox.claim(request.delivery_id)
        if record.state != "sending":
            return record
        try:
            sink.send(request)
        except Exception as exc:
            self.outbox.release_pending(
                request.delivery_id,
                f"{type(exc).__name__}: {exc}",
            )
            raise
        return self.outbox.mark_sent(request.delivery_id)
