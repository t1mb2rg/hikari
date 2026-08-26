from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
import platform
from typing import Any, Iterable, Protocol, runtime_checkable

from events.models import Event


HIKARI_CONTEXT_KEY = "_hikari_context"


@runtime_checkable
class ContextProvider(Protocol):
    """Cheap ambient-state provider used to contextualize observed events."""

    name: str

    def capture(self) -> dict[str, Any]:
        """Return the provider's current state without deep reasoning."""
        ...


@dataclass(frozen=True)
class ContextSnapshot:
    """A namespaced view of ambient state captured at one point in time."""

    captured_at: datetime
    providers: dict[str, dict[str, Any]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        timestamp = self.captured_at
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)

        return {
            "captured_at": timestamp.astimezone(timezone.utc).isoformat(),
            "providers": self.providers,
        }


class ContextCollector:
    """Collect interchangeable ambient context providers and enrich Events."""

    def __init__(self, providers: Iterable[ContextProvider] = ()) -> None:
        self.providers = list(providers)

        names = [provider.name for provider in self.providers]
        if len(names) != len(set(names)):
            raise ValueError("Context provider names must be unique")

    def capture(self) -> ContextSnapshot:
        values = {
            provider.name: dict(provider.capture())
            for provider in self.providers
        }
        return ContextSnapshot(
            captured_at=datetime.now(timezone.utc),
            providers=values,
        )

    def enrich(self, event: Event) -> Event:
        """Return a new Event containing a captured ambient context snapshot."""
        context = dict(event.context)
        context[HIKARI_CONTEXT_KEY] = self.capture().as_dict()
        return replace(event, context=context)


class TimeContextProvider:
    """Cheap time context from the host running Hikari."""

    name = "time"

    def capture(self) -> dict[str, Any]:
        now = datetime.now().astimezone()
        return {
            "local_iso": now.isoformat(),
            "weekday": now.strftime("%A"),
            "hour": now.hour,
            "utc_offset_seconds": int(now.utcoffset().total_seconds())
            if now.utcoffset() is not None
            else 0,
        }


class HostContextProvider:
    """Minimal device identity context for the node observing the event."""

    name = "host"

    def capture(self) -> dict[str, Any]:
        return {
            "hostname": platform.node(),
            "system": platform.system(),
            "release": platform.release(),
        }
