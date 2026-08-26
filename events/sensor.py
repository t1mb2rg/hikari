from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import Event


@runtime_checkable
class Sensor(Protocol):
    """Contract implemented by every Hikari environmental sensor.

    Sensors only know how to observe one source and normalize observations into
    ``Event`` objects. They do not know how events are stored, judged, reasoned
    about, or surfaced to the user.
    """

    name: str

    def poll(self) -> list[Event]:
        """Return newly observed events since the previous poll."""
        ...
