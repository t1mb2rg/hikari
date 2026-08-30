from __future__ import annotations

import json
from pathlib import Path

import pytest

from resident.windows_autostart import (
    AutostartConfig,
    RUN_KEY_PATH,
    RUN_VALUE_NAME,
    WindowsLoginAutostart,
)


def _repo(tmp_path: Path) -> Path:
    repository = tmp_path / "repo"
    repository.mkdir()
    return repository


def _python_pair(tmp_path: Path) -> tuple[Path, Path]:
    scripts = tmp_path / "venv with spaces" / "Scripts"
    scripts.mkdir(parents=True)
    python = scripts / "python.exe"
    pythonw = scripts / "pythonw.exe"
    python.write_text("", encoding="utf-8")
    pythonw.write_text("", encoding="utf-8")
    return python, pythonw


def _registry_fakes():
    state: dict[str, str] = {}

    def reader() -> str | None:
        return state.get("value")

    def writer(command: str) -> None:
        state["value"] = command

    def deleter() -> bool:
        return state.pop("value", None) is not None

    return state, reader, writer, deleter


def test_install_registers_current_user_run_value_without_secret(tmp_path: Path):
    repository = _repo(tmp_path)
    python, pythonw = _python_pair(tmp_path)
    env_file = tmp_path / ".env"
    secret = "never-write-this-secret"
    env_file.write_text(
        "HIKARI_MODEL_BASE_URL=https://api.example.invalid\n"
        "HIKARI_MODEL_NAME=test-model\n"
        f"HIKARI_MODEL_API_KEY={secret}\n",
        encoding="utf-8",
    )
    state, reader, writer, deleter = _registry_fakes()
    config_path = tmp_path / "local" / "autostart.json"

    config = AutostartConfig(
        repository=repository,
        state_dir=tmp_path / "state",
        interval=1.0,
        output="windows",
        reasoner="model",
        env_file=env_file,
        python_executable=str(python),
    )
    autostart = WindowsLoginAutostart(
        config_path=config_path,
        registration_reader=reader,
        registration_writer=writer,
        registration_deleter=deleter,
    )
    autostart.install(config)

    command = state["value"]
    assert str(pythonw) in command
    assert "resident.windows_autostart" in command
    assert "launch" in command
    assert str(config_path.resolve()) in command
    assert secret not in command

    persisted_text = config_path.read_text(encoding="utf-8")
    persisted = json.loads(persisted_text)
    assert persisted["repository"] == str(repository.resolve())
    assert persisted["env_file"] == str(env_file.resolve())
    assert secret not in persisted_text
    inspected = autostart.inspect()
    assert inspected.healthy is True
    assert inspected.reason == "ready"
    assert autostart.status() is True


def test_registry_boundary_is_current_user_run_key():
    assert RUN_KEY_PATH == r"Software\Microsoft\Windows\CurrentVersion\Run"
    assert RUN_VALUE_NAME == "Hikari Resident"


def test_model_autostart_requires_explicit_env_file(tmp_path: Path):
    with pytest.raises(ValueError, match="explicit --env-file"):
        AutostartConfig(
            repository=_repo(tmp_path),
            state_dir=tmp_path / "state",
            reasoner="model",
        )


def test_install_rejects_missing_python_before_persisting(tmp_path: Path):
    repository = _repo(tmp_path)
    state, reader, writer, deleter = _registry_fakes()
    config_path = tmp_path / "autostart.json"
    config = AutostartConfig(
        repository=repository,
        state_dir=tmp_path / "state",
        reasoner="simple",
        python_executable=str(tmp_path / "missing-python.exe"),
    )
    autostart = WindowsLoginAutostart(
        config_path=config_path,
        registration_reader=reader,
        registration_writer=writer,
        registration_deleter=deleter,
    )

    with pytest.raises(ValueError, match="Python does not exist"):
        autostart.install(config)

    assert state == {}
    assert not config_path.exists()


def test_reinstall_is_idempotent_and_replaces_only_hikari_value(tmp_path: Path):
    repository = _repo(tmp_path)
    writes: list[str] = []
    state, reader, _, deleter = _registry_fakes()

    def writer(command: str) -> None:
        writes.append(command)
        state["value"] = command

    config = AutostartConfig(
        repository=repository,
        state_dir=tmp_path / "state",
        reasoner="simple",
    )
    autostart = WindowsLoginAutostart(
        config_path=tmp_path / "autostart.json",
        registration_reader=reader,
        registration_writer=writer,
        registration_deleter=deleter,
    )
    autostart.install(config)
    autostart.install(config)

    assert len(writes) == 2
    assert writes[0] == writes[1]
    assert autostart.status() is True


def test_inspect_distinguishes_missing_registration_and_missing_config(tmp_path: Path):
    state, reader, writer, deleter = _registry_fakes()
    config_path = tmp_path / "autostart.json"
    autostart = WindowsLoginAutostart(
        config_path=config_path,
        registration_reader=reader,
        registration_writer=writer,
        registration_deleter=deleter,
    )

    missing = autostart.inspect()
    assert missing.installed is False
    assert missing.healthy is False
    assert missing.reason == "missing"

    state["value"] = "registered"
    missing_config = autostart.inspect()
    assert missing_config.installed is True
    assert missing_config.healthy is False
    assert missing_config.reason == "missing_config"


def test_inspect_reports_orphan_config_when_registration_is_missing(tmp_path: Path):
    state, reader, writer, deleter = _registry_fakes()
    config_path = tmp_path / "autostart.json"
    config_path.write_text("{}", encoding="utf-8")
    autostart = WindowsLoginAutostart(
        config_path=config_path,
        registration_reader=reader,
        registration_writer=writer,
        registration_deleter=deleter,
    )

    inspected = autostart.inspect()
    assert inspected.installed is False
    assert inspected.healthy is False
    assert inspected.reason == "orphan_config"


def test_inspect_reports_stale_config_when_saved_repo_disappears(tmp_path: Path):
    repository = _repo(tmp_path)
    state, reader, writer, deleter = _registry_fakes()
    config_path = tmp_path / "autostart.json"
    config = AutostartConfig(
        repository=repository,
        state_dir=tmp_path / "state",
        reasoner="simple",
    )
    autostart = WindowsLoginAutostart(
        config_path=config_path,
        registration_reader=reader,
        registration_writer=writer,
        registration_deleter=deleter,
    )
    autostart.install(config)
    repository.rmdir()

    inspected = autostart.inspect()
    assert inspected.installed is True
    assert inspected.healthy is False
    assert inspected.reason == "stale_config"
    assert "existing directory" in inspected.detail


def test_inspect_reports_stale_python_when_saved_venv_disappears(tmp_path: Path):
    repository = _repo(tmp_path)
    python, pythonw = _python_pair(tmp_path)
    state, reader, writer, deleter = _registry_fakes()
    config_path = tmp_path / "autostart.json"
    config = AutostartConfig(
        repository=repository,
        state_dir=tmp_path / "state",
        reasoner="simple",
        python_executable=str(python),
    )
    autostart = WindowsLoginAutostart(
        config_path=config_path,
        registration_reader=reader,
        registration_writer=writer,
        registration_deleter=deleter,
    )
    autostart.install(config)
    python.unlink()
    pythonw.unlink()

    inspected = autostart.inspect()
    assert inspected.installed is True
    assert inspected.healthy is False
    assert inspected.reason == "stale_python"
    assert str(python) in inspected.detail


def test_inspect_reports_registration_mismatch(tmp_path: Path):
    repository = _repo(tmp_path)
    state, reader, writer, deleter = _registry_fakes()
    config_path = tmp_path / "autostart.json"
    config = AutostartConfig(
        repository=repository,
        state_dir=tmp_path / "state",
        reasoner="simple",
    )
    autostart = WindowsLoginAutostart(
        config_path=config_path,
        registration_reader=reader,
        registration_writer=writer,
        registration_deleter=deleter,
    )
    autostart.install(config)
    state["value"] = "pythonw.exe -m somebody.else"

    inspected = autostart.inspect()
    assert inspected.installed is True
    assert inspected.healthy is False
    assert inspected.reason == "registration_mismatch"
    assert inspected.expected_command is not None


def test_run_now_uses_saved_config_without_shell(tmp_path: Path):
    repository = _repo(tmp_path)
    state, reader, writer, deleter = _registry_fakes()
    launched: list[AutostartConfig] = []
    config_path = tmp_path / "autostart.json"
    config = AutostartConfig(
        repository=repository,
        state_dir=tmp_path / "state",
        reasoner="simple",
    )
    autostart = WindowsLoginAutostart(
        config_path=config_path,
        registration_reader=reader,
        registration_writer=writer,
        registration_deleter=deleter,
        launcher=launched.append,
    )
    autostart.install(config)

    autostart.run_now()

    assert len(launched) == 1
    assert launched[0].repository == repository.resolve()
    assert launched[0].reasoner == "simple"


def test_run_now_rejects_stale_registration(tmp_path: Path):
    repository = _repo(tmp_path)
    state, reader, writer, deleter = _registry_fakes()
    config = AutostartConfig(
        repository=repository,
        state_dir=tmp_path / "state",
        reasoner="simple",
    )
    autostart = WindowsLoginAutostart(
        config_path=tmp_path / "autostart.json",
        registration_reader=reader,
        registration_writer=writer,
        registration_deleter=deleter,
    )
    autostart.install(config)
    state["value"] = "wrong command"

    with pytest.raises(RuntimeError, match="registration_mismatch"):
        autostart.run_now()


def test_uninstall_removes_registration_and_local_config(tmp_path: Path):
    repository = _repo(tmp_path)
    state, reader, writer, deleter = _registry_fakes()
    config_path = tmp_path / "autostart.json"
    config = AutostartConfig(
        repository=repository,
        state_dir=tmp_path / "state",
        reasoner="simple",
    )
    autostart = WindowsLoginAutostart(
        config_path=config_path,
        registration_reader=reader,
        registration_writer=writer,
        registration_deleter=deleter,
    )
    autostart.install(config)

    removed = autostart.uninstall()

    assert removed is True
    assert "value" not in state
    assert not config_path.exists()
    assert autostart.status() is False


def test_uninstall_is_noop_when_registration_is_absent(tmp_path: Path):
    state, reader, writer, deleter = _registry_fakes()
    autostart = WindowsLoginAutostart(
        config_path=tmp_path / "autostart.json",
        registration_reader=reader,
        registration_writer=writer,
        registration_deleter=deleter,
    )

    assert autostart.uninstall() is False
    assert state == {}