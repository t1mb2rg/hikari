from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from resident.environment_manager import (
    EnvironmentManager,
    EnvironmentManagerError,
    _PYTHON_VERSION_PROBE,
)


_RUNTIME_VERSION = f"{sys.version_info.major}.{sys.version_info.minor}"


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "pyproject.toml").write_text(
        "[project]\nname='hikari-test'\nversion='0.0.0'\n",
        encoding="utf-8",
    )
    (repository / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    return repository


def _fake_uv(tmp_path: Path) -> Path:
    executable = tmp_path / ("uv.exe" if os.name == "nt" else "uv")
    executable.write_text("fake", encoding="utf-8")
    executable.chmod(0o755)
    return executable


def test_candidate_identity_is_stable_and_does_not_target_live_venv(tmp_path: Path) -> None:
    manager = EnvironmentManager(_repository(tmp_path), tmp_path / "state")

    first = manager.candidate_for(extras=("windows-notify", "dev"))
    second = manager.candidate_for(extras=("dev", "windows-notify"))

    assert first.environment_id == second.environment_id
    assert Path(first.path).parent == (tmp_path / "state" / "environments").resolve()
    assert Path(first.path).name == first.environment_id
    assert Path(first.path).name != ".venv"


def test_build_targets_candidate_path_and_never_live_venv(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    calls: list[tuple[list[str], dict[str, object]]] = []

    def runner(argv, **kwargs):
        calls.append((list(argv), dict(kwargs)))
        if "sync" in argv:
            candidate = Path(kwargs["env"]["UV_PROJECT_ENVIRONMENT"])
            python = EnvironmentManager.python_path(candidate)
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_text("fake", encoding="utf-8")
        if len(argv) > 2 and argv[2] == _PYTHON_VERSION_PROBE:
            return subprocess.CompletedProcess(argv, 0, f"{_RUNTIME_VERSION}\n", "")
        return subprocess.CompletedProcess(argv, 0, "synced", "")

    manager = EnvironmentManager(
        repository,
        tmp_path / "state",
        uv_executable=str(_fake_uv(tmp_path)),
        runner=runner,
    )
    candidate = manager.build()

    assert candidate.status == "built"
    assert calls[0][0][1:3] == ["sync", "--locked"]
    assert calls[0][0][3:5] == ["--python", _RUNTIME_VERSION]
    assert calls[0][1]["stdin"] is subprocess.DEVNULL
    target = Path(calls[0][1]["env"]["UV_PROJECT_ENVIRONMENT"])
    assert target == Path(candidate.path)
    assert target != repository / ".venv"


def test_validate_requires_nested_process_probe_before_pytest(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    calls: list[list[str]] = []

    def runner(argv, **kwargs):
        calls.append(list(argv))
        if "sync" in argv:
            candidate = Path(kwargs["env"]["UV_PROJECT_ENVIRONMENT"])
            python = EnvironmentManager.python_path(candidate)
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_text("fake", encoding="utf-8")
        if len(argv) > 2 and argv[2] == _PYTHON_VERSION_PROBE:
            return subprocess.CompletedProcess(argv, 0, f"{_RUNTIME_VERSION}\n", "")
        return subprocess.CompletedProcess(argv, 0, "ok", "")

    manager = EnvironmentManager(
        repository,
        tmp_path / "state",
        uv_executable=str(_fake_uv(tmp_path)),
        runner=runner,
    )
    built = manager.build()
    verified = manager.validate(built.environment_id)

    assert verified.status == "verified"
    assert verified.test_returncode == 0
    assert calls[2][1] == "-c"
    assert calls[3][1:4] == ["-m", "pytest", "-q"]
    pointer = manager.promote(verified.environment_id)
    assert pointer["environment_id"] == verified.environment_id
    assert manager.current() == pointer


def test_failed_nested_probe_blocks_validation_and_promotion(tmp_path: Path) -> None:
    repository = _repository(tmp_path)

    def runner(argv, **kwargs):
        if "sync" in argv:
            candidate = Path(kwargs["env"]["UV_PROJECT_ENVIRONMENT"])
            python = EnvironmentManager.python_path(candidate)
            python.parent.mkdir(parents=True, exist_ok=True)
            python.write_text("fake", encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0, "ok", "")
        if len(argv) > 2 and argv[2] == _PYTHON_VERSION_PROBE:
            return subprocess.CompletedProcess(argv, 0, f"{_RUNTIME_VERSION}\n", "")
        return subprocess.CompletedProcess(argv, 6, "", "invalid handle")

    manager = EnvironmentManager(
        repository,
        tmp_path / "state",
        uv_executable=str(_fake_uv(tmp_path)),
        runner=runner,
    )
    built = manager.build()

    with pytest.raises(EnvironmentManagerError, match="nested process probe"):
        manager.validate(built.environment_id)
    failed = manager.load(built.environment_id)
    assert failed.status == "validation_environment_failed"
    with pytest.raises(EnvironmentManagerError, match="verified candidate"):
        manager.promote(failed.environment_id)
    assert not manager.pointer_path.exists()


def test_current_pointer_preserves_previous_environment_for_rollback(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    manager = EnvironmentManager(repository, tmp_path / "state")
    first = manager.candidate_for(extras=("dev",))
    first_python = manager.python_path(Path(first.path))
    first_python.parent.mkdir(parents=True)
    first_python.write_text("fake", encoding="utf-8")
    manager._atomic_json(
        manager.record_path(first.environment_id),
        {
            **first.to_mapping(),
            "status": "verified",
            "verified_at": 1.0,
            "test_returncode": 0,
        },
    )
    manager.promote(first.environment_id)

    (repository / "uv.lock").write_text("version = 2\n", encoding="utf-8")
    second = manager.candidate_for(extras=("dev",))
    second_python = manager.python_path(Path(second.path))
    second_python.parent.mkdir(parents=True)
    second_python.write_text("fake", encoding="utf-8")
    manager._atomic_json(
        manager.record_path(second.environment_id),
        {
            **second.to_mapping(),
            "status": "verified",
            "verified_at": 2.0,
            "test_returncode": 0,
        },
    )
    manager.promote(second.environment_id)
    assert manager.current_python(tmp_path / "fallback.exe") == second_python.resolve()

    rolled_back = manager.rollback()
    assert rolled_back["environment_id"] == first.environment_id
    assert rolled_back["previous_environment_id"] == second.environment_id
    assert json.loads(manager.pointer_path.read_text(encoding="utf-8")) == rolled_back
    assert manager.current_python(tmp_path / "fallback.exe") == first_python.resolve()


def test_current_python_uses_bootstrap_interpreter_without_promotion(tmp_path: Path) -> None:
    manager = EnvironmentManager(_repository(tmp_path), tmp_path / "state")
    fallback = tmp_path / "bootstrap" / "python.exe"

    assert manager.current_python(fallback) == fallback.resolve()


def test_first_promotion_can_roll_back_to_bootstrap_interpreter(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    manager = EnvironmentManager(repository, tmp_path / "state")
    candidate = manager.candidate_for(extras=("dev",))
    candidate_python = manager.python_path(Path(candidate.path))
    candidate_python.parent.mkdir(parents=True)
    candidate_python.write_text("fake", encoding="utf-8")
    manager._atomic_json(
        manager.record_path(candidate.environment_id),
        {
            **candidate.to_mapping(),
            "status": "verified",
            "verified_at": 1.0,
            "test_returncode": 0,
        },
    )
    fallback = tmp_path / "bootstrap" / "python.exe"
    manager.promote(candidate.environment_id)

    rolled_back = manager.rollback()

    assert rolled_back["environment_id"] is None
    assert rolled_back["previous_environment_id"] == candidate.environment_id
    assert not manager.pointer_path.exists()
    assert manager.current_python(fallback) == fallback.resolve()
