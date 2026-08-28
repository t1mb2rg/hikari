from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import os
from pathlib import Path

from dotenv import dotenv_values


MODEL_ENV_KEYS = (
    "HIKARI_MODEL_BASE_URL",
    "HIKARI_MODEL_NAME",
    "HIKARI_MODEL_API_KEY",
)
ENV_FILE_POINTER = "HIKARI_ENV_FILE"


@dataclass(frozen=True)
class RuntimeEnvironment:
    """Resolved runtime configuration without persisting secret values."""

    values: dict[str, str]
    env_file: Path | None

    def model_presence(self) -> dict[str, bool]:
        """Return secret-safe diagnostics for model configuration."""

        return {
            key: bool(self.values.get(key, "").strip())
            for key in MODEL_ENV_KEYS
        }


def source_checkout_root() -> Path | None:
    """Return the Hikari source root when running from a checkout/editable install."""

    candidate = Path(__file__).resolve().parents[1]
    if (candidate / "pyproject.toml").is_file() and (candidate / "resident").is_dir():
        return candidate
    return None


def resolve_env_file(
    explicit: str | Path | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    cwd: str | Path | None = None,
) -> Path | None:
    """Choose one env file using deterministic, caller-visible precedence.

    Precedence:
    1. explicit argument
    2. HIKARI_ENV_FILE from the process/caller environment
    3. `.env` in a Hikari source checkout
    4. `.env` in the supplied/current working directory

    Explicit/pointer paths must exist. Implicit default candidates are optional.
    """

    env = os.environ if environment is None else environment

    if explicit is not None and str(explicit).strip():
        path = Path(explicit).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"Hikari env file does not exist: {path}")
        return path

    pointer = env.get(ENV_FILE_POINTER, "").strip()
    if pointer:
        path = Path(pointer).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"{ENV_FILE_POINTER} points to a missing file: {path}")
        return path

    checkout = source_checkout_root()
    if checkout is not None:
        candidate = checkout / ".env"
        if candidate.is_file():
            return candidate.resolve()

    current = Path.cwd() if cwd is None else Path(cwd)
    candidate = current.expanduser().resolve() / ".env"
    if candidate.is_file():
        return candidate
    return None


def load_runtime_environment(
    *,
    env_file: str | Path | None = None,
    environment: Mapping[str, str] | None = None,
    cwd: str | Path | None = None,
) -> RuntimeEnvironment:
    """Merge optional dotenv configuration with process values.

    Process/caller environment always wins over the env file. dotenv values are
    never written back to os.environ by this function, which keeps tests and
    embedding callers deterministic.
    """

    process_values = dict(os.environ if environment is None else environment)
    selected = resolve_env_file(env_file, environment=process_values, cwd=cwd)

    file_values: dict[str, str] = {}
    if selected is not None:
        parsed = dotenv_values(selected)
        file_values = {
            str(key): str(value)
            for key, value in parsed.items()
            if key and value is not None
        }

    merged = file_values
    merged.update(process_values)
    return RuntimeEnvironment(values=merged, env_file=selected)
