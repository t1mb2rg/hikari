from pathlib import Path

from conversation.engine import ConversationEngine
from conversation.engineering_bridge import (
    ConversationEngineeringBridge,
    engineering_requirements_for_intent,
)
from conversation.models import UserTurn
from engineering.bindings import EngineeringConversationBindingStore
from engineering.session import EngineeringSessionStore
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


def test_intent_mapper_surfaces_delegated_but_unimplemented_outcomes() -> None:
    cases = {
        "把 engineering 分支 push 到远端": "engineering.git.push_non_protected",
        "给这个改动开 Draft PR": "engineering.git.open_or_update_draft_pr",
        "在 Hikari 项目里运行命令 python -V": "engineering.commands.run",
    }

    for text, expected in cases.items():
        assert engineering_requirements_for_intent(text) == (expected,)


def test_push_capability_gap_is_deterministic_and_does_not_enqueue_work(tmp_path: Path) -> None:
    bridge, engine, bindings = _bridge(tmp_path)

    reply = bridge.respond(
        engine,
        UserTurn("qq", "private:42", "把 engineering 分支 push 到远端"),
    )

    assert "能力缺口" in reply.text
    assert "engineering.git.push_non_protected" in reply.text
    assert "逐个动作给我授权" in reply.text
    assert bindings.for_conversation("qq", "private:42") is None


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
