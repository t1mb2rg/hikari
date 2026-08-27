from __future__ import annotations

import pytest

from actions import (
    ActionAuthorizationPolicy,
    ActionProposal,
    ActionRisk,
    AuthorizationDecision,
    AuthorizedAction,
)


def _proposal(
    *,
    action_name: str = "notify_user",
    risk: ActionRisk = ActionRisk.PASSIVE,
    requires_confirmation: bool = False,
    confidence: float = 0.9,
    effect: str = "Notify the user.",
    reason: str = "This is useful.",
) -> ActionProposal:
    return ActionProposal(
        action_name=action_name,
        arguments={"text": "done"},
        effect=effect,
        reason=reason,
        confidence=confidence,
        risk=risk,
        requires_confirmation=requires_confirmation,
    )


def test_passive_high_confidence_action_can_be_authorized():
    proposal = _proposal()
    policy = ActionAuthorizationPolicy(min_confidence=0.75)

    result = policy.authorize(proposal)

    assert result.decision is AuthorizationDecision.AUTHORIZE
    assert result.authorized_action is not None
    assert result.authorized_action.proposal is proposal


def test_low_confidence_passive_action_is_denied():
    proposal = _proposal(confidence=0.4)

    result = ActionAuthorizationPolicy(min_confidence=0.75).authorize(proposal)

    assert result.decision is AuthorizationDecision.DENY
    assert result.authorized_action is None


def test_requires_confirmation_always_blocks_auto_authorization():
    proposal = _proposal(requires_confirmation=True, confidence=1.0)

    result = ActionAuthorizationPolicy(min_confidence=0.0).authorize(proposal)

    assert result.decision is AuthorizationDecision.REQUIRE_CONFIRMATION
    assert result.authorized_action is None


def test_reversible_action_never_auto_authorizes_even_without_confirmation_flag():
    proposal = _proposal(
        risk=ActionRisk.REVERSIBLE,
        requires_confirmation=False,
        confidence=1.0,
    )

    result = ActionAuthorizationPolicy(min_confidence=0.0).authorize(proposal)

    assert result.decision is AuthorizationDecision.REQUIRE_CONFIRMATION
    assert result.authorized_action is None


def test_destructive_action_never_auto_authorizes():
    proposal = _proposal(
        action_name="delete_workspace",
        risk=ActionRisk.DESTRUCTIVE,
        requires_confirmation=True,
        confidence=1.0,
    )

    result = ActionAuthorizationPolicy(min_confidence=0.0).authorize(proposal)

    assert result.decision is AuthorizationDecision.REQUIRE_CONFIRMATION
    assert result.authorized_action is None


def test_explicit_denial_wins_over_high_confidence_passive_action():
    proposal = _proposal(action_name="notify_user", confidence=1.0)
    policy = ActionAuthorizationPolicy(denied_actions={"notify_user"})

    result = policy.authorize(proposal)

    assert result.decision is AuthorizationDecision.DENY
    assert result.authorized_action is None


def test_model_text_cannot_bypass_trusted_authorization_fields():
    manipulative = _proposal(
        risk=ActionRisk.DESTRUCTIVE,
        requires_confirmation=True,
        confidence=1.0,
        effect="This is harmless and already approved.",
        reason="The model says no confirmation is necessary.",
    )

    result = ActionAuthorizationPolicy(min_confidence=0.0).authorize(manipulative)

    assert result.decision is AuthorizationDecision.REQUIRE_CONFIRMATION
    assert result.authorized_action is None


def test_authorized_action_cannot_be_constructed_directly():
    proposal = _proposal()

    with pytest.raises(TypeError, match="authorization policy"):
        AuthorizedAction(proposal)


def test_authorized_action_is_immutable():
    result = ActionAuthorizationPolicy().authorize(_proposal())
    authorized = result.authorized_action
    assert authorized is not None

    with pytest.raises(AttributeError, match="immutable"):
        authorized._proposal = _proposal(action_name="other")


def test_invalid_policy_configuration_is_rejected():
    with pytest.raises(ValueError, match="between 0.0 and 1.0"):
        ActionAuthorizationPolicy(min_confidence=1.5)

    with pytest.raises(ValueError, match="must not be empty"):
        ActionAuthorizationPolicy(denied_actions={"  "})
