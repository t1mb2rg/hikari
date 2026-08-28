from __future__ import annotations

import pytest

from actions import (
    ActionExecutionError,
    ActionExecutor,
    ActionFeedbackSink,
    ActionRisk,
    WindowsToastNotifyAdapter,
)
from brain import Feedback


def test_feedback_becomes_one_trusted_passive_notification():
    feedback = Feedback(
        text="光在后台注意到了一个变化。",
        event_type="git.commit",
        importance=0.8,
    )

    proposal = ActionFeedbackSink.proposal_for(feedback)

    assert proposal.action_name == "notify_user"
    assert proposal.arguments == {"text": "光在后台注意到了一个变化。"}
    assert proposal.risk is ActionRisk.PASSIVE
    assert proposal.requires_confirmation is False
    assert proposal.confidence == 1.0


def test_feedback_sink_authorizes_then_dispatches_exact_text_once():
    calls: list[tuple[str, str]] = []
    sink = ActionFeedbackSink(
        ActionExecutor(
            [
                WindowsToastNotifyAdapter(
                    app_name="Hikari",
                    sender=lambda app, text: calls.append((app, text)),
                )
            ]
        )
    )

    sink.deliver(
        Feedback(
            text="我在终端外也能找到你了。",
            event_type="git.commit",
            importance=0.9,
        )
    )

    assert calls == [("Hikari", "我在终端外也能找到你了。")]


def test_feedback_sink_rejects_empty_text_before_side_effect():
    calls: list[tuple[str, str]] = []
    sink = ActionFeedbackSink(
        ActionExecutor(
            [
                WindowsToastNotifyAdapter(
                    sender=lambda app, text: calls.append((app, text)),
                )
            ]
        )
    )

    with pytest.raises(ActionExecutionError, match="feedback text"):
        sink.deliver(Feedback(text="   ", event_type="git.commit", importance=0.9))

    assert calls == []


def test_feedback_sink_rejects_non_feedback_input_before_side_effect():
    calls: list[tuple[str, str]] = []
    sink = ActionFeedbackSink(
        ActionExecutor(
            [
                WindowsToastNotifyAdapter(
                    sender=lambda app, text: calls.append((app, text)),
                )
            ]
        )
    )

    with pytest.raises(TypeError, match="only Feedback"):
        sink.deliver("hello")  # type: ignore[arg-type]

    assert calls == []
