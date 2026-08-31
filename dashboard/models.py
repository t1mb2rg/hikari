from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Mapping


class ComponentStatus(StrEnum):
    HEALTHY = "healthy"
    RUNNING = "running"
    WAITING = "waiting"
    WARNING = "warning"
    ERROR = "error"
    OFFLINE = "offline"
    IDLE = "idle"


@dataclass(frozen=True)
class ComponentSnapshot:
    component_id: str
    label: str
    status: ComponentStatus
    phase: str
    message: str
    updated_at: str | None = None
    blocking_on: str | None = None
    last_error: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "id": self.component_id,
            "label": self.label,
            "status": self.status.value,
            "phase": self.phase,
            "message": self.message,
            "updated_at": self.updated_at,
            "blocking_on": self.blocking_on,
            "last_error": self.last_error,
            "details": dict(self.details),
        }
