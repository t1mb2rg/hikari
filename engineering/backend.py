from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
import json
import os
import shutil
import subprocess
import threading

from .config import EngineeringBackendConfig


@dataclass(frozen=True, slots=True)
class EngineeringAgentEvent:
    """One compact, grounded activity item observed from Claude Code's stream."""

    kind: str
    summary: str


@dataclass(frozen=True, slots=True)
class EngineeringAgentResult:
    returncode: int
    stdout: str
    stderr: str
    final_message: str
    session_id: str
    events: tuple[EngineeringAgentEvent, ...] = ()


EventSink = Callable[[EngineeringAgentEvent], None]


def _claude_child_environment(values: Mapping[str, str]) -> dict[str, str]:
    """Remove ambient model overrides before starting Claude Code.

    Hikari owns the engineering model through ``HIKARI_ENGINEERING_MODEL`` and
    passes it explicitly with ``--model``. Conversation/runtime environments may
    still contain Claude Code model override variables for other workflows; those
    must not silently change the Engineering backend model or its side-models.
    Provider/authentication variables are preserved.
    """

    result: dict[str, str] = {}
    for key, value in values.items():
        upper = key.upper()
        if upper == "ANTHROPIC_MODEL":
            continue
        if upper.startswith("ANTHROPIC_DEFAULT_") and upper.endswith("_MODEL"):
            continue
        if upper == "CLAUDE_CODE_SUBAGENT_MODEL":
            continue
        result[key] = value
    return result


class ClaudeEngineeringBackend:
    """Thin Hikari harness around one Claude Code engineering session.

    Claude Code owns repository inspection, editing, task-appropriate validation,
    and repair inside its own agent loop. Hikari owns the outer authority boundary,
    durable session state, scope checks, Git history/publication, and delivery.

    The backend consumes Claude Code's ``stream-json`` output so Hikari can retain
    grounded activity evidence instead of waiting on one opaque final JSON blob.
    Process success is only transport success: a validated structured task report
    determines whether the requested work completed, failed, or was blocked.
    """

    def __init__(
        self,
        *,
        executable: str | None = None,
        max_turns: int | None = None,
        permission_mode: str = "plan",
        session_id: str | None = None,
        model: str | None = None,
        timeout_seconds: float | None = None,
        event_sink: EventSink | None = None,
    ) -> None:
        owned = EngineeringBackendConfig.from_mapping(os.environ)
        resolved_executable = (
            executable.strip()
            if isinstance(executable, str) and executable.strip()
            else owned.executable
        )
        resolved_max_turns = owned.max_turns if max_turns is None else int(max_turns)
        resolved_timeout = owned.timeout_seconds if timeout_seconds is None else float(timeout_seconds)
        resolved_model = model.strip() if isinstance(model, str) and model.strip() else owned.model

        if resolved_max_turns < 1:
            raise ValueError("engineering max_turns must be >= 1")
        if permission_mode not in {"plan", "auto", "acceptEdits", "manual", "dontAsk"}:
            raise ValueError(f"unsupported engineering permission mode: {permission_mode!r}")
        if resolved_timeout <= 0:
            raise ValueError("engineering backend timeout_seconds must be > 0")
        self.executable = resolved_executable
        self.max_turns = resolved_max_turns
        self.permission_mode = permission_mode
        self.session_id = session_id.strip() if isinstance(session_id, str) and session_id.strip() else None
        self.model = resolved_model
        self.timeout_seconds = resolved_timeout
        self._event_sink = event_sink

    def set_event_sink(self, sink: EventSink | None) -> None:
        self._event_sink = sink

    @staticmethod
    def _settings_json() -> str:
        # Claude may inspect/edit and run ordinary project-local validation.
        # Hikari retains Git history/publication and obvious external-impact actions.
        deny = [
            "Bash(git add:*)",
            "Bash(git commit:*)",
            "Bash(git reset:*)",
            "Bash(git clean:*)",
            "Bash(git checkout:*)",
            "Bash(git switch:*)",
            "Bash(git push:*)",
            "Bash(git pull:*)",
            "Bash(git fetch:*)",
            "Bash(gh pr create:*)",
            "Bash(gh pr merge:*)",
            "Bash(gh release create:*)",
            "Bash(kubectl apply:*)",
            "Bash(kubectl delete:*)",
            "Bash(terraform apply:*)",
            "Bash(terraform destroy:*)",
            "Bash(docker push:*)",
            "Bash(npm publish:*)",
            "Bash(claude:*)",
            "Bash(forge run:*)",
            "Bash(sudo:*)",
            "Read(~/.ssh/**)",
            "Write(~/.ssh/**)",
            "Edit(~/.ssh/**)",
            "Read(~/.aws/**)",
            "Write(~/.aws/**)",
            "Edit(~/.aws/**)",
            "Read(~/.git-credentials)",
            "Write(~/.git-credentials)",
            "Edit(~/.git-credentials)",
        ]
        return json.dumps(
            {
                "permissions": {
                    "deny": deny,
                    "disableBypassPermissionsMode": "disable",
                }
            }
        )

    def _failure(
        self,
        returncode: int,
        detail: str,
        *,
        stdout: str = "",
        session_id: str = "",
        events: tuple[EngineeringAgentEvent, ...] = (),
    ) -> EngineeringAgentResult:
        return EngineeringAgentResult(
            returncode=returncode,
            stdout=stdout,
            stderr=detail,
            final_message="",
            session_id=session_id or self.session_id or "",
            events=events,
        )

    @staticmethod
    def _tool_detail(payload: Mapping[str, object]) -> str:
        for key in ("command", "file_path", "path", "pattern", "query", "description"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                text = " ".join(value.strip().split())
                return text[:360]
        return ""

    @staticmethod
    def _tool_result_detail(content: object) -> str:
        if isinstance(content, str):
            return " ".join(content.strip().split())[:360]
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, Mapping):
                    text = item.get("text")
                    if isinstance(text, str) and text.strip():
                        parts.append(text.strip())
            return " ".join(" ".join(parts).split())[:360]
        return ""

    @classmethod
    def _events_from_payload(
        cls,
        payload: Mapping[str, object],
        tool_names: dict[str, str],
    ) -> tuple[EngineeringAgentEvent, ...]:
        event_type = str(payload.get("type", "")).strip()
        events: list[EngineeringAgentEvent] = []

        if event_type == "system" and payload.get("subtype") == "init":
            model = str(payload.get("model", "")).strip()
            mode = str(payload.get("permissionMode", "")).strip()
            detail = ", ".join(part for part in (model, mode) if part)
            events.append(
                EngineeringAgentEvent(
                    "system",
                    "Claude Code session started" + (f" ({detail})" if detail else ""),
                )
            )
            return tuple(events)

        message = payload.get("message")
        content = message.get("content") if isinstance(message, Mapping) else None
        if not isinstance(content, list):
            content = []

        if event_type == "assistant":
            for block in content:
                if not isinstance(block, Mapping) or block.get("type") != "tool_use":
                    continue
                name = str(block.get("name", "tool")).strip() or "tool"
                tool_id = str(block.get("id", "")).strip()
                if tool_id:
                    tool_names[tool_id] = name
                tool_input = block.get("input")
                detail = cls._tool_detail(tool_input) if isinstance(tool_input, Mapping) else ""
                events.append(
                    EngineeringAgentEvent(
                        "tool",
                        f"{name}: {detail}" if detail else name,
                    )
                )
            return tuple(events)

        if event_type == "user":
            for block in content:
                if not isinstance(block, Mapping) or block.get("type") != "tool_result":
                    continue
                tool_id = str(block.get("tool_use_id", "")).strip()
                name = tool_names.get(tool_id, "tool")
                failed = bool(block.get("is_error", False))
                detail = cls._tool_result_detail(block.get("content")) if failed else ""
                summary = f"{name} {'failed' if failed else 'completed'}"
                if detail:
                    summary += f": {detail}"
                events.append(EngineeringAgentEvent("tool_result", summary))
            return tuple(events)

        if event_type == "result":
            subtype = str(payload.get("subtype", "")).strip() or "unknown"
            events.append(EngineeringAgentEvent("result", f"Claude Code result: {subtype}"))
        return tuple(events)

    def run(self, worktree: str | Path, prompt: str) -> EngineeringAgentResult:
        executable = shutil.which(self.executable)
        if executable is None:
            candidate = Path(self.executable).expanduser()
            if candidate.is_absolute() and candidate.is_file():
                executable = str(candidate.resolve())
        if executable is None:
            return self._failure(
                127,
                "[claude-code:cli_not_found] Claude Code executable was not found in the Engineering Worker environment; "
                "configure HIKARI_ENGINEERING_CLAUDE_EXECUTABLE or fix Resident PATH",
            )

        root = Path(worktree).expanduser().resolve()
        try:
            result_schema = Path(__file__).with_name("backend_result.schema.json").read_text(encoding="utf-8")
        except OSError:
            return self._failure(126, "[claude-code:configuration_error] Hikari result schema is unavailable")
        argv = [
            executable,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--json-schema",
            result_schema,
            "--permission-mode",
            self.permission_mode,
            "--max-turns",
            str(self.max_turns),
            "--settings",
            self._settings_json(),
            "--model",
            self.model,
        ]
        if self.session_id:
            argv.extend(["--resume", self.session_id])

        try:
            proc = subprocess.Popen(
                argv,
                cwd=root,
                env=_claude_child_environment(os.environ),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except OSError as exc:
            return self._failure(
                126,
                f"[claude-code:spawn_failed] {type(exc).__name__}: {exc}",
            )

        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        payloads: list[dict[str, object]] = []
        events: list[EngineeringAgentEvent] = []
        tool_names: dict[str, str] = {}

        def read_stdout() -> None:
            if proc.stdout is None:
                return
            for line in proc.stdout:
                stdout_lines.append(line)
                try:
                    parsed = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(parsed, dict):
                    continue
                payloads.append(parsed)
                for event in self._events_from_payload(parsed, tool_names):
                    events.append(event)
                    if self._event_sink is not None:
                        try:
                            self._event_sink(event)
                        except Exception:
                            # Activity streaming is observability, not execution authority.
                            # The complete event set is still returned to the Worker.
                            pass

        def read_stderr() -> None:
            if proc.stderr is None:
                return
            for line in proc.stderr:
                stderr_lines.append(line)

        stdout_thread = threading.Thread(target=read_stdout, name="hikari-claude-stdout", daemon=True)
        stderr_thread = threading.Thread(target=read_stderr, name="hikari-claude-stderr", daemon=True)
        stdout_thread.start()
        stderr_thread.start()

        try:
            if proc.stdin is not None:
                boundary = (
                    "You are Hikari's engineering backend. Work only on the assigned repository task. "
                    "Hikari owns Git commit, push, PR publication and authority. Do not perform those actions, "
                    "change permissions, deploy, or read credentials. Perform appropriate validation and repair. "
                    "Use the required structured result schema to report task status, a concrete summary, and "
                    "validation evidence. Report ONLY the backend-assigned editing/inspection/validation stage. "
                    "After that stage is done, Hikari performs its scope check and commit. The user's overall "
                    "goal may include commit/push/PR; these are expected later Hikari-owned stages. Not doing "
                    "those stages yourself must NOT cause a blocked report. "
                    "Set status=blocked when permission or environment prevents your assigned stage; "
                    "set status=failed when requested work remains incomplete. Set completed only when the "
                    "requested outcome is established. An already-satisfied task may complete without edits "
                    "if you explain what you checked. A refusal, proposed plan, or successful reply alone "
                    "does not mean the task completed.\n\n"
                )
                proc.stdin.write(boundary + prompt)
                if not prompt.endswith("\n"):
                    proc.stdin.write("\n")
                proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass

        timed_out = False
        try:
            returncode = proc.wait(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.kill()
            try:
                returncode = proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                returncode = 124

        stdout_thread.join(timeout=2.0)
        stderr_thread.join(timeout=2.0)

        stdout = "".join(stdout_lines)
        stderr = "".join(stderr_lines)
        session_id = self.session_id or ""
        final_message = ""
        result_payload: Mapping[str, object] | None = None
        for payload in payloads:
            candidate_session = payload.get("session_id")
            if isinstance(candidate_session, str) and candidate_session.strip():
                session_id = candidate_session.strip()
            if payload.get("type") == "result":
                result_payload = payload
                candidate_result = payload.get("result")
                if isinstance(candidate_result, str):
                    final_message = candidate_result

        if session_id:
            self.session_id = session_id

        event_tuple = tuple(events)
        if timed_out:
            detail = f"[claude-code:timeout] Claude Code backend exceeded {self.timeout_seconds:g}s deadline"
            if stderr.strip():
                detail += "\n" + stderr.strip()[-800:]
            return self._failure(
                124,
                detail,
                stdout=stdout,
                session_id=session_id,
                events=event_tuple,
            )

        if result_payload is None and returncode == 0:
            returncode = 1
            missing = "[claude-code:missing_result] stream ended without a final result event"
            stderr = (stderr.rstrip() + "\n" + missing).strip()
        elif result_payload is not None:
            subtype = str(result_payload.get("subtype", "")).strip()
            is_error = bool(result_payload.get("is_error", False))
            if (is_error or subtype != "success") and returncode == 0:
                returncode = 1
                detail = f"[claude-code:{subtype or 'execution_error'}] Claude Code reported an unsuccessful result"
                stderr = (stderr.rstrip() + "\n" + detail).strip()

        if returncode == 0:
            # --json-schema exposes its validated object in structured_output.
            # Never upgrade legacy free-form result prose to a completed task.
            report = result_payload.get("structured_output") if result_payload is not None else None
            if (
                not isinstance(report, Mapping)
                or set(report) != {"status", "summary", "validation"}
                or not isinstance(report.get("status"), str)
                or report["status"] not in {"completed", "blocked", "failed"}
                or not isinstance(report.get("summary"), str)
                or not report["summary"].strip()
                or not isinstance(report.get("validation"), list)
                or not all(isinstance(item, str) for item in report["validation"])
            ):
                return self._failure(
                    1,
                    (stderr.rstrip() + "\n[claude-code:invalid_result] Completion did not satisfy the Hikari result contract").strip(),
                    stdout=stdout,
                    session_id=session_id,
                    events=event_tuple,
                )
            final_message = report["summary"].strip()
            for item in report["validation"]:
                if not item.strip():
                    continue
                event = EngineeringAgentEvent("validation", item.strip())
                events.append(event)
                if self._event_sink is not None:
                    try:
                        self._event_sink(event)
                    except Exception:
                        pass
            status = report["status"]
            if status != "completed":
                returncode = 77 if status == "blocked" else 1
                detail = f"[claude-code:{status}] {final_message}"
                stderr = (stderr.rstrip() + "\n" + detail).strip()

        return EngineeringAgentResult(
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            final_message=final_message,
            session_id=session_id,
            events=tuple(events),
        )
