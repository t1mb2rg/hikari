from __future__ import annotations

from pathlib import Path

import pytest

from actions import (
    ActionAuthorizationPolicy,
    ActionExecutionError,
    ActionExecutor,
    ActionProposal,
    ActionRisk,
    AuthorizationDecision,
    CreateLocalNoteAdapter,
)


def _proposal(
    *,
    risk: ActionRisk = ActionRisk.REVERSIBLE,
    requires_confirmation: bool = True,
    text: str = "光第一次经过确认修改了本机状态。",
) -> ActionProposal:
    return ActionProposal(
        action_name="create_local_note",
        arguments={"text": text},
        effect="create one bounded local note",
        reason="confirmed local action test",
        confidence=0.95,
        risk=risk,
        requires_confirmation=requires_confirmation,
    )


def test_reversible_action_requires_confirmation_then_authorizes():
    policy = ActionAuthorizationPolicy()
    proposal = _proposal()

    initial = policy.authorize(proposal)
    assert initial.decision is AuthorizationDecision.REQUIRE_CONFIRMATION
    assert initial.authorized_action is None

    confirmed = policy.confirm(proposal, approved=True)
    assert confirmed.decision is AuthorizationDecision.AUTHORIZE
    assert confirmed.authorized_action is not None
    assert confirmed.authorized_action.proposal is proposal


def test_external_denial_never_authorizes():
    result = ActionAuthorizationPolicy().confirm(_proposal(), approved=False)

    assert result.decision is AuthorizationDecision.DENY
    assert result.authorized_action is None


def test_destructive_action_remains_blocked_after_confirmation():
    proposal = _proposal(risk=ActionRisk.DESTRUCTIVE)
    result = ActionAuthorizationPolicy().confirm(proposal, approved=True)

    assert result.decision is AuthorizationDecision.DENY
    assert result.authorized_action is None


def test_denied_action_rule_still_wins_over_confirmation():
    policy = ActionAuthorizationPolicy(denied_actions={"create_local_note"})
    result = policy.confirm(_proposal(), approved=True)

    assert result.decision is AuthorizationDecision.DENY
    assert result.authorized_action is None


def test_create_local_note_writes_only_caller_owned_target(tmp_path: Path):
    target = tmp_path / "hikari-note.txt"
    proposal = _proposal(text="bounded local state")
    authorization = ActionAuthorizationPolicy().confirm(proposal, approved=True)
    assert authorization.authorized_action is not None

    result = ActionExecutor([CreateLocalNoteAdapter(target)]).execute(
        authorization.authorized_action
    )

    assert target.read_text(encoding="utf-8") == "bounded local state\n"
    assert result.success is True
    assert result.action_name == "create_local_note"


def test_model_arguments_cannot_choose_a_path(tmp_path: Path):
    target = tmp_path / "trusted.txt"
    other = tmp_path / "model-chosen.txt"
    proposal = ActionProposal(
        action_name="create_local_note",
        arguments={"text": "hello", "path": str(other)},
        effect="try to choose a path",
        reason="boundary test",
        confidence=0.95,
        risk=ActionRisk.REVERSIBLE,
        requires_confirmation=True,
    )
    authorization = ActionAuthorizationPolicy().confirm(proposal, approved=True)
    assert authorization.authorized_action is not None

    with pytest.raises(ActionExecutionError, match="exactly one `text`"):
        ActionExecutor([CreateLocalNoteAdapter(target)]).execute(
            authorization.authorized_action
        )

    assert not target.exists()
    assert not other.exists()


def test_existing_local_note_is_never_overwritten(tmp_path: Path):
    target = tmp_path / "existing.txt"
    target.write_text("original\n", encoding="utf-8")
    authorization = ActionAuthorizationPolicy().confirm(_proposal(), approved=True)
    assert authorization.authorized_action is not None

    with pytest.raises(ActionExecutionError, match="already exists"):
        ActionExecutor([CreateLocalNoteAdapter(target)]).execute(
            authorization.authorized_action
        )

    assert target.read_text(encoding="utf-8") == "original\n"


def test_missing_parent_fails_before_write(tmp_path: Path):
    target = tmp_path / "missing" / "note.txt"
    authorization = ActionAuthorizationPolicy().confirm(_proposal(), approved=True)
    assert authorization.authorized_action is not None

    with pytest.raises(ActionExecutionError, match="parent directory does not exist"):
        ActionExecutor([CreateLocalNoteAdapter(target)]).execute(
            authorization.authorized_action
        )

    assert not target.exists()
