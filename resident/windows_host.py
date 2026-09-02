from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

from .app import build_reasoner
from .environment import load_runtime_environment
from .windows_process_tree import snapshot_windows_process_tree


class WindowsResidentHostUnavailable(RuntimeError):
    """Raised when the real detached Windows host is requested off Windows."""


@dataclass(frozen=True)
class ResidentHostConfig:
    """Caller-owned process settings for one background Hikari resident."""

    repository: Path
    memory_path: Path
    state_dir: Path
    interval: float = 2.0
    output: str = "windows"
    reasoner: str = "model"
    env_file: Path | None = None

    def __post_init__(self) -> None:
        repository = Path(self.repository).expanduser().resolve()
        memory_path = Path(self.memory_path).expanduser().resolve()
        state_dir = Path(self.state_dir).expanduser().resolve()
        env_file = (
            Path(self.env_file).expanduser().resolve()
            if self.env_file is not None
            else None
        )
        interval = float(self.interval)

        if not repository.is_dir():
            raise ValueError(f"resident repository must be an existing directory: {repository}")
        if interval <= 0:
            raise ValueError("resident interval must be > 0")
        if self.output not in {"console", "windows"}:
            raise ValueError("resident output must be 'console' or 'windows'")
        if self.reasoner not in {"simple", "model"}:
            raise ValueError("resident reasoner must be 'simple' or 'model'")

        object.__setattr__(self, "repository", repository)
        object.__setattr__(self, "memory_path", memory_path)
        object.__setattr__(self, "state_dir", state_dir)
        object.__setattr__(self, "env_file", env_file)
        object.__setattr__(self, "interval", interval)

    @property
    def state_file(self) -> Path:
        return self.state_dir / "host.json"

    @property
    def log_file(self) -> Path:
        return self.state_dir / "resident.log"


@dataclass(frozen=True)
class HostState:
    pid: int
    started_at: str
    repository: str
    log_path: str

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "HostState":
        pid = data.get("pid")
        started_at = data.get("started_at")
        repository = data.get("repository")
        log_path = data.get("log_path")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            raise ValueError("host state requires a positive integer pid")
        if not isinstance(started_at, str) or not started_at.strip():
            raise ValueError("host state requires started_at")
        if not isinstance(repository, str) or not repository.strip():
            raise ValueError("host state requires repository")
        if not isinstance(log_path, str) or not log_path.strip():
            raise ValueError("host state requires log_path")
        return cls(
            pid=pid,
            started_at=started_at,
            repository=repository,
            log_path=log_path,
        )


@dataclass(frozen=True)
class HostStatus:
    running: bool
    state: HostState | None = None
    reason: str = "stopped"


@dataclass(frozen=True)
class HostStartResult:
    started: bool
    status: HostStatus


Launcher = Callable[[list[str], Path, Path, Mapping[str, str]], int]
ProcessProbe = Callable[[int], bool]
Terminator = Callable[[int], None]
ProcessTreeResolver = Callable[[int], Sequence[int]]


def default_state_dir(environment: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environment is None else environment
    local_app_data = env.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data) / "Hikari" / "resident"
    return Path.home() / ".hikari" / "resident"


def _select_background_python(
    python_executable: str,
    *,
    platform_name: str | None = None,
) -> str:
    """Prefer the windowless interpreter for a detached Windows resident.

    Windows Terminal can surface a new console window even when a console
    interpreter is launched with detached creation flags. A venv ships a
    sibling ``pythonw.exe`` specifically for GUI/background processes. Using it
    keeps the resident independent of any visible console while stdout/stderr
    remain explicitly redirected to the host log.
    """

    platform_name = os.name if platform_name is None else platform_name
    executable = Path(python_executable)
    if platform_name != "nt":
        return str(executable)

    if executable.name.lower() not in {"python.exe", "pythonw.exe"}:
        return str(executable)

    pythonw = executable.with_name("pythonw.exe")
    if pythonw.is_file():
        return str(pythonw)
    return str(executable)


def _select_runtime_child_python(python_executable: str) -> str:
    """Keep supervised children inside the selected virtual environment.

    The Windows background interpreter is normally ``pythonw.exe``. Children
    should use its sibling ``python.exe`` explicitly instead of rediscovering
    an interpreter through ``sys.executable`` after a Windows launcher hop.
    """

    executable = Path(python_executable)
    if executable.name.lower() == "pythonw.exe":
        python = executable.with_name("python.exe")
        if python.is_file():
            return str(python)
    return str(executable)


def _default_launcher(
    argv: list[str],
    cwd: Path,
    log_path: Path,
    environment: Mapping[str, str],
) -> int:
    if os.name != "nt":
        raise WindowsResidentHostUnavailable(
            "detached resident hosting is only available on Windows"
        )

    creationflags = 0
    creationflags |= int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    creationflags |= int(getattr(subprocess, "DETACHED_PROCESS", 0))
    creationflags |= int(getattr(subprocess, "CREATE_NO_WINDOW", 0))

    with log_path.open("ab") as log_handle:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            shell=False,
            close_fds=True,
            creationflags=creationflags,
        )
    return int(process.pid)


def _default_process_probe(pid: int) -> bool:
    """Check a Windows PID without sending it any signal."""

    if os.name != "nt":
        raise WindowsResidentHostUnavailable(
            "resident process probing is only available on Windows"
        )

    import ctypes
    from ctypes import wintypes

    synchronize = 0x00100000
    wait_timeout = 0x00000102
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(synchronize, False, pid)
    if not handle:
        return False
    try:
        return int(kernel32.WaitForSingleObject(handle, 0)) == wait_timeout
    finally:
        kernel32.CloseHandle(handle)


def _default_terminator(pid: int) -> None:
    if os.name != "nt":
        raise WindowsResidentHostUnavailable(
            "resident process termination is only available on Windows"
        )

    import ctypes
    from ctypes import wintypes

    process_terminate = 0x0001
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(process_terminate, False, pid)
    if not handle:
        raise ProcessLookupError(pid)
    try:
        if not kernel32.TerminateProcess(handle, 0):
            error = ctypes.get_last_error()
            raise OSError(error, f"failed to terminate resident process {pid}")
    finally:
        kernel32.CloseHandle(handle)


class WindowsResidentHost:
    """Own only the Windows process lifecycle around Hikari's resident app."""

    def __init__(
        self,
        config: ResidentHostConfig,
        *,
        environment: Mapping[str, str] | None = None,
        launcher: Launcher | None = None,
        process_probe: ProcessProbe | None = None,
        terminator: Terminator | None = None,
        process_tree_resolver: ProcessTreeResolver | None = None,
        python_executable: str | None = None,
    ) -> None:
        if not isinstance(config, ResidentHostConfig):
            raise TypeError("WindowsResidentHost requires ResidentHostConfig")
        self.config = config
        self.environment = dict(os.environ if environment is None else environment)
        self._launcher = launcher or _default_launcher
        self._process_probe = process_probe or _default_process_probe
        self._terminator = terminator or _default_terminator
        self._process_tree_resolver = process_tree_resolver or snapshot_windows_process_tree
        selected_python = (python_executable or sys.executable).strip()
        if not selected_python:
            raise ValueError("python_executable must not be empty")
        self.python_executable = _select_background_python(selected_python)

    def child_argv(self) -> list[str]:
        """Build the exact shell-free argv used for the background resident."""

        argv = [
            self.python_executable,
            "-m",
            "resident.app",
            str(self.config.repository),
            "--db",
            str(self.config.memory_path),
            "--interval",
            str(self.config.interval),
            "--output",
            self.config.output,
            "--reasoner",
            self.config.reasoner,
        ]
        if self.config.env_file is not None:
            argv.extend(["--env-file", str(self.config.env_file)])
        return argv

    def status(self) -> HostStatus:
        state = self._read_state()
        if state is None:
            return HostStatus(running=False, reason="stopped")

        if self._process_probe(state.pid):
            return HostStatus(running=True, state=state, reason="running")

        self._remove_state()
        return HostStatus(running=False, reason="stale_state")

    def start(self) -> HostStartResult:
        current = self.status()
        if current.running:
            return HostStartResult(started=False, status=current)

        runtime_environment = load_runtime_environment(
            env_file=self.config.env_file,
            environment=self.environment,
        )
        runtime_environment.values["HIKARI_RUNTIME_PYTHON"] = (
            _select_runtime_child_python(self.python_executable)
        )
        build_reasoner(
            self.config.reasoner,
            environment=runtime_environment.values,
        )

        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        pid = self._launcher(
            self.child_argv(),
            self.config.repository,
            self.config.log_file,
            runtime_environment.values,
        )
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            raise RuntimeError("resident launcher returned an invalid pid")

        state = HostState(
            pid=pid,
            started_at=datetime.now(timezone.utc).isoformat(),
            repository=str(self.config.repository),
            log_path=str(self.config.log_file),
        )
        self._write_state(state)
        return HostStartResult(
            started=True,
            status=HostStatus(running=True, state=state, reason="started"),
        )

    def stop(self) -> HostStatus:
        current = self.status()
        if not current.running or current.state is None:
            return current

        root_pid = current.state.pid
        try:
            process_tree = [
                int(pid)
                for pid in self._process_tree_resolver(root_pid)
                if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0
            ]
            if root_pid not in process_tree:
                process_tree.insert(0, root_pid)

            seen: set[int] = set()
            for pid in process_tree:
                if pid in seen:
                    continue
                seen.add(pid)
                try:
                    self._terminator(pid)
                except ProcessLookupError:
                    pass
        finally:
            self._remove_state()
        return HostStatus(running=False, reason="stopped")

    def _read_state(self) -> HostState | None:
        path = self.config.state_file
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("host state must be a JSON object")
            return HostState.from_mapping(data)
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            self._remove_state()
            return None

    def _write_state(self, state: HostState) -> None:
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.config.state_file.with_suffix(".tmp")
        payload = json.dumps(asdict(state), ensure_ascii=False, indent=2)
        temporary.write_text(payload + "\n", encoding="utf-8", newline="\n")
        temporary.replace(self.config.state_file)

    def _remove_state(self) -> None:
        try:
            self.config.state_file.unlink(missing_ok=True)
        except OSError:
            pass


def _add_state_dir_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--state-dir",
        default=None,
        help="host 状态/日志目录（默认：LOCALAPPDATA/Hikari/resident）",
    )


def _add_env_file_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--env-file",
        default=None,
        help="dotenv 配置文件；当前进程中的同名环境变量优先",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hikari-resident",
        description="管理 Hikari 的 Windows 后台驻留进程。",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="后台启动 Hikari")
    start.add_argument("repository", nargs="?", default=".")
    start.add_argument("--db", default=None)
    start.add_argument("--interval", type=float, default=2.0)
    start.add_argument("--output", choices=("console", "windows"), default="windows")
    start.add_argument("--reasoner", choices=("simple", "model"), default="model")
    _add_state_dir_argument(start)
    _add_env_file_argument(start)

    status = subparsers.add_parser("status", help="查看后台状态")
    _add_state_dir_argument(status)

    stop = subparsers.add_parser("stop", help="停止后台 Hikari")
    _add_state_dir_argument(stop)

    doctor = subparsers.add_parser("doctor", help="检查 Python/CLI/runtime env，不显示密钥")
    _add_env_file_argument(doctor)
    return parser


def _state_dir_from_args(value: str | None) -> Path:
    return Path(value).expanduser().resolve() if value else default_state_dir().resolve()


def _host_for_cli(args: argparse.Namespace) -> WindowsResidentHost:
    state_dir = _state_dir_from_args(args.state_dir)
    if args.command == "start":
        memory_path = (
            Path(args.db).expanduser().resolve()
            if args.db
            else (state_dir / "memory.db").resolve()
        )
        config = ResidentHostConfig(
            repository=Path(args.repository),
            memory_path=memory_path,
            state_dir=state_dir,
            interval=args.interval,
            output=args.output,
            reasoner=args.reasoner,
            env_file=Path(args.env_file) if args.env_file else None,
        )
    else:
        config = ResidentHostConfig(
            repository=Path.cwd(),
            memory_path=state_dir / "memory.db",
            state_dir=state_dir,
            reasoner="simple",
        )
    return WindowsResidentHost(config)


def _doctor(env_file: str | None) -> int:
    try:
        runtime_environment = load_runtime_environment(env_file=env_file)
    except ValueError as exc:
        print(f"Hikari 环境检查失败：{exc}")
        return 2

    print(f"Python：{Path(sys.executable).resolve()}")
    print(f"CLI：{Path(sys.argv[0]).resolve()}")
    if runtime_environment.env_file is None:
        print("Env：未找到 .env；仅使用当前进程环境")
    else:
        print(f"Env：{runtime_environment.env_file}")

    presence = runtime_environment.model_presence()
    for key in ("HIKARI_MODEL_BASE_URL", "HIKARI_MODEL_NAME", "HIKARI_MODEL_API_KEY"):
        print(f"{key}：{'已配置' if presence[key] else '未配置'}")

    return 0 if presence["HIKARI_MODEL_BASE_URL"] and presence["HIKARI_MODEL_NAME"] else 1


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "doctor":
        return _doctor(args.env_file)

    host = _host_for_cli(args)

    if args.command == "start":
        try:
            result = host.start()
        except (ValueError, WindowsResidentHostUnavailable) as exc:
            print(f"Hikari 后台启动失败：{exc}")
            return 2
        state = result.status.state
        if result.started and state is not None:
            print(f"Hikari 已在后台运行。PID={state.pid}")
            print(f"日志：{state.log_path}")
        elif state is not None:
            print(f"Hikari 已经在后台运行。PID={state.pid}")
        return 0

    if args.command == "status":
        status = host.status()
        if status.running and status.state is not None:
            print(f"Hikari 正在后台运行。PID={status.state.pid}")
            print(f"日志：{status.state.log_path}")
            return 0
        print("Hikari 当前没有在后台运行。")
        return 1

    status = host.stop()
    if status.running:
        print("Hikari 仍在后台运行。")
        return 2
    print("Hikari 已停止后台驻留。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
