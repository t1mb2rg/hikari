from __future__ import annotations

import json
from pathlib import Path

import pytest

from resident.windows_host import (
    ResidentHostConfig,
    WindowsResidentHost,
    _select_background_python,
)
from resident.windows_process_tree import ordered_process_tree


def _config(tmp_path: Path, **overrides: object) -> ResidentHostConfig:
    repository = tmp_path / "repo"
    repository.mkdir(exist_ok=True)
    values = {
        "repository": repository,
        "memory_path": tmp_path / "state" / "memory.db",
        "state_dir": tmp_path / "state",
        "interval": 1.5,
        "output": "windows",
        "reasoner": "simple",
    }
    values.update(overrides)
    return ResidentHostConfig(**values)  # type: ignore[arg-type]


def test_windows_background_python_prefers_pythonw_when_available(tmp_path: Path):
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    python = scripts / "python.exe"
    pythonw = scripts / "pythonw.exe"
    python.write_text("", encoding="utf-8")
    pythonw.write_text("", encoding="utf-8")

    selected = _select_background_python(str(python), platform_name="nt")

    assert selected == str(pythonw)


def test_windows_background_python_falls_back_when_pythonw_is_missing(tmp_path: Path):
    python = tmp_path / "python.exe"
    python.write_text("", encoding="utf-8")

    selected = _select_background_python(str(python), platform_name="nt")

    assert selected == str(python)


def test_ordered_process_tree_is_parent_first_for_supervisor_shutdown():
    parent_by_pid = {
        32496: 53860,
        55232: 32496,
        53480: 55232,
        90000: 1,
    }

    assert ordered_process_tree(53860, parent_by_pid) == [
        53860,
        32496,
        55232,
        53480,
    ]


def test_start_detaches_one_trusted_child_and_persists_minimal_state(tmp_path: Path):
    config = _config(tmp_path)
    alive: set[int] = set()
    launches: list[tuple[list[str], Path, Path, dict[str, str]]] = []

    def launcher(argv, cwd, log_path, environment):
        launches.append((list(argv), cwd, log_path, dict(environment)))
        alive.add(4242)
        return 4242

    host = WindowsResidentHost(
        config,
        environment={"HIKARI_MODEL_API_KEY": "never-persist-this"},
        launcher=launcher,
        process_probe=lambda pid: pid in alive,
        terminator=lambda pid: alive.discard(pid),
        python_executable="python-test",
    )

    result = host.start()

    assert result.started is True
    assert result.status.running is True
    assert result.status.state is not None
    assert result.status.state.pid == 4242
    assert len(launches) == 1

    argv, cwd, log_path, environment = launches[0]
    assert argv[:3] == ["python-test", "-m", "resident.app"]
    assert str(config.repository) in argv
    assert argv[argv.index("--output") + 1] == "windows"
    assert argv[argv.index("--reasoner") + 1] == "simple"
    assert cwd == config.repository
    assert log_path == config.log_file
    assert environment["HIKARI_MODEL_API_KEY"] == "never-persist-this"
    assert "never-persist-this" not in " ".join(argv)

    persisted_text = config.state_file.read_text(encoding="utf-8")
    persisted = json.loads(persisted_text)
    assert persisted["pid"] == 4242
    assert persisted["repository"] == str(config.repository)
    assert "api" not in persisted_text.lower()
    assert "never-persist-this" not in persisted_text


def test_second_start_does_not_duplicate_live_resident(tmp_path: Path):
    config = _config(tmp_path)
    alive = {7001}
    launches: list[list[str]] = []

    def launcher(argv, cwd, log_path, environment):
        launches.append(list(argv))
        return 7001

    host = WindowsResidentHost(
        config,
        environment={},
        launcher=launcher,
        process_probe=lambda pid: pid in alive,
        terminator=lambda pid: alive.discard(pid),
    )

    first = host.start()
    second = host.start()

    assert first.started is True
    assert second.started is False
    assert second.status.running is True
    assert len(launches) == 1


def test_status_cleans_stale_state(tmp_path: Path):
    config = _config(tmp_path)
    config.state_dir.mkdir(parents=True)
    config.state_file.write_text(
        json.dumps(
            {
                "pid": 5150,
                "started_at": "2026-08-28T00:00:00+00:00",
                "repository": str(config.repository),
                "log_path": str(config.log_file),
            }
        ),
        encoding="utf-8",
    )
    host = WindowsResidentHost(
        config,
        environment={},
        launcher=lambda *args: 1,
        process_probe=lambda pid: False,
        terminator=lambda pid: None,
    )

    status = host.status()

    assert status.running is False
    assert status.reason == "stale_state"
    assert not config.state_file.exists()


def test_stop_terminates_recorded_process_tree_parent_first_and_removes_state(tmp_path: Path):
    config = _config(tmp_path)
    alive: set[int] = set()
    terminated: list[int] = []

    def launcher(argv, cwd, log_path, environment):
        alive.update({8123, 8124, 8125, 8126})
        return 8123

    def terminator(pid: int) -> None:
        terminated.append(pid)
        if pid not in alive:
            raise ProcessLookupError(pid)
        alive.discard(pid)

    host = WindowsResidentHost(
        config,
        environment={},
        launcher=launcher,
        process_probe=lambda pid: pid in alive,
        terminator=terminator,
        process_tree_resolver=lambda pid: [8123, 8124, 8125, 8126],
    )
    host.start()

    status = host.stop()

    assert status.running is False
    assert terminated == [8123, 8124, 8125, 8126]
    assert alive == set()
    assert not config.state_file.exists()


def test_stop_ignores_descendant_that_exits_after_tree_snapshot(tmp_path: Path):
    config = _config(tmp_path)
    alive = {9100, 9101}
    terminated: list[int] = []

    def terminator(pid: int) -> None:
        terminated.append(pid)
        if pid not in alive:
            raise ProcessLookupError(pid)
        alive.remove(pid)

    host = WindowsResidentHost(
        config,
        environment={},
        launcher=lambda *args: 9100,
        process_probe=lambda pid: pid in alive,
        process_tree_resolver=lambda pid: [9100, 9101, 9102],
        terminator=terminator,
    )
    host.start()

    status = host.stop()

    assert status.running is False
    assert terminated == [9100, 9101, 9102]
    assert alive == set()
    assert not config.state_file.exists()


def test_model_mode_validates_runtime_environment_before_launch(tmp_path: Path):
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("", encoding="utf-8")
    config = _config(tmp_path, reasoner="model", env_file=empty_env)
    launches: list[list[str]] = []
    host = WindowsResidentHost(
        config,
        environment={},
        launcher=lambda argv, cwd, log_path, environment: launches.append(list(argv)) or 42,
        process_probe=lambda pid: False,
        terminator=lambda pid: None,
    )

    with pytest.raises(ValueError, match="HIKARI_MODEL_BASE_URL"):
        host.start()

    assert launches == []
    assert not config.state_file.exists()


def test_invalid_host_configuration_fails_before_process_control(tmp_path: Path):
    repository = tmp_path / "missing"
    with pytest.raises(ValueError, match="existing directory"):
        ResidentHostConfig(
            repository=repository,
            memory_path=tmp_path / "memory.db",
            state_dir=tmp_path / "state",
        )
