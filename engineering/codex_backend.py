from __future__ import annotations

from collections import deque
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import tomllib
from uuid import uuid4

from .backend import EngineeringAgentEvent, EngineeringAgentResult
from .config import EngineeringBackendConfig


_PERMISSION_PROFILE = "hikari-engineering"
_SANDBOX_MARKER = "HIKARI_ENGINEERING_RESTRICTED_SANDBOX_READY"
_SECRET_ENV_PATTERNS = ("*KEY*", "*TOKEN*", "*SECRET*", "*PASSWORD*", "*CREDENTIAL*")
_SAFE_DOTENV_TEMPLATES = frozenset({".env.example", ".env.sample", ".env.template"})


class CodexSandboxBoundaryError(ValueError):
    """The host cannot establish or safely clean its bounded execution context."""


def _is_link(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) &
                                            getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _bounded_tree(root: Path, *, max_depth: int = 32, max_entries: int = 100_000,
                  skip_git: bool = False):
    """Yield entries without following symlinks/junctions; incomplete inspection is fatal."""
    pending = [(root, 0)]
    seen = 0
    while pending:
        directory, depth = pending.pop()
        if _is_link(directory) or directory.resolve() != directory:
            raise CodexSandboxBoundaryError("workspace directory changed or links outside its lexical path")
        with os.scandir(directory) as entries:
            for entry in entries:
                seen += 1
                if seen > max_entries:
                    raise CodexSandboxBoundaryError("workspace credential scan exceeds its entry bound")
                path = directory / entry.name
                linked = _is_link(path)
                is_directory = entry.is_dir(follow_symlinks=False) and not linked
                yield path, linked, is_directory
                if is_directory and not (skip_git and entry.name == ".git"):
                    if depth >= max_depth:
                        raise CodexSandboxBoundaryError("workspace credential scan exceeds its depth bound")
                    pending.append((path, depth + 1))


def _workspace_credential_denies(root: Path, *, max_depth: int = 32,
                                  max_entries: int = 100_000) -> set[Path]:
    tracked = set()
    git = shutil.which("git")
    if git:
        result = subprocess.run([git, "-C", str(root), "ls-files", "--cached", "-z"],
                                capture_output=True, text=True, encoding="utf-8", errors="strict", timeout=10)
        if result.returncode == 0:
            tracked = set(result.stdout.split("\0"))
    denied = {root / ".env"}  # This runtime-secret path stays denied even before it exists.
    try:
        for path, linked, is_directory in _bounded_tree(root, max_depth=max_depth,
                                                       max_entries=max_entries, skip_git=True):
            if linked:
                denied.add(path)
                continue
            name = path.name.casefold()
            if name == ".env" or name.startswith(".env.") or name.endswith(".env"):
                if (not is_directory and path.name in _SAFE_DOTENV_TEMPLATES
                        and path.relative_to(root).as_posix() in tracked):
                    continue
                denied.add(path)
    except OSError as exc:
        raise CodexSandboxBoundaryError("workspace credential scan could not inspect every entry") from exc
    return denied


@contextmanager
def _private_worktree_temp(root: Path):
    """Create and remove exactly one new host-owned directory, never preexisting data."""
    temporary = root / (".hikari-tmp-" + uuid4().hex)
    try:
        temporary.mkdir(exist_ok=False)
        identity = temporary.stat()
    except OSError as exc:
        raise CodexSandboxBoundaryError("could not create the private worktree temporary directory") from exc
    try:
        yield temporary
    finally:
        _cleanup_private_worktree_temp(root, temporary, identity)


def _cleanup_private_worktree_temp(root: Path, temporary: Path, identity) -> None:
    try:
        if not temporary.exists() and not temporary.is_symlink():
            return  # A completed task may already have removed its empty scratch directory.
        current = temporary.lstat()
        if (temporary.parent != root or temporary.resolve() != temporary or _is_link(temporary)
                or (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino)
                or not re.fullmatch(r"\.hikari-tmp-[a-f0-9]{32}", temporary.name)):
            raise CodexSandboxBoundaryError("private temporary directory identity changed; cleanup refused")
        entries = list(_bounded_tree(temporary))
        if any(linked or path.resolve() != path or temporary not in path.parents
               for path, linked, _ in entries):
            raise CodexSandboxBoundaryError("private temporary directory contains a link; cleanup refused")
        # Validate the whole tree before deletion. Each entry is checked again;
        # no junction/symlink or substituted external directory is followed.
        for path, _, is_directory in sorted(entries, key=lambda item: len(item[0].parts), reverse=True):
            if _is_link(path) or path.resolve() != path:
                raise CodexSandboxBoundaryError("private temporary entry changed; cleanup refused")
            path.rmdir() if is_directory else path.unlink()
        temporary.rmdir()
    except OSError as exc:
        raise CodexSandboxBoundaryError("private worktree temporary cleanup failed; result cannot be committed") from exc


def _toml_inline(value: object) -> str:
    """Serialize a closed host-generated value as one TOML override, including path keys."""
    if type(value) is bool:
        return "true" if value else "false"
    if type(value) is int:
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(key, ensure_ascii=False) + "=" + _toml_inline(item)
                               for key, item in value.items()) + "}"
    raise TypeError("unsupported host-owned Codex configuration value")


def _trusted_runtime_and_git_reads(root: Path, executable: Path) -> set[Path]:
    """Resolve installed runtime and Git metadata paths; never accept paths from model text."""
    paths = {Path(sys.prefix).resolve(), Path(sys.base_prefix).resolve(),
             Path(sys.executable).resolve().parent, executable.resolve().parent}
    for name in ("git", "pwsh", "powershell"):
        found = shutil.which(name)
        if found:
            paths.add(Path(found).resolve().parent)
    git = shutil.which("git")
    if git:
        result = subprocess.run([git, "-C", str(root), "rev-parse", "--path-format=absolute",
                                 "--git-dir", "--git-common-dir"], capture_output=True,
                                text=True, encoding="utf-8", errors="strict", timeout=10)
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                path = Path(line)
                if not path.is_absolute():
                    raise ValueError("Git metadata path must be absolute")
                paths.add(path.resolve())
    return paths


def codex_permission_profile(root: Path, *, executable: Path, writable: bool,
                             environment: dict[str, str], temporary: Path | None = None) -> dict:
    """Command confidentiality boundary; unsupported platforms must refuse this profile."""
    root = root.resolve()
    filesystem: dict[str, object] = {
        ":root": "deny", ":minimal": "read", ":tmpdir": "deny", ":slash_tmp": "deny",
        ":workspace_roots": {".": "write" if writable else "read", ".git": "read", ".codex": "read"},
    }
    for path in sorted(_trusted_runtime_and_git_reads(root, executable), key=str):
        # An executable installed directly in an unrelated broad directory does
        # not justify granting that entire directory to the model's commands.
        if path == Path(path.anchor) or path == Path.home().resolve():
            raise ValueError("runtime read allowlist would expose a broad host directory")
        filesystem[str(path)] = "read"
    home = Path.home().resolve()
    denied = {home / name for name in (".codex", ".ssh", ".aws", ".azure", ".kube", ".gnupg",
                                       ".git-credentials", ".netrc", ".npmrc", ".pypirc", ".config/gh")}
    denied.add(Path(environment.get("CODEX_HOME", str(home / ".codex"))).expanduser().resolve())
    for name in ("APPDATA", "LOCALAPPDATA"):
        if environment.get(name):
            denied.add(Path(environment[name]).resolve() / "GitHub CLI")
    for path in denied:
        filesystem[str(path)] = "deny"
    for path in _workspace_credential_denies(root):
        filesystem[str(path)] = "deny"
    # Keep the credential-bearing files denied even if :minimal or an installed
    # runtime creates a narrower readable subtree beneath CODEX_HOME.
    codex_home = Path(environment.get("CODEX_HOME", str(home / ".codex"))).expanduser().resolve()
    for name in ("auth.json", "config.toml", ".sandbox-secrets"):
        filesystem[str(codex_home / name)] = "deny"
    if temporary is not None:
        if (temporary.parent != root or temporary.resolve() != temporary or _is_link(temporary)
                or not re.fullmatch(r"\.hikari-tmp-[a-f0-9]{32}", temporary.name)
                or any(environment.get(key) != str(temporary) for key in ("TEMP", "TMP", "TMPDIR"))):
            raise CodexSandboxBoundaryError("invalid private temporary directory allowance")
        filesystem[":tmpdir"] = "write"  # TMPDIR resolves only to this host-owned directory.
        filesystem[str(temporary)] = "write"
    return {"extends": ":workspace" if writable else ":read-only",
            "filesystem": filesystem, "network": {"enabled": False}}


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
        self.windows_sandbox = config.codex_sandbox
        self.session_id = session_id if session_id and session_id.startswith("codex:") else None
        self._event_sink = event_sink

    def set_event_sink(self, sink):
        self._event_sink = sink

    @staticmethod
    def _failure(code, detail, *, events=(), session_id=""):
        return EngineeringAgentResult(code, "", detail, "", session_id, tuple(events))

    def build_invocation(self, root: Path, *, temporary: Path | None = None) -> tuple[list[str], dict[str, str]]:
        executable = shutil.which(self.executable)
        if executable is None and Path(self.executable).is_absolute() and Path(self.executable).is_file():
            executable = str(Path(self.executable).resolve())
        if executable is None:
            raise FileNotFoundError("Codex executable is not available")
        route_args, env, configured_model = codex_provider_configuration(dict(os.environ))
        runtime_bin = str(Path(sys.executable).resolve().parent)
        runtime_environment = {
            "PATH": os.pathsep.join([runtime_bin, *[item for item in env.get("PATH", "").split(os.pathsep) if item and item != runtime_bin]]),
            "HIKARI_RUNTIME_PYTHON": str(Path(sys.executable).resolve()),
        }
        if sys.prefix != sys.base_prefix:
            runtime_environment["VIRTUAL_ENV"] = str(Path(sys.prefix).resolve())
        env.update(runtime_environment)
        if temporary is not None:
            env.update({key: str(temporary) for key in ("TEMP", "TMP", "TMPDIR")})
        shell_filters = {name: "exclude" for name in _SECRET_ENV_PATTERNS}
        for value in route_args:
            if value.startswith("model_providers.hikari_engineering.env_key="):
                # Provider keys need not contain a conventional secret word.
                # Preserve this value for Codex auth but remove it from commands.
                shell_filters[json.loads(value.split("=", 1)[1])] = "exclude"
        argv = [executable, "exec", "--json", "--color", "never", "--ignore-user-config", "--ignore-rules",
                "-c", 'approval_policy="never"',
                "-c", 'features.apps=false', "-c", 'web_search="disabled"',
                "-c", 'shell_environment_policy.inherit="core"',
                "-c", 'shell_environment_policy.experimental_use_profile=false',
                "-c", 'shell_environment_policy.ignore_default_excludes=false',
                "-c", "shell_environment_policy.filters=" + _toml_inline(shell_filters),
                "--output-schema", str(Path(__file__).with_name("backend_result.schema.json")),
                *route_args]
        shell_environment = dict(runtime_environment)
        if temporary is not None:
            shell_environment.update({key: str(temporary) for key in ("TEMP", "TMP", "TMPDIR")})
        argv.extend(["-c", "shell_environment_policy.set=" + _toml_inline(shell_environment)])
        model = self.model or configured_model
        if model:
            argv.extend(["--model", model])
        # One table avoids dotted -c parsing of quoted special path keys. Never
        # combine the profile with legacy --sandbox or fall back to broad reads.
        profile = codex_permission_profile(root, executable=Path(executable), writable=self.writable,
                                           environment=env, temporary=temporary)
        argv.extend(["-c", f'default_permissions="{_PERMISSION_PROFILE}"',
                     "-c", "permissions." + _PERMISSION_PROFILE + "=" + _toml_inline(profile)])
        if os.name == "nt":
            argv.extend(["-c", "windows.sandbox=" + json.dumps(self.windows_sandbox)])
        if self.session_id:
            raw = self.session_id.removeprefix("codex:")
            if not re.fullmatch(r"[a-fA-F0-9-]{36}", raw):
                raise ValueError("invalid Codex session identity")
            argv.extend(["resume", raw, "-"])
        else:
            argv.extend(["-C", str(root), "-"])
        return argv, env

    def _sandbox_preflight(self, root: Path, argv: list[str], env: dict[str, str]) -> EngineeringAgentResult | None:
        """Prove the selected command sandbox starts before any model-assigned code runs."""
        selected = []
        for index, value in enumerate(argv[:-1]):
            if value == "-c" and argv[index + 1].startswith(("permissions.", "windows.sandbox=", "shell_environment_policy.")):
                selected.extend(["-c", argv[index + 1]])
        command = [argv[0], "sandbox", "-P", _PERMISSION_PROFILE, "-C", str(root), "--include-managed-config",
                   *selected, "--", sys.executable, "-B", "-c", f"print({_SANDBOX_MARKER!r})"]
        try:
            result = self._run_sandbox_probe(command, root, env)
        except (OSError, subprocess.SubprocessError) as exc:
            return self._failure(77, "[codex:blocked] Restricted sandbox preflight could not start "
                f"({type(exc).__name__}); no model task was executed. Operator setup is required; no broader fallback was used.",
                session_id=self.session_id or "")
        if result.returncode or result.stdout.strip() != _SANDBOX_MARKER:
            # Report a known platform refusal; avoid surfacing arbitrary startup output.
            detail = "The configured restricted filesystem profile could not be enforced."
            if "Restricted read-only access requires the elevated Windows sandbox backend" in result.stderr:
                detail = "Restricted read-only access requires the elevated Windows sandbox backend; unelevated mode cannot enforce this profile."
            return self._failure(77, "[codex:blocked] " + detail +
                " No model task was executed. Configure a supported sandbox explicitly; no broader fallback was used.",
                session_id=self.session_id or "")
        return None

    def _run_sandbox_probe(self, command: list[str], root: Path, env: dict[str, str]):
        proc = subprocess.Popen(command, cwd=root, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
            start_new_session=os.name != "nt")
        try:
            stdout, stderr = proc.communicate(timeout=min(20.0, self.timeout_seconds))
        except subprocess.TimeoutExpired:
            self._kill_owned_tree(proc)
            proc.communicate(timeout=10)
            raise
        return subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)

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
            with _private_worktree_temp(root) as temporary:
                return self._run_with_private_temp(root, prompt, temporary)
        except CodexSandboxBoundaryError as exc:
            return self._failure(77, "[codex:blocked] " + str(exc) +
                "; no broader filesystem permission or cleanup fallback was used.", session_id=self.session_id or "")

    def _run_with_private_temp(self, root: Path, prompt: str, temporary: Path) -> EngineeringAgentResult:
        try:
            argv, env = self.build_invocation(root, temporary=temporary)
        except FileNotFoundError:
            return self._failure(127, "[codex:cli_not_found] Configure HIKARI_ENGINEERING_CODEX_EXECUTABLE")
        except CodexSandboxBoundaryError:
            raise
        except (OSError, ValueError, subprocess.SubprocessError):
            return self._failure(126, "[codex:configuration_error] Could not read the selected Codex model configuration")
        blocked = self._sandbox_preflight(root, argv, env)
        if blocked is not None:
            return blocked
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
                    "Your status reports ONLY the backend-assigned editing/inspection/validation stage. "
                    "When that stage is done, return completed; Hikari will then do its own scope check and commit. "
                    "The user's overall goal may include commit/push/PR: those are Hikari-owned later stages, "
                    "so not performing them yourself is expected and must NOT make your stage blocked. "
                    "Set status=blocked only when permissions or environment prevent your assigned stage; "
                    "set status=failed when that stage remains incomplete. Never set completed just because you can reply.\n\n")
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
