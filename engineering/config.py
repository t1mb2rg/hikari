from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass


DEFAULT_ENGINEERING_MODEL = "sonnet"
DEFAULT_ENGINEERING_BACKEND_TIMEOUT_SECONDS = 600.0
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


def validate_claude_code_model(value: str) -> str:
    """Validate the explicit model Hikari passes to Claude Code.

    Engineering must not silently inherit Conversation model names or ambient
    Claude Code configuration. The backend is Claude Code, so the first owned
    configuration accepts its stable aliases and explicit Claude model ids.
    """

    model = value.strip()
    if not model:
        raise ValueError("HIKARI_ENGINEERING_MODEL must not be empty")
    if model in {"sonnet", "opus", "haiku"} or model.startswith("claude-"):
        return model
    raise ValueError(
        "HIKARI_ENGINEERING_MODEL must be a Claude Code model alias "
        "(sonnet/opus/haiku) or an explicit claude-* model id; do not reuse "
        "HIKARI_MODEL_NAME or an unrelated ambient model setting"
    )


@dataclass(frozen=True, slots=True)
class EngineeringBackendConfig:
    model: str
    timeout_seconds: float
    max_turns: int

    @classmethod
    def from_mapping(cls, values: Mapping[str, str]) -> "EngineeringBackendConfig":
        model = validate_claude_code_model(
            values.get("HIKARI_ENGINEERING_MODEL", DEFAULT_ENGINEERING_MODEL)
        )
        return cls(
            model=model,
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

    def apply_to_environment(self, values: Mapping[str, str]) -> dict[str, str]:
        result = dict(values)
        result["HIKARI_ENGINEERING_MODEL"] = self.model
        result["HIKARI_ENGINEERING_BACKEND_TIMEOUT_SECONDS"] = f"{self.timeout_seconds:g}"
        result["HIKARI_ENGINEERING_MAX_TURNS"] = str(self.max_turns)
        return result
