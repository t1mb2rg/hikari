"""Ambient context collection for Hikari awareness."""

from .activity import (
    DeviceActivityContextProvider,
    InputActivityContextProvider,
    read_system_idle_seconds,
)
from .context import (
    ContextCollector,
    ContextProvider,
    ContextSnapshot,
    HostContextProvider,
    TimeContextProvider,
)
from .foreground import ForegroundContextProvider, read_foreground_window
from .lunar import ChineseCalendarContextProvider
from .schedule import ScheduleContextProvider, ScheduleEntry, ScheduleSource
from .user_state import UserState, UserStateInferer

__all__ = [
    "ChineseCalendarContextProvider",
    "ContextCollector",
    "ContextProvider",
    "ContextSnapshot",
    "DeviceActivityContextProvider",
    "ForegroundContextProvider",
    "HostContextProvider",
    "InputActivityContextProvider",
    "ScheduleContextProvider",
    "ScheduleEntry",
    "ScheduleSource",
    "TimeContextProvider",
    "UserState",
    "UserStateInferer",
    "read_foreground_window",
    "read_system_idle_seconds",
]
