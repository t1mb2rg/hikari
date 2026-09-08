from __future__ import annotations

from pathlib import Path

from conversation.engine import ConversationEngine
from conversation.engineering_intent import EngineeringIntentResolution
from conversation.models import UserTurn
from conversation.persistent_engineering_bridge import PersistentConversationEngineeringBridge
from engineering.bindings import EngineeringConversationBindingStore
from engineering.goal import EngineeringGoalStore
from engineering.maintainer_loop import PersistentMaintainerLoop
from engineering.session import EngineeringSessionStore
from memory.store import MemoryStore


class _ExplodingProvider:
    def complete(self, messages):
        raise AssertionError("persistent bridge boundary tests must not call the conversation model")


class _MultiEffectResolver:
    @staticmethod
    def is_candidate(text: str, *, bound_session: bool = False) -> bool:
        return True

    @staticmethod
    def resolve(text: str, *, capabilities, state=None) -> EngineeringIntentResolution:
        return EngineeringIntentResolution(
            engineering=True,
            goal="update README and publish a Draft PR",
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


class _ImplicitPushResolver:
    @staticmethod
    def is_candidate(text: str, *, bound_session: bool = False) -> bool:
        return True

    @staticmethod
    def resolve(text: str, *, capabilities, state=None) -> EngineeringIntentResolution:
        return EngineeringIntentResolution(
            engineering=True,
            goal="update README and publish a Draft PR",
            requested_effects=(
                "maintain_project",
                "open_or_update_draft_pr",
            ),
            required_capabilities=(
                "engineering.repository.read",
                "engineering.repository.write",
                "engineering.tests.run",
                "engineering.git.commit",
                "engineering.git.open_or_update_draft_pr",
            ),
        )


def _runtime(tmp_path: Path, resolver=None):
    repository = tmp_path / "repo"
    repository.mkdir()
    sessions = EngineeringSessionStore(tmp_path / "engineering")
    bindings = EngineeringConversationBindingStore(tmp_path / "engineering_bindings.json")
    goals = EngineeringGoalStore(tmp_path / "engineering_goals")
    bridge = PersistentConversationEngineeringBridge(
        sessions,
        bindings,
        repository=repository,
        intent_resolver=resolver or _MultiEffectResolver(),
        goals=goals,
    )
    engine = ConversationEngine(_ExplodingProvider(), MemoryStore(tmp_path / "memory.db"))
    return bridge, engine, sessions, bindings, goals


def test_multi_effect_request_becomes_one_durable_ordered_goal(tmp_path: Path) -> None:
    bridge, engine, sessions, bindings, goals = _runtime(tmp_path)

    reply = bridge.respond(
        engine,
        UserTurn(
            "qq",
            "private:42",
            "更新 README，完成后把 engineering 分支 push 到远端并开一个 Draft PR。",
        ),
    )

    assert "我来处理" in reply.text
    binding = bindings.for_conversation("qq", "private:42")
    assert binding is not None
    goal_states = goals.list_states()
    assert len(goal_states) == 1
    goal = goal_states[0]
    assert goal.status == "active"
    assert goal.session_id == binding.session_id
    assert [step.effect for step in goal.steps] == [
        "maintain_project",
        "push_engineering_branch",
        "open_or_update_draft_pr",
    ]
    assert goal.current_step_index == 0
    assert goal.current_step.status == "pending"
    assert goal.current_step.turn_id is None

    # Conversation only persists the goal. Resident owns work selection/enqueue.
    state = sessions.load(goal.session_id)
    assert state.status == "idle"
    assert state.current_turn_id is None

    outcome = PersistentMaintainerLoop(goals, sessions).advance_all()
    assert len(outcome) == 1
    goal = goals.load(goal.goal_id)
    assert goal.current_step.status == "queued"
    assert goal.current_step.turn_id is not None
    state = sessions.load(goal.session_id)
    assert state.status == "pending"
    assert state.current_turn_id == goal.current_step.turn_id
    turn = sessions.load_turn(state.session_id, state.current_turn_id or "")
    assert "Requested effect: maintain_project" in turn.context
    assert turn.authority.repository_write is True
    assert turn.authority.run_tests is True
    assert turn.authority.network is False
    assert turn.authority.publish is False


def test_draft_pr_goal_adds_push_prerequisite_without_user_babysitting(tmp_path: Path) -> None:
    bridge, engine, _, _, goals = _runtime(tmp_path, _ImplicitPushResolver())

    bridge.respond(
        engine,
        UserTurn(
            "qq",
            "private:42",
            "更新 README，完成后直接开一个 Draft PR。",
        ),
    )

    goal = goals.list_states()[0]
    assert [step.effect for step in goal.steps] == [
        "maintain_project",
        "push_engineering_branch",
        "open_or_update_draft_pr",
    ]


def test_status_query_reports_persistent_goal_step_not_only_raw_session(tmp_path: Path) -> None:
    bridge, engine, _, _, _ = _runtime(tmp_path)
    bridge.respond(
        engine,
        UserTurn(
            "qq",
            "private:42",
            "更新 README，完成后 push 并开 Draft PR。",
        ),
    )

    reply = bridge.respond(
        engine,
        UserTurn("qq", "private:42", "现在 Engineering 任务是什么状态？"),
    )

    assert "持久 Engineering 目标" in reply.text
    assert "第 1/3 步" in reply.text
    assert "maintain_project" in reply.text
    assert "pending" in reply.text


def test_second_multi_effect_goal_is_not_interleaved_with_active_goal(tmp_path: Path) -> None:
    bridge, engine, _, _, goals = _runtime(tmp_path)
    request = UserTurn(
        "qq",
        "private:42",
        "更新 README，完成后 push 并开 Draft PR。",
    )
    bridge.respond(engine, request)

    second = bridge.respond(engine, request)

    assert "不会把第二个工程目标" in second.text
    assert len(goals.list_states()) == 1
