from __future__ import annotations

import os

import pytest

from actions import (
    ActionAuthorizationPolicy,
    ActionExecutionError,
    ActionExecutor,
    ActionProposal,
    ActionRisk,
    ExecutionResult,
    WindowsNotificationUnavailable,
    WindowsToastNotifyAdapter,
)


def _authorized_notification(arguments: dict[str, object]):
    proposal = ActionProposal(
        action_name="notify_user",
        arguments=arguments,
        effect="show a desktop notification",
        reason="Windows notification adapter test",
        confidence=0.98,
        risk=ActionRisk.PASSIVE,
        requires_confirmation=False,
    )
    result = ActionAuthorizationPolicy().authorize(proposal)
    assert result.authorized_action is not None
    return result.authorized_action


def test_windows_toast_dispatches_authorized_notification_once():
    calls: list[tuple[str, str]] = []
    adapter = WindowsToastNotifyAdapter(
        app_name="Hikari",
        sender=lambda app, text: calls.append((app, text)),
    )
    executor = ActionExecutor([adapter])
    action = _authorized_notification({"text": "我找到你了。"})

    result = executor.execute(action)

    assert calls == [("Hikari", "我找到你了。")]
    assert result == ExecutionResult(
        action_name="notify_user",
        success=True,
        summary="Windows toast notification delivered",
    )


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"text": ""},
        {"text": 123},
        {"text": "hello", "extra": True},
    ],
)
def test_windows_toast_validates_before_sender(arguments):
    calls: list[tuple[str, str]] = []
    adapter = WindowsToastNotifyAdapter(
        sender=lambda app, text: calls.append((app, text)),
    )
    action = _authorized_notification(arguments)

    with pytest.raises(ActionExecutionError):
        adapter.execute(action)

    assert calls == []


def test_windows_toast_rejects_empty_app_name():
    with pytest.raises(ValueError, match="app_name"):
        WindowsToastNotifyAdapter(app_name="   ")


def test_default_windows_transport_fails_clearly_off_windows():
    if os.name == "nt":
        pytest.skip("non-Windows boundary test")

    adapter = WindowsToastNotifyAdapter()
    action = _authorized_notification({"text": "not delivered"})

    with pytest.raises(WindowsNotificationUnavailable, match="only available on Windows"):
        adapter.execute(action)
