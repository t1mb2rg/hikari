from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from core.delivery import DeliveryOutbox, DeliveryRequest, DeliveryRouter

from .bindings import EngineeringConversationBindingStore
from .session import EngineeringProtocolError, EngineeringSessionStore


@dataclass(frozen=True)
class EngineeringCompletionFacts:
    """Machine truth projected from one durable terminal engineering turn.

    This object deliberately contains no user-facing prose. Engineering owns the
    grounded result; a Conversation-owned renderer decides how Jarvis says it.
    """

    status: str
    goal: str
    summary: str
    changed_files: tuple[str, ...] = ()
    branch: str | None = None
    historical: bool = False


EngineeringCompletionRenderer = Callable[[EngineeringCompletionFacts, str, str], str]


class EngineeringCompletionDelivery:
    """Project terminal EngineeringSession truth into Hikari's durable delivery outbox.

    The Engineering layer owns durable facts and delivery idempotency, not Jarvis voice.
    When no renderer is attached, terminal state remains durable but no new user-facing
    text is manufactured here. Resident attaches the Conversation-owned renderer.

    Existing durable DeliveryOutbox rows are immutable historical facts and are never
    rewritten merely because presentation evolves.
    """

    def __init__(
        self,
        sessions: EngineeringSessionStore,
        bindings: EngineeringConversationBindingStore,
        outbox: DeliveryOutbox,
        *,
        renderer: EngineeringCompletionRenderer | None = None,
    ) -> None:
        if not isinstance(sessions, EngineeringSessionStore):
            raise TypeError("EngineeringCompletionDelivery requires EngineeringSessionStore")
        if not isinstance(bindings, EngineeringConversationBindingStore):
            raise TypeError("EngineeringCompletionDelivery requires EngineeringConversationBindingStore")
        if not isinstance(outbox, DeliveryOutbox):
            raise TypeError("EngineeringCompletionDelivery requires DeliveryOutbox")
        if renderer is not None and not callable(renderer):
            raise TypeError("EngineeringCompletionDelivery renderer must be callable or None")
        self.sessions = sessions
        self.bindings = bindings
        self.router = DeliveryRouter(outbox)
        self.renderer = renderer

    def pump(self) -> int:
        """Idempotently enqueue rendered terminal facts for bound conversations."""

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

            delivery_id = f"engineering:{state.session_id}:{turn_id}"

            # Delivery ids are durable idempotency keys. A record created by an
            # older Hikari version must retain its original text; resubmitting the
            # same id with a newer presentation format would correctly violate the
            # DeliveryOutbox immutability check.
            try:
                existing = self.router.outbox.get(delivery_id)
            except Exception:
                existing = None
            if existing is not None:
                if existing.state in {"pending", "sending", "sent", "uncertain"}:
                    submitted += 1
                continue

            # Engineering Worker intentionally constructs this class without a
            # renderer. That preserves terminal truth while preventing the worker
            # process from inventing Jarvis-facing prose. Resident owns rendering.
            if self.renderer is None:
                continue

            current = self.bindings.for_conversation(binding.channel, binding.conversation_id)
            historical = current is not None and current.session_id != state.session_id
            facts = EngineeringCompletionFacts(
                status=result.status,
                goal=turn.intent,
                summary=result.message,
                changed_files=tuple(result.changed_files),
                branch=state.workspace_branch,
                historical=historical,
            )
            text = self.renderer(facts, binding.channel, binding.conversation_id)
            if not isinstance(text, str) or not text.strip():
                raise ValueError("engineering completion renderer returned empty text")

            record = self.router.submit(
                DeliveryRequest(
                    delivery_id=delivery_id,
                    channel="qq",
                    recipient=recipient,
                    text=text.strip(),
                    source="engineering",
                )
            )
            if record.state in {"pending", "sending", "sent", "uncertain"}:
                submitted += 1
        return submitted
