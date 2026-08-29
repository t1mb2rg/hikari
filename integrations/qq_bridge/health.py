from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Callable


@dataclass(frozen=True)
class LinkHealthSnapshot:
    connected: bool
    bot_self_id: str | None
    connected_seconds: float | None
    last_event_seconds_ago: float | None
    last_probe_ok: bool | None
    last_probe_seconds_ago: float | None
    timeout_seconds: float
    healthy: bool


class OneBotLinkHealth:
    """Track transport liveness without confusing a quiet QQ session with failure."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 300.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be > 0")
        self.timeout_seconds = float(timeout_seconds)
        self._clock = clock
        self._connected = False
        self._bot_self_id: str | None = None
        self._connected_at: float | None = None
        self._last_event_at: float | None = None
        self._last_probe_at: float | None = None
        self._last_probe_ok: bool | None = None

    def mark_connected(self, bot_self_id: str | int) -> None:
        now = self._clock()
        self._connected = True
        self._bot_self_id = str(bot_self_id)
        self._connected_at = now
        self._last_event_at = now
        self._last_probe_at = None
        self._last_probe_ok = None

    def mark_disconnected(self) -> None:
        self._connected = False
        self._bot_self_id = None
        self._connected_at = None
        self._last_probe_ok = False

    def mark_event(self) -> None:
        if self._connected:
            self._last_event_at = self._clock()

    def mark_probe(self, ok: bool) -> None:
        self._last_probe_at = self._clock()
        self._last_probe_ok = bool(ok)
        if ok and self._connected:
            self._last_event_at = self._last_probe_at

    def needs_probe(self) -> bool:
        if not self._connected:
            return False
        now = self._clock()
        reference = self._last_event_at or self._connected_at
        if reference is None:
            return True
        return now - reference >= self.timeout_seconds

    def snapshot(self) -> LinkHealthSnapshot:
        now = self._clock()
        connected_seconds = (
            None if self._connected_at is None else max(0.0, now - self._connected_at)
        )
        last_event_seconds_ago = (
            None if self._last_event_at is None else max(0.0, now - self._last_event_at)
        )
        last_probe_seconds_ago = (
            None if self._last_probe_at is None else max(0.0, now - self._last_probe_at)
        )

        if not self._connected:
            healthy = False
        elif last_event_seconds_ago is None:
            healthy = False
        elif last_event_seconds_ago < self.timeout_seconds:
            healthy = True
        else:
            healthy = bool(
                self._last_probe_ok
                and last_probe_seconds_ago is not None
                and last_probe_seconds_ago < self.timeout_seconds
            )

        return LinkHealthSnapshot(
            connected=self._connected,
            bot_self_id=self._bot_self_id,
            connected_seconds=connected_seconds,
            last_event_seconds_ago=last_event_seconds_ago,
            last_probe_ok=self._last_probe_ok,
            last_probe_seconds_ago=last_probe_seconds_ago,
            timeout_seconds=self.timeout_seconds,
            healthy=healthy,
        )
