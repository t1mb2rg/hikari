from __future__ import annotations

from core.delivery import DeliveryOutbox, DeliveryRequest, DeliveryRouter

from .bindings import EngineeringConversationBindingStore
from .session import EngineeringProtocolError, EngineeringSessionStore


def _task_label(intent: str) -> str:
    text = " ".join(intent.split())
    if len(text) > 140:
        text = text[:137].rstrip() + "..."
    return text or "未命名工程任务"


class EngineeringCompletionDelivery:
    """Project terminal EngineeringSession results into Hikari's durable delivery outbox.

    Historical terminal results remain recoverable after crashes or binding rotation,
    but every user-visible delivery identifies the original task and whether it is a
    delayed historical result. This prevents an old failure from masquerading as the
    outcome of the user's current Engineering task.
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
                turn = self.sessions.load_turn(state.session_id, turn_id)
            except EngineeringProtocolError:
                continue
            if not binding.conversation_id.startswith("private:"):
                continue
            recipient = binding.conversation_id.removeprefix("private:").strip()
            if not recipient:
                continue

            current = self.bindings.for_conversation(binding.channel, binding.conversation_id)
            historical = current is not None and current.session_id != state.session_id
            task = _task_label(turn.intent)
            context = "补发旧工程任务结果" if historical else "工程任务结果"

            if result.status == "completed":
                text = f"{context}：已完成。\n任务：{task}\n\n{result.message}"
            elif result.status == "blocked":
                text = (
                    f"{context}：被权限或安全边界阻止。\n"
                    f"任务：{task}\n\n{result.message}"
                )
            else:
                text = f"{context}：没有完成。\n任务：{task}\n\n{result.message}"

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
