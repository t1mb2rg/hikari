from pathlib import Path

from conversation.engine import ConversationEngine
from conversation.engineering_bridge import ConversationEngineeringBridge
from conversation.engineering_intent import EngineeringIntentResolver
from conversation.models import UserTurn
from core.delegation import hikari_engineering_capabilities
from engineering.bindings import EngineeringConversationBindingStore
from engineering.session import EngineeringSessionStore
from memory.store import MemoryStore


class _SemanticProvider:
    def __init__(self, response: str) -> None:
        self.response = response
        self.messages = None

    def complete(self, messages):
        self.messages = tuple(messages)
        return self.response


class _ForbiddenProvider:
    def complete(self, messages):
        raise AssertionError("explicit high-impact intent must not call the model")


def test_resolver_treats_draft_pr_mention_inside_readme_update_as_documentation() -> None:
    provider = _SemanticProvider(
        '{"engineering": true, "goal": "update README capability status", '
        '"requested_effects": ["maintain_project"]}'
    )
    resolver = EngineeringIntentResolver(provider)

    resolution = resolver.resolve(
        "更新 README，让它写明 push 已实现，Draft PR 仍然是 capability gap。",
        capabilities=hikari_engineering_capabilities(True),
    )

    assert resolution.engineering is True
    assert resolution.requested_effects == ("maintain_project",)
    assert "engineering.repository.write" in resolution.required_capabilities
    assert "engineering.git.open_or_update_draft_pr" not in resolution.required_capabilities
    prompt = provider.messages[0].content
    assert "mentioning an engineering concept is not the same as requesting that effect" in prompt
    payload = provider.messages[1].content
    assert "engineering.git.open_or_update_draft_pr" in payload
    assert '"available": false' in payload


def test_resolver_maps_actual_draft_pr_request_to_draft_pr_effect() -> None:
    provider = _SemanticProvider(
        '{"engineering": true, "goal": "open review PR", '
        '"requested_effects": ["open_or_update_draft_pr"]}'
    )
    resolver = EngineeringIntentResolver(provider)

    resolution = resolver.resolve(
        "把这个 engineering branch 推上去以后开一个 Draft PR。",
        capabilities=hikari_engineering_capabilities(True),
    )

    assert resolution.requested_effects == ("open_or_update_draft_pr",)
    assert resolution.required_capabilities == (
        "engineering.git.open_or_update_draft_pr",
    )


def test_candidate_gate_does_not_spend_resolver_call_on_project_preference_chat() -> None:
    provider = _SemanticProvider("this should not be used")
    resolver = EngineeringIntentResolver(provider)

    assert resolver.is_candidate("你知道我平时做项目更喜欢什么样的开发方式吗") is False
    assert provider.messages is None


def test_explicit_protected_merge_is_caught_before_any_model_call() -> None:
    resolver = EngineeringIntentResolver(_ForbiddenProvider())

    assert resolver.is_candidate("把这个合并到 main") is True
    resolution = resolver.resolve(
        "把这个合并到 main",
        capabilities=hikari_engineering_capabilities(True),
    )

    assert resolution.requested_effects == ("merge_protected_branch",)
    assert resolution.required_capabilities == ("engineering.git.merge_protected",)


def test_invalid_model_output_falls_back_to_read_only_recent_update_query() -> None:
    resolver = EngineeringIntentResolver(_SemanticProvider("not-json"))

    resolution = resolver.resolve(
        "去看看 README 最近更新了什么",
        capabilities=hikari_engineering_capabilities(True),
    )

    assert resolution.requested_effects == ("inspect_project",)
    assert resolution.required_capabilities == ("engineering.repository.read",)


def test_invalid_model_output_keeps_readme_capability_sync_as_maintenance() -> None:
    resolver = EngineeringIntentResolver(_SemanticProvider("not-json"))

    resolution = resolver.resolve(
        "更新 README，让它写明 push 已实现，Draft PR 仍然是 capability gap。",
        capabilities=hikari_engineering_capabilities(True),
    )

    assert resolution.requested_effects == ("maintain_project",)
    assert "engineering.git.open_or_update_draft_pr" not in resolution.required_capabilities


def test_bridge_uses_semantic_effect_instead_of_draft_pr_substring(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    sessions = EngineeringSessionStore(tmp_path / "engineering")
    bindings = EngineeringConversationBindingStore(tmp_path / "bindings.json")
    provider = _SemanticProvider(
        '{"engineering": true, "goal": "synchronize README with runtime truth", '
        '"requested_effects": ["maintain_project"]}'
    )
    engine = ConversationEngine(provider, MemoryStore(tmp_path / "memory.db"))
    bridge = ConversationEngineeringBridge(
        sessions,
        bindings,
        repository=repository,
    )

    reply = bridge.respond(
        engine,
        UserTurn(
            "qq",
            "private:42",
            "更新 README 的 M7-07 Engineering Runtime 部分，让它反映当前真实能力："
            "项目内命令执行和 non-protected engineering branch push 已实现，"
            "Draft PR 仍然是 capability gap。只修改与这项状态同步直接相关的内容。",
        ),
    )

    assert "项目维护职责" in reply.text
    assert "能力缺口" not in reply.text
    binding = bindings.for_conversation("qq", "private:42")
    assert binding is not None
    state = sessions.load(binding.session_id)
    turn = sessions.load_turn(state.session_id, state.current_turn_id or "")
    assert turn.authority.repository_write is True
    assert turn.authority.run_tests is True
    assert turn.authority.network is False
    assert turn.authority.publish is False
