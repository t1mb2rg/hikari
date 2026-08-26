from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable


class ActionRisk(StrEnum):
    """Coarse trusted risk class for an action capability."""

    PASSIVE = "passive"
    REVERSIBLE = "reversible"
    DESTRUCTIVE = "destructive"


@dataclass(frozen=True)
class ActionSpec:
    """Caller-owned capability declaration exposed to Hikari's planner."""

    name: str
    description: str
    risk: ActionRisk
    requires_confirmation: bool

    def __post_init__(self) -> None:
        name = self.name.strip()
        description = self.description.strip()
        if not name:
            raise ValueError("action spec name must not be empty")
        if not description:
            raise ValueError("action spec description must not be empty")
        if not isinstance(self.requires_confirmation, bool):
            raise TypeError("requires_confirmation must be a bool")

        try:
            risk = self.risk if isinstance(self.risk, ActionRisk) else ActionRisk(str(self.risk))
        except ValueError as exc:
            raise ValueError(f"unknown action risk: {self.risk!r}") from exc

        if risk is ActionRisk.DESTRUCTIVE and not self.requires_confirmation:
            raise ValueError("destructive actions must require confirmation")

        object.__setattr__(self, "name", name)
        object.__setattr__(self, "description", description)
        object.__setattr__(self, "risk", risk)

    def describe(self) -> dict[str, object]:
        return {
            "name": self.name,
            "description": self.description,
            "risk": self.risk.value,
            "requires_confirmation": self.requires_confirmation,
        }


@dataclass(frozen=True)
class ActionProposal:
    """Structured intent only. Constructing a proposal does not execute anything."""

    action_name: str
    arguments: dict[str, object]
    effect: str
    reason: str
    confidence: float
    risk: ActionRisk
    requires_confirmation: bool

    def __post_init__(self) -> None:
        if not self.action_name.strip():
            raise ValueError("action proposal name must not be empty")
        if not isinstance(self.arguments, dict):
            raise TypeError("action proposal arguments must be a mapping")
        if not self.effect.strip():
            raise ValueError("action proposal effect must not be empty")
        if not self.reason.strip():
            raise ValueError("action proposal reason must not be empty")

        confidence = float(self.confidence)
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("action proposal confidence must be between 0.0 and 1.0")

        risk = self.risk if isinstance(self.risk, ActionRisk) else ActionRisk(str(self.risk))
        if risk is ActionRisk.DESTRUCTIVE and not self.requires_confirmation:
            raise ValueError("destructive action proposals must require confirmation")

        object.__setattr__(self, "action_name", self.action_name.strip())
        object.__setattr__(self, "effect", self.effect.strip())
        object.__setattr__(self, "reason", self.reason.strip())
        object.__setattr__(self, "confidence", confidence)
        object.__setattr__(self, "risk", risk)
        object.__setattr__(self, "arguments", dict(self.arguments))


class ActionCatalog:
    """Explicit allowlist of capabilities visible to one planner."""

    def __init__(self, specs: Iterable[ActionSpec]) -> None:
        indexed: dict[str, ActionSpec] = {}
        for spec in specs:
            if spec.name in indexed:
                raise ValueError(f"duplicate action spec: {spec.name}")
            indexed[spec.name] = spec
        if not indexed:
            raise ValueError("action catalog must contain at least one action")
        self._specs = indexed

    def get(self, name: str) -> ActionSpec | None:
        return self._specs.get(name)

    def describe(self) -> list[dict[str, object]]:
        return [spec.describe() for spec in self._specs.values()]

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._specs
