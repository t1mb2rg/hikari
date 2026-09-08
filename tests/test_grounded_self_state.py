from core.capabilities import describe_capabilities
from core.self_state import describe_self_state


def test_self_state_does_not_own_project_development_phase() -> None:
    state = describe_self_state({})

    assert "development" not in state
    encoded = repr(state)
    assert "M7-07" not in encoded
    assert "Capability-Aware Delegation" not in encoded


def test_self_state_anchors_jarvis_style_north_star() -> None:
    state = describe_self_state({})

    assert state["north_star"]["archetype"] == "jarvis_style_personal_ai"
    assert state["north_star"]["role"] == "persistent_personal_ai_assistant"
    assert "digital_life_claims" in state["north_star"]["not_a_project_target"]
    assert "simulated_human_consciousness" in state["north_star"]["not_a_project_target"]


def test_engineering_self_state_denies_direct_filesystem_perception_but_exposes_maintainer_execution() -> None:
    state = describe_self_state({"HIKARI_ENGINEERING_ENABLED": "true"})
    engineering = state["engineering"]

    assert engineering["conversation_read_only_enabled"] is True
    assert engineering["conversation_maintainer_session_enabled"] is True
    assert engineering["persistent_goal_enabled"] is True
    assert engineering["resident_maintainer_loop_enabled"] is True
    assert engineering["deterministic_work_selection_enabled"] is True
    assert engineering["bounded_retry_enabled"] is True
    assert engineering["goal_level_terminal_delivery_enabled"] is True
    assert engineering["relationship"] == "internal_hikari_capability"
    assert engineering["direct_filesystem_perception"] is False
    assert engineering["continuous_filesystem_perception"] is False
    assert engineering["repository_write_enabled"] is True
    assert engineering["project_tests_enabled"] is True
    assert engineering["engineering_branch_commit_enabled"] is True
    assert engineering["non_protected_push_enabled"] is True
    assert engineering["draft_pr_publish_enabled"] is True
    assert engineering["work_selection_policy"] == "oldest_unfinished_goal_per_project"
    assert engineering["worker_liveness"] == "not_asserted_by_self_state"


def test_delegated_authority_prefers_project_mandate_over_per_action_approval() -> None:
    state = describe_self_state({"HIKARI_ENGINEERING_ENABLED": "true"})
    delegation = state["delegated_authority"]

    assert delegation["default_human_role"] == "define_mandate_and_handle_exceptions"
    assert delegation["default_hikari_role"] == "execute_within_mandate"
    assert delegation["per_action_approval_is_not_default"] is True
    assert delegation["hikari_project_role"] == "maintainer"
    assert delegation["implemented_capability_is_separate_from_delegation"] is True


def test_awareness_is_not_reduced_to_engineering_request_response() -> None:
    state = describe_self_state({"HIKARI_ENGINEERING_ENABLED": "true"})
    awareness = state["awareness"]

    assert awareness["all_sensing_requires_explicit_request_response"] is False
    assert awareness["configured_sensors_may_observe_proactively"] is True
    assert awareness["engineering_session_is_not_the_only_observation_path"] is True
    assert awareness["filesystem_observation_via_engineering_is_direct_sensor"] is False


def test_self_state_separates_hikari_system_identity_from_jarvis_persona_and_models() -> None:
    state = describe_self_state({"HIKARI_ENGINEERING_ENABLED": "true"})

    identity = state["identity_scope"]
    assert identity["system_identity"] == "hikari"
    assert identity["conversation_persona"] == "jarvis"
    assert identity["persona_is_not_system_identity"] is True
    assert identity["model_is_not_identity"] is True
    assert identity["host_is_not_identity"] is True
    assert (
        state["cognition_topology"]["conversation"]["identity_relation"]
        == "part_of_hikari_not_hikari_itself"
    )
    assert (
        state["cognition_topology"]["engineering"]["identity_relation"]
        == "part_of_hikari_not_external_service"
    )
    assert state["cognition_topology"]["awareness"]["identity_relation"] == "part_of_hikari"


def test_engineering_terminal_delivery_does_not_require_conversation_rewrite() -> None:
    state = describe_self_state({"HIKARI_ENGINEERING_ENABLED": "true"})

    assert (
        state["delivery_semantics"]["conversation_model_consumption"]
        == "not_required_for_terminal_engineering_delivery"
    )
    assert state["delivery_semantics"]["intermediate_goal_step_is_user_terminal"] is False
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
    assert authority["engineering_write_session"] is True

    model = capabilities["capability_model"]
    assert model["engineering.repository.write"]["available"] is True
    assert model["engineering.repository.write"]["delegated"] is True
    assert model["engineering.git.push_non_protected"]["available"] is True
    assert model["engineering.git.push_non_protected"]["delegated"] is True
    assert model["engineering.git.open_or_update_draft_pr"]["available"] is True
    assert model["engineering.git.open_or_update_draft_pr"]["delegated"] is True

    mandate = capabilities["project_mandates"]["hikari"]
    assert mandate["role"] == "maintainer"
    assert "edit_project_files" in mandate["delegated_outcomes"]


def test_chat_does_not_claim_engineering_authority_when_runtime_disabled() -> None:
    capabilities = describe_capabilities({"HIKARI_ENGINEERING_ENABLED": "false"})

    assert capabilities["current_chat_authority"]["engineering_read_session"] is False
    assert capabilities["current_chat_authority"]["engineering_write_session"] is False
    assert capabilities["self_state"]["engineering"]["conversation_read_only_enabled"] is False
    assert capabilities["self_state"]["engineering"]["persistent_goal_enabled"] is False
    assert capabilities["self_state"]["engineering"]["resident_maintainer_loop_enabled"] is False
    assert capabilities["operational_state"]["overall"] == "unknown"
