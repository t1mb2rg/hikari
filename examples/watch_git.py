from __future__ import annotations

"""Compatibility entry point for Hikari's resident Git watcher.

The installable implementation now lives in :mod:`resident.app`; keeping these
exports avoids breaking existing physical gates and local commands.
"""

from resident.app import build_reasoner, build_runtime, main

__all__ = ["build_reasoner", "build_runtime", "main"]


if __name__ == "__main__":
    main()
