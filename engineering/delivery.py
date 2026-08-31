from __future__ import annotations

from core.delivery import DeliveryOutbox, DeliveryRequest, DeliveryRouter

from .bindings import EngineeringConversationBindingStore
from .session import EngineeringProtocolError, EngineeringSessionStore


class EngineeringCompletionDelivery:
    """Project terminal EngineeringSession results into Hikari's durable delivery outbox.

    This is not an external callback. The worker advances Hikari-owned session
    state, then this adapter exposes terminal internal state through the same
    M6 DeliveryOutbox already used by Presence.
    """

    def __init__(
        self,
        sessions: EngineeringSessionStore,
        bindings: EngineeringConversationBindingStore,
        outbox: DeliveryOutbox,
    ) -> None:
        if not isinstance(sessions, EngineeringSessionStore):
            raise TypeError("EngineeringCompletionDelivery requires EngineeringSessionStore")
        if not isinstance(bindings, EngineeringConversationBindingStore):
            raise TypeError("EngineeringCompletionDelivery requires EngineeringConversationBindingStore")
        if not isinstance(outbox, DeliveryOutbox):
            raise TypeError("EngineeringCompletionDelivery requires DeliveryOutbox")
        self.sessions = sessions
        self.bindings = bindings
        self.router = DeliveryRouter(outbox)

    def pump(self) -> int:
        """Idempotently enqueue terminal results that belong to a bound conversation."""

        submitted = 0
        for binding in self.bindings.all():
            if binding.channel != "qq":
                continue
            try:
                state = self.sessions.load(binding.session_id)
            except EngineeringProtocolError:
                continue
            if state.status not in {"completed", "failed", "blocked"}:
                continue
            turn_id = state.current_turn_id
            if not turn_id:
                continue
            try:
                result = self.sessions.load_result(state.session_id, turn_id)
            except EngineeringProtocolError:
                continue
            if not binding.conversation_id.startswith("private:"):
                continue
            recipient = binding.conversation_id.removeprefix("private:").strip()
            if not recipient:
                continue

            if result.status == "completed":
                text = "我看完了。\n\n" + result.message
            elif result.status == "blocked":
                text = "工程会话被权限或安全边界阻止了。\n\n" + result.message
            else:
                text = "工程会话没有完成。\n\n" + result.message

            delivery_id = f"engineering:{state.session_id}:{turn_id}"
            record = self.router.submit(
                DeliveryRequest(
                    delivery_id=delivery_id,
                    channel="qq",
                    recipient=recipient,
                    text=text,
                    source="engineering",
                )
            )
            if record.state in {"pending", "sending", "sent", "uncertain"}:
                submitted += 1
        return submitted
