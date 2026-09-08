from __future__ import annotations

from pathlib import Path

from engineering.goal import (
    EngineeringGoalState,
    EngineeringGoalStep,
    EngineeringGoalStore,
)
from engineering.maintainer import project_session_authority_ceiling
from engineering.maintainer_loop import PersistentMaintainerLoop
from engineering.session import (
    EngineeringResult,
    EngineeringSessionState,
    EngineeringSessionStore,
)


def _runtime(tmp_path: Path, *, effect: str = "maintain_project"):
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
    goal = EngineeringGoalState.create(
        project_id="hikari",
        session_id=session.session_id,
        goal="recover persistent engineering work",
        steps=(
            EngineeringGoalStep.create(
                effect=effect,
                instruction=f"do {effect}",
                step_id="step-1",
            ),
        ),
        source_channel="qq",
        source_conversation_id="private:42",
        goal_id="goal-1",
    )
    goals.create(goal)
    return sessions, goals, PersistentMaintainerLoop(goals, sessions)


def _save_current_result(
    sessions: EngineeringSessionStore,
    *,
    status: str,
    message: str,
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


def test_retryable_failed_step_gets_one_new_deterministic_attempt(tmp_path: Path) -> None:
    sessions, goals, loop = _runtime(tmp_path)
    first = loop.advance_once("goal-1")
    assert first.turn_id is not None
    _save_current_result(sessions, status="failed", message="backend failed")

    retry = loop.advance_once("goal-1")

    goal = goals.load("goal-1")
    assert goal.status == "active"
    assert goal.current_step.attempts == 2
    assert goal.current_step.turn_id is not None
    assert goal.current_step.turn_id != first.turn_id
    assert retry.action in {"recovered_enqueue", "waiting"}
    state = sessions.load("goal-session")
    assert state.status == "pending"
    assert state.current_turn_id == goal.current_step.turn_id


def test_second_failure_exhausts_retry_budget_and_stays_failed(tmp_path: Path) -> None:
    sessions, goals, loop = _runtime(tmp_path)
    loop.advance_once("goal-1")
    _save_current_result(sessions, status="failed", message="first failure")
    loop.advance_once("goal-1")
    _save_current_result(sessions, status="failed", message="second failure")

    terminal = loop.advance_once("goal-1")

    goal = goals.load("goal-1")
    assert terminal.status == "failed"
    assert goal.status == "failed"
    assert goal.current_step.attempts == 2
    assert goal.final_summary == "second failure"


def test_blocked_step_is_never_retried(tmp_path: Path) -> None:
    sessions, goals, loop = _runtime(tmp_path)
    loop.advance_once("goal-1")
    _save_current_result(sessions, status="blocked", message="authority boundary")

    outcome = loop.advance_once("goal-1")

    goal = goals.load("goal-1")
    assert outcome.status == "blocked"
    assert goal.status == "blocked"
    assert goal.current_step.attempts == 1


def test_project_command_failure_is_not_automatically_replayed(tmp_path: Path) -> None:
    sessions, goals, loop = _runtime(tmp_path, effect="run_project_command")
    loop.advance_once("goal-1")
    _save_current_result(sessions, status="failed", message="command failed")

    outcome = loop.advance_once("goal-1")

    goal = goals.load("goal-1")
    assert outcome.status == "failed"
    assert goal.status == "failed"
    assert goal.current_step.attempts == 1


def test_restarted_loop_recovers_a_persisted_retryable_failure(tmp_path: Path) -> None:
    sessions, goals, loop = _runtime(tmp_path)
    loop.advance_once("goal-1")
    _save_current_result(sessions, status="failed", message="backend failed")
    # First coordinator pass persists a failed goal, simulating a process boundary before retry.
    loop.coordinator.advance_once("goal-1")
    assert goals.load("goal-1").status == "failed"

    restarted = PersistentMaintainerLoop(
        EngineeringGoalStore(goals.root),
        EngineeringSessionStore(sessions.root),
    )
    outcome = restarted.advance_once("goal-1")

    goal = goals.load("goal-1")
    assert goal.status == "active"
    assert goal.current_step.attempts == 2
    assert outcome.action in {"recovered_enqueue", "waiting"}
