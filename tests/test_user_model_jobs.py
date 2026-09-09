from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sqlite3

import pytest

from conversation.engine import ConversationEngine
from conversation.models import UserTurn
from conversation.natural import NaturalConversationEngine
from memory.store import MemoryStore
from user_model import UserFactCandidate, UserFactCategory, UserModelService, UserModelStore, make_evidence_key
from user_model.jobs import UserModelJobError, UserModelJobStore, UserModelJobWorker


class Extractor:
    def __init__(self):
        self.calls = []

    def extract(self, **kwargs):
        self.calls.append(kwargs)
        source = kwargs["source_ref"]
        return [UserFactCandidate(UserFactCategory.PREFERENCE, "reply_style", kwargs["current_user_text"],
                                 kwargs["current_user_text"], .98, source,
                                 make_evidence_key(source, "preference", "reply_style"), kwargs["provenance"])]


def _runtime(tmp_path):
    jobs = UserModelJobStore(tmp_path / "jobs.db")
    service = UserModelService(UserModelStore(tmp_path / "facts.db"))
    extractor = Extractor()
    worker = UserModelJobWorker(jobs, extractor, service, retry_delay_seconds=0)
    return jobs, service, extractor, worker


def _enqueue(jobs, source="one", text="brief", history=()):
    return jobs.enqueue(source_ref=source, turn=UserTurn("qq", "private:42", text, actor_id="42"),
                        recent_history=history, observed_at="2026-09-10T01:00:00+00:00")


@pytest.mark.parametrize("engine_type", [ConversationEngine, NaturalConversationEngine])
def test_conversation_reply_enqueues_without_inline_extraction(tmp_path, engine_type):
    jobs, service, extractor, worker = _runtime(tmp_path)

    class Chat:
        calls = 0
        def complete(self, messages):
            self.calls += 1
            return "Noted."

    chat = Chat()
    engine = engine_type(chat, MemoryStore(tmp_path / "memory.db"), user_model_service=service,
                         user_fact_extractor=extractor, assimilation_sink=jobs)
    reply = engine.respond(UserTurn("qq", "private:42", "brief", actor_id="42"), source_ref="request")
    assert reply.text == "Noted."
    assert chat.calls == 1
    assert extractor.calls == []
    assert jobs.get("request")["status"] == "queued"
    assert service.store.active_facts() == []
    assert worker.drain_once()["status"] == "completed"
    assert service.store.active_facts()[0].source_ref == "request"


def test_shared_turn_never_enqueues_or_extracts(tmp_path):
    jobs, service, extractor, _ = _runtime(tmp_path)
    class Chat:
        def complete(self, messages):
            return "Shared chat reply."
    engine = NaturalConversationEngine(Chat(), MemoryStore(tmp_path / "memory.db"),
                                       user_model_service=service, user_fact_extractor=extractor, assimilation_sink=jobs)
    turn = UserTurn("qq", "group:7", "brief", actor_id="42", scope="shared")
    engine.respond(turn, source_ref="shared")
    assert jobs.get("shared") is None
    assert extractor.calls == []
    with pytest.raises(UserModelJobError, match="private"):
        jobs.enqueue(source_ref="direct-shared", turn=turn, recent_history=[])


def test_immutable_source_replay_checks_owner_text_and_history(tmp_path):
    jobs, _, _, _ = _runtime(tmp_path)
    first = _enqueue(jobs, history=[{"role": "user", "content": "I prefer short replies"}])
    assert _enqueue(jobs, history=[{"role": "user", "content": "I prefer short replies"}])["sequence"] == first["sequence"]
    for turn, history in [
        (UserTurn("qq", "private:42", "brief", actor_id="other"), first["payload"]["history"]),
        (UserTurn("qq", "private:42", "changed", actor_id="42"), first["payload"]["history"]),
        (UserTurn("qq", "private:42", "brief", actor_id="42"), []),
    ]:
        with pytest.raises(UserModelJobError, match="immutable private"):
            jobs.enqueue(source_ref="one", turn=turn, recent_history=history)
    with pytest.raises(UserModelJobError, match="different private owner"):
        jobs.get("one", turn=UserTurn("qq", "private:99", "brief", actor_id="99"))
    with sqlite3.connect(jobs.path) as db, pytest.raises(sqlite3.IntegrityError):
        db.execute("UPDATE user_model_jobs SET payload_json='{}' WHERE source_ref='one'")


def test_oldest_retry_blocks_newer_preference_until_it_finishes(tmp_path):
    jobs, service, extractor, _ = _runtime(tmp_path)
    _enqueue(jobs, "old", "brief")
    _enqueue(jobs, "new", "detailed")
    class Flaky:
        failed = False
        def extract(self, **kwargs):
            if not self.failed:
                self.failed = True
                raise TimeoutError("private transport detail must not be stored")
            return extractor.extract(**kwargs)
    worker = UserModelJobWorker(jobs, Flaky(), service, retry_delay_seconds=0)
    assert worker.drain_once()["status"] == "retry"
    assert jobs.get("new")["attempts"] == 0
    assert jobs.get("old")["last_error_type"] == "TimeoutError"
    assert worker.drain_once()["source_ref"] == "old"
    assert worker.drain_once()["source_ref"] == "new"
    assert service.store.active_facts()[0].value == "detailed"


def test_waiting_retry_does_not_skip_to_newer_job(tmp_path):
    jobs, service, _, _ = _runtime(tmp_path)
    _enqueue(jobs, "one")
    _enqueue(jobs, "two")
    class Fails:
        def extract(self, **kwargs):
            raise RuntimeError("unavailable")
    worker = UserModelJobWorker(jobs, Fails(), service, retry_delay_seconds=60)
    assert worker.drain_once()["status"] == "retry"
    assert worker.drain_once()["action"] == "wait"
    assert jobs.get("two")["attempts"] == 0


def test_attempts_are_bounded_and_failed_job_does_not_starve_later_work(tmp_path):
    jobs, service, _, _ = _runtime(tmp_path)
    _enqueue(jobs, "one")
    _enqueue(jobs, "two")
    class Fails:
        def extract(self, **kwargs):
            raise RuntimeError("transport")
    worker = UserModelJobWorker(jobs, Fails(), service, max_attempts=2, retry_delay_seconds=0)
    assert worker.drain_once()["status"] == "retry"
    assert worker.drain_once()["status"] == "failed"
    assert worker.drain_once()["source_ref"] == "two"
    assert jobs.get("one")["attempts"] == 2


def test_crash_before_candidate_persistence_reextracts_only_frozen_history(tmp_path):
    jobs, service, extractor, _ = _runtime(tmp_path)
    original_history = [{"role": "user", "content": "specific older context"}]
    _enqueue(jobs, history=original_history)
    class Interrupted:
        def extract(self, **kwargs):
            assert kwargs["recent_history"] == original_history
            raise KeyboardInterrupt("simulated process interruption")
    with pytest.raises(KeyboardInterrupt):
        UserModelJobWorker(jobs, Interrupted(), service).drain_once()
    assert jobs.get("one")["candidates"] is None
    original_history[0]["content"] = "later unrelated context"
    restarted = UserModelJobWorker(UserModelJobStore(jobs.path), extractor, service)
    assert restarted.drain_once()["status"] == "completed"
    assert extractor.calls[0]["recent_history"][0]["content"] == "specific older context"


def test_crash_after_candidates_are_persisted_does_not_call_extractor_again(tmp_path, monkeypatch):
    jobs, service, extractor, worker = _runtime(tmp_path)
    _enqueue(jobs)
    actual = service.assimilate
    def crash(*args, **kwargs):
        raise KeyboardInterrupt("after candidate commit")
    monkeypatch.setattr(service, "assimilate", crash)
    with pytest.raises(KeyboardInterrupt):
        worker.drain_once()
    assert jobs.get("one")["candidates"][0]["value"] == "brief"
    assert service.store.active_facts() == []
    monkeypatch.setattr(service, "assimilate", actual)
    assert UserModelJobWorker(UserModelJobStore(jobs.path), extractor, service).drain_once()["status"] == "completed"
    assert len(extractor.calls) == 1


def test_crash_after_assimilation_replays_frozen_candidates_idempotently(tmp_path, monkeypatch):
    jobs, service, extractor, worker = _runtime(tmp_path)
    _enqueue(jobs)
    update = jobs._update
    def crash(source, **values):
        if values.get("status") == "completed":
            raise KeyboardInterrupt("after facts commit")
        return update(source, **values)
    monkeypatch.setattr(jobs, "_update", crash)
    with pytest.raises(KeyboardInterrupt):
        worker.drain_once()
    assert len(service.store.active_facts()) == 1
    restarted = UserModelJobWorker(UserModelJobStore(jobs.path), extractor, service)
    assert restarted.drain_once()["status"] == "completed"
    assert len(extractor.calls) == 1
    assert len(service.store.active_facts()) == 1
    assert service.store.active_facts()[0].revision == 1


def test_frozen_candidates_cannot_be_cleared_or_replaced(tmp_path):
    jobs, _, _, worker = _runtime(tmp_path)
    _enqueue(jobs)
    worker.drain_once()
    for replacement in (None, "[]"):
        with sqlite3.connect(jobs.path) as db, pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE user_model_jobs SET candidates_json=? WHERE source_ref='one'", (replacement,))


def test_second_drain_cannot_extract_or_assimilate_while_first_is_live(tmp_path):
    jobs, service, extractor, _ = _runtime(tmp_path)
    _enqueue(jobs)
    other = UserModelJobWorker(UserModelJobStore(jobs.path), extractor, service)
    with jobs.drain_lock() as acquired:
        assert acquired
        assert other.drain_once() == {"status": "busy", "action": "wait"}
    assert extractor.calls == []
    assert other.drain_once()["status"] == "completed"


def test_empty_extraction_is_durable_and_never_retried(tmp_path):
    jobs, service, _, _ = _runtime(tmp_path)
    _enqueue(jobs)
    class Empty:
        calls = 0
        def extract(self, **kwargs):
            self.calls += 1
            return []
    extractor = Empty()
    worker = UserModelJobWorker(jobs, extractor, service)
    assert worker.drain_once()["candidate_count"] == 0
    assert jobs.get("one")["candidates"] == []
    assert worker.drain_once()["status"] == "idle"
    assert extractor.calls == 1


def test_extractor_cannot_rebind_candidate_to_another_source(tmp_path):
    jobs, service, extractor, _ = _runtime(tmp_path)
    _enqueue(jobs)
    class WrongSource:
        def extract(self, **kwargs):
            return [replace(extractor.extract(**kwargs)[0], source_ref="other")]
    worker = UserModelJobWorker(jobs, WrongSource(), service, max_attempts=1)
    assert worker.drain_once()["status"] == "failed"
    assert jobs.get("one")["candidates"] is None
    assert service.store.active_facts() == []


def test_sink_failure_cannot_report_success_or_fall_back_to_inline_model(tmp_path):
    class Chat:
        def complete(self, messages):
            return "Noted."
    extractor = Extractor()
    def unavailable_sink(**kwargs):
        raise OSError("queue unavailable")
    engine = ConversationEngine(Chat(), MemoryStore(tmp_path / "memory.db"),
                                 user_fact_extractor=extractor, assimilation_sink=unavailable_sink)
    with pytest.raises(OSError, match="queue unavailable"):
        engine.respond(UserTurn("qq", "private:42", "brief"), source_ref="failed-enqueue")
    assert extractor.calls == []


def test_engine_freezes_only_same_private_actor_history(tmp_path):
    jobs, _, _, _ = _runtime(tmp_path)
    class Chat:
        def complete(self, messages):
            return "Noted."
    memory = MemoryStore(tmp_path / "memory.db")
    for actor, scope, text in [("42", "private", "own earlier preference"), ("99", "private", "other actor"), ("42", "shared", "shared history")]:
        memory.remember_event("conversation.user", text, context={"channel": "qq", "conversation_id": "private:42", "actor_id": actor, "scope": scope}, importance=1)
    engine = ConversationEngine(Chat(), memory, assimilation_sink=jobs)
    engine.respond(UserTurn("qq", "private:42", "brief", actor_id="42"), source_ref="snapshot")
    assert jobs.get("snapshot")["payload"]["history"] == [{"role": "user", "content": "own earlier preference"}]
