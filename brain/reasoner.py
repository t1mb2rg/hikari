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
    """Deterministic fallback reasoner used to prove the proactive loop.

    Chinese is Hikari's default user-facing language. A model-backed reasoner
    can later adapt language from explicit user/context signals without changing
    the surrounding Presence pipeline.
    """

    def reason(self, event: Event, decision: AttentionDecision) -> Feedback:
        return Feedback(
            text=f"我注意到一件值得你看一眼的事：{event.content}",
            event_type=event.event_type,
            importance=decision.importance,
        )
