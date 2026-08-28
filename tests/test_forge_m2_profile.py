from __future__ import annotations

from pathlib import Path

import pytest

from actions import (
    ActionAuthorizationPolicy,
    ActionExecutionError,
    ActionProposal,
    ActionRisk,
    ForgeProjectProfile,
    ForgeProjectRegistry,
    ForgeTaskAdapter,
    build_forge_argv,
    forge_task_action_spec,
)


def _profile(tmp_path: Path, **overrides: object) -> ForgeProjectProfile:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    return ForgeProjectProfile(
        project_id="hikari",
        repository=repo,
        verification=["python -m pytest -q"],
        **overrides,
    )


def _proposal(arguments: dict[str, object]) -> ActionProposal:
    return ActionProposal(
        action_name="run_forge_task",
        arguments=arguments,
        effect="dispatch one bounded engineering task",
        reason="M2 profile boundary test",
        confidence=0.95,
        risk=ActionRisk.REVERSIBLE,
        requires_confirmation=True,
    )


def test_default_profile_preserves_existing_forge_cli_shape(tmp_path: Path):
    profile = _profile(tmp_path)
    argv = build_forge_argv(
        executable=profile.executable,
        task_file=tmp_path / "task.yaml",
        profile=profile,
    )

    assert "--worker-model" not in argv
    assert "--strong-model" not in argv
    assert "--reviewer-model" not in argv
    assert "--review-mode" not in argv
    assert "--allowed-path" not in argv
    assert profile.review_mode == "off"
    assert profile.worker_model is None
    assert profile.strong_model is None
    assert profile.reviewer_model is None


def test_trusted_m2_policy_flows_into_forge_argv(tmp_path: Path):
    profile = _profile(
        tmp_path,
        allowed_paths=["actions/", "tests/"],
        worker_model="flash-like",
        strong_model="strong-class",
        reviewer_model="judge-model",
        review_mode="evidence",
        escalation_threshold=3,
        broad_change_threshold=7,
    )

    argv = build_forge_argv(
        executable=profile.executable,
        task_file=tmp_path / "task.yaml",
        profile=profile,
    )

    assert argv[argv.index("--worker-model") + 1] == "flash-like"
    assert argv[argv.index("--strong-model") + 1] == "strong-class"
    assert argv[argv.index("--reviewer-model") + 1] == "judge-model"
    assert argv[argv.index("--review-mode") + 1] == "evidence"
    assert argv[argv.index("--escalation-threshold") + 1] == "3"
    assert argv[argv.index("--broad-change-threshold") + 1] == "7"
    assert [argv[i + 1] for i, part in enumerate(argv[:-1]) if part == "--allowed-path"] == [
        "actions/",
        "tests/",
    ]

    # M5-06 deliberately does not expose remote delivery authority.
    for forbidden in ("--remote", "--target-branch"):
        assert forbidden not in argv


def test_enabled_semantic_review_requires_trusted_reviewer_model(tmp_path: Path):
    for mode in ("evidence", "always"):
        with pytest.raises(ValueError, match="reviewer_model"):
            _profile(tmp_path, review_mode=mode)


def test_codex_profile_cannot_carry_claude_m2_model_policy(tmp_path: Path):
    with pytest.raises(ValueError, match="requires the claude backend"):
        _profile(tmp_path, backend="codex", worker_model="flash-like")

    with pytest.raises(ValueError, match="requires the claude backend"):
        _profile(
            tmp_path,
            backend="codex",
            reviewer_model="judge-model",
            review_mode="always",
        )


def test_model_cannot_smuggle_m2_policy_through_action_arguments(tmp_path: Path):
    profile = _profile(
        tmp_path,
        worker_model="trusted-worker",
        strong_model="trusted-strong",
        reviewer_model="trusted-reviewer",
        review_mode="always",
    )
    registry = ForgeProjectRegistry([profile])
    calls: list[list[str]] = []
    adapter = ForgeTaskAdapter(registry, work_dir=tmp_path / "tasks", runner=lambda argv: calls.append(argv) or 0)

    proposal = _proposal(
        {
            "project_id": "hikari",
            "goal": "Make a small verified change.",
            "constraints": [],
            "acceptance": ["Tests pass."],
            "worker_model": "model-chosen",
        }
    )
    authorization = ActionAuthorizationPolicy().confirm(proposal, approved=True)
    assert authorization.authorized_action is not None

    with pytest.raises(ActionExecutionError, match="rejected: worker_model"):
        adapter.execute(authorization.authorized_action)

    assert calls == []


def test_model_visible_forge_spec_still_exposes_only_engineering_intent():
    description = forge_task_action_spec().description.lower()

    for trusted_setting in (
        "worker_model",
        "strong_model",
        "reviewer_model",
        "review_mode",
        "escalation_threshold",
        "broad_change_threshold",
        "allowed_path",
        "remote",
        "target_branch",
    ):
        assert trusted_setting not in description
