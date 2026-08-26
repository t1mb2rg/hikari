from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol, runtime_checkable

from attention.policy import AttentionDecision, AttentionPolicy
from awareness.context import ContextCollector
from brain.reasoner import Feedback, Reasoner
from events.models import Event
from memory.candidates import MemoryCandidate, MemoryCandidatePolicy
from memory.recall import MemoryRecallPolicy, memories_as_context
from memory.store import MemoryEvent, MemoryStore
from personality import HIKARI_PERSONALITY_KEY, PersonalityProfile, personality_as_context


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
    candidate: MemoryCandidate | None


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
        candidate_policy: MemoryCandidatePolicy | None = None,
        recall_policy: MemoryRecallPolicy | None = None,
        personality_profile: PersonalityProfile | None = None,
    ) -> None:
        self.memory = memory
        self.attention = attention
        self.reasoner = reasoner
        self.feedback_sink = feedback_sink
        self.context_collector = context_collector
        self.candidate_policy = candidate_policy
        self.recall_policy = recall_policy
        self.personality_profile = personality_profile

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

        candidate = (
            self.candidate_policy.propose(remembered)
            if self.candidate_policy is not None
            else None
        )

        if not decision.should_intervene:
            return InterventionResult(
                remembered=remembered,
                decision=decision,
                feedback=None,
                candidate=candidate,
            )

        reasoning_context = dict(event.context)

        if self.recall_policy is not None:
            recalled = self.recall_policy.recall(self.memory, event.event_type)
            if recalled:
                reasoning_context["_hikari_recall"] = memories_as_context(recalled)

        if self.personality_profile is not None:
            reasoning_context[HIKARI_PERSONALITY_KEY] = personality_as_context(
                self.personality_profile
            )

        reasoning_event = (
            replace(event, context=reasoning_context)
            if reasoning_context != event.context
            else event
        )

        feedback = self.reasoner.reason(reasoning_event, decision)
        self.feedback_sink.deliver(feedback)
        return InterventionResult(
            remembered=remembered,
            decision=decision,
            feedback=feedback,
            candidate=candidate,
        )
