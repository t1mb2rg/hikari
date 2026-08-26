from __future__ import annotations

from collections.abc import Callable, Iterable

from .models import Event
from .sensor import Sensor


class SensorRunner:
    """Poll interchangeable sensors and collect their normalized events."""

    def __init__(
        self,
        sensors: Iterable[Sensor],
        on_event: Callable[[Event], None] | None = None,
    ) -> None:
        self.sensors = list(sensors)
        self.on_event = on_event

    def poll_once(self) -> list[Event]:
        events: list[Event] = []

        for sensor in self.sensors:
            observed = sensor.poll()
            events.extend(observed)

            if self.on_event is not None:
                for event in observed:
                    self.on_event(event)

        return events
