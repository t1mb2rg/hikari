from __future__ import annotations

from pathlib import Path

from engineering.goal import EngineeringGoalState, EngineeringGoalStep, EngineeringGoalStore
from engineering.maintainer import project_session_authority_ceiling
from engineering.maintainer_loop import PersistentMaintainerLoop
from engineering.session import EngineeringSessionState, EngineeringSessionStore


def _create_goal(
    sessions: EngineeringSessionStore,
    goals: EngineeringGoalStore,
    *,
    repository: Path,
    session_id: str,
    goal_id: str,
    created_at: float,
) -> None:
    session = EngineeringSessionState.create(
        project_id="hikari",
        repository=repository,
        authority_ceiling=project_session_authority_ceiling(),
        session_id=session_id,
    )
    sessions.create(session)
    goal = EngineeringGoalState.create(
        project_id="hikari",
        session_id=session_id,
        goal=goal_id,
        steps=(
            EngineeringGoalStep.create(
                effect="maintain_project",
                instruction=f"do {goal_id}",
                step_id="step-1",
            ),
        ),
        source_channel="qq",
        source_conversation_id=f"private:{goal_id}",
        goal_id=goal_id,
    )
    goals.create(
        EngineeringGoalState(
            goal_id=goal.goal_id,
            project_id=goal.project_id,
            session_id=goal.session_id,
            goal=goal.goal,
            steps=goal.steps,
            status=goal.status,
            current_step_index=goal.current_step_index,
            source_channel=goal.source_channel,
            source_conversation_id=goal.source_conversation_id,
            final_summary=goal.final_summary,
            created_at=created_at,
            updated_at=created_at,
        )
    )


def test_resident_selects_only_oldest_unfinished_goal_per_project(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    sessions = EngineeringSessionStore(tmp_path / "engineering")
    goals = EngineeringGoalStore(tmp_path / "engineering_goals")
    _create_goal(
        sessions,
        goals,
        repository=repository,
        session_id="session-old",
        goal_id="goal-old",
        created_at=10.0,
    )
    _create_goal(
        sessions,
        goals,
        repository=repository,
        session_id="session-new",
        goal_id="goal-new",
        created_at=20.0,
    )

    outcomes = PersistentMaintainerLoop(goals, sessions).advance_all()

    assert len(outcomes) == 1
    assert outcomes[0].goal_id == "goal-old"
    assert sessions.load("session-old").status == "pending"
    assert sessions.load("session-new").status == "idle"
    assert goals.load("goal-new").current_step.turn_id is None
