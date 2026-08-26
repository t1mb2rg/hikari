from __future__ import annotations

from collections.abc import Callable
import platform


ForegroundReader = Callable[[], dict[str, object] | None]


def read_foreground_window() -> dict[str, object] | None:
    """Return lightweight metadata for the current foreground window on Windows."""
    if platform.system() != "Windows":
        return None

    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None

        title_length = user32.GetWindowTextLengthW(hwnd)
        title_buffer = ctypes.create_unicode_buffer(max(1, title_length + 1))
        user32.GetWindowTextW(hwnd, title_buffer, len(title_buffer))

        class_buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_buffer, len(class_buffer))

        process_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))

        return {
            "title": title_buffer.value,
            "class_name": class_buffer.value,
            "process_id": int(process_id.value),
        }
    except (AttributeError, OSError, TypeError, ValueError):
        return None


class ForegroundContextProvider:
    """Raw foreground-window signal for later user-state inference."""

    name = "foreground"

    def __init__(self, *, foreground_reader: ForegroundReader = read_foreground_window) -> None:
        self.foreground_reader = foreground_reader

    def capture(self) -> dict[str, object]:
        window = self.foreground_reader()
        if window is None:
            return {
                "supported": platform.system() == "Windows",
                "available": False,
            }

        return {
            "supported": True,
            "available": True,
            "title": str(window.get("title", "")),
            "class_name": str(window.get("class_name", "")),
            "process_id": int(window.get("process_id", 0) or 0),
        }
