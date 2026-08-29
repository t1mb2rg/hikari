from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import sys
import time

from core.runtime import ResidentPresenceRuntime
from conversation.remote import ConversationWebSocketHost
from websockets.asyncio.server import serve


ProcessFactory = Callable[..., subprocess.Popen[bytes]]
Clock = Callable[[], float]


def runtime_bool(
    environment: Mapping[str, str],
    name: str,
    *,
    default: bool,
) -> bool:
    """Parse one explicit runtime boolean without accepting ambiguous values."""

    raw = environment.get(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be one of: 1/0, true/false, yes/no, on/off")


@dataclass(frozen=True)
class QQBridgeProcessConfig:
    """Process-only settings for Hikari's own QQ transport child."""

    repository: Path
    log_path: Path
    environment: Mapping[str, str]
    env_file: Path | None = None
    python_executable: str = sys.executable
    restart_initial_seconds: float = 1.0
    restart_max_seconds: float = 30.0
    stable_reset_seconds: float = 30.0

    def __post_init__(self) -> None:
        repository = Path(self.repository).expanduser().resolve()
        log_path = Path(self.log_path).expanduser().resolve()
        env_file = (
            Path(self.env_file).expanduser().resolve()
            if self.env_file is not None
            else None
        )
        python_executable = str(self.python_executable).strip()
        initial = float(self.restart_initial_seconds)
        maximum = float(self.restart_max_seconds)
        stable = float(self.stable_reset_seconds)

        if not repository.is_dir():
            raise ValueError(f"QQ bridge repository must exist: {repository}")
        if env_file is not None and not env_file.is_file():
            raise ValueError(f"QQ bridge env file does not exist: {env_file}")
        if not python_executable:
            raise ValueError("QQ bridge python_executable must not be empty")
        if initial <= 0 or maximum <= 0 or stable <= 0:
            raise ValueError("QQ bridge restart timing values must be > 0")
        if maximum < initial:
            raise ValueError("QQ bridge restart_max_seconds must be >= restart_initial_seconds")

        object.__setattr__(self, "repository", repository)
        object.__setattr__(self, "log_path", log_path)
        object.__setattr__(self, "env_file", env_file)
        object.__setattr__(self, "python_executable", python_executable)
        object.__setattr__(self, "restart_initial_seconds", initial)
        object.__setattr__(self, "restart_max_seconds", maximum)
        object.__setattr__(self, "stable_reset_seconds", stable)
        object.__setattr__(self, "environment", dict(self.environment))

    def argv(self) -> list[str]:
        argv = [
            self.python_executable,
            "-m",
            "integrations.qq_bridge.app",
        ]
        if self.env_file is not None:
            argv.extend(["--env-file", str(self.env_file)])
        return argv


class QQBridgeSupervisor:
    """Restart only Hikari's QQ bridge child, never NapCat or QQ itself."""

    def __init__(
        self,
        config: QQBridgeProcessConfig,
        *,
        process_factory: ProcessFactory = subprocess.Popen,
        clock: Clock = time.monotonic,
    ) -> None:
        if not isinstance(config, QQBridgeProcessConfig):
            raise TypeError("QQBridgeSupervisor requires QQBridgeProcessConfig")
        self.config = config
        self._process_factory = process_factory
        self._clock = clock
        self.process: subprocess.Popen[bytes] | None = None
        self.restart_count = 0

    @property
    def pid(self) -> int | None:
        process = self.process
        if process is None or process.poll() is not None:
            return None
        return int(process.pid)

    def _creationflags(self) -> int:
        if os.name != "nt":
            return 0
        flags = 0
        flags |= int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        flags |= int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return flags

    def start_once(self) -> subprocess.Popen[bytes]:
        self.config.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.config.log_path.open("ab") as log_handle:
            process = self._process_factory(
                self.config.argv(),
                cwd=self.config.repository,
                env=dict(self.config.environment),
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                shell=False,
                close_fds=True,
                creationflags=self._creationflags(),
            )
        self.process = process
        return process

    async def run(self, stop_event: asyncio.Event) -> None:
        delay = self.config.restart_initial_seconds
        while not stop_event.is_set():
            process = self.start_once()
            started_at = self._clock()

            while process.poll() is None and not stop_event.is_set():
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=0.5)
                except TimeoutError:
                    pass

            if stop_event.is_set():
                await self._stop_process(process)
                return

            uptime = max(0.0, self._clock() - started_at)
            self.restart_count += 1
            if uptime >= self.config.stable_reset_seconds:
                delay = self.config.restart_initial_seconds

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
            except TimeoutError:
                pass
            if stop_event.is_set():
                return
            delay = min(delay * 2.0, self.config.restart_max_seconds)

    async def _stop_process(self, process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(asyncio.to_thread(process.wait), timeout=5.0)
        except TimeoutError:
            process.kill()
            await asyncio.to_thread(process.wait)


class UnifiedResidentService:
    """Own Presence and Conversation in one Hikari process.

    The optional QQ bridge remains a child integration process so NoneBot and
    OneBot stay outside Hikari cognition. The supervisor owns only that child;
    NapCat remains an external, human-managed endpoint.
    """

    def __init__(
        self,
        presence: ResidentPresenceRuntime,
        conversation_host: ConversationWebSocketHost,
        *,
        bind_host: str,
        bind_port: int,
        qq_supervisor: QQBridgeSupervisor | None = None,
    ) -> None:
        if not isinstance(presence, ResidentPresenceRuntime):
            raise TypeError("UnifiedResidentService requires ResidentPresenceRuntime")
        if not isinstance(conversation_host, ConversationWebSocketHost):
            raise TypeError("UnifiedResidentService requires ConversationWebSocketHost")
        if not bind_host.strip():
            raise ValueError("bind_host must not be empty")
        if not 0 <= int(bind_port) <= 65535:
            raise ValueError("bind_port must be between 0 and 65535")

        self.presence = presence
        self.conversation_host = conversation_host
        self.bind_host = bind_host.strip()
        self.bind_port = int(bind_port)
        self.qq_supervisor = qq_supervisor
        self.stop_event = asyncio.Event()
        self.started_event = asyncio.Event()
        self.bound_port: int | None = None

    def stop(self) -> None:
        self.stop_event.set()

    async def _presence_loop(self) -> None:
        while not self.stop_event.is_set() and self.presence.running:
            await asyncio.to_thread(self.presence.cycle_once)
            if self.stop_event.is_set() or not self.presence.running:
                return
            try:
                await asyncio.wait_for(
                    self.stop_event.wait(),
                    timeout=self.presence.poll_interval,
                )
            except TimeoutError:
                pass

    async def run(self) -> None:
        print(self.presence.start(), flush=True)
        tasks: list[asyncio.Task[None]] = []
        try:
            async with serve(
                self.conversation_host.handle,
                self.bind_host,
                self.bind_port,
                max_size=1024 * 1024,
                ping_interval=20,
                ping_timeout=20,
            ) as server:
                if server.sockets:
                    self.bound_port = int(server.sockets[0].getsockname()[1])
                else:
                    self.bound_port = self.bind_port
                self.started_event.set()

                tasks.append(asyncio.create_task(self._presence_loop()))
                if self.qq_supervisor is not None:
                    tasks.append(
                        asyncio.create_task(self.qq_supervisor.run(self.stop_event))
                    )

                stop_waiter = asyncio.create_task(self.stop_event.wait())
                wait_set: set[asyncio.Task[object]] = {stop_waiter}
                wait_set.update(tasks)  # type: ignore[arg-type]
                done, _ = await asyncio.wait(
                    wait_set,
                    return_when=asyncio.FIRST_COMPLETED,
                )

                for task in done:
                    if task is stop_waiter:
                        continue
                    exception = task.exception()
                    if exception is not None:
                        raise exception
                self.stop_event.set()
                await stop_waiter
        finally:
            self.stop_event.set()
            self.presence.stop()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            print("Hikari 休息了。", flush=True)
