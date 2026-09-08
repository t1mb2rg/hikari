from dataclasses import replace
from pathlib import Path

from conversation.engine import ConversationEngine
from conversation.engineering_bridge import (
    ConversationEngineeringBridge,
    engineering_requirements_for_intent,
)
from conversation.models import UserTurn
from engineering.bindings import (
    EngineeringConversationBinding,
    EngineeringConversationBindingStore,
)
from engineering.maintainer import project_session_authority_ceiling
from engineering.session import EngineeringSessionState, EngineeringSessionStore
from memory.store import MemoryStore


class _ExplodingProvider:
    def complete(self, messages):
        raise AssertionError("delegation boundary routing must not call the conversation model")


def _bridge(tmp_path: Path):
    repository = tmp_path / "repo"
    repository.mkdir()
    sessions = EngineeringSessionStore(tmp_path / "engineering")
    bindings = EngineeringConversationBindingStore(tmp_path / "engineering_bindings.json")
    bridge = ConversationEngineeringBridge(
        sessions,
        bindings,
        repository=repository,
    )
    engine = ConversationEngine(_ExplodingProvider(), MemoryStore(tmp_path / "memory.db"))
    return bridge, engine, bindings


def test_intent_mapper_prioritizes_high_impact_boundaries_over_routine_write_verbs() -> None:
    cases = {
        "帮我修改 Hikari 项目的 secret 配置": "engineering.secrets.modify",
        "实现 Hikari 的生产部署": "engineering.production.deploy",
        "把这个合并到 main": "engineering.git.merge_protected",
        "把工程分支 force push 一次": "engineering.git.force_push",
        "给 Hikari 做破坏性数据迁移": "engineering.data.destructive_migration",
        "扩展 Hikari 的权限边界": "engineering.permissions.expand",
        "修改项目北极星": "engineering.project.change_north_star",
        "这个方案会产生显著外部成本，直接执行": "engineering.external_cost.material",
    }

    for text, expected in cases.items():
        assert engineering_requirements_for_intent(text) == (expected,)


def test_intent_mapper_surfaces_delegated_engineering_outcomes() -> None:
    cases = {
        "把 engineering 分支 push 到远端": "engineering.git.push_non_protected",
        "给这个改动开 Draft PR": "engineering.git.open_or_update_draft_pr",
        "在 Hikari 项目里运行命令 python -V": "engineering.commands.run",
    }

    for text, expected in cases.items():
        assert engineering_requirements_for_intent(text) == (expected,)


def test_ordinary_token_parser_edit_is_not_mistaken_for_secret_modification() -> None:
    requirements = engineering_requirements_for_intent("帮我修改 token parser 模块")

    assert requirements is not None
    assert "engineering.repository.write" in requirements
    assert "engineering.secrets.modify" not in requirements


def test_push_without_bound_engineering_branch_does_not_create_empty_remote_branch(
    tmp_path: Path,
) -> None:
    bridge, engine, bindings = _bridge(tmp_path)

    reply = bridge.respond(
        engine,
        UserTurn("qq", "private:42", "把 engineering 分支 push 到远端"),
    )

    assert "没有已经提交的 Engineering 分支" in reply.text
    assert "空远端分支" in reply.text
    assert bindings.for_conversation("qq", "private:42") is None


def test_bound_engineering_branch_routes_to_narrow_publish_turn(tmp_path: Path) -> None:
    bridge, engine, bindings = _bridge(tmp_path)
    sessions = EngineeringSessionStore(tmp_path / "engineering")
    workspace = tmp_path / "engineering-worktree"
    workspace.mkdir()
    state = EngineeringSessionState.create(
        project_id="hikari",
        repository=tmp_path / "repo",
        authority_ceiling=project_session_authority_ceiling(),
        session_id="push-session",
    )
    state = replace(
        state,
        status="completed",
        workspace_path=str(workspace),
        workspace_branch="hikari/engineering/push-session",
        baseline_commit="baseline-sha",
    )
    sessions.create(state)
    bindings.bind(
        EngineeringConversationBinding(
            session_id=state.session_id,
            channel="qq",
            conversation_id="private:42",
        )
    )

    reply = bridge.respond(
        engine,
        UserTurn("qq", "private:42", "把这个 engineering 分支 push 到远端"),
    )

    assert "不会 force push 或 merge" in reply.text
    saved = sessions.load(state.session_id)
    assert saved.status == "pending"
    assert saved.current_turn_id is not None
    turn = sessions.load_turn(saved.session_id, saved.current_turn_id)
    assert turn.authority.repository_read is True
    assert turn.authority.repository_write is False
    assert turn.authority.run_commands is False
    assert turn.authority.run_tests is False
    assert turn.authority.network is True
    assert turn.authority.publish is True
    assert turn.authority.outside_repo is False


def test_project_command_routes_to_non_mutating_engineering_turn(tmp_path: Path) -> None:
    bridge, engine, bindings = _bridge(tmp_path)

    reply = bridge.respond(
        engine,
        UserTurn("qq", "private:42", "在 Hikari 项目里运行命令 python -V"),
    )

    assert "项目内命令工程会话" in reply.text
    assert "仓库写入、网络或发布权限" in reply.text
    binding = bindings.for_conversation("qq", "private:42")
    assert binding is not None
    sessions = EngineeringSessionStore(tmp_path / "engineering")
    state = sessions.load(binding.session_id)
    assert state.status == "pending"
    assert state.current_turn_id is not None
    engineering_turn = sessions.load_turn(state.session_id, state.current_turn_id)
    assert engineering_turn.authority.repository_read is True
    assert engineering_turn.authority.run_commands is True
    assert engineering_turn.authority.repository_write is False
    assert engineering_turn.authority.run_tests is False
    assert engineering_turn.authority.network is False
    assert engineering_turn.authority.publish is False


def test_secret_change_escalates_before_routine_write_and_does_not_enqueue_work(
    tmp_path: Path,
) -> None:
    bridge, engine, bindings = _bridge(tmp_path)

    reply = bridge.respond(
        engine,
        UserTurn("qq", "private:42", "帮我修改 Hikari 项目的 secret 配置"),
    )

    assert "mandate 之外的影响边界" in reply.text
    assert "engineering.secrets.modify" in reply.text
    assert "能力缺口" not in reply.text
    assert bindings.for_conversation("qq", "private:42") is None


def test_protected_merge_escalates_without_model_or_engineering_session(tmp_path: Path) -> None:
    bridge, engine, bindings = _bridge(tmp_path)

    reply = bridge.respond(
        engine,
        UserTurn("qq", "private:42", "把这个合并到 main"),
    )

    assert "mandate 之外的影响边界" in reply.text
    assert "engineering.git.merge_protected" in reply.text
    assert bindings.for_conversation("qq", "private:42") is None
