from __future__ import annotations

from attention import AttentionPolicy
from brain import Feedback
from core.presence import PresencePipeline
from events import Event
from learning import LEARNED_CONTEXT_KEY, LearningAssimilationPolicy
from memory import MemoryKind, MemoryStore


class CapturingReasoner:
    def __init__(self) -> None:
        self.calls = 0
        self.events: list[Event] = []

    def reason(self, event, decision):
        self.calls += 1
        self.events.append(event)
        return Feedback(
            text="captured",
            event_type=event.event_type,
            importance=decision.importance,
        )


class CapturingSink:
    def __init__(self) -> None:
        self.feedback: list[Feedback] = []

    def deliver(self, feedback: Feedback) -> None:
        self.feedback.append(feedback)


def test_assimilation_selects_only_confident_learned_memory(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    store.remember_memory(MemoryKind.EPISODIC, "raw experience", confidence=1.0)
    old_user_model = store.remember_memory(
        MemoryKind.USER_MODEL,
        "older accepted user understanding",
        confidence=0.90,
    )
    store.remember_memory(
        MemoryKind.SEMANTIC,
        "weak semantic guess",
        confidence=0.60,
    )
    new_semantic = store.remember_memory(
        MemoryKind.SEMANTIC,
        "new accepted semantic learning",
        confidence=0.85,
    )

    policy = LearningAssimilationPolicy(min_confidence=0.75, limit=2)
    recalled = policy.recall(store)

    assert [memory.id for memory in recalled] == [new_semantic.id, old_user_model.id]
    assert all(memory.kind in {MemoryKind.USER_MODEL, MemoryKind.SEMANTIC} for memory in recalled)
    assert all(memory.confidence >= 0.75 for memory in recalled)


def test_presence_assimilates_learning_only_on_reasoning_path(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    learned = store.remember_memory(
        MemoryKind.USER_MODEL,
        "The user prefers short milestone loops.",
        confidence=0.95,
    )
    reasoner = CapturingReasoner()
    sink = CapturingSink()
    pipeline = PresencePipeline(
        memory=store,
        attention=AttentionPolicy(
            threshold=0.7,
            event_importance={"milestone.passed": 0.9, "ambient.tick": 0.1},
        ),
        reasoner=reasoner,
        feedback_sink=sink,
        assimilation_policy=LearningAssimilationPolicy(),
    )

    original_context = {"source": "test"}
    result = pipeline.handle(
        Event(
            event_type="milestone.passed",
            source="tests",
            content="A milestone passed.",
            context=original_context,
        )
    )

    assert reasoner.calls == 1
    assert result.remembered.context == original_context
    assert LEARNED_CONTEXT_KEY not in result.remembered.context
    reasoning_context = reasoner.events[0].context
    assert reasoning_context[LEARNED_CONTEXT_KEY][0]["id"] == learned.id
    assert reasoning_context[LEARNED_CONTEXT_KEY][0]["content"] == learned.content

    quiet = pipeline.handle(
        Event(
            event_type="ambient.tick",
            source="tests",
            content="Nothing important changed.",
        )
    )

    assert quiet.feedback is None
    assert reasoner.calls == 1


def test_presence_omits_learning_context_when_nothing_is_eligible(tmp_path):
    store = MemoryStore(tmp_path / "memory.db")
    store.remember_memory(
        MemoryKind.USER_MODEL,
        "low confidence hypothesis",
        confidence=0.4,
    )
    store.remember_memory(
        MemoryKind.EPISODIC,
        "high confidence raw experience",
        confidence=1.0,
    )
    reasoner = CapturingReasoner()
    pipeline = PresencePipeline(
        memory=store,
        attention=AttentionPolicy(
            threshold=0.7,
            event_importance={"milestone.passed": 0.9},
        ),
        reasoner=reasoner,
        feedback_sink=CapturingSink(),
        assimilation_policy=LearningAssimilationPolicy(min_confidence=0.75),
    )

    pipeline.handle(
        Event(
            event_type="milestone.passed",
            source="tests",
            content="A milestone passed.",
        )
    )

    assert reasoner.calls == 1
    assert LEARNED_CONTEXT_KEY not in reasoner.events[0].context
