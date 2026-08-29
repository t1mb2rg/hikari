from __future__ import annotations

from actions import ActionExecutor, ActionFeedbackSink, WindowsToastNotifyAdapter
from brain.reasoner import Feedback
from core.delivery import DeliveryRecord, DeliveryRequest, DeliveryRouter
from core.presence_policy import PresenceDecision
from events.models import Event


class WindowsDeliverySink:
    """Immediate local leaf used behind M6's durable DeliveryRouter."""

    def __init__(self) -> None:
        self.feedback = ActionFeedbackSink(
            ActionExecutor([WindowsToastNotifyAdapter(app_name="Hikari")])
        )

    def send(self, request: DeliveryRequest) -> None:
        if request.channel != "windows" or request.recipient != "local":
            raise ValueError("Windows delivery sink accepts only the local windows recipient")
        self.feedback.deliver(
            Feedback(
                text=request.text,
                event_type="presence.delivery",
                importance=1.0,
            )
        )


class RoutedPresenceDelivery:
    """Translate an approved PresenceDecision into the trusted M6-11 boundary.

    The model contributes only `feedback.text`. Channel, delivery id and recipient
    all come from deterministic policy/runtime configuration.
    """

    def __init__(
        self,
        router: DeliveryRouter,
        *,
        qq_recipient: str | None = None,
    ) -> None:
        if not isinstance(router, DeliveryRouter):
            raise TypeError("RoutedPresenceDelivery requires DeliveryRouter")
        recipient = qq_recipient.strip() if isinstance(qq_recipient, str) else None
        self.router = router
        self.qq_recipient = recipient or None

    def deliver(
        self,
        event: Event,
        feedback: Feedback,
        decision: PresenceDecision,
    ) -> DeliveryRecord:
        if not isinstance(event, Event):
            raise TypeError("Presence delivery requires Event")
        if not isinstance(feedback, Feedback):
            raise TypeError("Presence delivery requires Feedback")
        if not isinstance(decision, PresenceDecision):
            raise TypeError("Presence delivery requires PresenceDecision")
        if not decision.should_deliver:
            raise ValueError("refusing to route a suppressed Presence decision")

        if decision.channel == "windows":
            recipient = "local"
        elif decision.channel == "qq":
            recipient = self.qq_recipient
            if recipient is None:
                raise ValueError("trusted QQ proactive recipient is not configured")
        else:
            raise ValueError(f"unsupported Presence delivery channel: {decision.channel}")

        request = DeliveryRequest(
            delivery_id=decision.delivery_id,
            channel=decision.channel,
            recipient=recipient,
            text=feedback.text,
            source=f"presence:{event.event_type}",
        )
        return self.router.submit(request)
