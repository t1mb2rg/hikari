from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import sys

from .app import build_reasoner
from .environment import load_runtime_environment
from .windows_host import default_state_dir


TASK_NAME = "Hikari Resident"


class WindowsAutostartUnavailable(RuntimeError):
    """Raised when real Windows Task Scheduler control is requested elsewhere."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


CommandRunner = Callable[[list[str]], CommandResult]


@dataclass(frozen=True)
class AutostartConfig:
    """Caller-owned startup settings persisted only as a scheduled task action."""

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

    def task_action_argv(self) -> list[str]:
        argv = [
            self.windowless_python,
            "-m",
            "resident.windows_host",
            "start",
            str(self.repository),
            "--interval",
            str(self.interval),
            "--output",
            self.output,
            "--reasoner",
            self.reasoner,
            "--state-dir",
            str(self.state_dir),
        ]
        if self.env_file is not None:
            argv.extend(["--env-file", str(self.env_file)])
        return argv

    def task_action_command(self) -> str:
        return subprocess.list2cmdline(self.task_action_argv())


def _default_runner(argv: list[str]) -> CommandResult:
    if os.name != "nt":
        raise WindowsAutostartUnavailable(
            "Windows login autostart is only available on Windows"
        )

    completed = subprocess.run(
        argv,
        check=False,
        capture_output=True,
        text=True,
        shell=False,
        creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
    )
    return CommandResult(
        returncode=int(completed.returncode),
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )


class WindowsLoginAutostart:
    """Explicit, reversible user-logon registration through Task Scheduler."""

    def __init__(
        self,
        *,
        runner: CommandRunner | None = None,
        task_name: str = TASK_NAME,
    ) -> None:
        task_name = task_name.strip()
        if not task_name:
            raise ValueError("task_name must not be empty")
        self.task_name = task_name
        self._runner = runner or _default_runner

    def status(self) -> bool:
        result = self._runner(
            ["schtasks.exe", "/Query", "/TN", self.task_name]
        )
        return result.returncode == 0

    def install(self, config: AutostartConfig) -> None:
        runtime_environment = load_runtime_environment(
            env_file=config.env_file,
            environment={},
        )
        build_reasoner(
            config.reasoner,
            environment=runtime_environment.values,
        )

        result = self._runner(
            [
                "schtasks.exe",
                "/Create",
                "/TN",
                self.task_name,
                "/TR",
                config.task_action_command(),
                "/SC",
                "ONLOGON",
                "/RL",
                "LIMITED",
                "/IT",
                "/F",
            ]
        )
        if result.returncode != 0:
            raise RuntimeError(self._format_failure("register", result))

    def run_now(self) -> None:
        result = self._runner(
            ["schtasks.exe", "/Run", "/TN", self.task_name]
        )
        if result.returncode != 0:
            raise RuntimeError(self._format_failure("run", result))

    def uninstall(self) -> bool:
        if not self.status():
            return False
        result = self._runner(
            ["schtasks.exe", "/Delete", "/TN", self.task_name, "/F"]
        )
        if result.returncode != 0:
            raise RuntimeError(self._format_failure("delete", result))
        return True

    @staticmethod
    def _format_failure(operation: str, result: CommandResult) -> str:
        detail = (result.stderr or result.stdout).strip()
        if detail:
            return f"Task Scheduler {operation} failed: {detail}"
        return f"Task Scheduler {operation} failed with exit code {result.returncode}"


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
    subparsers.add_parser("run-now", help="立即触发一次已注册的任务")
    subparsers.add_parser("uninstall", help="移除登录自启动")
    return parser


def _state_dir(value: str | None) -> Path:
    if value:
        return Path(value).expanduser().resolve()
    return default_state_dir().resolve()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
            print("已请求 Task Scheduler 启动 Hikari。")
            return 0

        removed = autostart.uninstall()
        if removed:
            print("Hikari 登录自启动已移除。")
        else:
            print("Hikari 登录自启动原本就未注册。")
        return 0
    except (ValueError, RuntimeError, WindowsAutostartUnavailable) as exc:
        print(f"Hikari 登录自启动操作失败：{exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
