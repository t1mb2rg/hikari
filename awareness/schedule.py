from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Protocol, runtime_checkable


@dataclass(frozen=True)
class ScheduleEntry:
    """Normalized schedule item independent from any calendar vendor."""

    title: str
    starts_at: datetime
    ends_at: datetime | None = None
    source: str = "unknown"
    location: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "title": self.title,
            "starts_at": _as_utc_iso(self.starts_at),
            "ends_at": _as_utc_iso(self.ends_at) if self.ends_at is not None else None,
            "source": self.source,
            "location": self.location,
        }


@runtime_checkable
class ScheduleSource(Protocol):
    """Adapter contract for calendars, agendas, reminders, or other schedule stores."""

    name: str

    def list_entries(self, start: datetime, end: datetime) -> Iterable[ScheduleEntry]:
        ...


class ScheduleContextProvider:
    """Expose current and upcoming schedule items as cheap ambient context."""

    name = "schedule"

    def __init__(
        self,
        source: ScheduleSource,
        *,
        lookahead: timedelta = timedelta(hours=24),
        now_provider=lambda: datetime.now(timezone.utc),
    ) -> None:
        if lookahead.total_seconds() < 0:
            raise ValueError("lookahead must be non-negative")

        self.source = source
        self.lookahead = lookahead
        self.now_provider = now_provider

    def capture(self) -> dict[str, object]:
        now = _ensure_aware(self.now_provider())
        end = now + self.lookahead
        entries = sorted(
            self.source.list_entries(now, end),
            key=lambda item: _ensure_aware(item.starts_at),
        )

        current: list[ScheduleEntry] = []
        upcoming: list[ScheduleEntry] = []

        for entry in entries:
            starts_at = _ensure_aware(entry.starts_at)
            ends_at = _ensure_aware(entry.ends_at) if entry.ends_at is not None else None

            if starts_at <= now and (ends_at is None or ends_at > now):
                current.append(entry)
            elif starts_at > now:
                upcoming.append(entry)

        return {
            "source": self.source.name,
            "lookahead_seconds": self.lookahead.total_seconds(),
            "current": [entry.as_dict() for entry in current],
            "upcoming": [entry.as_dict() for entry in upcoming],
        }


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _as_utc_iso(value: datetime) -> str:
    return _ensure_aware(value).astimezone(timezone.utc).isoformat()
