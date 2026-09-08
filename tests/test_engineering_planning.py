import pytest

from engineering.planning import build_engineering_goal_plan
from engineering.session import EngineeringProtocolError


def test_persistent_plan_inserts_push_before_requested_draft_pr() -> None:
    plan = build_engineering_goal_plan(
        goal="更新 README 并交付 Draft PR",
        requested_effects=("maintain_project", "open_or_update_draft_pr"),
        original_request="更新 README，完成后开 Draft PR",
    )

    assert tuple(step.effect for step in plan.steps) == (
        "maintain_project",
        "push_engineering_branch",
        "open_or_update_draft_pr",
    )
    assert "engineering.git.push_non_protected" in plan.required_capabilities
    assert "engineering.git.open_or_update_draft_pr" in plan.required_capabilities
    assert "merge" not in " ".join(plan.required_capabilities).lower()
    assert "force" not in " ".join(plan.required_capabilities).lower()


def test_maintainer_instruction_starts_with_human_goal_but_retains_original_scope() -> None:
    plan = build_engineering_goal_plan(
        goal="为 M7-C 新建一份毕业证据文档",
        requested_effects=("maintain_project", "open_or_update_draft_pr"),
        original_request=(
            "只新建 docs/M7-C_GRADUATION.md，并写入指定内容；不要修改其他文件。"
            "完成后开 Draft PR。"
        ),
    )

    maintain = plan.steps[0]
    assert maintain.effect == "maintain_project"
    assert maintain.instruction.startswith("为 M7-C 新建一份毕业证据文档\n")
    assert "docs/M7-C_GRADUATION.md" in maintain.instruction
    assert "不要修改其他文件" in maintain.instruction


def test_persistent_publish_plan_deduplicates_and_orders_effects() -> None:
    plan = build_engineering_goal_plan(
        goal="发布工程结果",
        requested_effects=(
            "open_or_update_draft_pr",
            "push_engineering_branch",
            "push_engineering_branch",
        ),
        original_request="push 后开 Draft PR",
    )

    assert tuple(step.effect for step in plan.steps) == (
        "push_engineering_branch",
        "open_or_update_draft_pr",
    )


def test_persistent_plan_does_not_mix_read_only_or_command_effects_with_publish() -> None:
    with pytest.raises(EngineeringProtocolError, match="cannot be mixed"):
        build_engineering_goal_plan(
            goal="检查然后发布",
            requested_effects=("inspect_project", "push_engineering_branch"),
            original_request="检查项目然后 push",
        )


def test_persistent_plan_rejects_unknown_or_high_impact_effects() -> None:
    with pytest.raises(EngineeringProtocolError, match="unsupported persistent engineering effect"):
        build_engineering_goal_plan(
            goal="不要这样做",
            requested_effects=("merge_protected_branch",),
            original_request="merge main",
        )
