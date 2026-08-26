from awareness import (
    ContextCollector,
    HostContextProvider,
    TimeContextProvider,
)
from awareness.context import HIKARI_CONTEXT_KEY
from attention import AttentionPolicy
from core.presence import PresencePipeline
from events import Event
from memory.store import MemoryStore


class FakeContextProvider:
    def __init__(self, name: str, values: dict):
        self.name = name
        self.values = values

    def capture(self) -> dict:
        return dict(self.values)


class ExplodingReasoner:
    def reason(self, event, decision):
        raise AssertionError("Reasoner must not run for a quiet event")


class NullSink:
    def deliver(self, feedback) -> None:
        raise AssertionError("Quiet event must not produce feedback")


def test_context_collector_namespaces_multiple_providers():
    collector = ContextCollector(
        [
            FakeContextProvider("activity", {"state": "coding"}),
            FakeContextProvider("schedule", {"busy": True}),
        ]
    )

    snapshot = collector.capture().as_dict()

    assert snapshot["captured_at"]
    assert snapshot["providers"] == {
        "activity": {"state": "coding"},
        "schedule": {"busy": True},
    }


def test_context_collector_enriches_event_without_losing_sensor_context():
    collector = ContextCollector(
        [FakeContextProvider("activity", {"state": "coding"})]
    )
    event = Event(
        event_type="test.change",
        source="fake",
        content="Something changed",
        context={"sensor_value": 42},
    )

    enriched = collector.enrich(event)

    assert event.context == {"sensor_value": 42}
    assert enriched.context["sensor_value"] == 42
    assert enriched.context[HIKARI_CONTEXT_KEY]["providers"]["activity"] == {
        "state": "coding"
    }


def test_context_provider_names_must_be_unique():
    try:
        ContextCollector(
            [
                FakeContextProvider("same", {"value": 1}),
                FakeContextProvider("same", {"value": 2}),
            ]
        )
    except ValueError:
        pass
    else:
        raise AssertionError("Duplicate context provider names should be rejected")


def test_presence_persists_ambient_context_before_silence(tmp_path):
    memory = MemoryStore(tmp_path / "memory.db")
    pipeline = PresencePipeline(
        memory=memory,
        attention=AttentionPolicy(
            threshold=0.7,
            event_importance={"test.quiet": 0.2},
        ),
        reasoner=ExplodingReasoner(),
        feedback_sink=NullSink(),
        context_collector=ContextCollector(
            [FakeContextProvider("activity", {"state": "coding"})]
        ),
    )

    result = pipeline.handle(
        Event(
            event_type="test.quiet",
            source="fake",
            content="Background change",
        )
    )

    remembered = memory.get_event(result.remembered.id)
    assert remembered is not None
    assert remembered.context[HIKARI_CONTEXT_KEY]["providers"]["activity"] == {
        "state": "coding"
    }
    assert result.feedback is None


def test_builtin_context_providers_are_cheap_structured_sources():
    time_context = TimeContextProvider().capture()
    host_context = HostContextProvider().capture()

    assert isinstance(time_context["hour"], int)
    assert time_context["local_iso"]
    assert "hostname" in host_context
    assert "system" in host_context
