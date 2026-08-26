from attention import AttentionPolicy
from brain import Feedback
from core.presence import PresencePipeline
from events import Event
from memory import MemoryStore
from personality import HIKARI_PERSONALITY_KEY, load_personality, personality_as_context


class CollectingSink:
    def __init__(self) -> None:
        self.feedback: list[Feedback] = []

    def deliver(self, feedback: Feedback) -> None:
        self.feedback.append(feedback)


class CapturingReasoner:
    def __init__(self) -> None:
        self.event: Event | None = None

    def reason(self, event, decision):
        self.event = event
        return Feedback(
            text="captured",
            event_type=event.event_type,
            importance=decision.importance,
        )


class ExplodingReasoner:
    def reason(self, event, decision):
        raise AssertionError("Reasoner must not run for a quiet event")


def test_personality_profile_serializes_for_reasoning():
    profile = load_personality()

    assert personality_as_context(profile) == profile.describe()


def test_presence_passes_personality_only_to_reasoner_context(tmp_path):
    profile = load_personality()
    memory = MemoryStore(tmp_path / "memory.db")
    reasoner = CapturingReasoner()
    sink = CollectingSink()
    pipeline = PresencePipeline(
        memory=memory,
        attention=AttentionPolicy(
            threshold=0.5,
            event_importance={"test.personality": 0.9},
        ),
        reasoner=reasoner,
        feedback_sink=sink,
        personality_profile=profile,
    )

    result = pipeline.handle(
        Event(
            event_type="test.personality",
            source="fake",
            content="Personality should reach cognition",
            context={"sensor_fact": "preserve me"},
        )
    )

    assert result.feedback is not None
    assert reasoner.event is not None
    assert reasoner.event.context[HIKARI_PERSONALITY_KEY] == profile.describe()
    assert reasoner.event.context["sensor_fact"] == "preserve me"

    persisted = memory.get_event(result.remembered.id)
    assert persisted is not None
    assert persisted.context == {"sensor_fact": "preserve me"}
    assert HIKARI_PERSONALITY_KEY not in persisted.context


def test_quiet_event_does_not_enter_personality_reasoning_path(tmp_path):
    profile = load_personality()
    memory = MemoryStore(tmp_path / "memory.db")
    pipeline = PresencePipeline(
        memory=memory,
        attention=AttentionPolicy(
            threshold=0.9,
            event_importance={"test.quiet": 0.2},
        ),
        reasoner=ExplodingReasoner(),
        feedback_sink=CollectingSink(),
        personality_profile=profile,
    )

    result = pipeline.handle(
        Event(
            event_type="test.quiet",
            source="fake",
            content="No need to reason",
        )
    )

    assert result.feedback is None
    persisted = memory.get_event(result.remembered.id)
    assert persisted is not None
    assert HIKARI_PERSONALITY_KEY not in persisted.context
