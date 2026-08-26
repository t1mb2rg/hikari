from datetime import datetime, timedelta, timezone

from attention import AttentionPolicy
from awareness import ContextCollector
from awareness.schedule import ScheduleContextProvider, ScheduleEntry
from brain import SimpleReasoner
from core.presence import PresencePipeline
from events import Event
from memory.store import MemoryStore


NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)


class FakeScheduleSource:
    name = "fake-calendar"

    def __init__(self, entries):
        self.entries = list(entries)

    def list_entries(self, start, end):
        return [
            entry
            for entry in self.entries
            if entry.starts_at <= end
            and (entry.ends_at is None or entry.ends_at > start)
        ]


class NullSink:
    def deliver(self, feedback):
        raise AssertionError("Quiet test event should not produce feedback")


class ExplodingReasoner:
    def reason(self, event, decision):
        raise AssertionError("Quiet test event should not invoke reasoning")


def test_schedule_provider_classifies_current_and_upcoming_items():
    source = FakeScheduleSource(
        [
            ScheduleEntry(
                title="Current meeting",
                starts_at=NOW - timedelta(minutes=30),
                ends_at=NOW + timedelta(minutes=30),
                source="fake-calendar",
            ),
            ScheduleEntry(
                title="Later task",
                starts_at=NOW + timedelta(hours=2),
                ends_at=NOW + timedelta(hours=3),
                source="fake-calendar",
            ),
        ]
    )
    provider = ScheduleContextProvider(
        source,
        lookahead=timedelta(hours=6),
        now_provider=lambda: NOW,
    )

    context = provider.capture()

    assert context["source"] == "fake-calendar"
    assert [item["title"] for item in context["current"]] == ["Current meeting"]
    assert [item["title"] for item in context["upcoming"]] == ["Later task"]


def test_schedule_provider_handles_empty_agenda():
    provider = ScheduleContextProvider(
        FakeScheduleSource([]),
        now_provider=lambda: NOW,
    )

    context = provider.capture()

    assert context["current"] == []
    assert context["upcoming"] == []


def test_presence_persists_schedule_context_without_knowing_calendar_source(tmp_path):
    source = FakeScheduleSource(
        [
            ScheduleEntry(
                title="Upcoming lab session",
                starts_at=NOW + timedelta(hours=1),
                source="fake-calendar",
            )
        ]
    )
    schedule = ScheduleContextProvider(
        source,
        now_provider=lambda: NOW,
    )
    memory = MemoryStore(tmp_path / "memory.db")
    pipeline = PresencePipeline(
        memory=memory,
        attention=AttentionPolicy(
            threshold=0.7,
            event_importance={"test.context": 0.1},
        ),
        reasoner=ExplodingReasoner(),
        feedback_sink=NullSink(),
        context_collector=ContextCollector([schedule]),
    )

    result = pipeline.handle(
        Event(
            event_type="test.context",
            source="fake",
            content="Context capture test",
            occurred_at=NOW,
        )
    )

    saved_context = result.remembered.context["_hikari_context"]["providers"]["schedule"]
    assert saved_context["source"] == "fake-calendar"
    assert saved_context["upcoming"][0]["title"] == "Upcoming lab session"
