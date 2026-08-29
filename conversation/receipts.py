from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3

from .models import AssistantReply, UserTurn


@dataclass(frozen=True)
class ConversationReceipt:
    request_id: str
    turn: UserTurn
    reply: AssistantReply


class ConversationReceiptStore:
    """Small durable idempotency store for remote conversation requests."""

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
                CREATE TABLE IF NOT EXISTS conversation_receipts (
                    request_id TEXT PRIMARY KEY,
                    channel TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    user_text TEXT NOT NULL,
                    reply_text TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    def get(self, request_id: str) -> ConversationReceipt | None:
        request_id = str(request_id).strip()
        if not request_id:
            raise ValueError("request_id must not be empty")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT request_id, channel, conversation_id, user_text, reply_text
                FROM conversation_receipts
                WHERE request_id = ?
                """,
                (request_id,),
            ).fetchone()
        if row is None:
            return None
        turn = UserTurn(
            channel=row["channel"],
            conversation_id=row["conversation_id"],
            text=row["user_text"],
        )
        reply = AssistantReply(
            channel=row["channel"],
            conversation_id=row["conversation_id"],
            text=row["reply_text"],
        )
        return ConversationReceipt(request_id=request_id, turn=turn, reply=reply)

    def save(
        self,
        request_id: str,
        turn: UserTurn,
        reply: AssistantReply,
    ) -> ConversationReceipt:
        request_id = str(request_id).strip()
        if not request_id:
            raise ValueError("request_id must not be empty")
        if not isinstance(turn, UserTurn):
            raise TypeError("turn must be UserTurn")
        if not isinstance(reply, AssistantReply):
            raise TypeError("reply must be AssistantReply")
        if reply.channel != turn.channel or reply.conversation_id != turn.conversation_id:
            raise ValueError("reply must target the same conversation as the turn")

        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO conversation_receipts (
                    request_id, channel, conversation_id, user_text, reply_text
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    turn.channel,
                    turn.conversation_id,
                    turn.text,
                    reply.text,
                ),
            )

        stored = self.get(request_id)
        if stored is None:
            raise RuntimeError("failed to persist conversation receipt")
        if stored.turn != turn:
            raise ValueError("request_id was reused for a different user turn")
        if stored.reply != reply:
            raise ValueError("request_id was reused for a different assistant reply")
        return stored
