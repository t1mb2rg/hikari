from __future__ import annotations

import json
from pathlib import Path

from core.delivery import DeliveryOutbox
from engineering.bindings import (
    EngineeringConversationBinding,
    EngineeringConversationBindingStore,
)
from engineering.delivery import EngineeringCompletionDelivery, EngineeringCompletionFacts
from engineering.goal import EngineeringGoalState, EngineeringGoalStep, EngineeringGoalStore
from engineering.maintainer import project_session_authority_ceiling
from engineering.planning import build_engineering_goal_plan
from engineering.session import EngineeringResult, EngineeringSessionState, EngineeringSessionStore


def _resident(
    root: Path,
    rendered: list[EngineeringCompletionFacts],
) -> EngineeringCompletionDelivery:
    sessions = EngineeringSessionStore(root / "engineering")
    bindings = EngineeringConversationBindingStore(root / "engineering_bindings.json")
    outbox = DeliveryOutbox(root / "proactive_delivery.db")

    def renderer(facts: EngineeringCompletionFacts, channel: str, conversation_id: str) -> str:
        rendered.append(facts)
        return f"terminal:{facts.status}:{facts.goal}"

    return EngineeringCompletionDelivery(
        sessions,
        bindings,
        outbox,
        renderer=renderer,
    )


def _worker(root: Path) -> EngineeringCompletionDelivery:
    return EngineeringCompletionDelivery(
        EngineeringSessionStore(root / "engineering"),
        EngineeringConversationBindingStore(root / "engineering_bindings.json"),
        DeliveryOutbox(root / "proactive_delivery.db"),
    )


def _write_result_without_state(
    sessions: EngineeringSessionStore,
    session_id: str,
    result: EngineeringResult,
) -> None:
    path = sessions.root / session_id / "results" / f"{result.turn_id}.json"
    path.write_text(
        json.dumps(result.to_mapping(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def test_m7c_goal_survives_multiple_runtime_boundaries_without_duplicate_effects_or_delivery(
    tmp_path: Path,
) -> None:
    root = tmp_path / "resident"
    repository = tmp_path / "repo"
    repository.mkdir()
    sessions = EngineeringSessionStore(root / "engineering")
    bindings = EngineeringConversationBindingStore(root / "engineering_bindings.json")
    goals = EngineeringGoalStore(root / "engineering_goals")

    session = EngineeringSessionState.create(
        project_id="hikari",
        repository=repository,
        authority_ceiling=project_session_authority_ceiling(),
        session_id="graduation-session",
    )
    sessions.create(session)
    bindings.bind(
        EngineeringConversationBinding(
            session_id=session.session_id,
            channel="qq",
            conversation_id="private:42",
        )
    )
    plan = build_engineering_goal_plan(
        goal="create M7-C graduation evidence and open a Draft PR",
        requested_effects=("maintain_project", "open_or_update_draft_pr"),
        original_request=(
            "create docs/M7-C_GRADUATION.md with the requested evidence only, "
            "then open a Draft PR"
        ),
    )
    goal = EngineeringGoalState.create(
        project_id="hikari",
        session_id=session.session_id,
        goal=plan.goal,
        steps=tuple(
            EngineeringGoalStep.create(
                effect=step.effect,
                instruction=step.instruction,
                step_id=f"step-{index + 1}",
            )
            for index, step in enumerate(plan.steps)
        ),
        source_channel="qq",
        source_conversation_id="private:42",
        goal_id="graduation-goal",
    )
    goals.create(goal)
    assert tuple(step.effect for step in goal.steps) == (
        "maintain_project",
        "push_engineering_branch",
        "open_or_update_draft_pr",
    )

    rendered: list[EngineeringCompletionFacts] = []

    # Resident incarnation #1 discovers the durable goal and creates exactly one first turn.
    resident_1 = _resident(root, rendered)
    assert resident_1.pump() == 0
    goal = goals.load(goal.goal_id)
    first_turn = goal.current_step.turn_id
    assert first_turn is not None
    assert goal.current_step.attempts == 1
    first_authority = sessions.load_turn(session.session_id, first_turn).authority
    assert first_authority.repository_write is True
    assert first_authority.network is False
    assert first_authority.outside_repo is False

    # The Worker dies after marking the turn running. New Worker ownership must recover
    # the same turn id instead of creating attempt 2 or a duplicate maintenance action.
    sessions.update_runtime(session.session_id, status="running", latest_summary="editing")
    _worker(root).pump()
    recovered = sessions.load(session.session_id)
    assert recovered.status == "pending"
    assert recovered.current_turn_id == first_turn
    goal = goals.load(goal.goal_id)
    assert goal.current_step.turn_id == first_turn
    assert goal.current_step.attempts == 1

    sessions.save_result(
        session.session_id,
        EngineeringResult(
            turn_id=first_turn,
            status="completed",
            message="graduation evidence committed",
            changed_files=("docs/M7-C_GRADUATION.md",),
            completed_at=100.0,
        ),
    )

    # Resident incarnation #2 consumes the real result and autonomously selects push.
    resident_2 = _resident(root, rendered)
    assert resident_2.pump() == 0
    goal = goals.load(goal.goal_id)
    assert goal.current_step_index == 1
    push_turn = goal.current_step.turn_id
    assert push_turn is not None and push_turn != first_turn
    push_authority = sessions.load_turn(session.session_id, push_turn).authority
    assert push_authority.publish is True
    assert push_authority.repository_write is False
    assert push_authority.outside_repo is False

    # Crash window: the push result reaches durable storage before state.json is updated.
    # New Worker ownership must finalize that exact result rather than replaying the push.
    sessions.update_runtime(session.session_id, status="running", latest_summary="pushing")
    push_result = EngineeringResult(
        turn_id=push_turn,
        status="completed",
        message="engineering branch pushed",
        completed_at=200.0,
    )
    _write_result_without_state(sessions, session.session_id, push_result)
    _worker(root).pump()
    finalized_push = sessions.load(session.session_id)
    assert finalized_push.status == "completed"
    assert finalized_push.current_turn_id == push_turn
    assert finalized_push.latest_summary == "engineering branch pushed"

    # Resident incarnation #3 advances to Draft PR without another user message.
    resident_3 = _resident(root, rendered)
    assert resident_3.pump() == 0
    goal = goals.load(goal.goal_id)
    assert goal.current_step_index == 2
    pr_turn = goal.current_step.turn_id
    assert pr_turn is not None and pr_turn not in {first_turn, push_turn}
    pr_authority = sessions.load_turn(session.session_id, pr_turn).authority
    assert pr_authority.publish is True
    assert pr_authority.repository_write is False
    assert pr_authority.outside_repo is False
    sessions.save_result(
        session.session_id,
        EngineeringResult(
            turn_id=pr_turn,
            status="completed",
            message="Draft PR #999 created",
            completed_at=300.0,
        ),
    )

    # Resident incarnation #4 reaches whole-goal terminal truth and enqueues one delivery.
    resident_4 = _resident(root, rendered)
    assert resident_4.pump() == 1
    terminal = goals.load(goal.goal_id)
    assert terminal.status == "completed"
    assert [step.status for step in terminal.steps] == [
        "completed",
        "completed",
        "completed",
    ]
    assert [step.attempts for step in terminal.steps] == [1, 1, 1]
    assert len(rendered) == 1
    assert rendered[0].status == "completed"
    assert rendered[0].changed_files == ("docs/M7-C_GRADUATION.md",)

    outbox = DeliveryOutbox(root / "proactive_delivery.db")
    delivery_id = f"engineering-goal:{goal.goal_id}"
    record = outbox.get(delivery_id)
    assert record is not None
    assert record.state == "pending"
    assert record.request.text == f"terminal:completed:{plan.goal}"

    # Repeated/restarted Resident pumps reuse the same delivery id and never render a
    # second terminal message.
    resident_5 = _resident(root, rendered)
    assert resident_5.pump() == 1
    assert len(rendered) == 1
    assert outbox.get(delivery_id) == record

    # If the QQ transport crashes after claiming the terminal message, quarantine the
    # uncertain send rather than enqueueing/retrying a duplicate automatically.
    outbox.claim(delivery_id)
    assert outbox.recover_inflight() == 1
    uncertain = outbox.get(delivery_id)
    assert uncertain is not None and uncertain.state == "uncertain"
    resident_6 = _resident(root, rendered)
    assert resident_6.pump() == 1
    assert len(rendered) == 1
    still_uncertain = outbox.get(delivery_id)
    assert still_uncertain is not None
    assert still_uncertain.state == "uncertain"
    assert still_uncertain.attempts == 1
