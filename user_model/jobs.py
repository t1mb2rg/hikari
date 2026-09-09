"""Durable ordered private fact extraction outside the conversation reply path."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import time

from conversation.models import UserTurn

from .models import UserFactCandidate, UserFactCategory
from .extractor import MAX_CANDIDATES_PER_TURN


class UserModelJobError(ValueError):
    pass


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _owner(turn):
    if not isinstance(turn, UserTurn) or turn.is_shared:
        raise UserModelJobError("user-model jobs require a private conversation")
    return {"channel": turn.channel, "conversation_id": turn.conversation_id,
            "actor_id": turn.actor_id, "scope": turn.scope}


class UserModelJobStore:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS user_model_jobs (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, source_ref TEXT UNIQUE NOT NULL,
                    payload_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
                    candidates_json TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                    retry_at REAL NOT NULL DEFAULT 0, last_error_type TEXT,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL);
                CREATE TRIGGER IF NOT EXISTS immutable_user_model_job_source
                BEFORE UPDATE OF source_ref,payload_json ON user_model_jobs
                BEGIN SELECT RAISE(ABORT, 'user-model source snapshots are immutable'); END;
                CREATE TRIGGER IF NOT EXISTS immutable_user_model_job_candidates
                BEFORE UPDATE OF candidates_json ON user_model_jobs
                WHEN OLD.candidates_json IS NOT NULL AND NEW.candidates_json IS NOT OLD.candidates_json
                BEGIN SELECT RAISE(ABORT, 'extracted candidates are immutable'); END;
            """)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _row(row):
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        raw = result.pop("candidates_json")
        result["candidates"] = json.loads(raw) if raw is not None else None
        return result

    def enqueue(self, *, source_ref: str, turn: UserTurn, recent_history, observed_at: str | None = None):
        owner = _owner(turn)
        if not isinstance(source_ref, str) or not source_ref.strip() or len(source_ref) > 256:
            raise UserModelJobError("user-model job requires a stable bounded source_ref")
        if not isinstance(recent_history, (list, tuple)):
            raise UserModelJobError("job history must be a private message sequence")
        history = []
        for item in recent_history[-6:]:
            if (not isinstance(item, dict) or item.get("role") not in {"user", "assistant"}
                    or not isinstance(item.get("content"), str)):
                raise UserModelJobError("job history must contain role and content")
            history.append({"role": item["role"], "content": item["content"]})
        immutable = {"owner": owner, "text": turn.text, "history": history}
        if len(_canonical(immutable).encode("utf-8")) > 256_000:
            raise UserModelJobError("user-model job snapshot is too large")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = self._row(db.execute("SELECT * FROM user_model_jobs WHERE source_ref=?", (source_ref,)).fetchone())
            if old:
                if any(old["payload"].get(key) != value for key, value in immutable.items()):
                    raise UserModelJobError("source_ref conflicts with immutable private request")
                return old
            timestamp = observed_at or datetime.now(timezone.utc).isoformat()
            parsed_time = datetime.fromisoformat(timestamp)
            if parsed_time.tzinfo is None:
                raise UserModelJobError("source timestamp must include timezone")
            payload = {**immutable, "observed_at": parsed_time.astimezone(timezone.utc).isoformat()}
            now = time.time()
            db.execute("INSERT INTO user_model_jobs(source_ref,payload_json,created_at,updated_at) VALUES(?,?,?,?)",
                       (source_ref, _canonical(payload), now, now))
        return self.get(source_ref, turn=turn)

    __call__ = enqueue

    def get(self, source_ref: str, *, turn: UserTurn | None = None):
        with self._db() as db:
            result = self._row(db.execute("SELECT * FROM user_model_jobs WHERE source_ref=?", (source_ref,)).fetchone())
        if result is not None and turn is not None and result["payload"]["owner"] != _owner(turn):
            raise UserModelJobError("user-model job belongs to a different private owner")
        return result

    def _oldest(self):
        with self._db() as db:
            return self._row(db.execute("SELECT * FROM user_model_jobs WHERE status NOT IN ('completed','failed') ORDER BY sequence LIMIT 1").fetchone())

    def _update(self, source_ref, **values):
        allowed = {"status", "candidates_json", "attempts", "retry_at", "last_error_type"}
        if not values or set(values) - allowed:
            raise UserModelJobError("unsupported job state update")
        with self._db() as db:
            names = [*values, "updated_at"]
            db.execute("UPDATE user_model_jobs SET " + ",".join(name + "=?" for name in names) + " WHERE source_ref=?",
                       (*values.values(), time.time(), source_ref))

    @contextmanager
    def drain_lock(self):
        """One OS-owned drain lock; crash recovery never steals a live worker lease."""
        lock_path = self.path.with_suffix(self.path.suffix + ".drain.lock")
        acquired = False
        with lock_path.open("a+b") as stream:
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            try:
                import os
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except (OSError, BlockingIOError):
                pass
            try:
                yield acquired
            finally:
                if acquired:
                    stream.seek(0)
                    if os.name == "nt":
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class UserModelJobWorker:
    """Extract and assimilate one oldest job, with durable candidates and bounded retries.

    The extractor retains its existing strict semantics. Configure its provider
    timeout at construction; this worker never starts background model threads.
    """
    def __init__(self, store: UserModelJobStore, extractor, service, *, max_attempts: int = 3,
                 retry_delay_seconds: float = 5):
        if not isinstance(store, UserModelJobStore) or type(max_attempts) is not int or max_attempts < 1:
            raise ValueError("worker needs a job store and positive max_attempts")
        if retry_delay_seconds < 0:
            raise ValueError("retry delay must not be negative")
        self.store, self.extractor, self.service = store, extractor, service
        self.max_attempts, self.retry_delay_seconds = max_attempts, retry_delay_seconds

    def drain_once(self) -> dict:
        with self.store.drain_lock() as acquired:
            if not acquired:
                return {"status": "busy", "action": "wait"}
            job = self.store._oldest()
            if job is None:
                return {"status": "idle", "action": "none"}
            source = job["source_ref"]
            if job["retry_at"] > time.time():
                return {"status": "retry", "action": "wait", "source_ref": source, "retry_at": job["retry_at"]}
            if job["attempts"] >= self.max_attempts:
                self.store._update(source, status="failed", last_error_type="AttemptsExhausted")
                return {"status": "failed", "action": "exhausted", "source_ref": source, "attempts": job["attempts"]}
            attempt = job["attempts"] + 1
            self.store._update(source, status="extracting" if job["candidates"] is None else "candidates_ready", attempts=attempt)
            try:
                payload = job["payload"]
                owner = payload["owner"]
                if owner.get("scope") != "private":
                    raise UserModelJobError("shared job snapshot cannot be assimilated")
                provenance = {"source": "successful_conversation_turn", "source_ref": source,
                              "channel": owner["channel"], "conversation_id": owner["conversation_id"], "scope": "private"}
                if owner.get("actor_id") is not None:
                    provenance["actor_id"] = owner["actor_id"]
                frozen = job["candidates"]
                if frozen is None:
                    proposed = self.extractor.extract(source_ref=source, current_user_text=payload["text"],
                                                      recent_history=payload["history"], provenance=provenance)
                    if not isinstance(proposed, (list, tuple)) or len(proposed) > MAX_CANDIDATES_PER_TURN:
                        raise UserModelJobError("extractor returned an invalid candidate batch")
                    candidates = []
                    for candidate in proposed:
                        if candidate.source_ref != source:
                            raise UserModelJobError("extractor changed source request identity")
                        candidates.append(self.service.normalize_candidate(replace(candidate, provenance=provenance)))
                    frozen = [{**asdict(candidate), "category": candidate.category.value} for candidate in candidates]
                    self.store._update(source, status="candidates_ready", candidates_json=_canonical(frozen))
                candidates = [UserFactCandidate(**{**item, "category": UserFactCategory(item["category"])}) for item in frozen]
                self.service.assimilate(candidates, observed_at=datetime.fromisoformat(payload["observed_at"]))
                self.store._update(source, status="completed", last_error_type=None, retry_at=0)
                return {"status": "completed", "action": "assimilated", "source_ref": source,
                        "attempts": attempt, "candidate_count": len(candidates)}
            except Exception as exc:
                status = "failed" if attempt >= self.max_attempts else "retry"
                self.store._update(source, status=status, last_error_type=type(exc).__name__,
                                   retry_at=time.time() + self.retry_delay_seconds if status == "retry" else 0)
                return {"status": status, "action": "degraded", "source_ref": source,
                        "attempts": attempt, "error_type": type(exc).__name__}
