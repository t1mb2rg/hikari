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


def test_implemented_maintainer_edit_test_commit_is_executable() -> None:
    capabilities = hikari_engineering_capabilities(True)

    assessment = assess_task_capabilities(
        [
            "engineering.repository.read",
            "engineering.repository.write",
            "engineering.tests.run",
            "engineering.git.commit",
        ],
        capabilities,
    )

    assert assessment.status == ASSESSMENT_EXECUTABLE
    assert assessment.missing == ()
    assert assessment.escalation == ()
    assert capabilities["engineering.repository.write"].delegated is True
    assert capabilities["engineering.repository.write"].available is True


def test_implemented_project_command_execution_is_executable() -> None:
    capabilities = hikari_engineering_capabilities(True)

    assessment = assess_task_capabilities(
        ["engineering.commands.run"],
        capabilities,
    )

    assert assessment.status == ASSESSMENT_EXECUTABLE
    assert assessment.available == ("engineering.commands.run",)
    assert assessment.missing == ()
    assert capabilities["engineering.commands.run"].delegated is True
    assert capabilities["engineering.commands.run"].available is True
    assert capabilities["engineering.commands.run"].scope == "isolated_project_worktree_non_mutating"


def test_implemented_non_protected_push_is_executable() -> None:
    capabilities = hikari_engineering_capabilities(True)

    assessment = assess_task_capabilities(
        ["engineering.git.push_non_protected"],
        capabilities,
    )

    assert assessment.status == ASSESSMENT_EXECUTABLE
    assert assessment.available == ("engineering.git.push_non_protected",)
    assert assessment.missing == ()
    assert assessment.escalation == ()
    assert capabilities["engineering.git.push_non_protected"].delegated is True
    assert capabilities["engineering.git.push_non_protected"].available is True
    assert capabilities["engineering.git.push_non_protected"].scope == "isolated_engineering_branch_to_origin"


def test_draft_pr_remains_a_delegated_capability_gap() -> None:
    capabilities = hikari_engineering_capabilities(True)

    assessment = assess_task_capabilities(
        ["engineering.git.open_or_update_draft_pr"],
        capabilities,
    )

    assert assessment.status == ASSESSMENT_CAPABILITY_GAP
    assert assessment.missing == ("engineering.git.open_or_update_draft_pr",)
    assert assessment.escalation == ()


def test_protected_merge_is_authority_escalation_not_capability_gap() -> None:
    capabilities = hikari_engineering_capabilities(True)

    assessment = assess_task_capabilities(
        ["engineering.git.merge_protected"],
        capabilities,
    )

    assert assessment.status == ASSESSMENT_ESCALATION_REQUIRED
    assert assessment.missing == ()
    assert assessment.escalation == ("engineering.git.merge_protected",)


def test_all_declared_high_impact_boundaries_are_machine_enforced() -> None:
    capabilities = hikari_engineering_capabilities(True)
    boundary_capabilities = (
        "engineering.git.merge_protected",
        "engineering.git.force_push",
        "engineering.secrets.modify",
        "engineering.production.deploy",
        "engineering.data.destructive_migration",
        "engineering.permissions.expand",
        "engineering.project.change_north_star",
        "engineering.external_cost.material",
    )

    for capability_key in boundary_capabilities:
        capability = capabilities[capability_key]
        assessment = assess_task_capabilities([capability_key], capabilities)

        assert capability.delegated is False
        assert capability.escalation_required is True
        assert assessment.status == ASSESSMENT_ESCALATION_REQUIRED
        assert assessment.missing == ()
        assert assessment.escalation == (capability_key,)


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
