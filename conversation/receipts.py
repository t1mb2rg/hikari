from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import sqlite3
from threading import RLock

from .models import AssistantReply, UserTurn


# Bound memory use while sharing locks across processors/store instances in one host.
# The durable claim below is the authority across processes and host restarts.
_REQUEST_LOCKS = tuple(RLock() for _ in range(64))


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
                CREATE TABLE IF NOT EXISTS conversation_request_claims (
                    request_id TEXT PRIMARY KEY,
                    channel TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    user_text TEXT NOT NULL,
                    actor_id TEXT,
                    scope TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('processing', 'uncertain', 'completed')),
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS conversation_receipts (
                    request_id TEXT PRIMARY KEY,
                    channel TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    user_text TEXT NOT NULL,
                    actor_id TEXT,
                    scope TEXT NOT NULL DEFAULT 'private',
                    reply_text TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(conversation_receipts)")
            }
            if "actor_id" not in columns:
                connection.execute(
                    "ALTER TABLE conversation_receipts ADD COLUMN actor_id TEXT"
                )
            if "scope" not in columns:
                connection.execute(
                    "ALTER TABLE conversation_receipts ADD COLUMN scope TEXT NOT NULL DEFAULT 'private'"
                )

            connection.execute(
                "UPDATE conversation_receipts SET scope = 'shared' WHERE conversation_id LIKE 'group:%'"
            )
            connection.execute(
                "UPDATE conversation_receipts SET scope = 'private' WHERE conversation_id LIKE 'private:%'"
            )
            connection.execute(
                """
                UPDATE conversation_receipts
                SET actor_id = substr(conversation_id, 9)
                WHERE actor_id IS NULL AND conversation_id LIKE 'private:%'
                """
            )

    def request_lock(self, request_id: str):
        key = (os.path.normcase(str(self.path)), request_id.strip())
        return _REQUEST_LOCKS[hash(key) % len(_REQUEST_LOCKS)]

    def claim(self, request_id: str, turn: UserTurn) -> bool:
        """Claim before any model/action call; an unfinished claim is never replayed.

        Claims deliberately have no expiring lease. A process may have created an
        Engineering Goal before dying, so elapsed time cannot prove retry is safe.
        Completed pre-claim receipts remain authoritative during additive migration.
        """
        request_id = str(request_id).strip()
        if not request_id:
            raise ValueError("request_id must not be empty")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM conversation_receipts WHERE request_id = ?", (request_id,)
            ).fetchone()
            if row is None:
                row = connection.execute(
                    "SELECT * FROM conversation_request_claims WHERE request_id = ?", (request_id,)
                ).fetchone()
            if row is not None:
                existing = UserTurn(
                    row["channel"], row["conversation_id"], row["user_text"],
                    actor_id=row["actor_id"], scope=row["scope"],
                )
                if not existing.same_wire_turn(turn):
                    raise ValueError("request_id was reused for a different user turn")
                return False
            connection.execute(
                """INSERT INTO conversation_request_claims
                   (request_id, channel, conversation_id, user_text, actor_id, scope, state)
                   VALUES (?, ?, ?, ?, ?, ?, 'processing')""",
                (request_id, turn.channel, turn.conversation_id, turn.text, turn.actor_id, turn.scope),
            )
        return True

    def mark_uncertain(self, request_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE conversation_request_claims SET state = 'uncertain'
                   WHERE request_id = ? AND state = 'processing'""", (request_id,),
            )

    def get(self, request_id: str) -> ConversationReceipt | None:
        request_id = str(request_id).strip()
        if not request_id:
            raise ValueError("request_id must not be empty")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT request_id, channel, conversation_id, user_text,
                       actor_id, scope, reply_text
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
            actor_id=row["actor_id"],
            scope=row["scope"],
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
                    request_id, channel, conversation_id, user_text,
                    actor_id, scope, reply_text
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    turn.channel,
                    turn.conversation_id,
                    turn.text,
                    turn.actor_id,
                    turn.scope,
                    reply.text,
                ),
            )
            connection.execute(
                "UPDATE conversation_request_claims SET state = 'completed' WHERE request_id = ?",
                (request_id,),
            )

        stored = self.get(request_id)
        if stored is None:
            raise RuntimeError("failed to persist conversation receipt")
        if not stored.turn.same_wire_turn(turn):
            raise ValueError("request_id was reused for a different user turn")
        if stored.reply != reply:
            raise ValueError("request_id was reused for a different assistant reply")
        return stored
