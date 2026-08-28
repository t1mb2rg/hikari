"""Hikari Core runtime lifecycles.

`HikariRuntime` preserves the minimal M0 liveness contract. M6 adds
`ResidentPresenceRuntime`, which owns the continuous sensor -> Event ->
Presence loop without teaching sensors anything about cognition.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

from core.identity import HikariIdentity, load_identity
from core.presence import InterventionResult, PresencePipeline
from events.models import Event
from events.sensor import Sensor


def heartbeat() -> str:
    """Return the minimal liveness signal used by early M0 checks."""
    return "Hikari is awake."


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
        return f"{identity.name} is awake."

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
            print("Hikari is resting.")


@dataclass(frozen=True)
class SensorFailure:
    """Bounded evidence that one sensor failed during a resident cycle."""

    sensor_name: str
    error_type: str
    message: str


@dataclass(frozen=True)
class PresenceCycleResult:
    """Observable result of one cheap resident polling cycle."""

    events: tuple[Event, ...]
    interventions: tuple[InterventionResult, ...]
    sensor_failures: tuple[SensorFailure, ...]


@dataclass
class ResidentPresenceRuntime:
    """Continuously connect environmental sensors to Hikari's PresencePipeline.

    M6-01 deliberately stays synchronous. Sensors are polled sequentially and
    each normalized Event is handed to the existing PresencePipeline in order.
    A sensor polling failure is isolated and reported so one flaky input source
    cannot stop other sensors or later cycles. Core PresencePipeline failures
    are intentionally not swallowed: memory/cognition failures should fail loud
    rather than being mistaken for an ordinary sensor outage.
    """

    sensors: Iterable[Sensor]
    pipeline: PresencePipeline
    poll_interval: float = 2.0
    sleeper: Callable[[float], None] = time.sleep
    on_sensor_failure: Callable[[SensorFailure], None] | None = None
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
        return f"{identity.name} is present."

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
        failures: list[SensorFailure] = []

        for sensor in self._sensors:
            try:
                observed = sensor.poll()
            except Exception as exc:
                failure = SensorFailure(
                    sensor_name=sensor.name,
                    error_type=type(exc).__name__,
                    message=str(exc),
                )
                failures.append(failure)
                if self.on_sensor_failure is not None:
                    self.on_sensor_failure(failure)
                continue

            for event in observed:
                if not isinstance(event, Event):
                    raise TypeError(
                        f"sensor {sensor.name!r} returned non-Event value: {type(event).__name__}"
                    )
                events.append(event)
                interventions.append(self.pipeline.handle(event))

        return PresenceCycleResult(
            events=tuple(events),
            interventions=tuple(interventions),
            sensor_failures=tuple(failures),
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
            print("Hikari is resting.")


def main() -> None:
    HikariRuntime().run_forever()


if __name__ == "__main__":
    main()
