from __future__ import annotations

from pathlib import Path

import pytest

from resident.environment import load_runtime_environment, resolve_env_file


def test_env_file_loads_model_values_without_mutating_process_mapping(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "HIKARI_MODEL_BASE_URL=https://example.invalid\n"
        "HIKARI_MODEL_NAME=file-model\n"
        "HIKARI_MODEL_API_KEY=file-secret\n",
        encoding="utf-8",
    )
    process = {"PATH": "test-path"}

    runtime = load_runtime_environment(env_file=env_file, environment=process)

    assert runtime.env_file == env_file.resolve()
    assert runtime.values["HIKARI_MODEL_BASE_URL"] == "https://example.invalid"
    assert runtime.values["HIKARI_MODEL_NAME"] == "file-model"
    assert runtime.values["HIKARI_MODEL_API_KEY"] == "file-secret"
    assert process == {"PATH": "test-path"}


def test_process_environment_overrides_env_file(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "HIKARI_MODEL_BASE_URL=https://from-file.invalid\n"
        "HIKARI_MODEL_NAME=file-model\n"
        "HIKARI_MODEL_API_KEY=file-secret\n",
        encoding="utf-8",
    )

    runtime = load_runtime_environment(
        env_file=env_file,
        environment={
            "HIKARI_MODEL_NAME": "process-model",
            "HIKARI_MODEL_API_KEY": "process-secret",
        },
    )

    assert runtime.values["HIKARI_MODEL_BASE_URL"] == "https://from-file.invalid"
    assert runtime.values["HIKARI_MODEL_NAME"] == "process-model"
    assert runtime.values["HIKARI_MODEL_API_KEY"] == "process-secret"


def test_env_file_pointer_is_respected(tmp_path: Path):
    env_file = tmp_path / "hikari.env"
    env_file.write_text("HIKARI_MODEL_NAME=pointed-model\n", encoding="utf-8")

    resolved = resolve_env_file(
        environment={"HIKARI_ENV_FILE": str(env_file)},
        cwd=tmp_path / "elsewhere",
    )

    assert resolved == env_file.resolve()


def test_explicit_missing_env_file_fails_closed(tmp_path: Path):
    with pytest.raises(ValueError, match="does not exist"):
        load_runtime_environment(
            env_file=tmp_path / "missing.env",
            environment={},
        )


def test_model_presence_does_not_expose_secret_values(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "HIKARI_MODEL_BASE_URL=https://example.invalid\n"
        "HIKARI_MODEL_NAME=model\n"
        "HIKARI_MODEL_API_KEY=super-secret\n",
        encoding="utf-8",
    )

    runtime = load_runtime_environment(env_file=env_file, environment={})
    presence = runtime.model_presence()

    assert presence == {
        "HIKARI_MODEL_BASE_URL": True,
        "HIKARI_MODEL_NAME": True,
        "HIKARI_MODEL_API_KEY": True,
    }
    assert "super-secret" not in repr(presence)
