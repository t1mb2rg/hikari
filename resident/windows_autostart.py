from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

from .app import build_reasoner
from .environment import load_runtime_environment
from .windows_host import ResidentHostConfig, WindowsResidentHost, default_state_dir


RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = "Hikari Resident"
AUTOSTART_CONFIG_VERSION = 1


class WindowsAutostartUnavailable(RuntimeError):
    """Raised when real Windows login-autostart control is requested elsewhere."""


@dataclass(frozen=True)
class AutostartConfig:
    """Caller-owned settings persisted without model secret values."""

    repository: Path
    state_dir: Path
    interval: float = 2.0
    output: str = "windows"
    reasoner: str = "model"
    env_file: Path | None = None
    python_executable: str = sys.executable

    def __post_init__(self) -> None:
        repository = Path(self.repository).expanduser().resolve()
        state_dir = Path(self.state_dir).expanduser().resolve()
        env_file = (
            Path(self.env_file).expanduser().resolve()
            if self.env_file is not None
            else None
        )
        interval = float(self.interval)
        python_executable = str(self.python_executable).strip()

        if not repository.is_dir():
            raise ValueError(f"autostart repository must be an existing directory: {repository}")
        if interval <= 0:
            raise ValueError("autostart interval must be > 0")
        if self.output not in {"console", "windows"}:
            raise ValueError("autostart output must be 'console' or 'windows'")
        if self.reasoner not in {"simple", "model"}:
            raise ValueError("autostart reasoner must be 'simple' or 'model'")
        if not python_executable:
            raise ValueError("python_executable must not be empty")
        if env_file is not None and not env_file.is_file():
            raise ValueError(f"autostart env file does not exist: {env_file}")
        if self.reasoner == "model" and env_file is None:
            raise ValueError("model autostart requires an explicit --env-file")

        object.__setattr__(self, "repository", repository)
        object.__setattr__(self, "state_dir", state_dir)
        object.__setattr__(self, "env_file", env_file)
        object.__setattr__(self, "interval", interval)
        object.__setattr__(self, "python_executable", python_executable)

    @property
    def windowless_python(self) -> str:
        executable = Path(self.python_executable)
        if executable.name.lower() in {"python.exe", "pythonw.exe"}:
            pythonw = executable.with_name("pythonw.exe")
            if pythonw.is_file():
                return str(pythonw)
        return str(executable)

    def to_mapping(self) -> dict[str, object]:
        return {
            "version": AUTOSTART_CONFIG_VERSION,
            "repository": str(self.repository),
            "state_dir": str(self.state_dir),
            "interval": self.interval,
            "output": self.output,
            "reasoner": self.reasoner,
            "env_file": str(self.env_file) if self.env_file is not None else None,
            "python_executable": self.python_executable,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "AutostartConfig":
        if data.get("version") != AUTOSTART_CONFIG_VERSION:
            raise ValueError("unsupported autostart config version")
        repository = data.get("repository")
        state_dir = data.get("state_dir")
        interval = data.get("interval", 2.0)
        output = data.get("output", "windows")
        reasoner = data.get("reasoner", "model")
        env_file = data.get("env_file")
        python_executable = data.get("python_executable")
        if not isinstance(repository, str) or not repository.strip():
            raise ValueError("autostart config requires repository")
        if not isinstance(state_dir, str) or not state_dir.strip():
            raise ValueError("autostart config requires state_dir")
        if not isinstance(output, str) or not isinstance(reasoner, str):
            raise ValueError("autostart config requires string output/reasoner")
        if env_file is not None and not isinstance(env_file, str):
            raise ValueError("autostart config env_file must be a string or null")
        if not isinstance(python_executable, str) or not python_executable.strip():
            raise ValueError("autostart config requires python_executable")
        return cls(
            repository=Path(repository),
            state_dir=Path(state_dir),
            interval=float(interval),
            output=output,
            reasoner=reasoner,
            env_file=Path(env_file) if env_file else None,
            python_executable=python_executable,
        )


RegistrationReader = Callable[[], str | None]
RegistrationWriter = Callable[[str], None]
RegistrationDeleter = Callable[[], bool]
ConfigLauncher = Callable[[AutostartConfig], None]


def default_autostart_config_path(
    environment: Mapping[str, str] | None = None,
) -> Path:
    env = os.environ if environment is None else environment
    local_app_data = env.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data) / "Hikari" / "autostart.json"
    return Path.home() / ".hikari" / "autostart.json"


def _default_registration_reader() -> str | None:
    if os.name != "nt":
        raise WindowsAutostartUnavailable(
            "Windows login autostart is only available on Windows"
        )

    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            RUN_KEY_PATH,
            0,
            winreg.KEY_QUERY_VALUE,
        ) as key:
            value, value_type = winreg.QueryValueEx(key, RUN_VALUE_NAME)
    except FileNotFoundError:
        return None
    if value_type != winreg.REG_SZ or not isinstance(value, str):
        return None
    return value


def _default_registration_writer(command: str) -> None:
    if os.name != "nt":
        raise WindowsAutostartUnavailable(
            "Windows login autostart is only available on Windows"
        )

    import winreg

    with winreg.CreateKeyEx(
        winreg.HKEY_CURRENT_USER,
        RUN_KEY_PATH,
        0,
        winreg.KEY_SET_VALUE,
    ) as key:
        winreg.SetValueEx(key, RUN_VALUE_NAME, 0, winreg.REG_SZ, command)


def _default_registration_deleter() -> bool:
    if os.name != "nt":
        raise WindowsAutostartUnavailable(
            "Windows login autostart is only available on Windows"
        )

    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            RUN_KEY_PATH,
            0,
            winreg.KEY_SET_VALUE,
        ) as key:
            winreg.DeleteValue(key, RUN_VALUE_NAME)
    except FileNotFoundError:
        return False
    return True


def _launch_config(config: AutostartConfig) -> None:
    host_config = ResidentHostConfig(
        repository=config.repository,
        memory_path=config.state_dir / "memory.db",
        state_dir=config.state_dir,
        interval=config.interval,
        output=config.output,
        reasoner=config.reasoner,
        env_file=config.env_file,
    )
    WindowsResidentHost(
        host_config,
        python_executable=config.python_executable,
    ).start()


class WindowsLoginAutostart:
    """Explicit, reversible current-user login registration through HKCU Run.

    Task Scheduler creation is not a reliable standard-user boundary on modern
    Windows: local task registration can require administrator permission even
    when the task itself is configured for limited privileges. HKCU Run is the
    native per-user logon boundary we need here: it requires no elevation, runs
    only after this user signs in, and is independently removable.
    """

    def __init__(
        self,
        *,
        config_path: str | Path | None = None,
        registration_reader: RegistrationReader | None = None,
        registration_writer: RegistrationWriter | None = None,
        registration_deleter: RegistrationDeleter | None = None,
        launcher: ConfigLauncher | None = None,
    ) -> None:
        self.config_path = (
            Path(config_path).expanduser().resolve()
            if config_path is not None
            else default_autostart_config_path().resolve()
        )
        self._registration_reader = registration_reader or _default_registration_reader
        self._registration_writer = registration_writer or _default_registration_writer
        self._registration_deleter = registration_deleter or _default_registration_deleter
        self._launcher = launcher or _launch_config

    def registration_command(self, config: AutostartConfig) -> str:
        argv = [
            config.windowless_python,
            "-m",
            "resident.windows_autostart",
            "launch",
            "--config",
            str(self.config_path),
        ]
        return subprocess.list2cmdline(argv)

    def status(self) -> bool:
        value = self._registration_reader()
        return bool(value and self.config_path.is_file())

    def install(self, config: AutostartConfig) -> None:
        runtime_environment = load_runtime_environment(
            env_file=config.env_file,
            environment={},
        )
        build_reasoner(
            config.reasoner,
            environment=runtime_environment.values,
        )

        self._write_config(config)
        try:
            self._registration_writer(self.registration_command(config))
        except Exception:
            self._remove_config()
            raise

    def run_now(self) -> None:
        config = self._read_config()
        if config is None:
            raise RuntimeError("Hikari autostart config is missing or invalid")
        self._launcher(config)

    def uninstall(self) -> bool:
        removed = self._registration_deleter()
        self._remove_config()
        return removed

    def _read_config(self) -> AutostartConfig | None:
        if not self.config_path.is_file():
            return None
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("autostart config must be an object")
            return AutostartConfig.from_mapping(data)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return None

    def _write_config(self, config: AutostartConfig) -> None:
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.config_path.with_suffix(".tmp")
        payload = json.dumps(config.to_mapping(), ensure_ascii=False, indent=2)
        temporary.write_text(payload + "\n", encoding="utf-8", newline="\n")
        temporary.replace(self.config_path)

    def _remove_config(self) -> None:
        try:
            self.config_path.unlink(missing_ok=True)
        except OSError:
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hikari-autostart",
        description="管理 Hikari 的 Windows 登录自启动。",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    install = subparsers.add_parser("install", help="显式注册登录自启动")
    install.add_argument("repository", nargs="?", default=".")
    install.add_argument("--interval", type=float, default=2.0)
    install.add_argument("--output", choices=("console", "windows"), default="windows")
    install.add_argument("--reasoner", choices=("simple", "model"), default="model")
    install.add_argument("--env-file", default=None)
    install.add_argument("--state-dir", default=None)

    subparsers.add_parser("status", help="查看登录自启动是否已注册")
    subparsers.add_parser("run-now", help="立即按已保存配置启动一次")
    subparsers.add_parser("uninstall", help="移除登录自启动")

    launch = subparsers.add_parser("launch", help=argparse.SUPPRESS)
    launch.add_argument("--config", required=True)
    return parser


def _state_dir(value: str | None) -> Path:
    if value:
        return Path(value).expanduser().resolve()
    return default_state_dir().resolve()


def _launch_from_path(path: str | Path) -> int:
    autostart = WindowsLoginAutostart(config_path=path)
    config = autostart._read_config()
    if config is None:
        return 2
    try:
        autostart._launcher(config)
    except (ValueError, RuntimeError, WindowsAutostartUnavailable):
        return 2
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "launch":
        return _launch_from_path(args.config)

    autostart = WindowsLoginAutostart()

    try:
        if args.command == "install":
            config = AutostartConfig(
                repository=Path(args.repository),
                state_dir=_state_dir(args.state_dir),
                interval=args.interval,
                output=args.output,
                reasoner=args.reasoner,
                env_file=Path(args.env_file) if args.env_file else None,
            )
            autostart.install(config)
            print("Hikari 登录自启动已注册。")
            return 0

        if args.command == "status":
            if autostart.status():
                print("Hikari 登录自启动已注册。")
                return 0
            print("Hikari 登录自启动尚未注册。")
            return 1

        if args.command == "run-now":
            if not autostart.status():
                print("Hikari 登录自启动尚未注册。")
                return 1
            autostart.run_now()
            print("已按登录自启动配置请求 Hikari 启动。")
            return 0

        removed = autostart.uninstall()
        if removed:
            print("Hikari 登录自启动已移除。")
        else:
            print("Hikari 登录自启动原本就未注册。")
        return 0
    except (OSError, ValueError, RuntimeError, WindowsAutostartUnavailable) as exc:
        print(f"Hikari 登录自启动操作失败：{exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
