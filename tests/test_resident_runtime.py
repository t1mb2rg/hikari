from __future__ import annotations

from dataclasses import dataclass

import pytest

from brain.model_reasoner import ModelCognitionError
from core.runtime import CognitionFailure, ResidentPresenceRuntime, SensorFailure
from events import Event


@dataclass
class _Sensor:
    name: str
    batches: list[object]
    polls: int = 0

    def poll(self) -> list[Event]:
        self.polls += 1
        if not self.batches:
            return []
        batch = self.batches.pop(0)
        if isinstance(batch, Exception):
            raise batch
        return list(batch)


class _RecordingPipeline:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def handle(self, event: Event):
        self.events.append(event)
        return f"handled:{event.content}"


class _FailingPipeline(_RecordingPipeline):
    def handle(self, event: Event):
        super().handle(event)
        raise RuntimeError("presence failed")


class _CognitionFailingPipeline(_RecordingPipeline):
    def handle(self, event: Event):
        self.events.append(event)
        if event.content == "provider-down":
            raise ModelCognitionError(
                "model provider is temporarily unavailable",
                provider_error_type="URLError",
            )
        return f"handled:{event.content}"


def _event(content: str, *, source: str = "test") -> Event:
    return Event(event_type="test.event", source=source, content=content)


def test_quiet_cycle_does_not_call_presence_pipeline():
    sensor = _Sensor("quiet", [[]])
    pipeline = _RecordingPipeline()
    runtime = ResidentPresenceRuntime([sensor], pipeline)  # type: ignore[arg-type]

    result = runtime.cycle_once()

    assert result.events == ()
    assert result.interventions == ()
    assert result.sensor_failures == ()
    assert result.cognition_failures == ()
    assert pipeline.events == []


def test_cycle_processes_multiple_sensor_events_in_order():
    first = _event("first", source="alpha")
    second = _event("second", source="alpha")
    third = _event("third", source="beta")
    pipeline = _RecordingPipeline()
    runtime = ResidentPresenceRuntime(
        [
            _Sensor("alpha", [[first, second]]),
            _Sensor("beta", [[third]]),
        ],
        pipeline,  # type: ignore[arg-type]
    )

    result = runtime.cycle_once()

    assert result.events == (first, second, third)
    assert result.interventions == (
        "handled:first",
        "handled:second",
        "handled:third",
    )
    assert result.cognition_failures == ()
    assert pipeline.events == [first, second, third]


def test_failed_sensor_is_recorded_without_blocking_healthy_sensor():
    healthy_event = _event("healthy", source="healthy")
    pipeline = _RecordingPipeline()
    reported: list[SensorFailure] = []
    runtime = ResidentPresenceRuntime(
        [
            _Sensor("broken", [RuntimeError("device disappeared")]),
            _Sensor("healthy", [[healthy_event]]),
        ],
        pipeline,  # type: ignore[arg-type]
        on_sensor_failure=reported.append,
    )

    result = runtime.cycle_once()

    assert result.events == (healthy_event,)
    assert pipeline.events == [healthy_event]
    assert len(result.sensor_failures) == 1
    failure = result.sensor_failures[0]
    assert failure.sensor_name == "broken"
    assert failure.error_type == "RuntimeError"
    assert failure.message == "device disappeared"
    assert reported == [failure]


def test_expected_model_cognition_failure_is_bounded_and_later_event_continues():
    failed = _event("provider-down")
    healthy = _event("provider-back")
    reported: list[CognitionFailure] = []
    pipeline = _CognitionFailingPipeline()
    runtime = ResidentPresenceRuntime(
        [_Sensor("sensor", [[failed, healthy]])],
        pipeline,  # type: ignore[arg-type]
        on_cognition_failure=reported.append,
    )

    result = runtime.cycle_once()

    assert result.events == (failed, healthy)
    assert result.interventions == ("handled:provider-back",)
    assert len(result.cognition_failures) == 1
    failure = result.cognition_failures[0]
    assert failure.event_type == "test.event"
    assert failure.source == "test"
    assert failure.error_type == "ModelCognitionError"
    assert failure.provider_error_type == "URLError"
    assert reported == [failure]
    assert pipeline.events == [failed, healthy]


def test_presence_pipeline_programming_failure_is_not_silently_swallowed():
    event = _event("important")
    runtime = ResidentPresenceRuntime(
        [_Sensor("sensor", [[event]])],
        _FailingPipeline(),  # type: ignore[arg-type]
    )

    with pytest.raises(RuntimeError, match="presence failed"):
        runtime.cycle_once()


def test_run_forever_starts_cycles_sleeps_and_stops_cleanly(capsys):
    sensor = _Sensor("quiet", [[], []])
    pipeline = _RecordingPipeline()
    sleeps: list[float] = []
    runtime: ResidentPresenceRuntime

    def sleeper(seconds: float) -> None:
        sleeps.append(seconds)
        runtime.stop()

    runtime = ResidentPresenceRuntime(
        [sensor],
        pipeline,  # type: ignore[arg-type]
        poll_interval=0.25,
        sleeper=sleeper,
    )

    runtime.run_forever()

    assert runtime.running is False
    assert runtime.identity is not None
    assert runtime.identity.name == "Hikari"
    assert sensor.polls == 1
    assert sleeps == [0.25]
    output = capsys.readouterr().out
    assert "Hikari 在这里。" in output
    assert "Hikari 休息了。" in output


def test_resident_runtime_rejects_non_positive_interval():
    with pytest.raises(ValueError, match="poll_interval"):
        ResidentPresenceRuntime([], _RecordingPipeline(), poll_interval=0)  # type: ignore[arg-type]


def test_resident_runtime_rejects_non_sensor_objects():
    class NotASensor:
        pass

    with pytest.raises(TypeError, match="Sensor implementations"):
        ResidentPresenceRuntime([NotASensor()], _RecordingPipeline())  # type: ignore[list-item,arg-type]
