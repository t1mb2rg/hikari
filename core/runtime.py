"""Hikari Core runtime lifecycles.

`HikariRuntime` preserves the minimal M0 liveness contract. M6 adds
`ResidentPresenceRuntime`, which owns the continuous sensor -> Event ->
Presence loop without teaching sensors anything about cognition.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

from brain.model_reasoner import ModelCognitionError
from core.identity import HikariIdentity, load_identity
from core.presence import InterventionResult, PresencePipeline
from events.models import Event
from events.sensor import Sensor


def heartbeat() -> str:
    """Return the minimal liveness signal used by early M0 checks."""
    return "Hikari 醒着。"


@dataclass
class HikariRuntime:
    """Minimal lifecycle runtime for Hikari Core."""

    heartbeat_interval: float = 5.0
    sleeper: Callable[[float], None] = time.sleep
    identity: HikariIdentity | None = field(default=None, init=False)
    running: bool = field(default=False, init=False)

    def initialize(self) -> HikariIdentity:
        """Load the stable identity required by the runtime."""
        if self.identity is None:
            self.identity = load_identity()
        return self.identity

    def start(self) -> str:
        """Initialize Hikari and enter the running state."""
        identity = self.initialize()
        self.running = True
        return f"{identity.name} 醒着。"

    def stop(self) -> None:
        """Leave the running state without discarding loaded identity."""
        self.running = False

    def run_forever(self) -> None:
        """Remain alive until stopped or interrupted by the host process."""
        print(self.start())
        try:
            while self.running:
                self.sleeper(self.heartbeat_interval)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()
            print("Hikari 休息了。")


@dataclass(frozen=True)
class SensorFailure:
    """Bounded evidence that one sensor failed during a resident cycle."""

    sensor_name: str
    error_type: str
    message: str


@dataclass(frozen=True)
class CognitionFailure:
    """Bounded evidence that external model cognition failed for one event."""

    event_type: str
    source: str
    error_type: str
    provider_error_type: str | None


@dataclass(frozen=True)
class PresenceCycleResult:
    """Observable result of one cheap resident polling cycle."""

    events: tuple[Event, ...]
    interventions: tuple[InterventionResult, ...]
    sensor_failures: tuple[SensorFailure, ...]
    cognition_failures: tuple[CognitionFailure, ...]


@dataclass
class ResidentPresenceRuntime:
    """Continuously connect environmental sensors to Hikari's PresencePipeline.

    Sensors are polled sequentially and each normalized Event is handed to the
    existing PresencePipeline in order. Sensor failures are isolated. Expected
    external-model failures raised as `ModelCognitionError` are also isolated so
    a temporary provider outage cannot kill Conversation, QQ supervision, or the
    resident shell. Other PresencePipeline exceptions still fail loud because
    they may represent state corruption or a programming invariant violation.
    """

    sensors: Iterable[Sensor]
    pipeline: PresencePipeline
    poll_interval: float = 2.0
    sleeper: Callable[[float], None] = time.sleep
    on_sensor_failure: Callable[[SensorFailure], None] | None = None
    on_cognition_failure: Callable[[CognitionFailure], None] | None = None
    identity: HikariIdentity | None = field(default=None, init=False)
    running: bool = field(default=False, init=False)
    _sensors: list[Sensor] = field(default_factory=list, init=False, repr=False)

    def __post_init__(self) -> None:
        interval = float(self.poll_interval)
        if interval <= 0:
            raise ValueError("poll_interval must be > 0")
        self.poll_interval = interval
        self._sensors = list(self.sensors)
        for sensor in self._sensors:
            if not isinstance(sensor, Sensor):
                raise TypeError("ResidentPresenceRuntime accepts only Sensor implementations")

    def initialize(self) -> HikariIdentity:
        """Load stable identity without starting observation yet."""
        if self.identity is None:
            self.identity = load_identity()
        return self.identity

    def start(self) -> str:
        """Enter the resident running state."""
        identity = self.initialize()
        self.running = True
        return f"{identity.name} 在这里。"

    def stop(self) -> None:
        """Stop future resident cycles without discarding identity."""
        self.running = False

    def cycle_once(self) -> PresenceCycleResult:
        """Poll every sensor once and process newly observed Events in order.

        A quiet cycle does not call PresencePipeline at all. This keeps idle
        operation cheap and guarantees that no Reasoner/model work can happen
        merely because the resident loop woke up.
        """

        events: list[Event] = []
        interventions: list[InterventionResult] = []
        sensor_failures: list[SensorFailure] = []
        cognition_failures: list[CognitionFailure] = []

        for sensor in self._sensors:
            try:
                observed = sensor.poll()
            except Exception as exc:
                failure = SensorFailure(
                    sensor_name=sensor.name,
                    error_type=type(exc).__name__,
                    message=str(exc),
                )
                sensor_failures.append(failure)
                if self.on_sensor_failure is not None:
                    self.on_sensor_failure(failure)
                continue

            for event in observed:
                if not isinstance(event, Event):
                    raise TypeError(
                        f"sensor {sensor.name!r} returned non-Event value: {type(event).__name__}"
                    )
                events.append(event)
                try:
                    intervention = self.pipeline.handle(event)
                except ModelCognitionError as exc:
                    failure = CognitionFailure(
                        event_type=event.event_type,
                        source=event.source,
                        error_type=type(exc).__name__,
                        provider_error_type=exc.provider_error_type,
                    )
                    cognition_failures.append(failure)
                    if self.on_cognition_failure is not None:
                        self.on_cognition_failure(failure)
                    else:
                        provider = failure.provider_error_type or "unknown"
                        print(
                            "Hikari Presence cognition unavailable: "
                            f"event={failure.event_type}, provider_error={provider}",
                            flush=True,
                        )
                    continue
                interventions.append(intervention)

        return PresenceCycleResult(
            events=tuple(events),
            interventions=tuple(interventions),
            sensor_failures=tuple(sensor_failures),
            cognition_failures=tuple(cognition_failures),
        )

    def run_forever(self) -> None:
        """Stay resident until `stop()` or Ctrl+C.

        The first cycle runs immediately. This lets stateful sensors establish
        their own baseline without an example-specific warm-up loop.
        """

        print(self.start())
        try:
            while self.running:
                self.cycle_once()
                if self.running:
                    self.sleeper(self.poll_interval)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()
            print("Hikari 休息了。")


def main() -> None:
    HikariRuntime().run_forever()


if __name__ == "__main__":
    main()
