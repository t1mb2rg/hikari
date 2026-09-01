from core.delegation import (
    ASSESSMENT_CAPABILITY_GAP,
    ASSESSMENT_ESCALATION_REQUIRED,
    ASSESSMENT_EXECUTABLE,
    assess_task_capabilities,
    hikari_engineering_capabilities,
    hikari_project_mandate,
)


def test_hikari_project_mandate_delegates_routine_maintainer_outcomes() -> None:
    mandate = hikari_project_mandate(True)

    assert mandate.active is True
    assert mandate.role == "maintainer"
    assert "edit_project_files" in mandate.delegated_outcomes
    assert "run_tests" in mandate.delegated_outcomes
    assert "commit_engineering_changes" in mandate.delegated_outcomes
    assert "open_or_update_draft_pr" in mandate.delegated_outcomes
    assert "merge_protected_branch" in mandate.escalation_outcomes
    assert "permission_boundary_expansion" in mandate.escalation_outcomes


def test_delegated_but_unimplemented_write_is_capability_gap_not_permission_request() -> None:
    capabilities = hikari_engineering_capabilities(True)

    assessment = assess_task_capabilities(
        ["engineering.repository.read", "engineering.repository.write"],
        capabilities,
    )

    assert assessment.status == ASSESSMENT_CAPABILITY_GAP
    assert assessment.available == ("engineering.repository.read",)
    assert assessment.missing == ("engineering.repository.write",)
    assert assessment.escalation == ()
    assert capabilities["engineering.repository.write"].delegated is True
    assert capabilities["engineering.repository.write"].available is False


def test_protected_merge_is_authority_escalation_not_capability_gap() -> None:
    capabilities = hikari_engineering_capabilities(True)

    assessment = assess_task_capabilities(
        ["engineering.git.merge_protected"],
        capabilities,
    )

    assert assessment.status == ASSESSMENT_ESCALATION_REQUIRED
    assert assessment.missing == ()
    assert assessment.escalation == ("engineering.git.merge_protected",)


def test_implemented_delegated_read_is_executable() -> None:
    capabilities = hikari_engineering_capabilities(True)

    assessment = assess_task_capabilities(
        ["engineering.repository.read"],
        capabilities,
    )

    assert assessment.status == ASSESSMENT_EXECUTABLE
    assert assessment.available == ("engineering.repository.read",)
    assert assessment.missing == ()
    assert assessment.escalation == ()


def test_disabled_engineering_runtime_does_not_masquerade_as_delegated_execution() -> None:
    capabilities = hikari_engineering_capabilities(False)

    assessment = assess_task_capabilities(
        ["engineering.repository.read"],
        capabilities,
    )

    assert assessment.status == ASSESSMENT_ESCALATION_REQUIRED
    assert assessment.escalation == ("engineering.repository.read",)


def test_unknown_capability_is_a_grounded_gap_not_invented_ability() -> None:
    capabilities = hikari_engineering_capabilities(True)

    assessment = assess_task_capabilities(
        ["engineering.future.magic"],
        capabilities,
    )

    assert assessment.status == ASSESSMENT_CAPABILITY_GAP
    assert assessment.missing == ("engineering.future.magic",)
