from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from attention.policy import AttentionDecision
from events.models import Event


@dataclass(frozen=True)
class Feedback:
    """A user-facing message produced after an event passes Attention."""

    text: str
    event_type: str
    importance: float


@runtime_checkable
class Reasoner(Protocol):
    """Replaceable cognition layer used only for noteworthy events."""

    def reason(self, event: Event, decision: AttentionDecision) -> Feedback:
        ...


class SimpleReasoner:
    """Deterministic M0 reasoner used to prove the proactive loop.

    A model-backed implementation can replace this later without changing the
    surrounding Presence pipeline.
    """

    def reason(self, event: Event, decision: AttentionDecision) -> Feedback:
        return Feedback(
            text=f"I noticed something worth your attention: {event.content}",
            event_type=event.event_type,
            importance=decision.importance,
        )
