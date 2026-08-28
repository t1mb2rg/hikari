from __future__ import annotations

from brain.reasoner import Feedback

from .authorization import ActionAuthorizationPolicy, AuthorizationDecision
from .contract import ActionProposal, ActionRisk
from .execution import ActionExecutionError, ActionExecutor


class ActionFeedbackSink:
    """Deliver proactive Feedback through Hikari's trusted passive action path.

    Presence only sees the FeedbackSink shape (`deliver(feedback)`). This adapter
    converts that already-decided user-facing feedback into exactly one passive
    `notify_user` proposal, runs deterministic authorization, then dispatches it
    through an explicit ActionExecutor. The feedback text can come from a model;
    all capability metadata remains trusted code.
    """

    def __init__(
        self,
        executor: ActionExecutor,
        *,
        authorization: ActionAuthorizationPolicy | None = None,
    ) -> None:
        if not isinstance(executor, ActionExecutor):
            raise TypeError("ActionFeedbackSink requires an ActionExecutor")
        self.executor = executor
        self.authorization = authorization or ActionAuthorizationPolicy()

    @staticmethod
    def proposal_for(feedback: Feedback) -> ActionProposal:
        if not isinstance(feedback, Feedback):
            raise TypeError("ActionFeedbackSink accepts only Feedback")
        if not isinstance(feedback.text, str) or not feedback.text.strip():
            raise ActionExecutionError("feedback text must be a non-empty string")

        return ActionProposal(
            action_name="notify_user",
            arguments={"text": feedback.text.strip()},
            effect="deliver one proactive Hikari notification to the user",
            reason=(
                "Presence already decided this feedback should be surfaced; "
                "use the trusted passive notification channel"
            ),
            confidence=1.0,
            risk=ActionRisk.PASSIVE,
            requires_confirmation=False,
        )

    def deliver(self, feedback: Feedback) -> None:
        proposal = self.proposal_for(feedback)
        authorization = self.authorization.authorize(proposal)
        if (
            authorization.decision is not AuthorizationDecision.AUTHORIZE
            or authorization.authorized_action is None
        ):
            raise ActionExecutionError(
                "trusted proactive notification was not authorized: "
                f"{authorization.reason}"
            )
        self.executor.execute(authorization.authorized_action)
