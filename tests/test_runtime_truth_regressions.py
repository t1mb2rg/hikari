from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import replace
import json
from pathlib import Path
from threading import Barrier, Event, Lock

import pytest

from conversation.engine import ConversationEngine
from conversation.engineering_bridge import ConversationEngineeringBridge
from conversation.models import AssistantReply, UserTurn
from conversation.receipts import ConversationReceiptStore
from conversation.remote import ConversationRequestProcessor
from core.delivery import DeliveryOutbox
from engineering.bindings import EngineeringConversationBinding, EngineeringConversationBindingStore
from engineering.delivery import EngineeringCompletionDelivery
from engineering.goal import EngineeringGoalState, EngineeringGoalStep, EngineeringGoalStore, EngineeringGoalStoreError
from engineering.maintainer_loop import PersistentMaintainerLoop
from engineering.session import EngineeringAuthority, EngineeringResult, EngineeringSessionState, EngineeringSessionStore, EngineeringTurn
from engineering.worker import EngineeringWorker


class ControlledEngine(ConversationEngine):
    """Fake only Hikari's cognition seam; receipt stores and concurrency are real."""

    def __init__(self, *, block: bool = False):
        self.calls = 0
        self.started = Event()
        self.release = Event()
        self.counter_lock = Lock()
        if not block:
            self.release.set()

    def respond(self, turn, *, source_ref=None):
        with self.counter_lock:
            self.calls += 1
        self.started.set()
        assert self.release.wait(timeout=5)
        return AssistantReply(turn.channel, turn.conversation_id, "one result")


def test_concurrent_retries_across_processors_call_engine_once(tmp_path: Path):
    engine = ControlledEngine(block=True)
    path = tmp_path / "receipts.db"
    first = ConversationRequestProcessor(engine, ConversationReceiptStore(path))
    second = ConversationRequestProcessor(engine, ConversationReceiptStore(path))
    turn = UserTurn("qq", "private:42", "hello", actor_id="42")
    with ThreadPoolExecutor(max_workers=2) as pool:
        original = pool.submit(first.process, "same-request", turn)
        try:
            assert engine.started.wait(timeout=5)
            retried = pool.submit(second.process, "same-request", turn)
            with pytest.raises(FutureTimeout):
                retried.result(timeout=0.1)
        finally:
            engine.release.set()
        reply, duplicate = original.result(timeout=5)
        repeated, repeated_duplicate = retried.result(timeout=5)
    assert engine.calls == 1
    assert reply == repeated
    assert duplicate is False and repeated_duplicate is True


def test_sqlite_claim_is_atomic_across_store_instances(tmp_path: Path):
    stores = [ConversationReceiptStore(tmp_path / "receipts.db") for _ in range(2)]
    barrier = Barrier(2)
    turn = UserTurn("qq", "private:42", "hello")

    def claim(store):
        barrier.wait(timeout=5)
        return store.claim("same-request", turn)

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(claim, stores)) == [False, True]


def test_unfinished_claim_survives_host_reconstruction_without_reexecution(tmp_path: Path):
    path = tmp_path / "receipts.db"
    turn = UserTurn("qq", "private:42", "do work", actor_id="42")
    assert ConversationReceiptStore(path).claim("interrupted", turn)
    engine = ControlledEngine()
    restarted = ConversationRequestProcessor(engine, ConversationReceiptStore(path))
    reply, duplicate = restarted.process("interrupted", turn)
    assert duplicate and engine.calls == 0
    assert "无法确认" in reply.text and "没有重新执行" in reply.text
    assert restarted.receipts.get("interrupted") is None
    with pytest.raises(ValueError, match="different user turn"):
        restarted.process("interrupted", replace(turn, scope="shared"))


def test_receipt_write_failure_after_action_does_not_repeat_action(tmp_path: Path, monkeypatch):
    store = ConversationReceiptStore(tmp_path / "receipts.db")
    engine = ControlledEngine()
    goals = EngineeringGoalStore(tmp_path / "engineering_goals")

    class GoalBridge:
        def respond(self, engine, turn, *, source_ref=None):
            goals.create(EngineeringGoalState.create(
                project_id="hikari", session_id="session", goal="durable work",
                steps=(EngineeringGoalStep.create(effect="inspect_project", instruction="inspect"),),
            ))
            return AssistantReply(turn.channel, turn.conversation_id, "accepted")

    processor = ConversationRequestProcessor(engine, store, action_bridge=GoalBridge())

    def fail_save(*args, **kwargs):
        raise OSError("simulated receipt write failure")

    monkeypatch.setattr(store, "save", fail_save)
    turn = UserTurn("qq", "private:42", "inspect")
    with pytest.raises(OSError, match="receipt write failure"):
        processor.process("action-request", turn)
    restarted = ConversationRequestProcessor(
        engine, ConversationReceiptStore(store.path), action_bridge=GoalBridge(),
    )
    reply, duplicate = restarted.process("action-request", turn)
    assert duplicate and "无法确认" in reply.text
    assert len(goals.list_states()) == 1
    assert engine.calls == 0
    # Failure of one request must not disable subsequent independent messages.
    restarted.process("new-request", UserTurn("qq", "private:42", "new inspection"))
    assert len(goals.list_states()) == 2


def test_legacy_receipt_needs_no_claim_and_keeps_original_reply(tmp_path: Path):
    store = ConversationReceiptStore(tmp_path / "receipts.db")
    turn = UserTurn("qq", "private:42", "old request")
    reply = AssistantReply("qq", "private:42", "historical reply")
    store.save("legacy", turn, reply)
    engine = ControlledEngine()
    processor = ConversationRequestProcessor(engine, ConversationReceiptStore(store.path))
    assert processor.process("legacy", turn) == (reply, True)
    assert engine.calls == 0


def _engineering(tmp_path):
    sessions = EngineeringSessionStore(tmp_path / "engineering")
    session = EngineeringSessionState.create(
        project_id="hikari", repository=tmp_path,
        authority_ceiling=EngineeringAuthority.read_only(),
    )
    sessions.create(session)
    bindings = EngineeringConversationBindingStore(tmp_path / "bindings.json")
    bindings.bind(EngineeringConversationBinding(
        session_id=session.session_id, channel="qq", conversation_id="private:42",
    ))
    goals = EngineeringGoalStore(tmp_path / "engineering_goals")
    bridge = ConversationEngineeringBridge(sessions, bindings, repository=tmp_path, goals=goals)
    return sessions, session, bindings, goals, bridge


def _finished_goal(goals, session):
    step = EngineeringGoalStep(
        step_id="old-step", effect="inspect_project", instruction="old work",
        turn_id="old-turn", attempts=1, status="completed",
        result_status="completed", result_message="old result",
    )
    goal = replace(EngineeringGoalState.create(
        project_id="hikari", session_id=session.session_id, goal="OLD goal", steps=(step,),
    ), status="completed", final_summary="old result")
    goals.create(goal)
    return goal


@pytest.mark.parametrize("status", ["pending", "running", "completed", "failed", "blocked"])
def test_old_goal_cannot_mask_new_single_turn(tmp_path: Path, status: str):
    sessions, session, _, goals, bridge = _engineering(tmp_path)
    _finished_goal(goals, session)
    turn = EngineeringTurn.create(intent="NEW inspection", authority=EngineeringAuthority.read_only())
    sessions.enqueue_turn(session.session_id, turn)
    if status in {"completed", "failed", "blocked"}:
        sessions.save_result(session.session_id, EngineeringResult(
            turn_id=turn.turn_id, status=status, message="NEW result",
        ))
    elif status == "running":
        sessions.update_runtime(session.session_id, status=status, latest_summary="NEW progress")
    reply = bridge._status_reply(UserTurn("qq", "private:42", "status"))
    assert "NEW inspection" in reply.text and status in reply.text
    assert "OLD goal" not in reply.text and "old result" not in reply.text


def test_active_goal_is_visible_before_first_enqueue_despite_previous_turn(tmp_path: Path):
    sessions, session, _, goals, bridge = _engineering(tmp_path)
    old = _finished_goal(goals, session)
    sessions.save(replace(session, status="completed", current_turn_id="old-turn"))
    active = EngineeringGoalState.create(
        project_id="hikari", session_id=session.session_id, goal="NEW active goal",
        steps=(EngineeringGoalStep.create(effect="inspect_project", instruction="new work"),),
    )
    goals.create(active)
    reply = bridge._status_reply(UserTurn("qq", "private:42", "status"))
    assert "NEW active goal" in reply.text and "active" in reply.text
    assert old.goal not in reply.text


@pytest.mark.parametrize("corruption", ["json", "unicode", "schema", "identity"])
def test_unreadable_goal_stops_scheduling_and_reports_record(tmp_path: Path, corruption: str):
    sessions, session, _, goals, bridge = _engineering(tmp_path)
    goal = _finished_goal(goals, session)
    path = goals.root / f"{goal.goal_id}.json"
    payload = goal.to_mapping()
    if corruption == "json":
        path.write_text("{broken", encoding="utf-8")
    elif corruption == "unicode":
        path.write_bytes(b"\xff")
    else:
        payload["steps" if corruption == "schema" else "goal_id"] = (
            None if corruption == "schema" else "wrong-identity"
        )
        path.write_text(json.dumps(payload), encoding="utf-8")
    original = path.read_bytes()
    with pytest.raises(EngineeringGoalStoreError, match=goal.goal_id):
        PersistentMaintainerLoop(goals, sessions).advance_all()
    reply = bridge._status_reply(UserTurn("qq", "private:42", "status"))
    assert "不可读取" in reply.text and goal.goal_id in reply.text
    assert path.read_bytes() == original
    assert sessions.load(session.session_id).status == "idle"


def test_corrupt_goal_blocks_worker_replay_execution_and_delivery(tmp_path: Path):
    sessions, session, bindings, goals, _ = _engineering(tmp_path)
    goals.root.mkdir()
    (goals.root / "broken.json").write_text("{broken", encoding="utf-8")
    turn = EngineeringTurn.create(intent="pending work", authority=EngineeringAuthority.read_only())
    sessions.enqueue_turn(session.session_id, turn)
    with pytest.raises(EngineeringGoalStoreError):
        EngineeringWorker(sessions).run_once()
    assert sessions.load(session.session_id).status == "pending"
    sessions.update_runtime(session.session_id, status="running", latest_summary="interrupted")
    outbox = DeliveryOutbox(tmp_path / "outbox.db")
    for renderer in (None, lambda *_: "must not be delivered"):
        delivery = EngineeringCompletionDelivery(sessions, bindings, outbox, renderer=renderer)
        with pytest.raises(EngineeringGoalStoreError):
            delivery.pump()
    assert sessions.load(session.session_id).status == "running"
    assert outbox.pending() == []
