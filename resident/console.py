"""Explicit UTF-8 output for command-line tools, including redirected Windows pipes."""
from __future__ import annotations

import sys


def configure_utf8_output() -> None:
    # Python's redirected streams otherwise inherit the local ANSI code page on
    # Windows. Chinese status output must not turn a successful durable action
    # into a process failure while printing its receipt. In-memory capture streams
    # already accept Unicode and do not need reconfiguration.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="backslashreplace")
