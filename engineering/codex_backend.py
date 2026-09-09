from __future__ import annotations

from collections import deque
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import threading
import tomllib

from .backend import EngineeringAgentEvent, EngineeringAgentResult
from .config import EngineeringBackendConfig


def codex_provider_configuration(environment: dict[str, str]) -> tuple[list[str], dict[str, str], str]:
    """Reuse the user's model route/auth, without inheriting plugins, hooks or permission overrides.

    A saved literal bearer token is translated into a per-invocation environment
    variable, never a command-line argument or a newly written credentials file.
    """
    env = {key: value for key, value in environment.items()
           if not key.startswith("CODEX_") or key in {"CODEX_HOME", "CODEX_API_KEY"}}
    home = Path(env.get("CODEX_HOME", str(Path.home() / ".codex")))
    path = home / "config.toml"
    config = tomllib.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    configured_model = str(config.get("model", "")).strip()
    provider_name = str(config.get("model_provider", "openai"))
    provider = config.get("model_providers", {}).get(provider_name, {})
    args: list[str] = []
    if provider and provider_name != "openai":
        args.extend(["-c", 'model_provider="hikari_engineering"'])
        allowed = {"name", "base_url", "wire_api", "env_key", "requires_openai_auth", "request_max_retries", "stream_max_retries", "stream_idle_timeout_ms"}
        route = {key: value for key, value in provider.items() if key in allowed}
        token = provider.get("experimental_bearer_token")
        if isinstance(token, str) and token:
            env["HIKARI_CODEX_RUNTIME_TOKEN"] = token
            route["env_key"] = "HIKARI_CODEX_RUNTIME_TOKEN"
        for key, value in route.items():
            if isinstance(value, (str, bool, int)):
                args.extend(["-c", f"model_providers.hikari_engineering.{key}={json.dumps(value)}"])
    return args, env, configured_model


class CodexEngineeringBackend:
    """Codex exec adapter with explicit sandbox, streamed evidence and scoped resume IDs."""

    def __init__(self, *, writable: bool = False, session_id: str | None = None,
                 executable: str | None = None, model: str | None = None,
                 timeout_seconds: float | None = None, event_sink=None):
        config = EngineeringBackendConfig.from_mapping(os.environ)
        self.executable = executable or os.environ.get("HIKARI_ENGINEERING_CODEX_EXECUTABLE", "codex")
        self.model = model if model is not None else config.codex_model
        self.timeout_seconds = timeout_seconds if timeout_seconds is not None else config.timeout_seconds
        if self.timeout_seconds <= 0:
            raise ValueError("Codex backend timeout must be positive")
        self.writable = writable
        self.session_id = session_id if session_id and session_id.startswith("codex:") else None
        self._event_sink = event_sink

    def set_event_sink(self, sink):
        self._event_sink = sink

    @staticmethod
    def _failure(code, detail, *, events=(), session_id=""):
        return EngineeringAgentResult(code, "", detail, "", session_id, tuple(events))

    def build_invocation(self, root: Path) -> tuple[list[str], dict[str, str]]:
        executable = shutil.which(self.executable)
        if executable is None and Path(self.executable).is_absolute() and Path(self.executable).is_file():
            executable = str(Path(self.executable).resolve())
        if executable is None:
            raise FileNotFoundError("Codex executable is not available")
        route_args, env, configured_model = codex_provider_configuration(dict(os.environ))
        argv = [executable, "exec", "--json", "--color", "never", "--ignore-user-config", "--ignore-rules",
                "-c", 'approval_policy="never"',
                "-c", 'shell_environment_policy.exclude=["*KEY*","*TOKEN*","*SECRET*"]',
                "--output-schema", str(Path(__file__).with_name("backend_result.schema.json")),
                *route_args]
        model = self.model or configured_model
        if model:
            argv.extend(["--model", model])
        # Current clients may enforce named profiles even when legacy --sandbox is
        # passed. Select one system explicitly; never inherit the desktop's profile.
        permission = ":workspace" if self.writable else ":read-only"
        argv.extend(["-c", f"default_permissions={json.dumps(permission)}"])
        if os.name == "nt":
            argv.extend(["-c", 'windows.sandbox="unelevated"'])
        if self.session_id:
            raw = self.session_id.removeprefix("codex:")
            if not re.fullmatch(r"[a-fA-F0-9-]{36}", raw):
                raise ValueError("invalid Codex session identity")
            argv.extend(["resume", raw, "-"])
        else:
            argv.extend(["-C", str(root), "-"])
        return argv, env

    @staticmethod
    def _kill_owned_tree(proc):
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, timeout=10)
        else:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def run(self, worktree: str | Path, prompt: str) -> EngineeringAgentResult:
        root = Path(worktree).expanduser().resolve()
        try:
            argv, env = self.build_invocation(root)
        except FileNotFoundError:
            return self._failure(127, "[codex:cli_not_found] Configure HIKARI_ENGINEERING_CODEX_EXECUTABLE")
        except (OSError, ValueError):
            return self._failure(126, "[codex:configuration_error] Could not read the selected Codex model configuration")
        try:
            proc = subprocess.Popen(argv, cwd=root, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
                                    start_new_session=os.name != "nt", bufsize=1)
        except OSError as exc:
            return self._failure(126, f"[codex:spawn_failed] {type(exc).__name__}")
        state = {"session": self.session_id or "", "final": "", "completed": False, "failed": False}
        output, errors, events = deque(maxlen=256), deque(maxlen=64), deque(maxlen=256)

        def emit(kind, summary):
            event = EngineeringAgentEvent(kind, summary[:1000])
            events.append(event)
            if self._event_sink:
                try:
                    self._event_sink(event)
                except Exception:
                    pass

        def read_stdout():
            for line in proc.stdout:
                output.append(line[:16000])
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(item, dict):
                    continue
                event_type = item.get("type")
                if event_type == "thread.started":
                    state["session"] = "codex:" + str(item.get("thread_id", ""))
                elif event_type == "turn.completed":
                    state["completed"] = True
                elif event_type in {"turn.failed", "error"}:
                    state["failed"] = True
                    emit("error", "Codex reported a failed turn")
                elif event_type in {"item.started", "item.completed", "item.updated"}:
                    payload = item.get("item", {})
                    if not isinstance(payload, dict):
                        continue
                    kind = payload.get("type", "activity")
                    if kind == "agent_message" and event_type == "item.completed":
                        state["final"] = str(payload.get("text", ""))
                    elif kind == "command_execution":
                        emit("command", f"Codex: {payload.get('command', '')} [{payload.get('status', '')}, exit={payload.get('exit_code', 'unknown')}]")
                    elif kind == "file_change":
                        emit("edit", "Codex changed project files: " + ", ".join(str(c.get("path", "")) for c in payload.get("changes", [])))

        def read_stderr():
            for line in proc.stderr:
                errors.append(line[:4000])

        readers = [threading.Thread(target=read_stdout, daemon=True), threading.Thread(target=read_stderr, daemon=True)]
        for reader in readers:
            reader.start()
        boundary = ("You are Hikari's engineering backend. Work only on the assigned repository task. "
                    "Hikari owns git commit, push, PR publication and authority. Do not perform those actions, "
                    "change permissions, deploy, or read credentials. Perform appropriate tests and repair, "
                    "then report concrete changes and validation evidence using the required JSON schema. "
                    "Set status=blocked when permissions or environment prevent the requested work; "
                    "set status=failed when it remains incomplete. Never set completed just because you can reply.\n\n")
        try:
            proc.stdin.write(boundary + prompt + "\n")
            proc.stdin.close()
        except (OSError, BrokenPipeError):
            pass
        timed_out = False
        try:
            code = proc.wait(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._kill_owned_tree(proc)
            code = proc.wait(timeout=10)
        for reader in readers:
            reader.join(timeout=2)
        if timed_out:
            return self._failure(124, f"[codex:timeout] Backend exceeded {self.timeout_seconds:g}s", events=events, session_id=state["session"])
        if code == 0 and (not state["completed"] or state["failed"] or not state["final"].strip()):
            code = 1
            errors.append("[codex:missing_result] No successful completed turn with a final message")
        if code == 0:
            try:
                report = json.loads(state["final"])
                if (not isinstance(report, dict) or report.get("status") not in {"completed", "blocked", "failed"}
                        or not isinstance(report.get("summary"), str) or not report["summary"].strip()
                        or not isinstance(report.get("validation"), list)
                        or not all(isinstance(item, str) for item in report["validation"])):
                    raise ValueError("invalid report")
                state["final"] = report["summary"]
                for validation in report["validation"]:
                    emit("validation", validation)
                if report["status"] != "completed":
                    code = 77 if report["status"] == "blocked" else 1
                    errors.append(f"[codex:{report['status']}] {report['summary']}")
            except (ValueError, KeyError, TypeError):
                code = 1
                errors.append("[codex:invalid_result] Completion did not satisfy the Hikari result contract")
        self.session_id = state["session"] or self.session_id
        return EngineeringAgentResult(code, "".join(output), "".join(errors), state["final"],
                                      state["session"], tuple(events))
