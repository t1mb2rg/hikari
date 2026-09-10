"""Small producer-owned observations; dashboards only read these files."""
from __future__ import annotations

import json
import os
from pathlib import Path
import time
from uuid import uuid4


def record_observation(root: Path, component: str, status: str, **details) -> bool:
    if component not in {"conversation", "model", "qq", "resident", "engineering_backend"}:
        raise ValueError("unsupported observed component")
    directory = Path(root) / "observations"
    temporary = directory / f".{component}.{uuid4().hex}.tmp"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "component": component, "status": status,
                   "pid": os.getpid(), "observed_at": time.time(), "details": details}
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, directory / f"{component}.json")
        return True
    except OSError:
        return False  # Observability failure must not terminate an accepted task.
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def read_observation(root: Path, component: str) -> dict | None:
    path = Path(root) / "observations" / f"{component}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(payload, dict) or payload.get("version") != 1 or payload.get("component") != component:
        raise ValueError("invalid observation")
    if not isinstance(payload.get("pid"), int) or not isinstance(payload.get("observed_at"), (float, int)):
        raise ValueError("invalid observation identity/time")
    return payload
