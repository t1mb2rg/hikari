"""Installable resident application and host boundaries for Hikari.

The package deliberately avoids importing executable submodules eagerly. This
keeps ``python -m resident.app`` and ``python -m resident.windows_host`` free of
runpy re-import warnings while preserving the small public convenience API.
"""

from __future__ import annotations

from typing import Any


__all__ = [
    "HostStartResult",
    "HostState",
    "HostStatus",
    "ResidentHostConfig",
    "WindowsResidentHost",
    "WindowsResidentHostUnavailable",
    "build_reasoner",
    "build_runtime",
    "default_state_dir",
]


def __getattr__(name: str) -> Any:
    if name in {"build_reasoner", "build_runtime"}:
        from . import app

        return getattr(app, name)

    if name in {
        "HostStartResult",
        "HostState",
        "HostStatus",
        "ResidentHostConfig",
        "WindowsResidentHost",
        "WindowsResidentHostUnavailable",
        "default_state_dir",
    }:
        from . import windows_host

        return getattr(windows_host, name)

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
