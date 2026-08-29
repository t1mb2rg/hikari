from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol, runtime_checkable

from attention.policy import AttentionDecision, AttentionPolicy
from awareness.context import ContextCollector
from brain.reasoner import Feedback, Reasoner
from core.presence_policy import PresenceDecision, PresencePolicy
from events.models import Event
from learning import (
    LEARNED_CONTEXT_KEY,
    LearningAssimilationPolicy,
    learned_memories_as_context,
)
from memory.candidates import MemoryCandidate, MemoryCandidatePolicy
from memory.recall import MemoryRecallPolicy, memories_as_context
from memory.store import MemoryEvent, MemoryStore
from personality import (
    HIKARI_EMOTION_KEY,
    HIKARI_PERSONALITY_KEY,
    EmotionPolicy,
    EmotionState,
    PersonalityProfile,
    emotion_as_context,
    personality_as_context,
)


@runtime_checkable
class FeedbackSink(Protocol):
    """Destination adapter for legacy/direct proactive feedback."""

    def deliver(self, feedback: Feedback) -> None:
        ...


@runtime_checkable
class ProactiveDeliverySink(Protocol):
    """M6 delivery boundary for policy-approved Presence feedback."""

    def deliver(
        self,
        event: Event,
        feedback: Feedback,
        decision: PresenceDecision,
    ) -> object:
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
    emotion: EmotionState | None = None
    presence_decision: PresenceDecision | None = None


class PresencePipeline:
    """Core proactive path from normalized Event to optional feedback.

    Attention answers whether an event is worth deeper cognition. When an M6
    PresencePolicy is configured, it separately decides whether Hikari may
    interrupt the user *now*. Suppressed events never invoke the Reasoner.
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
        assimilation_policy: LearningAssimilationPolicy | None = None,
        personality_profile: PersonalityProfile | None = None,
        emotion_state: EmotionState | None = None,
        emotion_policy: EmotionPolicy | None = None,
        presence_policy: PresencePolicy | None = None,
        proactive_delivery_sink: ProactiveDeliverySink | None = None,
    ) -> None:
        if proactive_delivery_sink is not None and presence_policy is None:
            raise ValueError("proactive_delivery_sink requires presence_policy")
        self.memory = memory
        self.attention = attention
        self.reasoner = reasoner
        self.feedback_sink = feedback_sink
        self.context_collector = context_collector
        self.candidate_policy = candidate_policy
        self.recall_policy = recall_policy
        self.assimilation_policy = assimilation_policy
        self.personality_profile = personality_profile
        self.emotion_policy = emotion_policy
        self.presence_policy = presence_policy
        self.proactive_delivery_sink = proactive_delivery_sink
        self.emotion_state = (
            emotion_state
            if emotion_state is not None
            else emotion_policy.baseline if emotion_policy is not None else None
        )

    @property
    def current_emotion(self) -> EmotionState | None:
        return self.emotion_state

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

        if self.emotion_policy is not None:
            state = self.emotion_state or self.emotion_policy.baseline
            self.emotion_state = self.emotion_policy.transition(state, event, decision)

        if not decision.should_intervene:
            return InterventionResult(
                remembered=remembered,
                decision=decision,
                feedback=None,
                candidate=candidate,
                emotion=self.emotion_state,
                presence_decision=None,
            )

        presence_decision: PresenceDecision | None = None
        if self.presence_policy is not None:
            presence_decision = self.presence_policy.evaluate(event, decision)
            if not presence_decision.should_deliver:
                return InterventionResult(
                    remembered=remembered,
                    decision=decision,
                    feedback=None,
                    candidate=candidate,
                    emotion=self.emotion_state,
                    presence_decision=presence_decision,
                )

        reasoning_context = dict(event.context)

        if self.recall_policy is not None:
            recalled = self.recall_policy.recall(self.memory, event.event_type)
            if recalled:
                reasoning_context["_hikari_recall"] = memories_as_context(recalled)

        if self.assimilation_policy is not None:
            learned = self.assimilation_policy.recall(self.memory)
            if learned:
                reasoning_context[LEARNED_CONTEXT_KEY] = learned_memories_as_context(
                    learned
                )

        if self.personality_profile is not None:
            reasoning_context[HIKARI_PERSONALITY_KEY] = personality_as_context(
                self.personality_profile
            )

        if self.emotion_state is not None:
            reasoning_context[HIKARI_EMOTION_KEY] = emotion_as_context(self.emotion_state)

        reasoning_event = (
            replace(event, context=reasoning_context)
            if reasoning_context != event.context
            else event
        )

        feedback = self.reasoner.reason(reasoning_event, decision)
        if self.proactive_delivery_sink is not None:
            if presence_decision is None:
                raise RuntimeError("proactive delivery reached without PresenceDecision")
            self.proactive_delivery_sink.deliver(event, feedback, presence_decision)
        else:
            self.feedback_sink.deliver(feedback)

        if self.presence_policy is not None:
            if presence_decision is None:
                raise RuntimeError("PresencePolicy accepted without PresenceDecision")
            self.presence_policy.mark_accepted(presence_decision)

        return InterventionResult(
            remembered=remembered,
            decision=decision,
            feedback=feedback,
            candidate=candidate,
            emotion=self.emotion_state,
            presence_decision=presence_decision,
        )
