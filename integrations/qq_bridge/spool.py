from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3

from conversation.models import AssistantReply, UserTurn


@dataclass(frozen=True)
class BridgeSpoolItem:
    request_id: str
    turn: UserTurn
    reply_text: str | None
    state: str

    @property
    def reply(self) -> AssistantReply | None:
        if self.reply_text is None:
            return None
        return AssistantReply(
            channel=self.turn.channel,
            conversation_id=self.turn.conversation_id,
            text=self.reply_text,
        )


class BridgeSpool:
    """Durable at-least-once handoff state for the QQ transport edge."""

    VALID_STATES = {"pending", "replied", "sent"}

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS qq_bridge_spool (
                    request_id TEXT PRIMARY KEY,
                    channel TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    user_text TEXT NOT NULL,
                    reply_text TEXT,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    @staticmethod
    def _row_to_item(row: sqlite3.Row) -> BridgeSpoolItem:
        return BridgeSpoolItem(
            request_id=row["request_id"],
            turn=UserTurn(
                channel=row["channel"],
                conversation_id=row["conversation_id"],
                text=row["user_text"],
            ),
            reply_text=row["reply_text"],
            state=row["state"],
        )

    def get(self, request_id: str) -> BridgeSpoolItem | None:
        request_id = str(request_id).strip()
        if not request_id:
            raise ValueError("request_id must not be empty")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT request_id, channel, conversation_id, user_text, reply_text, state
                FROM qq_bridge_spool
                WHERE request_id = ?
                """,
                (request_id,),
            ).fetchone()
        return None if row is None else self._row_to_item(row)

    def record_turn(self, request_id: str, turn: UserTurn) -> BridgeSpoolItem:
        request_id = str(request_id).strip()
        if not request_id:
            raise ValueError("request_id must not be empty")
        if not isinstance(turn, UserTurn):
            raise TypeError("turn must be UserTurn")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO qq_bridge_spool (
                    request_id, channel, conversation_id, user_text, state
                ) VALUES (?, ?, ?, ?, 'pending')
                """,
                (request_id, turn.channel, turn.conversation_id, turn.text),
            )
        item = self.get(request_id)
        if item is None:
            raise RuntimeError("failed to persist QQ bridge turn")
        if item.turn != turn:
            raise ValueError("request_id was reused for a different QQ turn")
        return item

    def set_reply(self, request_id: str, reply: AssistantReply) -> BridgeSpoolItem:
        if not isinstance(reply, AssistantReply):
            raise TypeError("reply must be AssistantReply")
        item = self.get(request_id)
        if item is None:
            raise KeyError(request_id)
        if reply.channel != item.turn.channel or reply.conversation_id != item.turn.conversation_id:
            raise ValueError("reply must target the spooled conversation")
        if item.reply_text is not None and item.reply_text != reply.text:
            raise ValueError("request_id already has a different reply")
        if item.state == "sent":
            return item
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE qq_bridge_spool
                SET reply_text = ?, state = 'replied', updated_at = CURRENT_TIMESTAMP
                WHERE request_id = ?
                """,
                (reply.text, request_id),
            )
        updated = self.get(request_id)
        if updated is None:
            raise RuntimeError("failed to update QQ bridge reply")
        return updated

    def mark_sent(self, request_id: str) -> BridgeSpoolItem:
        item = self.get(request_id)
        if item is None:
            raise KeyError(request_id)
        if item.reply_text is None:
            raise ValueError("cannot mark QQ bridge item sent before a reply exists")
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE qq_bridge_spool
                SET state = 'sent', updated_at = CURRENT_TIMESTAMP
                WHERE request_id = ?
                """,
                (request_id,),
            )
        updated = self.get(request_id)
        if updated is None:
            raise RuntimeError("failed to mark QQ bridge item sent")
        return updated

    def unsent(self, limit: int = 100) -> list[BridgeSpoolItem]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT request_id, channel, conversation_id, user_text, reply_text, state
                FROM qq_bridge_spool
                WHERE state != 'sent'
                ORDER BY created_at ASC, request_id ASC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [self._row_to_item(row) for row in rows]
