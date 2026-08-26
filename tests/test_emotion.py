import pytest

from attention import AttentionDecision, AttentionPolicy
from brain import Feedback
from core.presence import PresencePipeline
from events import Event
from memory import MemoryStore
from personality import (
    HIKARI_EMOTION_KEY,
    EmotionPolicy,
    EmotionState,
)


class CollectingSink:
    def __init__(self):
        self.feedback = []

    def deliver(self, feedback):
        self.feedback.append(feedback)


class ExplodingReasoner:
    def reason(self, event, decision):
        raise AssertionError("Reasoner must not run for a quiet event")


class CapturingReasoner:
    def __init__(self):
        self.events = []

    def reason(self, event, decision):
        self.events.append(event)
        return Feedback(
            text="emotion observed",
            event_type=event.event_type,
            importance=decision.importance,
        )


def _state(**overrides):
    levels = {
        "curiosity": 0.4,
        "concern": 0.2,
        "satisfaction": 0.3,
        "frustration": 0.1,
    }
    levels.update(overrides)
    return EmotionState(levels=levels)


def test_emotion_state_rejects_invalid_dimensions_and_values():
    with pytest.raises(ValueError, match="missing emotion dimensions"):
        EmotionState(levels={"curiosity": 0.5})

    with pytest.raises(ValueError, match="between 0.0 and 1.0"):
        _state(frustration=1.2)


def test_emotion_policy_transition_is_deterministic_and_bounded():
    state = _state()
    policy = EmotionPolicy(
        {
            "test.failure": {
                "concern": 0.5,
                "frustration": 2.0,
            }
        },
        baseline=state,
        settle_rate=0.0,
    )
    event = Event(event_type="test.failure", source="test", content="failure")
    decision = AttentionDecision(
        should_intervene=True,
        importance=0.8,
        reason="test",
    )

    first = policy.transition(state, event, decision)
    second = policy.transition(state, event, decision)

    assert first == second
    assert first.levels["concern"] == pytest.approx(0.6)
    assert first.levels["frustration"] == 1.0
    assert first.levels["curiosity"] == 0.4


def test_quiet_event_updates_emotion_without_reasoning_or_history_pollution(tmp_path):
    memory = MemoryStore(tmp_path / "memory.db")
    initial = _state()
    pipeline = PresencePipeline(
        memory=memory,
        attention=AttentionPolicy(
            threshold=0.8,
            event_importance={"test.quiet": 0.4},
        ),
        reasoner=ExplodingReasoner(),
        feedback_sink=CollectingSink(),
        emotion_state=initial,
        emotion_policy=EmotionPolicy(
            {"test.quiet": {"curiosity": 0.25}},
            baseline=initial,
            settle_rate=0.0,
        ),
    )

    result = pipeline.handle(
        Event(event_type="test.quiet", source="test", content="quiet change")
    )

    assert result.feedback is None
    assert result.emotion is not None
    assert result.emotion.levels["curiosity"] == pytest.approx(0.5)
    assert pipeline.current_emotion == result.emotion
    persisted = memory.get_event(result.remembered.id)
    assert HIKARI_EMOTION_KEY not in persisted.context


def test_noteworthy_event_passes_current_emotion_only_to_reasoner(tmp_path):
    memory = MemoryStore(tmp_path / "memory.db")
    initial = _state()
    reasoner = CapturingReasoner()
    pipeline = PresencePipeline(
        memory=memory,
        attention=AttentionPolicy(
            threshold=0.5,
            event_importance={"test.important": 0.9},
        ),
        reasoner=reasoner,
        feedback_sink=CollectingSink(),
        emotion_state=initial,
        emotion_policy=EmotionPolicy(
            {"test.important": {"satisfaction": 0.2}},
            baseline=initial,
            settle_rate=0.0,
        ),
    )

    result = pipeline.handle(
        Event(event_type="test.important", source="test", content="meaningful change")
    )

    assert result.feedback is not None
    assert len(reasoner.events) == 1
    reasoning_context = reasoner.events[0].context
    assert HIKARI_EMOTION_KEY in reasoning_context
    assert reasoning_context[HIKARI_EMOTION_KEY]["levels"]["satisfaction"] == pytest.approx(0.48)

    persisted = memory.get_event(result.remembered.id)
    assert HIKARI_EMOTION_KEY not in persisted.context
