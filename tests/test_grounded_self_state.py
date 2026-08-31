from core.capabilities import describe_capabilities
from core.self_state import describe_self_state


def test_self_state_uses_canonical_m7_05_development_state() -> None:
    state = describe_self_state({})

    assert state["development"]["milestone"] == "M7"
    assert state["development"]["active_slice"] == "M7-05"
    assert state["development"]["source"] == "runtime_manifest"


def test_engineering_self_state_denies_direct_filesystem_perception() -> None:
    state = describe_self_state({"HIKARI_ENGINEERING_ENABLED": "true"})
    engineering = state["engineering"]

    assert engineering["conversation_read_only_enabled"] is True
    assert engineering["relationship"] == "internal_hikari_capability"
    assert engineering["direct_filesystem_perception"] is False
    assert engineering["continuous_filesystem_perception"] is False
    assert engineering["repository_write_enabled"] is False
    assert engineering["worker_liveness"] == "not_asserted_by_self_state"


def test_self_state_does_not_equate_conversation_model_with_hikari_identity() -> None:
    state = describe_self_state({"HIKARI_ENGINEERING_ENABLED": "true"})

    assert state["identity_scope"]["system_identity"] == "hikari"
    assert state["identity_scope"]["model_is_not_identity"] is True
    assert (
        state["cognition_topology"]["conversation"]["identity_relation"]
        == "part_of_hikari_not_hikari_itself"
    )
    assert (
        state["cognition_topology"]["engineering"]["identity_relation"]
        == "part_of_hikari_not_external_service"
    )


def test_engineering_terminal_delivery_does_not_require_conversation_rewrite() -> None:
    state = describe_self_state({"HIKARI_ENGINEERING_ENABLED": "true"})

    assert (
        state["delivery_semantics"]["conversation_model_consumption"]
        == "not_required_for_terminal_engineering_delivery"
    )
    result_model = state["engineering"]["result_model"]
    assert "conversation" not in result_model.lower()


def test_capabilities_replace_external_forge_with_internal_engineering_runtime() -> None:
    capabilities = describe_capabilities({"HIKARI_ENGINEERING_ENABLED": "true"})

    assert "forge" not in capabilities
    assert capabilities["engineering_runtime"]["available"] is True
    assert capabilities["engineering_runtime"]["relationship"] == "internal_hikari_capability"

    authority = capabilities["current_chat_authority"]
    assert authority["direct_shell"] is False
    assert authority["direct_filesystem"] is False
    assert authority["engineering_read_session"] is True
    assert authority["engineering_write_session"] is False


def test_chat_does_not_claim_engineering_authority_when_runtime_disabled() -> None:
    capabilities = describe_capabilities({"HIKARI_ENGINEERING_ENABLED": "false"})

    assert capabilities["current_chat_authority"]["engineering_read_session"] is False
    assert capabilities["self_state"]["engineering"]["conversation_read_only_enabled"] is False
