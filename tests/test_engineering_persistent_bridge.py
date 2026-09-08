from pathlib import Path

from conversation.engine import ConversationEngine
from conversation.engineering_bridge import ConversationEngineeringBridge
from conversation.engineering_intent import EngineeringIntentResolution
from conversation.models import UserTurn
from engineering.bindings import EngineeringConversationBindingStore
from engineering.goal import EngineeringGoalStore
from engineering.session import EngineeringSessionStore
from memory.store import MemoryStore


class _Provider:
    def complete(self, messages):
        return "unused"


class _MultiEffectResolver:
    def is_candidate(self, text: str, *, bound_session: bool = False) -> bool:
        return True

    def resolve(self, text, *, capabilities, state=None):
        return EngineeringIntentResolution(
            engineering=True,
            goal="更新 README 并交付为 Draft PR",
            requested_effects=("maintain_project", "open_or_update_draft_pr"),
            required_capabilities=(
                "engineering.repository.read",
                "engineering.repository.write",
                "engineering.tests.run",
                "engineering.git.commit",
                "engineering.git.open_or_update_draft_pr",
            ),
        )


class _SingleEffectResolver:
    def is_candidate(self, text: str, *, bound_session: bool = False) -> bool:
        return True

    def resolve(self, text, *, capabilities, state=None):
        return EngineeringIntentResolution(
            engineering=True,
            goal="另一个维护任务",
            requested_effects=("maintain_project",),
            required_capabilities=(
                "engineering.repository.read",
                "engineering.repository.write",
                "engineering.tests.run",
                "engineering.git.commit",
            ),
        )


def _runtime(tmp_path: Path, resolver):
    repository = tmp_path / "repo"
    repository.mkdir(exist_ok=True)
    sessions = EngineeringSessionStore(tmp_path / "engineering")
    bindings = EngineeringConversationBindingStore(tmp_path / "engineering_bindings.json")
    goals = EngineeringGoalStore(tmp_path / "engineering_goals")
    bridge = ConversationEngineeringBridge(
        sessions,
        bindings,
        repository=repository,
        intent_resolver=resolver,
        goals=goals,
    )
    engine = ConversationEngine(_Provider(), MemoryStore(tmp_path / "memory.db"))
    return bridge, engine, sessions, bindings, goals


def test_multi_effect_request_creates_one_persistent_goal_and_queues_first_step(
    tmp_path: Path,
) -> None:
    bridge, engine, sessions, bindings, goals = _runtime(tmp_path, _MultiEffectResolver())

    reply = bridge.respond(
        engine,
        UserTurn("qq", "private:42", "更新 README，完成后 push 并开 Draft PR"),
    )

    assert "持久 maintainer loop" in reply.text
    binding = bindings.for_conversation("qq", "private:42")
    assert binding is not None
    state = sessions.load(binding.session_id)
    assert state.status == "pending"
    assert state.current_turn_id is not None

    persisted = goals.list_states()
    assert len(persisted) == 1
    goal = persisted[0]
    assert goal.status == "active"
    assert goal.session_id == state.session_id
    assert tuple(step.effect for step in goal.steps) == (
        "maintain_project",
        "push_engineering_branch",
        "open_or_update_draft_pr",
    )
    assert goal.current_step_index == 0
    assert goal.current_step.status == "queued"
    assert goal.current_step.turn_id == state.current_turn_id

    first_turn = sessions.load_turn(state.session_id, state.current_turn_id)
    assert first_turn.authority.repository_write is True
    assert first_turn.authority.run_tests is True
    assert first_turn.authority.network is False
    assert first_turn.authority.publish is False
    assert "Do only this step" in first_turn.context


def test_active_persistent_goal_rejects_interleaved_engineering_turn(tmp_path: Path) -> None:
    bridge, engine, sessions, bindings, goals = _runtime(tmp_path, _MultiEffectResolver())
    bridge.respond(
        engine,
        UserTurn("qq", "private:42", "更新 README，完成后开 Draft PR"),
    )

    bridge.intent_resolver = _SingleEffectResolver()
    reply = bridge.respond(
        engine,
        UserTurn("qq", "private:42", "顺手再修改一下别的文件"),
    )

    assert "不会往同一个 EngineeringSession 里插入另一个 turn" in reply.text
    assert len(goals.list_states()) == 1
    binding = bindings.for_conversation("qq", "private:42")
    assert binding is not None
    state = sessions.load(binding.session_id)
    assert state.current_turn_id == goals.list_states()[0].current_step.turn_id


def test_status_query_reports_goal_level_progress_without_model(tmp_path: Path) -> None:
    bridge, engine, _, _, _ = _runtime(tmp_path, _MultiEffectResolver())
    bridge.respond(
        engine,
        UserTurn("qq", "private:42", "更新 README，完成后开 Draft PR"),
    )

    reply = bridge.respond(
        engine,
        UserTurn("qq", "private:42", "现在 Engineering 任务是什么状态？"),
    )

    assert "持久 Engineering Goal" in reply.text
    assert "步骤 1/3" in reply.text
    assert "`maintain_project`" in reply.text
    assert "`active`" in reply.text
