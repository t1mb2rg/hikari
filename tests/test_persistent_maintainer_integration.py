from __future__ import annotations

from pathlib import Path

from conversation.engine import ConversationEngine
from conversation.engineering_intent import EngineeringIntentResolution
from conversation.models import UserTurn
from conversation.persistent_engineering_bridge import PersistentConversationEngineeringBridge
from core.delivery import DeliveryOutbox
from engineering.bindings import EngineeringConversationBindingStore
from engineering.delivery import EngineeringCompletionDelivery, EngineeringCompletionFacts
from engineering.goal import EngineeringGoalStore
from engineering.session import EngineeringResult, EngineeringSessionStore
from memory.store import MemoryStore


class _ExplodingProvider:
    def complete(self, messages):
        raise AssertionError("integration test must not call the conversation model")


class _ThreeStepResolver:
    @staticmethod
    def is_candidate(text: str, *, bound_session: bool = False) -> bool:
        return True

    @staticmethod
    def resolve(text: str, *, capabilities, state=None) -> EngineeringIntentResolution:
        return EngineeringIntentResolution(
            engineering=True,
            goal="change README, push branch, and open Draft PR",
            requested_effects=(
                "maintain_project",
                "push_engineering_branch",
                "open_or_update_draft_pr",
            ),
            required_capabilities=(
                "engineering.repository.read",
                "engineering.repository.write",
                "engineering.tests.run",
                "engineering.git.commit",
                "engineering.git.push_non_protected",
                "engineering.git.open_or_update_draft_pr",
            ),
        )


def test_one_conversation_goal_advances_three_steps_and_delivers_once(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    sessions = EngineeringSessionStore(tmp_path / "engineering")
    bindings = EngineeringConversationBindingStore(tmp_path / "engineering_bindings.json")
    goals = EngineeringGoalStore(tmp_path / "engineering_goals")
    bridge = PersistentConversationEngineeringBridge(
        sessions,
        bindings,
        repository=repository,
        intent_resolver=_ThreeStepResolver(),
        goals=goals,
    )
    engine = ConversationEngine(_ExplodingProvider(), MemoryStore(tmp_path / "memory.db"))

    accepted = bridge.respond(
        engine,
        UserTurn(
            "qq",
            "private:42",
            "更新 README，完成后 push 分支并开 Draft PR。",
        ),
    )
    assert "我来处理" in accepted.text
    goal = goals.list_states()[0]
    assert goal.current_step.status == "pending"

    outbox = DeliveryOutbox(tmp_path / "proactive_delivery.db")
    rendered: list[EngineeringCompletionFacts] = []

    def renderer(facts: EngineeringCompletionFacts, channel: str, conversation_id: str) -> str:
        rendered.append(facts)
        return f"done:{facts.status}:{facts.goal}"

    resident_pump = EngineeringCompletionDelivery(
        sessions,
        bindings,
        outbox,
        renderer=renderer,
    )

    # Resident discovers the durable goal and chooses its first step without another user turn.
    assert resident_pump.pump() == 0
    goal = goals.load(goal.goal_id)
    first_turn = goal.current_step.turn_id
    assert first_turn is not None
    assert sessions.load(goal.session_id).current_turn_id == first_turn
    sessions.save_result(
        goal.session_id,
        EngineeringResult(
            turn_id=first_turn,
            status="completed",
            message="README committed",
            changed_files=("README.md",),
        ),
    )

    # Next Resident pump consumes the result and autonomously queues push.
    assert resident_pump.pump() == 0
    goal = goals.load(goal.goal_id)
    assert goal.current_step_index == 1
    second_turn = goal.current_step.turn_id
    assert second_turn is not None and second_turn != first_turn
    sessions.save_result(
        goal.session_id,
        EngineeringResult(
            turn_id=second_turn,
            status="completed",
            message="engineering branch pushed",
        ),
    )

    # Next pump autonomously queues Draft PR publication.
    assert resident_pump.pump() == 0
    goal = goals.load(goal.goal_id)
    assert goal.current_step_index == 2
    third_turn = goal.current_step.turn_id
    assert third_turn is not None and third_turn not in {first_turn, second_turn}
    sessions.save_result(
        goal.session_id,
        EngineeringResult(
            turn_id=third_turn,
            status="completed",
            message="Draft PR #123 created",
        ),
    )

    # Only whole-goal terminal truth is delivered.
    assert resident_pump.pump() == 1
    goal = goals.load(goal.goal_id)
    assert goal.status == "completed"
    assert [step.status for step in goal.steps] == [
        "completed",
        "completed",
        "completed",
    ]
    assert len(rendered) == 1
    assert rendered[0].status == "completed"
    assert rendered[0].changed_files == ("README.md",)
    assert "README committed" in rendered[0].summary
    assert "engineering branch pushed" in rendered[0].summary
    assert "Draft PR #123 created" in rendered[0].summary

    record = outbox.get(f"engineering-goal:{goal.goal_id}")
    assert record is not None
    assert record.request.text == "done:completed:change README, push branch, and open Draft PR"
