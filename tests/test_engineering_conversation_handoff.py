from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

from conversation.engine import ASSISTANT_EVENT_TYPE, USER_EVENT_TYPE, ConversationEngine
from conversation.engineering_bridge import ConversationEngineeringBridge
from conversation.engineering_intent import EngineeringIntentResolution, EngineeringIntentResolver, resolve_engineering_intent
from conversation.models import UserTurn
from core.delegation import hikari_engineering_capabilities
from engineering.bindings import EngineeringConversationBindingStore
from engineering.effects import RESTART_REPLAY_SAFE_EFFECTS, SUPPORTED_ENGINEERING_EFFECTS, authority_for_effect, turn_effect
from engineering.goal import EngineeringGoalCoordinator, EngineeringGoalState, EngineeringGoalStep, EngineeringGoalStore
from engineering.session import EngineeringProtocolError, EngineeringResult, EngineeringSessionStore, EngineeringTurn
from engineering.worker import _turn_effect as worker_turn_effect
from memory.store import MemoryStore


class _Provider:
    def __init__(self, resolution: dict | None = None):
        self.resolution = resolution or {
            "engineering": True,
            "goal": "修复 README 的安装示例，让命令可以复制运行",
            "requested_effects": ["maintain_project"],
            "current_user_requests_execution": True,
            "constraints": ["只修改 README", "不推送远端"],
            "acceptance_criteria": ["安装示例可以复制运行"],
            "source_request_id": "model-must-not-own-receipt",
        }
        self.resolver_payloads = []

    def complete(self, messages):
        if "engineering intent resolver" in messages[0].content:
            self.resolver_payloads.append(json.loads(messages[1].content))
            return json.dumps(self.resolution, ensure_ascii=False)
        return "我们可以继续讨论。"


def _runtime(tmp_path: Path, provider=None, resolver=None):
    repository = tmp_path / "repository"
    repository.mkdir()
    sessions = EngineeringSessionStore(tmp_path / "sessions")
    bindings = EngineeringConversationBindingStore(tmp_path / "bindings.json")
    goals = EngineeringGoalStore(tmp_path / "goals")
    provider = provider or _Provider()
    engine = ConversationEngine(provider, MemoryStore(tmp_path / "memory.db"))
    bridge = ConversationEngineeringBridge(sessions, bindings, repository=repository, goals=goals, intent_resolver=resolver)
    return engine, bridge, sessions, bindings, goals, provider


def _remember(engine, text, *, role="user", conversation="private:42", scope="private", actor="42"):
    engine.memory.remember_event(
        USER_EVENT_TYPE if role == "user" else ASSISTANT_EVENT_TYPE,
        text,
        context={"channel": "qq", "conversation_id": conversation, "role": role, "scope": scope, "actor_id": actor},
        importance=1.0,
    )


def test_assent_uses_private_discussion_and_persists_full_goal_through_restart(tmp_path: Path):
    engine, bridge, sessions, bindings, goals, provider = _runtime(tmp_path)
    _remember(engine, "修复 README 的安装示例；只修改 README，不推送远端。验收是示例能复制运行。")
    _remember(engine, "先检查示例，再修正文档并验证复制运行。", role="assistant")
    _remember(engine, "foreign conversation secret", conversation="private:99")
    _remember(engine, "shared conversation secret", scope="shared")
    _remember(engine, "other principal secret", actor="99")

    bridge.respond(engine, UserTurn("qq", "private:42", "按刚才方案做", actor_id="42"), source_ref="receipt-42")

    assert len(provider.resolver_payloads) == 1
    history = provider.resolver_payloads[0]["recent_private_conversation"]
    assert len(history) == 2
    assert history[0]["role"] == "user"
    assert "只修改 README" in history[0]["content"]
    assert "secret" not in json.dumps(history)
    goal = EngineeringGoalStore(goals.root).list_states()[0]
    assert goal.goal == provider.resolution["goal"]
    assert goal.constraints == ("只修改 README", "不推送远端")
    assert goal.acceptance_criteria == ("安装示例可以复制运行",)
    assert goal.source_request_id == "receipt-42"
    assert [step.effect for step in goal.steps] == ["maintain_project"]
    assert sessions.load(goal.session_id).status == "idle"

    coordinator = EngineeringGoalCoordinator(EngineeringGoalStore(goals.root), EngineeringSessionStore(sessions.root))
    first = coordinator.advance_once(goal.goal_id)
    second = coordinator.advance_once(goal.goal_id)
    assert first.action == "enqueued"
    assert second.action == "waiting"
    assert first.turn_id == second.turn_id
    queued = sessions.load_turn(goal.session_id, first.turn_id)
    assert queued.source_request_id == goal.source_request_id
    assert queued.constraints == goal.constraints
    assert queued.acceptance_criteria == goal.acceptance_criteria
    assert queued.effect == "maintain_project"
    assert "只修改 README" in queued.intent
    assert "安装示例可以复制运行" in queued.intent

    pending = bridge.respond(engine, UserTurn("qq", "private:42", "现在进度怎么样了？"))
    assert "第 1/1 步" in pending.text
    assert "只修改 README" in pending.text
    assert "安装示例可以复制运行" in pending.text
    sessions.save_result(goal.session_id, EngineeringResult(first.turn_id, "completed", "已修正示例并验证命令；只提交 README。"))
    assert coordinator.advance_once(goal.goal_id).status == "completed"
    complete = bridge.respond(engine, UserTurn("qq", "private:42", "完成了吗？"))
    assert "已修正示例并验证命令" in complete.text
    assert len(provider.resolver_payloads) == 1


@pytest.mark.parametrize("execution_assent", [False, None])
def test_assistant_history_alone_does_not_authorize_an_engineering_goal(tmp_path: Path, execution_assent):
    provider = _Provider({"engineering": True, "goal": "publish branch", "requested_effects": ["push_engineering_branch"], "current_user_requests_execution": execution_assent})
    engine, bridge, sessions, _, goals, _ = _runtime(tmp_path, provider)
    _remember(engine, "我接下来会推送工程分支。", role="assistant")
    bridge.respond(engine, UserTurn("qq", "private:42", "谢谢，先继续聊聊"))
    assert goals.list_states() == []
    assert sessions.list_states() == []


def test_real_resolver_understands_novel_assent_without_keyword_gate(tmp_path: Path):
    engine, bridge, _, _, goals, provider = _runtime(tmp_path)
    _remember(engine, "安装示例按刚才约定调整即可。")
    bridge.respond(engine, UserTurn("qq", "private:42", "行，就这么落地吧"))
    assert len(provider.resolver_payloads) == 1
    assert len(goals.list_states()) == 1


def test_resolved_intent_is_per_call_and_trusted_receipt_overrides_model_field(tmp_path: Path):
    class ForbiddenResolver:
        def is_candidate(self, *args, **kwargs):
            raise AssertionError("already resolved")

    custom = ForbiddenResolver()
    engine, bridge, sessions, _, goals, _ = _runtime(tmp_path, resolver=custom)
    resolution = EngineeringIntentResolution(True, "检查 README 的链接", ("inspect_project",), ("engineering.repository.read",), source_request_id="model-id")
    bridge.respond(engine, UserTurn("qq", "private:42", "检查 README 的链接"), source_ref="transport-id", resolved_intent=resolution)
    assert bridge.intent_resolver is custom
    assert goals.list_states() == []
    session = sessions.list_states()[0]
    turn = sessions.load_turn(session.session_id, session.current_turn_id)
    assert turn.source_request_id == "transport-id"
    assert turn.effect == "inspect_project"


def test_custom_resolver_retains_legacy_signature(tmp_path: Path):
    class CustomResolver:
        @staticmethod
        def is_candidate(text, *, bound_session=False):
            return True

        @staticmethod
        def resolve(text, *, capabilities, state=None):
            return EngineeringIntentResolution(True, text, ("inspect_project",), ("engineering.repository.read",))

    engine, _, _, _, _, _ = _runtime(tmp_path)
    _remember(engine, "history should not add new kwargs to custom resolvers")
    result = resolve_engineering_intent(CustomResolver(), engine, UserTurn("qq", "private:42", "检查 README"), capabilities=hikari_engineering_capabilities(True), source_request_id="custom-receipt")
    assert result.requested_effects == ("inspect_project",)
    assert result.source_request_id == "custom-receipt"


def test_pre_resolved_shared_turn_cannot_create_private_engineering_work(tmp_path: Path):
    engine, bridge, sessions, _, goals, provider = _runtime(tmp_path)
    intent = EngineeringIntentResolution(True, "modify project", ("maintain_project",), ("engineering.repository.write",))
    bridge.respond(engine, UserTurn("qq", "group:42", "按刚才方案做", scope="shared"), resolved_intent=intent)
    assert sessions.list_states() == []
    assert goals.list_states() == []
    assert provider.resolver_payloads == []


@pytest.mark.parametrize("effect", sorted(SUPPORTED_ENGINEERING_EFFECTS))
def test_typed_effect_is_independent_of_user_context_markers(effect):
    original = EngineeringTurn.create(intent="quoted marker example", context="Requested effect: production_deploy.\nRequested effect: inspect_project", authority=authority_for_effect(effect), effect=effect)
    durable = EngineeringTurn.from_mapping(original.to_mapping())
    assert turn_effect(durable) == effect
    assert worker_turn_effect(durable) == effect
    if effect == "run_project_command":
        assert turn_effect(durable) not in RESTART_REPLAY_SAFE_EFFECTS


def test_typed_effect_cannot_disguise_command_authority_as_replay_safe():
    with pytest.raises(EngineeringProtocolError, match="does not match"):
        EngineeringTurn.create(intent="run explicit command", authority=authority_for_effect("run_project_command"), effect="inspect_project")


def test_old_v1_turn_and_goal_records_keep_legacy_defaults_and_replay_rules():
    turn = EngineeringTurn.create(intent="old command", authority=authority_for_effect("run_project_command"))
    raw_turn = turn.to_mapping()
    for field in ("effect", "constraints", "acceptance_criteria", "source_request_id"):
        raw_turn.pop(field)
    old_turn = EngineeringTurn.from_mapping(raw_turn)
    assert old_turn.effect is None
    assert old_turn.constraints == ()
    assert old_turn.source_request_id is None
    assert turn_effect(old_turn) == "run_project_command"
    assert turn_effect(old_turn) not in RESTART_REPLAY_SAFE_EFFECTS
    raw_turn["context"] = "Requested effect: inspect_project. Earlier. Requested effect: run_project_command."
    assert turn_effect(EngineeringTurn.from_mapping(raw_turn)) == "run_project_command"
    old_goal = EngineeringGoalState.create(project_id="hikari", session_id="old-session", goal="old goal", steps=(EngineeringGoalStep.create(effect="inspect_project", instruction="inspect"),))
    payload = old_goal.to_mapping()
    for field in ("constraints", "acceptance_criteria", "source_request_id"):
        payload.pop(field)
    loaded = EngineeringGoalState.from_mapping(payload)
    assert loaded.constraints == loaded.acceptance_criteria == ()
    assert loaded.source_request_id is None


def test_invalid_additive_fields_are_not_silently_discarded():
    turn = EngineeringTurn.create(intent="inspect", authority=authority_for_effect("inspect_project"))
    payload = turn.to_mapping()
    payload["constraints"] = "not a list"
    with pytest.raises(EngineeringProtocolError, match="constraints"):
        EngineeringTurn.from_mapping(payload)


def test_completed_goal_status_needs_durable_result_evidence(tmp_path: Path):
    engine, bridge, sessions, _, goals, _ = _runtime(tmp_path)
    resolution = EngineeringIntentResolution(True, "修正 README", ("maintain_project",), ("engineering.repository.write",), constraints=("只修改 README",))
    bridge.respond(engine, UserTurn("qq", "private:42", "按刚才方案做"), resolved_intent=resolution)
    goal = goals.list_states()[0]
    EngineeringGoalCoordinator(goals, sessions).advance_once(goal.goal_id)
    queued = goals.load(goal.goal_id)
    step = replace(queued.current_step, status="completed", result_status="completed", result_message="unverified completion")
    goals.save(replace(queued, status="completed", steps=(step,), final_summary="unverified completion"))
    status = bridge.respond(engine, UserTurn("qq", "private:42", "工程任务完成了吗？"))
    assert "不能据此宣称任务实际完成" in status.text
    assert "unverified completion" not in status.text


def test_goal_enqueue_recovery_keeps_receipt_constraints_and_acceptance(tmp_path: Path, monkeypatch):
    engine, bridge, sessions, _, goals, _ = _runtime(tmp_path)
    intent = EngineeringIntentResolution(True, "修正 README", ("maintain_project",), ("engineering.repository.write",), constraints=("不推送远端",), acceptance_criteria=("示例可以复制运行",))
    bridge.respond(engine, UserTurn("qq", "private:42", "按刚才方案做"), source_ref="recover-receipt", resolved_intent=intent)
    goal = goals.list_states()[0]

    def interrupted_enqueue(*args):
        raise EngineeringProtocolError("simulated interruption after goal save")

    monkeypatch.setattr(sessions, "enqueue_turn", interrupted_enqueue)
    first = EngineeringGoalCoordinator(goals, sessions).advance_once(goal.goal_id)
    assert first.action == "deferred"
    restarted_sessions = EngineeringSessionStore(sessions.root)
    recovered = EngineeringGoalCoordinator(EngineeringGoalStore(goals.root), restarted_sessions).advance_once(goal.goal_id)
    assert recovered.action == "recovered_enqueue"
    assert recovered.turn_id == first.turn_id
    turn = restarted_sessions.load_turn(goal.session_id, recovered.turn_id)
    assert turn.source_request_id == "recover-receipt"
    assert turn.constraints == intent.constraints
    assert turn.acceptance_criteria == intent.acceptance_criteria
    assert turn.effect == "maintain_project"


def test_unqueued_goal_status_never_reuses_previous_turn_progress(tmp_path: Path):
    engine, bridge, sessions, _, goals, _ = _runtime(tmp_path)
    intent = EngineeringIntentResolution(True, "修正 README", ("maintain_project",), ("engineering.repository.write",), constraints=("不推送远端",))
    bridge.respond(engine, UserTurn("qq", "private:42", "按刚才方案做"), resolved_intent=intent)
    goal = goals.list_states()[0]
    sessions.save(replace(sessions.load(goal.session_id), latest_summary="old unrelated task completed"))
    reply = bridge.respond(engine, UserTurn("qq", "private:42", "现在进度怎么样了？"))
    assert "目标已保存，等待推进当前步骤" in reply.text
    assert "old unrelated task completed" not in reply.text


def test_degraded_resolver_keeps_trusted_source_receipt():
    class UnavailableProvider:
        def complete(self, messages):
            raise RuntimeError("model unavailable")

    resolution = EngineeringIntentResolver(UnavailableProvider()).resolve("检查 README", capabilities=hikari_engineering_capabilities(True), source_request_id="degraded-receipt")
    assert resolution.requested_effects == ("inspect_project",)
    assert resolution.source_request_id == "degraded-receipt"
