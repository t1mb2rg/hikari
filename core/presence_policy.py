from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, time, timezone
import hashlib
from pathlib import Path
import sqlite3
from typing import Any, Callable

from attention.policy import AttentionDecision
from awareness.context import HIKARI_CONTEXT_KEY, ContextSnapshot
from awareness.user_state import UserState, UserStateInferer
from events.models import Event


VALID_PRESENCE_CHANNELS = frozenset({"windows", "qq"})


def _parse_bool(values: Mapping[str, str], name: str, *, default: bool) -> bool:
    raw = values.get(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _parse_nonnegative(values: Mapping[str, str], name: str, *, default: float) -> float:
    raw = values.get(name)
    if raw is None or not raw.strip():
        return float(default)
    try:
        value = float(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if value < 0:
        raise ValueError(f"{name} must be >= 0")
    return value


def _parse_unit_interval(values: Mapping[str, str], name: str, *, default: float) -> float:
    value = _parse_nonnegative(values, name, default=default)
    if value > 1:
        raise ValueError(f"{name} must be between 0 and 1")
    return value


def _parse_clock(value: str, *, name: str) -> time:
    text = value.strip()
    pieces = text.split(":")
    if len(pieces) != 2:
        raise ValueError(f"{name} must use HH:MM")
    try:
        hour = int(pieces[0])
        minute = int(pieces[1])
    except ValueError as exc:
        raise ValueError(f"{name} must use HH:MM") from exc
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError(f"{name} must be a valid 24-hour time")
    return time(hour=hour, minute=minute)


def _clock_minutes(value: time) -> int:
    return value.hour * 60 + value.minute


@dataclass(frozen=True)
class PresencePolicyConfig:
    """Deterministic, runtime-owned interruption policy configuration."""

    channel: str = "windows"
    quiet_hours_enabled: bool = False
    quiet_start: time = time(23, 0)
    quiet_end: time = time(7, 0)
    cooldown_seconds: float = 300.0
    duplicate_window_seconds: float = 3600.0
    urgent_threshold: float = 0.95
    busy_foreground_patterns: tuple[str, ...] = ()
    suppress_active_schedule: bool = True

    def __post_init__(self) -> None:
        channel = self.channel.strip().lower()
        if channel not in VALID_PRESENCE_CHANNELS:
            raise ValueError("presence channel must be 'windows' or 'qq'")
        if self.cooldown_seconds < 0:
            raise ValueError("cooldown_seconds must be >= 0")
        if self.duplicate_window_seconds < 0:
            raise ValueError("duplicate_window_seconds must be >= 0")
        if not 0 <= self.urgent_threshold <= 1:
            raise ValueError("urgent_threshold must be between 0 and 1")
        patterns = tuple(
            pattern.strip().casefold()
            for pattern in self.busy_foreground_patterns
            if pattern.strip()
        )
        object.__setattr__(self, "channel", channel)
        object.__setattr__(self, "cooldown_seconds", float(self.cooldown_seconds))
        object.__setattr__(
            self,
            "duplicate_window_seconds",
            float(self.duplicate_window_seconds),
        )
        object.__setattr__(self, "urgent_threshold", float(self.urgent_threshold))
        object.__setattr__(self, "busy_foreground_patterns", patterns)

    @classmethod
    def from_mapping(
        cls,
        values: Mapping[str, str],
        *,
        default_channel: str = "windows",
    ) -> "PresencePolicyConfig":
        channel = values.get("HIKARI_PRESENCE_CHANNEL", default_channel).strip().lower()
        start = _parse_clock(
            values.get("HIKARI_PRESENCE_QUIET_START", "23:00"),
            name="HIKARI_PRESENCE_QUIET_START",
        )
        end = _parse_clock(
            values.get("HIKARI_PRESENCE_QUIET_END", "07:00"),
            name="HIKARI_PRESENCE_QUIET_END",
        )
        raw_patterns = values.get("HIKARI_PRESENCE_BUSY_FOREGROUND_PATTERNS", "")
        patterns = tuple(part for part in raw_patterns.split(",") if part.strip())
        return cls(
            channel=channel,
            quiet_hours_enabled=_parse_bool(
                values,
                "HIKARI_PRESENCE_QUIET_HOURS_ENABLED",
                default=False,
            ),
            quiet_start=start,
            quiet_end=end,
            cooldown_seconds=_parse_nonnegative(
                values,
                "HIKARI_PRESENCE_COOLDOWN_SECONDS",
                default=300.0,
            ),
            duplicate_window_seconds=_parse_nonnegative(
                values,
                "HIKARI_PRESENCE_DUPLICATE_WINDOW_SECONDS",
                default=3600.0,
            ),
            urgent_threshold=_parse_unit_interval(
                values,
                "HIKARI_PRESENCE_URGENT_THRESHOLD",
                default=0.95,
            ),
            busy_foreground_patterns=patterns,
            suppress_active_schedule=_parse_bool(
                values,
                "HIKARI_PRESENCE_SUPPRESS_ACTIVE_SCHEDULE",
                default=True,
            ),
        )


@dataclass(frozen=True)
class PresenceDecision:
    """Auditable decision about whether Hikari may interrupt right now."""

    should_deliver: bool
    channel: str
    urgent: bool
    reason: str
    fingerprint: str
    delivery_id: str
    user_state: UserState | None = None


class PresencePolicyStore:
    """Durable suppression state so restarts do not reset cooldown/deduplication."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS presence_acceptance (
                    fingerprint TEXT PRIMARY KEY,
                    accepted_at REAL NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS presence_meta (
                    key TEXT PRIMARY KEY,
                    value REAL NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def accepted_at(self, fingerprint: str) -> float | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT accepted_at FROM presence_acceptance WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchone()
        return None if row is None else float(row[0])

    def last_accepted_at(self) -> float | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM presence_meta WHERE key = 'last_accepted_at'"
            ).fetchone()
        return None if row is None else float(row[0])

    def record_acceptance(self, fingerprint: str, accepted_at: float) -> None:
        timestamp = float(accepted_at)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO presence_acceptance (fingerprint, accepted_at)
                VALUES (?, ?)
                ON CONFLICT(fingerprint) DO UPDATE SET accepted_at = excluded.accepted_at
                """,
                (fingerprint, timestamp),
            )
            connection.execute(
                """
                INSERT INTO presence_meta (key, value)
                VALUES ('last_accepted_at', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (timestamp,),
            )


class PresencePolicy:
    """Cheap deterministic interruption policy placed after Attention."""

    def __init__(
        self,
        config: PresencePolicyConfig,
        store: PresencePolicyStore,
        *,
        clock: Callable[[], datetime] | None = None,
        user_state_inferer: UserStateInferer | None = None,
    ) -> None:
        if not isinstance(config, PresencePolicyConfig):
            raise TypeError("PresencePolicy requires PresencePolicyConfig")
        if not isinstance(store, PresencePolicyStore):
            raise TypeError("PresencePolicy requires PresencePolicyStore")
        self.config = config
        self.store = store
        self.clock = clock or (lambda: datetime.now().astimezone())
        self.user_state_inferer = user_state_inferer or UserStateInferer()

    @staticmethod
    def fingerprint(event: Event) -> str:
        normalized_content = " ".join(event.content.split())
        payload = f"{event.event_type}\0{event.source}\0{normalized_content}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def delivery_id(event: Event, fingerprint: str) -> str:
        occurred = event.occurred_at
        if occurred.tzinfo is None:
            occurred = occurred.replace(tzinfo=timezone.utc)
        payload = f"{fingerprint}\0{occurred.astimezone(timezone.utc).isoformat()}"
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
        return f"presence:{digest}"

    def evaluate(self, event: Event, attention: AttentionDecision) -> PresenceDecision:
        fingerprint = self.fingerprint(event)
        delivery_id = self.delivery_id(event, fingerprint)
        user_state = self._user_state(event)
        urgent = attention.importance >= self.config.urgent_threshold

        if not attention.should_intervene:
            return PresenceDecision(
                False,
                self.config.channel,
                urgent,
                "attention rejected event",
                fingerprint,
                delivery_id,
                user_state,
            )

        now = self._now()
        timestamp = now.timestamp()

        previous = self.store.accepted_at(fingerprint)
        if previous is not None and self._inside_window(
            timestamp,
            previous,
            self.config.duplicate_window_seconds,
        ):
            return PresenceDecision(
                False,
                self.config.channel,
                urgent,
                "duplicate suppression window",
                fingerprint,
                delivery_id,
                user_state,
            )

        if urgent:
            return PresenceDecision(
                True,
                self.config.channel,
                True,
                "urgent threshold bypass",
                fingerprint,
                delivery_id,
                user_state,
            )

        last = self.store.last_accepted_at()
        if last is not None and self._inside_window(
            timestamp,
            last,
            self.config.cooldown_seconds,
        ):
            return PresenceDecision(
                False,
                self.config.channel,
                False,
                "global cooldown active",
                fingerprint,
                delivery_id,
                user_state,
            )

        local_time = self._local_time(event, fallback=now)
        if self.config.quiet_hours_enabled and self._inside_quiet_hours(local_time):
            return PresenceDecision(
                False,
                self.config.channel,
                False,
                "quiet hours",
                fingerprint,
                delivery_id,
                user_state,
            )

        busy_reason = self._busy_reason(event, user_state)
        if busy_reason is not None:
            return PresenceDecision(
                False,
                self.config.channel,
                False,
                busy_reason,
                fingerprint,
                delivery_id,
                user_state,
            )

        return PresenceDecision(
            True,
            self.config.channel,
            False,
            "allowed",
            fingerprint,
            delivery_id,
            user_state,
        )

    def mark_accepted(self, decision: PresenceDecision) -> None:
        if not decision.should_deliver:
            raise ValueError("cannot mark a suppressed Presence decision as accepted")
        self.store.record_acceptance(decision.fingerprint, self._now().timestamp())

    def _now(self) -> datetime:
        value = self.clock()
        if not isinstance(value, datetime):
            raise TypeError("PresencePolicy clock must return datetime")
        if value.tzinfo is None:
            value = value.astimezone()
        return value

    @staticmethod
    def _inside_window(now: float, previous: float, window_seconds: float) -> bool:
        if window_seconds <= 0:
            return False
        age = now - previous
        return age < 0 or age < window_seconds

    def _inside_quiet_hours(self, local_value: datetime) -> bool:
        current = local_value.hour * 60 + local_value.minute
        start = _clock_minutes(self.config.quiet_start)
        end = _clock_minutes(self.config.quiet_end)
        if start == end:
            return True
        if start < end:
            return start <= current < end
        return current >= start or current < end

    def _snapshot(self, event: Event) -> ContextSnapshot | None:
        raw = event.context.get(HIKARI_CONTEXT_KEY)
        if not isinstance(raw, Mapping):
            return None
        providers_raw = raw.get("providers")
        if not isinstance(providers_raw, Mapping):
            return None
        providers: dict[str, dict[str, Any]] = {}
        for name, value in providers_raw.items():
            if isinstance(name, str) and isinstance(value, Mapping):
                providers[name] = dict(value)
        captured_raw = raw.get("captured_at")
        try:
            captured = datetime.fromisoformat(str(captured_raw))
        except (TypeError, ValueError):
            captured = datetime.now(timezone.utc)
        if captured.tzinfo is None:
            captured = captured.replace(tzinfo=timezone.utc)
        return ContextSnapshot(captured_at=captured, providers=providers)

    def _user_state(self, event: Event) -> UserState | None:
        snapshot = self._snapshot(event)
        if snapshot is None:
            return None
        return self.user_state_inferer.infer(snapshot)

    def _local_time(self, event: Event, *, fallback: datetime) -> datetime:
        snapshot = self._snapshot(event)
        if snapshot is not None:
            time_context = snapshot.providers.get("time", {})
            raw = time_context.get("local_iso")
            if isinstance(raw, str) and raw.strip():
                try:
                    parsed = datetime.fromisoformat(raw.strip())
                except ValueError:
                    pass
                else:
                    if parsed.tzinfo is None:
                        parsed = parsed.astimezone()
                    return parsed
        return fallback.astimezone()

    def _busy_reason(self, event: Event, user_state: UserState | None) -> str | None:
        snapshot = self._snapshot(event)
        if snapshot is None:
            return None

        if (
            self.config.suppress_active_schedule
            and user_state is not None
            and user_state.interruptibility == "likely_busy"
        ):
            return "active schedule suggests low interruptibility"

        foreground = snapshot.providers.get("foreground", {})
        if foreground.get("available") is True:
            title = str(foreground.get("title", "")).casefold()
            for pattern in self.config.busy_foreground_patterns:
                if pattern in title:
                    return f"foreground matches busy pattern: {pattern}"
        return None
