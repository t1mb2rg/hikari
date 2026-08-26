from __future__ import annotations

import json

import pytest

from actions import (
    ActionCatalog,
    ActionPlanningError,
    ActionRisk,
    ActionSpec,
    ModelActionPlanner,
)
from attention import AttentionDecision
from brain import Feedback
from events import Event


class FakeProvider:
    def __init__(self, response: str) -> None:
        self.response = response
        self.calls = 0
        self.messages = None

    def complete(self, messages):
        self.calls += 1
        self.messages = messages
        return self.response


def _decision(*, intervene: bool = True) -> AttentionDecision:
    return AttentionDecision(
        should_intervene=intervene,
        importance=0.95 if intervene else 0.2,
        reason="test attention",
    )


def _event() -> Event:
    return Event(
        event_type="test.action",
        source="tests",
        content="The milestone passed and a concise next step may be useful.",
        context={"project": "hikari"},
    )


def _catalog() -> ActionCatalog:
    return ActionCatalog(
        [
            ActionSpec(
                name="notify_user",
                description="Deliver one concise notification to the user.",
                risk=ActionRisk.PASSIVE,
                requires_confirmation=False,
            ),
            ActionSpec(
                name="delete_workspace",
                description="Delete a workspace and its contents.",
                risk=ActionRisk.DESTRUCTIVE,
                requires_confirmation=True,
            ),
        ]
    )


def test_registered_action_becomes_proposal_with_trusted_policy():
    provider = FakeProvider(
        json.dumps(
            {
                "decision": "propose",
                "action": "notify_user",
                "arguments": {"text": "M5-01 is ready to close."},
                "effect": "The user receives one concise status update.",
                "reason": "The milestone has completed.",
                "confidence": 0.93,
            }
        )
    )
    planner = ModelActionPlanner(provider, _catalog())

    proposal = planner.plan(
        _event(),
        _decision(),
        feedback=Feedback(
            text="The milestone is complete.",
            event_type="test.action",
            importance=0.95,
        ),
    )

    assert proposal is not None
    assert proposal.action_name == "notify_user"
    assert proposal.arguments == {"text": "M5-01 is ready to close."}
    assert proposal.risk is ActionRisk.PASSIVE
    assert proposal.requires_confirmation is False
    assert provider.calls == 1


def test_none_returns_no_proposal():
    provider = FakeProvider('{"decision":"none","reason":"No useful allowed action."}')
    planner = ModelActionPlanner(provider, _catalog())

    assert planner.plan(_event(), _decision()) is None
    assert provider.calls == 1


def test_quiet_decision_makes_zero_provider_calls():
    provider = FakeProvider('{"decision":"propose"}')
    planner = ModelActionPlanner(provider, _catalog())

    assert planner.plan(_event(), _decision(intervene=False)) is None
    assert provider.calls == 0


def test_unknown_action_is_rejected():
    provider = FakeProvider(
        json.dumps(
            {
                "decision": "propose",
                "action": "run_shell",
                "arguments": {"command": "whoami"},
                "effect": "Runs a shell command.",
                "reason": "I want to inspect the host.",
                "confidence": 0.9,
            }
        )
    )
    planner = ModelActionPlanner(provider, _catalog())

    with pytest.raises(ActionPlanningError, match="unregistered action"):
        planner.plan(_event(), _decision())


def test_model_cannot_self_authorize_destructive_action():
    provider = FakeProvider(
        json.dumps(
            {
                "decision": "propose",
                "action": "delete_workspace",
                "arguments": {"workspace": "scratch"},
                "effect": "Deletes the scratch workspace.",
                "reason": "Cleanup was requested.",
                "confidence": 0.88,
                "risk": "passive",
                "requires_confirmation": False,
            }
        )
    )
    planner = ModelActionPlanner(provider, _catalog())

    proposal = planner.plan(_event(), _decision())

    assert proposal is not None
    assert proposal.risk is ActionRisk.DESTRUCTIVE
    assert proposal.requires_confirmation is True


def test_destructive_spec_cannot_disable_confirmation():
    with pytest.raises(ValueError, match="must require confirmation"):
        ActionSpec(
            name="dangerous",
            description="A destructive test capability.",
            risk=ActionRisk.DESTRUCTIVE,
            requires_confirmation=False,
        )


def test_malformed_arguments_and_confidence_are_rejected():
    bad_arguments = FakeProvider(
        '{"decision":"propose","action":"notify_user","arguments":[],"effect":"x","reason":"y","confidence":0.8}'
    )
    with pytest.raises(ActionPlanningError, match="arguments"):
        ModelActionPlanner(bad_arguments, _catalog()).plan(_event(), _decision())

    bad_confidence = FakeProvider(
        '{"decision":"propose","action":"notify_user","arguments":{},"effect":"x","reason":"y","confidence":true}'
    )
    with pytest.raises(ActionPlanningError, match="confidence"):
        ModelActionPlanner(bad_confidence, _catalog()).plan(_event(), _decision())
