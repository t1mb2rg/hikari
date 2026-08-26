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


class InputActivityContextProvider:
    """Raw ambient signal describing recent local keyboard/mouse input.

    This provider deliberately does not infer whether the user is present,
    focused, or away. It reports only a cheap observable signal that a later
    user-state layer may combine with other context sources.
    """

    name = "input_activity"

    def __init__(
        self,
        *,
        recent_input_threshold_seconds: float = 120.0,
        idle_seconds_reader: IdleSecondsReader = read_system_idle_seconds,
    ) -> None:
        if recent_input_threshold_seconds < 0:
            raise ValueError("recent_input_threshold_seconds must be non-negative")

        self.recent_input_threshold_seconds = float(recent_input_threshold_seconds)
        self.idle_seconds_reader = idle_seconds_reader

    def capture(self) -> dict[str, object]:
        idle_seconds = self.idle_seconds_reader()

        if idle_seconds is None:
            return {
                "supported": False,
                "recent_input": None,
            }

        idle_seconds = max(0.0, float(idle_seconds))
        recent_input = idle_seconds < self.recent_input_threshold_seconds

        return {
            "supported": True,
            "recent_input": recent_input,
            "idle_seconds": round(idle_seconds, 3),
            "recent_input_threshold_seconds": self.recent_input_threshold_seconds,
        }


class DeviceActivityContextProvider(InputActivityContextProvider):
    """Compatibility alias for the pre-M1-02 provider name.

    New code should use InputActivityContextProvider. The returned data has raw
    input semantics and must not be interpreted as user presence.
    """

    def __init__(
        self,
        *,
        active_threshold_seconds: float = 120.0,
        idle_seconds_reader: IdleSecondsReader = read_system_idle_seconds,
    ) -> None:
        super().__init__(
            recent_input_threshold_seconds=active_threshold_seconds,
            idle_seconds_reader=idle_seconds_reader,
        )
