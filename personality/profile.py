from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


PROFILE_PATH = Path(__file__).with_name("profile.yaml")
REQUIRED_TRAITS = (
    "warmth",
    "directness",
    "curiosity",
    "assertiveness",
    "patience",
)


@dataclass(frozen=True)
class PersonalityProfile:
    """Slow-changing, model-independent Hikari personality baseline."""

    version: str
    traits: dict[str, float]

    def __post_init__(self) -> None:
        if not self.version.strip():
            raise ValueError("personality version must not be empty")

        expected = set(REQUIRED_TRAITS)
        actual = set(self.traits)
        missing = expected - actual
        unknown = actual - expected
        if missing:
            raise ValueError(f"missing personality traits: {', '.join(sorted(missing))}")
        if unknown:
            raise ValueError(f"unknown personality traits: {', '.join(sorted(unknown))}")

        normalized: dict[str, float] = {}
        for name in REQUIRED_TRAITS:
            value = float(self.traits[name])
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"personality trait {name!r} must be between 0.0 and 1.0")
            normalized[name] = value

        object.__setattr__(self, "traits", normalized)

    def describe(self) -> dict[str, object]:
        return {
            "version": self.version,
            "traits": dict(self.traits),
        }


def load_personality(path: str | Path = PROFILE_PATH) -> PersonalityProfile:
    profile_path = Path(path)
    with profile_path.open("r", encoding="utf-8") as file:
        data = yaml.safe_load(file)

    if not isinstance(data, dict):
        raise ValueError("personality profile must be a mapping")

    version = data.get("version")
    traits = data.get("traits")
    if not isinstance(version, str):
        raise ValueError("personality profile requires a string version")
    if not isinstance(traits, dict):
        raise ValueError("personality profile requires a traits mapping")

    return PersonalityProfile(version=version, traits=traits)
