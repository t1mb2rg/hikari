from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
import shutil


DEFAULT_ENGINEERING_EXECUTABLE = "claude"
DEFAULT_ENGINEERING_MODEL = "sonnet"
DEFAULT_ENGINEERING_BACKEND_TIMEOUT_SECONDS = 300.0
DEFAULT_ENGINEERING_MAX_TURNS = 30


def _positive_float(values: Mapping[str, str], name: str, default: float) -> float:
    raw = values.get(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if value <= 0:
        raise ValueError(f"{name} must be > 0")
    return value


def _positive_int(values: Mapping[str, str], name: str, default: int) -> int:
    raw = values.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be > 0")
    return value


def _owned_text(values: Mapping[str, str], name: str, default: str) -> str:
    value = values.get(name, default).strip()
    if not value:
        raise ValueError(f"{name} must not be empty")
    if any(ch in value for ch in ("\x00", "\r", "\n")):
        raise ValueError(f"{name} contains unsupported control characters")
    if len(value) > 240:
        raise ValueError(f"{name} is unexpectedly long")
    return value


@dataclass(frozen=True, slots=True)
class EngineeringBackendConfig:
    executable: str
    model: str
    timeout_seconds: float
    max_turns: int

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> "EngineeringBackendConfig":
        return cls(
            executable=_owned_text(
                values,
                "HIKARI_ENGINEERING_CLAUDE_EXECUTABLE",
                DEFAULT_ENGINEERING_EXECUTABLE,
            ),
            model=_owned_text(
                values,
                "HIKARI_ENGINEERING_MODEL",
                DEFAULT_ENGINEERING_MODEL,
            ),
            timeout_seconds=_positive_float(
                values,
                "HIKARI_ENGINEERING_BACKEND_TIMEOUT_SECONDS",
                DEFAULT_ENGINEERING_BACKEND_TIMEOUT_SECONDS,
            ),
            max_turns=_positive_int(
                values,
                "HIKARI_ENGINEERING_MAX_TURNS",
                DEFAULT_ENGINEERING_MAX_TURNS,
            ),
        )

    def resolve_executable(self, *, path: str | None = None) -> str | None:
        candidate = Path(self.executable).expanduser()
        if candidate.is_absolute():
            return str(candidate.resolve()) if candidate.is_file() else None
        return shutil.which(self.executable, path=path)

    def validate_runtime(self, values: Mapping[str, str]) -> None:
        if self.resolve_executable(path=values.get("PATH")) is None:
            raise ValueError(
                "HIKARI Engineering Runtime cannot find Claude Code executable "
                f"{self.executable!r}; configure HIKARI_ENGINEERING_CLAUDE_EXECUTABLE "
                "or fix Resident PATH"
            )

    def apply_to_environment(self, values: Mapping[str, str]) -> dict[str, str]:
        result = dict(values)
        result["HIKARI_ENGINEERING_CLAUDE_EXECUTABLE"] = self.executable
        result["HIKARI_ENGINEERING_MODEL"] = self.model
        result["HIKARI_ENGINEERING_BACKEND_TIMEOUT_SECONDS"] = f"{self.timeout_seconds:g}"
        result["HIKARI_ENGINEERING_MAX_TURNS"] = str(self.max_turns)
        return result
