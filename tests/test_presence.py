from attention import AttentionPolicy
from brain import Feedback, SimpleReasoner
from core.presence import PresencePipeline
from events import Event
from events.runner import SensorRunner
from memory import MemoryCandidatePolicy, MemoryKind
from memory.store import MemoryStore


class CollectingSink:
    def __init__(self):
        self.feedback: list[Feedback] = []

    def deliver(self, feedback: Feedback) -> None:
        self.feedback.append(feedback)


class ExplodingReasoner:
    def reason(self, event, decision):
        raise AssertionError("Reasoner must not run for a quiet event")


class OneShotSensor:
    name = "fake"

    def __init__(self, event: Event):
        self.event = event
        self.emitted = False

    def poll(self) -> list[Event]:
        if self.emitted:
            return []
        self.emitted = True
        return [self.event]


def test_quiet_event_is_remembered_without_reasoning(tmp_path):
    memory = MemoryStore(tmp_path / "memory.db")
    sink = CollectingSink()
    pipeline = PresencePipeline(
        memory=memory,
        attention=AttentionPolicy(
            threshold=0.7,
            event_importance={"test.quiet": 0.2},
        ),
        reasoner=ExplodingReasoner(),
        feedback_sink=sink,
    )

    result = pipeline.handle(
        Event(
            event_type="test.quiet",
            source="fake",
            content="Background change",
        )
    )

    assert result.feedback is None
    assert result.candidate is None
    assert result.decision.should_intervene is False
    assert result.remembered.importance == 0.2
    assert memory.get_event(result.remembered.id) == result.remembered
    assert sink.feedback == []


def test_noteworthy_event_completes_feedback_loop(tmp_path):
    memory = MemoryStore(tmp_path / "memory.db")
    sink = CollectingSink()
    pipeline = PresencePipeline(
        memory=memory,
        attention=AttentionPolicy(
            threshold=0.7,
            event_importance={"test.important": 0.9},
        ),
        reasoner=SimpleReasoner(),
        feedback_sink=sink,
    )

    result = pipeline.handle(
        Event(
            event_type="test.important",
            source="fake",
            content="A meaningful change happened",
        )
    )

    assert result.feedback is not None
    assert result.candidate is None
    assert result.remembered.importance == 0.9
    assert sink.feedback == [result.feedback]
    assert "A meaningful change happened" in result.feedback.text


def test_memory_candidate_can_be_proposed_while_presence_stays_silent(tmp_path):
    memory = MemoryStore(tmp_path / "memory.db")
    sink = CollectingSink()
    pipeline = PresencePipeline(
        memory=memory,
        attention=AttentionPolicy(
            threshold=0.95,
            event_importance={"test.memory": 0.9},
        ),
        reasoner=ExplodingReasoner(),
        feedback_sink=sink,
        candidate_policy=MemoryCandidatePolicy(
            {"test.memory": MemoryKind.EPISODIC},
            min_importance=0.8,
        ),
    )

    result = pipeline.handle(
        Event(
            event_type="test.memory",
            source="fake",
            content="Worth remembering without interrupting",
        )
    )

    assert result.decision.should_intervene is False
    assert result.feedback is None
    assert result.candidate is not None
    assert result.candidate.kind is MemoryKind.EPISODIC
    assert result.candidate.source_event_id == result.remembered.id
    assert result.candidate.salience == 0.9
    assert sink.feedback == []
    assert memory.recent_memories() == []


def test_sensor_runner_can_drive_presence_without_sensor_specific_core_code(tmp_path):
    memory = MemoryStore(tmp_path / "memory.db")
    sink = CollectingSink()
    pipeline = PresencePipeline(
        memory=memory,
        attention=AttentionPolicy(
            threshold=0.5,
            event_importance={"fake.change": 0.8},
        ),
        reasoner=SimpleReasoner(),
        feedback_sink=sink,
    )
    sensor = OneShotSensor(
        Event(
            event_type="fake.change",
            source="fake",
            content="Observed without a user prompt",
        )
    )
    runner = SensorRunner([sensor], on_event=pipeline.handle)

    observed = runner.poll_once()

    assert len(observed) == 1
    assert len(memory.recent_events()) == 1
    assert len(sink.feedback) == 1
    assert "Observed without a user prompt" in sink.feedback[0].text
    assert runner.poll_once() == []
