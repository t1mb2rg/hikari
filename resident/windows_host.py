from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
from typing import Any

from .app import build_reasoner


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

    def __post_init__(self) -> None:
        repository = Path(self.repository).expanduser().resolve()
        memory_path = Path(self.memory_path).expanduser().resolve()
        state_dir = Path(self.state_dir).expanduser().resolve()
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


def default_state_dir(environment: Mapping[str, str] | None = None) -> Path:
    env = os.environ if environment is None else environment
    local_app_data = env.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data) / "Hikari" / "resident"
    return Path.home() / ".hikari" / "resident"


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
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def _default_terminator(pid: int) -> None:
    os.kill(pid, signal.SIGTERM)


class WindowsResidentHost:
    """Own only the Windows process lifecycle around Hikari's resident app.

    Cognition, Presence, memory, notification, and action authority remain in
    their existing layers. This host only launches one trusted child argv,
    records minimal local process state, and later probes/stops that PID.
    """

    def __init__(
        self,
        config: ResidentHostConfig,
        *,
        environment: Mapping[str, str] | None = None,
        launcher: Launcher | None = None,
        process_probe: ProcessProbe | None = None,
        terminator: Terminator | None = None,
        python_executable: str | None = None,
    ) -> None:
        if not isinstance(config, ResidentHostConfig):
            raise TypeError("WindowsResidentHost requires ResidentHostConfig")
        self.config = config
        self.environment = dict(os.environ if environment is None else environment)
        self._launcher = launcher or _default_launcher
        self._process_probe = process_probe or _default_process_probe
        self._terminator = terminator or _default_terminator
        self.python_executable = (python_executable or sys.executable).strip()
        if not self.python_executable:
            raise ValueError("python_executable must not be empty")

    def child_argv(self) -> list[str]:
        """Build the exact shell-free argv used for the background resident."""

        return [
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

        # Validate model-mode runtime configuration before detaching. This does
        # not perform a network call and never persists credentials.
        build_reasoner(self.config.reasoner, environment=self.environment)

        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        pid = self._launcher(
            self.child_argv(),
            self.config.repository,
            self.config.log_file,
            self.environment,
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

        try:
            self._terminator(current.state.pid)
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

    status = subparsers.add_parser("status", help="查看后台状态")
    _add_state_dir_argument(status)

    stop = subparsers.add_parser("stop", help="停止后台 Hikari")
    _add_state_dir_argument(stop)
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
        )
    else:
        # status/stop only need the state location. The repository/memory fields
        # are inert because no child argv is built for these commands.
        config = ResidentHostConfig(
            repository=Path.cwd(),
            memory_path=state_dir / "memory.db",
            state_dir=state_dir,
            reasoner="simple",
        )
    return WindowsResidentHost(config)


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
