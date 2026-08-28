from __future__ import annotations

from pathlib import Path

import pytest

from resident.windows_autostart import (
    AutostartConfig,
    CommandResult,
    TASK_NAME,
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


def test_install_registers_current_user_logon_task_without_secret_in_action(tmp_path: Path):
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
    commands: list[list[str]] = []

    def runner(argv: list[str]) -> CommandResult:
        commands.append(list(argv))
        return CommandResult(0)

    config = AutostartConfig(
        repository=repository,
        state_dir=tmp_path / "state",
        interval=1.0,
        output="windows",
        reasoner="model",
        env_file=env_file,
        python_executable=str(python),
    )
    WindowsLoginAutostart(runner=runner).install(config)

    assert len(commands) == 1
    command = commands[0]
    assert command[:4] == ["schtasks.exe", "/Create", "/TN", TASK_NAME]
    assert command[command.index("/SC") + 1] == "ONLOGON"
    assert command[command.index("/RL") + 1] == "LIMITED"
    assert "/IT" in command
    assert "/F" in command

    action = command[command.index("/TR") + 1]
    assert str(pythonw) in action
    assert "resident.windows_host" in action
    assert "start" in action
    assert str(repository.resolve()) in action
    assert str(env_file.resolve()) in action
    assert secret not in action
    assert secret not in " ".join(command)


def test_model_autostart_requires_explicit_env_file(tmp_path: Path):
    with pytest.raises(ValueError, match="explicit --env-file"):
        AutostartConfig(
            repository=_repo(tmp_path),
            state_dir=tmp_path / "state",
            reasoner="model",
        )


def test_reinstall_is_idempotent_by_forcing_only_hikari_task(tmp_path: Path):
    repository = _repo(tmp_path)
    commands: list[list[str]] = []

    def runner(argv: list[str]) -> CommandResult:
        commands.append(list(argv))
        return CommandResult(0)

    config = AutostartConfig(
        repository=repository,
        state_dir=tmp_path / "state",
        reasoner="simple",
    )
    autostart = WindowsLoginAutostart(runner=runner)
    autostart.install(config)
    autostart.install(config)

    assert len(commands) == 2
    assert all(command[command.index("/TN") + 1] == TASK_NAME for command in commands)
    assert all("/F" in command for command in commands)


def test_status_distinguishes_registered_and_unregistered():
    results = iter([CommandResult(0), CommandResult(1)])
    commands: list[list[str]] = []

    def runner(argv: list[str]) -> CommandResult:
        commands.append(list(argv))
        return next(results)

    autostart = WindowsLoginAutostart(runner=runner)

    assert autostart.status() is True
    assert autostart.status() is False
    assert commands == [
        ["schtasks.exe", "/Query", "/TN", TASK_NAME],
        ["schtasks.exe", "/Query", "/TN", TASK_NAME],
    ]


def test_run_now_uses_registered_task_boundary():
    commands: list[list[str]] = []

    def runner(argv: list[str]) -> CommandResult:
        commands.append(list(argv))
        return CommandResult(0)

    WindowsLoginAutostart(runner=runner).run_now()

    assert commands == [["schtasks.exe", "/Run", "/TN", TASK_NAME]]


def test_uninstall_removes_only_hikari_task_when_registered():
    commands: list[list[str]] = []

    def runner(argv: list[str]) -> CommandResult:
        commands.append(list(argv))
        return CommandResult(0)

    removed = WindowsLoginAutostart(runner=runner).uninstall()

    assert removed is True
    assert commands == [
        ["schtasks.exe", "/Query", "/TN", TASK_NAME],
        ["schtasks.exe", "/Delete", "/TN", TASK_NAME, "/F"],
    ]


def test_uninstall_is_noop_when_task_is_absent():
    commands: list[list[str]] = []

    def runner(argv: list[str]) -> CommandResult:
        commands.append(list(argv))
        return CommandResult(1)

    removed = WindowsLoginAutostart(runner=runner).uninstall()

    assert removed is False
    assert commands == [["schtasks.exe", "/Query", "/TN", TASK_NAME]]
