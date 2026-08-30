from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from .models import (
    AssimilationDecision,
    AssimilationResult,
    UserFact,
    UserFactCandidate,
    UserFactCategory,
    UserFactEvidence,
    UserFactStatus,
)


SCHEMA_VERSION = 1


class UserModelStore:
    """Independent SQLite persistence for the current and historical user model."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA foreign_keys = ON")
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
            current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if current_version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"user model schema {current_version} is newer than supported {SCHEMA_VERSION}"
                )
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS user_facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    category TEXT NOT NULL,
                    fact_key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    statement TEXT NOT NULL,
                    status TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    revision INTEGER NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_confirmed_at TEXT NOT NULL,
                    supersedes_id INTEGER,
                    evidence_key TEXT NOT NULL UNIQUE,
                    source_ref TEXT NOT NULL,
                    provenance_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(supersedes_id) REFERENCES user_facts(id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS user_fact_evidence (
                    evidence_key TEXT PRIMARY KEY,
                    source_ref TEXT NOT NULL,
                    fact_id INTEGER NOT NULL,
                    decision TEXT NOT NULL,
                    candidate_json TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    FOREIGN KEY(fact_id) REFERENCES user_facts(id)
                )
                """
            )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS one_active_user_fact_per_key
                ON user_facts(category, fact_key)
                WHERE status = 'active'
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS user_facts_status_index
                ON user_facts(status, category, fact_key)
                """
            )
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def assimilate(
        self,
        candidates: Sequence[UserFactCandidate],
        *,
        observed_at: datetime | None = None,
    ) -> list[AssimilationResult]:
        if not candidates:
            return []
        timestamp = observed_at or datetime.now(timezone.utc)
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        timestamp_text = timestamp.astimezone(timezone.utc).isoformat()

        results: list[AssimilationResult] = []
        with self._connect() as connection:
            # Serialize the short read-decide-write window so concurrent successful
            # conversations cannot both create an active revision for one key.
            connection.execute("BEGIN IMMEDIATE")
            for candidate in candidates:
                results.append(
                    self._assimilate_one(connection, candidate, timestamp_text)
                )
        return results

    def _assimilate_one(
        self,
        connection: sqlite3.Connection,
        candidate: UserFactCandidate,
        timestamp: str,
    ) -> AssimilationResult:
        existing_evidence = connection.execute(
            "SELECT * FROM user_fact_evidence WHERE evidence_key = ?",
            (candidate.evidence_key,),
        ).fetchone()
        if existing_evidence is not None:
            fact = self._fact_by_id(connection, int(existing_evidence["fact_id"]))
            return AssimilationResult(
                decision=AssimilationDecision.DUPLICATE,
                fact=fact,
                evidence_key=candidate.evidence_key,
            )

        current_row = connection.execute(
            """
            SELECT * FROM user_facts
            WHERE category = ? AND fact_key = ? AND status = 'active'
            ORDER BY revision DESC, id DESC
            LIMIT 1
            """,
            (candidate.category.value, candidate.key),
        ).fetchone()

        if current_row is None:
            fact_id = self._insert_fact(
                connection,
                candidate,
                status=UserFactStatus.ACTIVE,
                revision=1,
                supersedes_id=None,
                first_seen_at=timestamp,
                timestamp=timestamp,
            )
            decision = AssimilationDecision.CREATED
        else:
            current = self._row_to_fact(current_row)
            if current.value == candidate.value:
                connection.execute(
                    """
                    UPDATE user_facts
                    SET confidence = MAX(confidence, ?),
                        last_confirmed_at = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (candidate.confidence, timestamp, timestamp, current.id),
                )
                fact_id = current.id
                decision = AssimilationDecision.CONFIRMED
            elif candidate.confidence >= current.confidence:
                connection.execute(
                    """
                    UPDATE user_facts
                    SET status = 'superseded', updated_at = ?
                    WHERE id = ? AND status = 'active'
                    """,
                    (timestamp, current.id),
                )
                fact_id = self._insert_fact(
                    connection,
                    candidate,
                    status=UserFactStatus.ACTIVE,
                    revision=current.revision + 1,
                    supersedes_id=current.id,
                    first_seen_at=current.first_seen_at,
                    timestamp=timestamp,
                )
                decision = AssimilationDecision.SUPERSEDED
            else:
                fact_id = self._insert_fact(
                    connection,
                    candidate,
                    status=UserFactStatus.DISPUTED,
                    revision=current.revision + 1,
                    supersedes_id=current.id,
                    first_seen_at=current.first_seen_at,
                    timestamp=timestamp,
                )
                decision = AssimilationDecision.DISPUTED

        candidate_json = json.dumps(
            {
                "category": candidate.category.value,
                "key": candidate.key,
                "value": candidate.value,
                "statement": candidate.statement,
                "confidence": candidate.confidence,
                "provenance": dict(candidate.provenance),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        connection.execute(
            """
            INSERT INTO user_fact_evidence (
                evidence_key, source_ref, fact_id, decision, candidate_json, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                candidate.evidence_key,
                candidate.source_ref,
                fact_id,
                decision.value,
                candidate_json,
                timestamp,
            ),
        )
        return AssimilationResult(
            decision=decision,
            fact=self._fact_by_id(connection, fact_id),
            evidence_key=candidate.evidence_key,
        )

    @staticmethod
    def _insert_fact(
        connection: sqlite3.Connection,
        candidate: UserFactCandidate,
        *,
        status: UserFactStatus,
        revision: int,
        supersedes_id: int | None,
        first_seen_at: str,
        timestamp: str,
    ) -> int:
        cursor = connection.execute(
            """
            INSERT INTO user_facts (
                category, fact_key, value, statement, status, confidence, revision,
                first_seen_at, last_confirmed_at, supersedes_id, evidence_key,
                source_ref, provenance_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate.category.value,
                candidate.key,
                candidate.value,
                candidate.statement,
                status.value,
                candidate.confidence,
                revision,
                first_seen_at,
                timestamp,
                supersedes_id,
                candidate.evidence_key,
                candidate.source_ref,
                json.dumps(dict(candidate.provenance), ensure_ascii=False, sort_keys=True),
                timestamp,
                timestamp,
            ),
        )
        return int(cursor.lastrowid)

    def get(self, fact_id: int) -> UserFact | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM user_facts WHERE id = ?",
                (int(fact_id),),
            ).fetchone()
        return self._row_to_fact(row) if row is not None else None

    def active_facts(self, limit: int = 200) -> list[UserFact]:
        if limit <= 0:
            return []
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM user_facts
                WHERE status = 'active'
                ORDER BY last_confirmed_at DESC, id DESC
                LIMIT ?
                """,
                (int(limit),),
            ).fetchall()
        return [self._row_to_fact(row) for row in rows]

    def audit_history(
        self,
        *,
        category: UserFactCategory | None = None,
        key: str | None = None,
    ) -> list[UserFact]:
        clauses: list[str] = []
        params: list[object] = []
        if category is not None:
            clauses.append("category = ?")
            params.append(category.value)
        if key is not None:
            clauses.append("fact_key = ?")
            params.append(key)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM user_facts{where} ORDER BY id ASC",
                tuple(params),
            ).fetchall()
        return [self._row_to_fact(row) for row in rows]

    def evidence_history(self) -> list[UserFactEvidence]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM user_fact_evidence ORDER BY observed_at ASC, evidence_key ASC"
            ).fetchall()
        return [self._row_to_evidence(row) for row in rows]

    def _fact_by_id(self, connection: sqlite3.Connection, fact_id: int) -> UserFact:
        row = connection.execute(
            "SELECT * FROM user_facts WHERE id = ?",
            (fact_id,),
        ).fetchone()
        if row is None:
            raise RuntimeError(f"user fact disappeared during transaction: {fact_id}")
        return self._row_to_fact(row)

    @staticmethod
    def _row_to_fact(row: sqlite3.Row) -> UserFact:
        supersedes_id = row["supersedes_id"]
        return UserFact(
            id=int(row["id"]),
            category=UserFactCategory(str(row["category"])),
            key=str(row["fact_key"]),
            value=str(row["value"]),
            statement=str(row["statement"]),
            status=UserFactStatus(str(row["status"])),
            confidence=float(row["confidence"]),
            revision=int(row["revision"]),
            first_seen_at=str(row["first_seen_at"]),
            last_confirmed_at=str(row["last_confirmed_at"]),
            supersedes_id=(int(supersedes_id) if supersedes_id is not None else None),
            evidence_key=str(row["evidence_key"]),
            source_ref=str(row["source_ref"]),
            provenance=json.loads(str(row["provenance_json"])),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _row_to_evidence(row: sqlite3.Row) -> UserFactEvidence:
        return UserFactEvidence(
            evidence_key=str(row["evidence_key"]),
            source_ref=str(row["source_ref"]),
            fact_id=int(row["fact_id"]),
            decision=AssimilationDecision(str(row["decision"])),
            candidate_json=str(row["candidate_json"]),
            observed_at=str(row["observed_at"]),
        )
