from __future__ import annotations

from collections.abc import Callable
import platform


IdleSecondsReader = Callable[[], float | None]


def read_system_idle_seconds() -> float | None:
    """Return local input idle time when supported by the current platform."""
    if platform.system() != "Windows":
        return None

    try:
        import ctypes
        from ctypes import wintypes

        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.UINT),
                ("dwTime", wintypes.DWORD),
            ]

        info = LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)

        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None

        # GetLastInputInfo and GetTickCount both use 32-bit millisecond ticks.
        # Masking the subtraction preserves the correct elapsed value across wrap.
        current_tick = int(ctypes.windll.kernel32.GetTickCount())
        elapsed_ms = (current_tick - int(info.dwTime)) & 0xFFFFFFFF
        return elapsed_ms / 1000.0
    except (AttributeError, OSError, TypeError, ValueError):
        return None


class DeviceActivityContextProvider:
    """Ambient user-presence signal derived from local input idle time."""

    name = "activity"

    def __init__(
        self,
        *,
        active_threshold_seconds: float = 120.0,
        idle_seconds_reader: IdleSecondsReader = read_system_idle_seconds,
    ) -> None:
        if active_threshold_seconds < 0:
            raise ValueError("active_threshold_seconds must be non-negative")

        self.active_threshold_seconds = float(active_threshold_seconds)
        self.idle_seconds_reader = idle_seconds_reader

    def capture(self) -> dict[str, object]:
        idle_seconds = self.idle_seconds_reader()

        if idle_seconds is None:
            return {
                "supported": False,
                "state": "unknown",
            }

        idle_seconds = max(0.0, float(idle_seconds))
        state = (
            "active"
            if idle_seconds < self.active_threshold_seconds
            else "idle"
        )

        return {
            "supported": True,
            "state": state,
            "idle_seconds": round(idle_seconds, 3),
            "active_threshold_seconds": self.active_threshold_seconds,
        }
