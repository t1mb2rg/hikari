from __future__ import annotations

from collections.abc import Mapping
import os
from pathlib import Path


def default_state_dir(environment: Mapping[str, str] | None = None) -> Path:
    """Return the per-user durable Hikari resident state directory.

    Path discovery is intentionally independent of Windows process hosting so
    Conversation and integration edges can locate shared resident state without
    importing the resident lifecycle controller.
    """

    env = os.environ if environment is None else environment
    local_app_data = env.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data) / "Hikari" / "resident"
    return Path.home() / ".hikari" / "resident"
