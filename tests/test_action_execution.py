from __future__ import annotations

import pytest

from actions import (
    ActionAuthorizationPolicy,
    ActionExecutionError,
    ActionExecutor,
    ActionProposal,
    ActionRisk,
    ConsoleNotifyAdapter,
    ExecutionResult,
)


def _proposal(
    *,
    action_name: str = "notify_user",
    arguments: dict[str, object] | None = None,
    risk: ActionRisk = ActionRisk.PASSIVE,
    requires_confirmation: bool = False,
    confidence: float = 0.95,
) -> ActionProposal:
    return ActionProposal(
        action_name=action_name,
        arguments=arguments if arguments is not None else {"text": "Hikari acted."},
        effect="notify the user",
        reason="bounded execution test",
        confidence=confidence,
        risk=risk,
        requires_confirmation=requires_confirmation,
    )


def _authorize(proposal: ActionProposal):
    result = ActionAuthorizationPolicy().authorize(proposal)
    assert result.authorized_action is not None
    return result.authorized_action


def test_authorized_console_notification_executes_once(capsys):
    action = _authorize(_proposal(arguments={"text": "光第一次真的做了一件事。"}))
    executor = ActionExecutor([ConsoleNotifyAdapter()])

    result = executor.execute(action)

    assert capsys.readouterr().out == "光第一次真的做了一件事。\n"
    assert result == ExecutionResult(
        action_name="notify_user",
        success=True,
        summary="console notification delivered",
    )


def test_executor_rejects_raw_proposal_before_side_effect(capsys):
    executor = ActionExecutor([ConsoleNotifyAdapter()])

    with pytest.raises(TypeError, match="only AuthorizedAction"):
        executor.execute(_proposal())  # type: ignore[arg-type]

    assert capsys.readouterr().out == ""


def test_unknown_action_is_rejected_before_side_effect(capsys):
    proposal = _proposal(action_name="unknown_action")
    action = _authorize(proposal)
    executor = ActionExecutor([ConsoleNotifyAdapter()])

    with pytest.raises(ActionExecutionError, match="no registered adapter"):
        executor.execute(action)

    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"text": ""},
        {"text": 123},
        {"text": "hello", "extra": True},
    ],
)
def test_console_notification_validates_arguments_before_print(arguments, capsys):
    action = _authorize(_proposal(arguments=arguments))
    executor = ActionExecutor([ConsoleNotifyAdapter()])

    with pytest.raises(ActionExecutionError):
        executor.execute(action)

    assert capsys.readouterr().out == ""


def test_adapter_rejects_authorized_action_for_another_name(capsys):
    action = _authorize(_proposal(action_name="other_passive"))
    adapter = ConsoleNotifyAdapter()

    with pytest.raises(ActionExecutionError, match="cannot execute"):
        adapter.execute(action)

    assert capsys.readouterr().out == ""


def test_non_authorized_action_never_reaches_executor():
    proposal = _proposal(
        risk=ActionRisk.REVERSIBLE,
        requires_confirmation=True,
    )
    authorization = ActionAuthorizationPolicy().authorize(proposal)

    assert authorization.authorized_action is None
