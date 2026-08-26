from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from attention.policy import AttentionDecision, AttentionPolicy
from awareness.context import ContextCollector
from brain.reasoner import Feedback, Reasoner
from events.models import Event
from memory.store import MemoryEvent, MemoryStore


@runtime_checkable
class FeedbackSink(Protocol):
    """Destination adapter for proactive feedback."""

    def deliver(self, feedback: Feedback) -> None:
        ...


class ConsoleFeedbackSink:
    """Minimal M0 output adapter."""

    def deliver(self, feedback: Feedback) -> None:
        print(feedback.text)


@dataclass(frozen=True)
class InterventionResult:
    remembered: MemoryEvent
    decision: AttentionDecision
    feedback: Feedback | None


class PresencePipeline:
    """Core proactive path from normalized Event to optional feedback.

    The pipeline deliberately knows nothing about Git, calendars, devices, or
    any other concrete sensor. It also knows nothing about the final feedback
    destination beyond the FeedbackSink contract.
    """

    def __init__(
        self,
        *,
        memory: MemoryStore,
        attention: AttentionPolicy,
        reasoner: Reasoner,
        feedback_sink: FeedbackSink,
        context_collector: ContextCollector | None = None,
    ) -> None:
        self.memory = memory
        self.attention = attention
        self.reasoner = reasoner
        self.feedback_sink = feedback_sink
        self.context_collector = context_collector

    def handle(self, event: Event) -> InterventionResult:
        if self.context_collector is not None:
            event = self.context_collector.enrich(event)

        remembered = self.memory.remember_event(
            event.event_type,
            event.content,
            context=event.context,
            occurred_at=event.occurred_at,
        )

        decision = self.attention.evaluate(event)
        remembered = self.memory.update_importance(
            remembered.id,
            decision.importance,
        )

        if not decision.should_intervene:
            return InterventionResult(
                remembered=remembered,
                decision=decision,
                feedback=None,
            )

        feedback = self.reasoner.reason(event, decision)
        self.feedback_sink.deliver(feedback)
        return InterventionResult(
            remembered=remembered,
            decision=decision,
            feedback=feedback,
        )
