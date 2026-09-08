from __future__ import annotations

from pathlib import Path

from core.delivery import DeliveryOutbox
from engineering.bindings import (
    EngineeringConversationBinding,
    EngineeringConversationBindingStore,
)
from engineering.delivery import EngineeringCompletionDelivery, EngineeringCompletionFacts
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
)


def _runtime(tmp_path: Path):
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
    bindings = EngineeringConversationBindingStore(tmp_path / "engineering_bindings.json")
    bindings.bind(
        EngineeringConversationBinding(
            session_id=session.session_id,
            channel="qq",
            conversation_id="private:42",
        )
    )
    goals = EngineeringGoalStore(tmp_path / "engineering_goals")
    goal = EngineeringGoalState.create(
        project_id="hikari",
        session_id=session.session_id,
        goal="edit then publish",
        steps=(
            EngineeringGoalStep.create(
                effect="maintain_project",
                instruction="edit README",
                step_id="step-1",
            ),
            EngineeringGoalStep.create(
                effect="push_engineering_branch",
                instruction="push branch",
                step_id="step-2",
            ),
        ),
        source_channel="qq",
        source_conversation_id="private:42",
        goal_id="goal-1",
    )
    goals.create(goal)
    coordinator = EngineeringGoalCoordinator(goals, sessions)
    coordinator.advance_once(goal.goal_id)
    outbox = DeliveryOutbox(tmp_path / "proactive_delivery.db")
    rendered: list[EngineeringCompletionFacts] = []

    def renderer(facts: EngineeringCompletionFacts, channel: str, conversation_id: str) -> str:
        rendered.append(facts)
        return f"{facts.status}:{facts.goal}"

    delivery = EngineeringCompletionDelivery(
        sessions,
        bindings,
        outbox,
        renderer=renderer,
    )
    return sessions, goals, outbox, delivery, rendered


def _complete_current(
    sessions: EngineeringSessionStore,
    *,
    message: str,
    changed_files: tuple[str, ...] = (),
) -> str:
    state = sessions.load("goal-session")
    assert state.current_turn_id is not None
    turn_id = state.current_turn_id
    sessions.save_result(
        state.session_id,
        EngineeringResult(
            turn_id=turn_id,
            status="completed",
            message=message,
            changed_files=changed_files,
        ),
    )
    return turn_id


def test_intermediate_goal_step_does_not_emit_false_whole_task_completion(tmp_path: Path) -> None:
    sessions, goals, outbox, delivery, rendered = _runtime(tmp_path)
    first_turn = _complete_current(
        sessions,
        message="README committed",
        changed_files=("README.md",),
    )

    submitted = delivery.pump()

    goal = goals.load("goal-1")
    assert goal.status == "active"
    assert goal.current_step_index == 1
    assert goal.current_step.status == "queued"
    assert submitted == 0
    assert rendered == []
    assert outbox.get(f"engineering:goal-session:{first_turn}") is None


def test_whole_goal_terminal_emits_exactly_one_goal_delivery(tmp_path: Path) -> None:
    sessions, goals, outbox, delivery, rendered = _runtime(tmp_path)
    first_turn = _complete_current(
        sessions,
        message="README committed",
        changed_files=("README.md",),
    )
    assert delivery.pump() == 0
    second_turn = _complete_current(sessions, message="branch pushed")

    first_submit = delivery.pump()
    second_submit = delivery.pump()

    goal = goals.load("goal-1")
    assert goal.status == "completed"
    assert first_submit == 1
    assert second_submit == 1
    assert len(rendered) == 1
    facts = rendered[0]
    assert facts.status == "completed"
    assert facts.goal == "edit then publish"
    assert facts.changed_files == ("README.md",)
    assert "README committed" in facts.summary
    assert "branch pushed" in facts.summary

    record = outbox.get("engineering-goal:goal-1")
    assert record is not None
    assert record.request.text == "completed:edit then publish"
    assert outbox.get(f"engineering:goal-session:{first_turn}") is None
    assert outbox.get(f"engineering:goal-session:{second_turn}") is None
