from __future__ import annotations

import json
from pathlib import Path

from resident.windows_host import ResidentHostConfig, WindowsResidentHost


def test_model_host_loads_env_file_into_child_without_persisting_secret(tmp_path: Path):
    repository = tmp_path / "repo"
    repository.mkdir()
    env_file = tmp_path / "hikari.env"
    env_file.write_text(
        "HIKARI_MODEL_BASE_URL=https://example.invalid\n"
        "HIKARI_MODEL_NAME=env-model\n"
        "HIKARI_MODEL_API_KEY=env-secret\n",
        encoding="utf-8",
    )
    config = ResidentHostConfig(
        repository=repository,
        memory_path=tmp_path / "state" / "memory.db",
        state_dir=tmp_path / "state",
        output="windows",
        reasoner="model",
        env_file=env_file,
    )
    launches: list[tuple[list[str], dict[str, str]]] = []

    def launcher(argv, cwd, log_path, environment):
        launches.append((list(argv), dict(environment)))
        return 4242

    host = WindowsResidentHost(
        config,
        environment={"PATH": "test"},
        launcher=launcher,
        process_probe=lambda pid: False,
        terminator=lambda pid: None,
        python_executable="python-test",
    )

    result = host.start()

    assert result.started is True
    assert len(launches) == 1
    argv, child_environment = launches[0]
    assert argv[argv.index("--env-file") + 1] == str(env_file.resolve())
    assert child_environment["HIKARI_MODEL_BASE_URL"] == "https://example.invalid"
    assert child_environment["HIKARI_MODEL_NAME"] == "env-model"
    assert child_environment["HIKARI_MODEL_API_KEY"] == "env-secret"

    state_text = config.state_file.read_text(encoding="utf-8")
    assert "env-secret" not in state_text
    assert "HIKARI_MODEL_API_KEY" not in state_text
    assert "env_file" not in json.loads(state_text)


def test_process_env_wins_over_host_env_file(tmp_path: Path):
    repository = tmp_path / "repo"
    repository.mkdir()
    env_file = tmp_path / "hikari.env"
    env_file.write_text(
        "HIKARI_MODEL_BASE_URL=https://file.invalid\n"
        "HIKARI_MODEL_NAME=file-model\n",
        encoding="utf-8",
    )
    config = ResidentHostConfig(
        repository=repository,
        memory_path=tmp_path / "memory.db",
        state_dir=tmp_path / "state",
        reasoner="model",
        env_file=env_file,
    )
    launched: list[dict[str, str]] = []

    host = WindowsResidentHost(
        config,
        environment={
            "HIKARI_MODEL_BASE_URL": "https://process.invalid",
            "HIKARI_MODEL_NAME": "process-model",
        },
        launcher=lambda argv, cwd, log_path, environment: launched.append(dict(environment)) or 5000,
        process_probe=lambda pid: False,
        terminator=lambda pid: None,
    )

    host.start()

    assert launched[0]["HIKARI_MODEL_BASE_URL"] == "https://process.invalid"
    assert launched[0]["HIKARI_MODEL_NAME"] == "process-model"
