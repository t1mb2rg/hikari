from __future__ import annotations

from dataclasses import dataclass

from .context import ContextSnapshot


@dataclass(frozen=True)
class UserState:
    """Conservative interpretation derived from already-captured context."""

    engagement: str
    interruptibility: str
    confidence: float
    evidence: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "engagement": self.engagement,
            "interruptibility": self.interruptibility,
            "confidence": self.confidence,
            "evidence": list(self.evidence),
        }


class UserStateInferer:
    """Cheap, deterministic user-state inference.

    Evidence is combined conservatively. A current schedule item can justify a
    `likely_busy` result, while foreground/input signals alone never claim that
    the user is definitely free. Unknown remains an intentional result.
    """

    def infer(self, snapshot: ContextSnapshot) -> UserState:
        providers = snapshot.providers
        activity = providers.get("input_activity", {})
        foreground = providers.get("foreground", {})
        schedule = providers.get("schedule", {})
        time_context = providers.get("time", {})

        recent_input = activity.get("recent_input")
        foreground_available = foreground.get("available") is True
        current_schedule = schedule.get("current", [])

        evidence: list[str] = []
        if recent_input is True:
            evidence.append("recent local input observed")
        elif recent_input is False:
            evidence.append("no recent local input observed")
        else:
            evidence.append("recent input signal unavailable")

        if foreground_available:
            title = str(foreground.get("title", "")).strip()
            evidence.append(
                f"foreground window available: {title}" if title else "foreground window available"
            )
        elif "foreground" in providers:
            evidence.append("foreground window unavailable")

        if current_schedule:
            evidence.append("schedule has current item(s)")

        if "hour" in time_context:
            evidence.append(f"local hour: {time_context['hour']}")

        if recent_input is True and foreground_available:
            engagement = "interactive"
            confidence = 0.8
        elif recent_input is False:
            engagement = "passive_or_unknown"
            confidence = 0.4 if foreground_available else 0.3
        else:
            engagement = "unknown"
            confidence = 0.2 if foreground_available else 0.1

        if current_schedule:
            interruptibility = "likely_busy"
            confidence = max(confidence, 0.75)
        elif recent_input is True and foreground_available:
            interruptibility = "likely_available"
        else:
            interruptibility = "unknown"

        return UserState(
            engagement=engagement,
            interruptibility=interruptibility,
            confidence=confidence,
            evidence=tuple(evidence),
        )
