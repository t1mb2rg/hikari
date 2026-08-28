from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


VOICE_PATH = Path(__file__).with_name("voice.yaml")


@dataclass(frozen=True)
class VoiceProfile:
    """Stable conversational-expression guidance separate from trait weights."""

    version: str
    stance: dict[str, Any]
    cadence: dict[str, Any]
    habits: tuple[str, ...]
    avoid: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.version.strip():
            raise ValueError("voice profile version must not be empty")
        if not isinstance(self.stance, dict) or not isinstance(self.cadence, dict):
            raise ValueError("voice stance/cadence must be mappings")
        if not self.habits:
            raise ValueError("voice profile requires at least one habit")

    def describe(self) -> dict[str, object]:
        return {
            "version": self.version,
            "stance": dict(self.stance),
            "cadence": dict(self.cadence),
            "habits": list(self.habits),
            "avoid": list(self.avoid),
        }


def load_voice(path: str | Path = VOICE_PATH) -> VoiceProfile:
    profile_path = Path(path)
    with profile_path.open("r", encoding="utf-8") as file:
        data = yaml.safe_load(file)

    if not isinstance(data, dict):
        raise ValueError("voice profile must be a mapping")

    version = data.get("version")
    stance = data.get("stance")
    cadence = data.get("cadence")
    habits = data.get("habits")
    avoid = data.get("avoid")
    if not isinstance(version, str):
        raise ValueError("voice profile requires a string version")
    if not isinstance(stance, dict) or not isinstance(cadence, dict):
        raise ValueError("voice profile requires stance and cadence mappings")
    if not isinstance(habits, list) or not all(isinstance(item, str) for item in habits):
        raise ValueError("voice profile habits must be a string list")
    if not isinstance(avoid, list) or not all(isinstance(item, str) for item in avoid):
        raise ValueError("voice profile avoid must be a string list")

    return VoiceProfile(
        version=version,
        stance=dict(stance),
        cadence=dict(cadence),
        habits=tuple(item.strip() for item in habits if item.strip()),
        avoid=tuple(item.strip() for item in avoid if item.strip()),
    )
