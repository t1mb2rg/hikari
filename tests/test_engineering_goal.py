from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from engineering.goal import (
    EngineeringGoalCoordinator,
    EngineeringGoalState,
    EngineeringGoalStep,
    EngineeringGoalStore,
)
from engineering.maintainer import project_session_authority_ceiling
from engineering.session import (
    EngineeringResult,
    EngineeringSessionState,
    EngineeringSessionStore,
    EngineeringTurn,
)


def _runtime(tmp_path: Path, *, effects: tuple[str, ...] = ("maintain_project", "push_engineering_branch")):
    repository = tmp_path / "repo"
    repository.mkdir()
    sessions = EngineeringSessionStore(tmp_path / "engineering")
    session = EngineeringSessionState.create(
        project_id="hikari",
        repository=repository,
        authority_ceiling=project_session_authority_ceiling(),
        session_id="goal-session",
    )
    sessions.create(session)
    goals = EngineeringGoalStore(tmp_path / "engineering_goals")
    steps = tuple(
        EngineeringGoalStep.create(
            effect=effect,
            instruction=f"do step {index}: {effect}",
            step_id=f"step-{index}",
        )
        for index, effect in enumerate(effects, start=1)
    )
    goal = EngineeringGoalState.create(
        project_id="hikari",
        session_id=session.session_id,
        goal="complete a persistent engineering goal",
        steps=steps,
        source_channel="qq",
        source_conversation_id="private:42",
        goal_id="goal-1",
    )
    goals.create(goal)
    return sessions, goals, EngineeringGoalCoordinator(goals, sessions)


def _complete_current_turn(
    sessions: EngineeringSessionStore,
    *,
    status: str = "completed",
    message: str = "step finished",
) -> str:
    state = sessions.load("goal-session")
    assert state.current_turn_id is not None
    turn_id = state.current_turn_id
    sessions.save_result(
        state.session_id,
        EngineeringResult(
            turn_id=turn_id,
            status=status,
            message=message,
        ),
    )
    return turn_id


def test_goal_store_round_trips_source_and_ordered_steps(tmp_path: Path) -> None:
    sessions, goals, _ = _runtime(tmp_path)

    loaded = goals.load("goal-1")

    assert sessions.load(loaded.session_id).project_id == "hikari"
    assert loaded.status == "active"
    assert loaded.source_channel == "qq"
    assert loaded.source_conversation_id == "private:42"
    assert [step.effect for step in loaded.steps] == [
        "maintain_project",
        "push_engineering_branch",
    ]
    assert loaded.current_step.step_id == "step-1"


def test_first_goal_step_enqueues_one_deterministic_turn(tmp_path: Path) -> None:
    sessions, goals, coordinator = _runtime(tmp_path)

    outcome = coordinator.advance_once("goal-1")

    assert outcome.action == "enqueued"
    state = sessions.load("goal-session")
    assert state.status == "pending"
    assert state.current_turn_id == outcome.turn_id
    goal = goals.load("goal-1")
    step = goal.current_step
    assert step.status == "queued"
    assert step.attempts == 1
    assert step.turn_id == outcome.turn_id
    turn = sessions.load_turn(state.session_id, state.current_turn_id)
    assert turn.intent == "do step 1: maintain_project"
    assert "Requested effect: maintain_project" in turn.context
    assert turn.authority.repository_write is True
    assert turn.authority.run_tests is True
    assert turn.authority.network is False
    assert turn.authority.publish is False


def test_repeated_or_restarted_pump_does_not_duplicate_active_turn(tmp_path: Path) -> None:
    sessions, goals, coordinator = _runtime(tmp_path)
    first = coordinator.advance_once("goal-1")
    assert first.turn_id is not None

    restarted = EngineeringGoalCoordinator(
        EngineeringGoalStore(goals.root),
        EngineeringSessionStore(sessions.root),
    )
    second = restarted.advance_once("goal-1")

    assert second.action == "waiting"
    assert second.turn_id == first.turn_id
    goal = goals.load("goal-1")
    assert goal.current_step.attempts == 1
    assert sessions.load("goal-session").current_turn_id == first.turn_id


def test_completed_step_advances_and_enqueues_next_step_without_user_turn(tmp_path: Path) -> None:
    sessions, goals, coordinator = _runtime(tmp_path)
    first = coordinator.advance_once("goal-1")
    assert first.turn_id is not None
    _complete_current_turn(sessions, message="edit committed")

    outcome = coordinator.advance_once("goal-1")

    assert outcome.action == "enqueued"
    goal = goals.load("goal-1")
    assert goal.status == "active"
    assert goal.current_step_index == 1
    assert goal.steps[0].status == "completed"
    assert goal.steps[0].result_message == "edit committed"
    assert goal.steps[1].status == "queued"
    assert goal.steps[1].turn_id == outcome.turn_id
    second_turn = sessions.load_turn(goal.session_id, outcome.turn_id or "")
    assert "Requested effect: push_engineering_branch" in second_turn.context
    assert second_turn.authority.repository_write is False
    assert second_turn.authority.network is True
    assert second_turn.authority.publish is True


def test_last_completed_step_marks_entire_goal_completed(tmp_path: Path) -> None:
    sessions, goals, coordinator = _runtime(tmp_path)
    coordinator.advance_once("goal-1")
    _complete_current_turn(sessions, message="edit committed")
    second = coordinator.advance_once("goal-1")
    assert second.turn_id is not None
    _complete_current_turn(sessions, message="branch pushed")

    outcome = coordinator.advance_once("goal-1")

    assert outcome.action == "goal_terminal"
    assert outcome.status == "completed"
    goal = goals.load("goal-1")
    assert goal.status == "completed"
    assert goal.final_summary == "branch pushed"
    assert [step.status for step in goal.steps] == ["completed", "completed"]


def test_failed_step_stops_goal_and_never_enqueues_later_step(tmp_path: Path) -> None:
    sessions, goals, coordinator = _runtime(tmp_path)
    first = coordinator.advance_once("goal-1")
    assert first.turn_id is not None
    _complete_current_turn(sessions, status="failed", message="backend failed")

    outcome = coordinator.advance_once("goal-1")

    assert outcome.status == "failed"
    assert outcome.action == "goal_terminal"
    goal = goals.load("goal-1")
    assert goal.status == "failed"
    assert goal.steps[0].status == "failed"
    assert goal.steps[1].status == "pending"
    assert goal.steps[1].turn_id is None


def test_blocked_step_stops_goal_without_crossing_boundary(tmp_path: Path) -> None:
    sessions, goals, coordinator = _runtime(tmp_path)
    coordinator.advance_once("goal-1")
    _complete_current_turn(sessions, status="blocked", message="authority boundary")

    outcome = coordinator.advance_once("goal-1")

    assert outcome.status == "blocked"
    goal = goals.load("goal-1")
    assert goal.status == "blocked"
    assert goal.final_summary == "authority boundary"
    assert goal.steps[1].status == "pending"


def test_goal_blocks_if_session_has_a_different_active_turn(tmp_path: Path) -> None:
    sessions, goals, coordinator = _runtime(tmp_path)
    sessions.enqueue_turn(
        "goal-session",
        EngineeringTurn.create(
            intent="unrelated",
            authority=project_session_authority_ceiling(),
        ),
    )

    outcome = coordinator.advance_once("goal-1")

    assert outcome.action == "conflict"
    assert outcome.status == "blocked"
    goal = goals.load("goal-1")
    assert goal.status == "blocked"
    assert goal.current_step.turn_id is None


def test_terminal_session_without_result_blocks_instead_of_inventing_completion(tmp_path: Path) -> None:
    sessions, goals, coordinator = _runtime(tmp_path)
    first = coordinator.advance_once("goal-1")
    assert first.turn_id is not None
    sessions.update_runtime("goal-session", status="completed", latest_summary="looks done")

    outcome = coordinator.advance_once("goal-1")

    assert outcome.action == "missing_result"
    assert outcome.status == "blocked"
    goal = goals.load("goal-1")
    assert "durable EngineeringResult is missing" in goal.final_summary


def test_persisted_turn_identity_can_recover_enqueue_after_crash_window(tmp_path: Path) -> None:
    sessions, goals, coordinator = _runtime(tmp_path, effects=("maintain_project",))
    goal = goals.load("goal-1")
    step = goal.current_step
    turn_id = coordinator._stable_turn_id(goal.goal_id, step.step_id, 1)
    queued = replace(
        step,
        status="queued",
        turn_id=turn_id,
        attempts=1,
    )
    goals.save(replace(goal, steps=(queued,)))

    restarted = EngineeringGoalCoordinator(
        EngineeringGoalStore(goals.root),
        EngineeringSessionStore(sessions.root),
    )
    outcome = restarted.advance_once("goal-1")

    assert outcome.action == "recovered_enqueue"
    assert outcome.turn_id == turn_id
    state = sessions.load("goal-session")
    assert state.current_turn_id == turn_id
    assert sessions.load_turn(state.session_id, turn_id).turn_id == turn_id
