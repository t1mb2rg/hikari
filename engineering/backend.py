from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import os
import shutil
import subprocess

from .config import EngineeringBackendConfig


@dataclass(frozen=True, slots=True)
class EngineeringAgentResult:
    returncode: int
    stdout: str
    stderr: str
    final_message: str
    session_id: str


class ClaudeEngineeringBackend:
    """Hikari-owned Claude Code backend for one engineering session.

    The backend session id is preserved by Hikari EngineeringSession state so a
    later turn can resume the same coding-agent context. Read-only turns run in
    Claude Code's plan mode. Maintainer turns may use ``acceptEdits`` to modify
    the isolated worktree, while Hikari's Worker retains ownership of testing,
    Git commit, publication, and external side effects.

    Model and deadline are owned by Hikari's engineering configuration. When a
    caller does not provide them explicitly, this backend resolves
    ``HIKARI_ENGINEERING_*`` instead of inheriting Conversation model names or
    ambient Claude Code model selection.
    """

    def __init__(
        self,
        *,
        executable: str = "claude",
        max_turns: int | None = None,
        permission_mode: str = "plan",
        session_id: str | None = None,
        model: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        owned = EngineeringBackendConfig.from_mapping(os.environ)
        resolved_max_turns = owned.max_turns if max_turns is None else int(max_turns)
        resolved_timeout = owned.timeout_seconds if timeout_seconds is None else float(timeout_seconds)
        resolved_model = model.strip() if isinstance(model, str) and model.strip() else owned.model

        if resolved_max_turns < 1:
            raise ValueError("engineering max_turns must be >= 1")
        if permission_mode not in {"plan", "auto", "acceptEdits", "manual", "dontAsk"}:
            raise ValueError(f"unsupported engineering permission mode: {permission_mode!r}")
        if resolved_timeout <= 0:
            raise ValueError("engineering backend timeout_seconds must be > 0")
        self.executable = executable
        self.max_turns = resolved_max_turns
        self.permission_mode = permission_mode
        self.session_id = session_id.strip() if isinstance(session_id, str) and session_id.strip() else None
        self.model = resolved_model
        self.timeout_seconds = resolved_timeout

    @staticmethod
    def _settings_json() -> str:
        # Claude may inspect and, in maintainer mode, edit the isolated worktree.
        # Hikari's Worker owns Git history/publication and obvious external-impact
        # operations so those actions remain system-governed rather than backend-governed.
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

    def run(self, worktree: str | Path, prompt: str) -> EngineeringAgentResult:
        if shutil.which(self.executable) is None:
            raise RuntimeError(
                "Claude Code CLI not found. Install/authenticate Claude Code before using Hikari Engineering Runtime."
            )
        root = Path(worktree).expanduser().resolve()
        argv = [
            self.executable,
            "-p",
            "--output-format",
            "json",
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
            proc = subprocess.run(
                argv,
                cwd=root,
                input=prompt,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"Claude Code backend exceeded {self.timeout_seconds:g}s deadline"
            ) from exc

        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        payload: dict[str, object] = {}
        try:
            parsed = json.loads(stdout)
            if isinstance(parsed, dict):
                payload = parsed
        except json.JSONDecodeError:
            payload = {}
        session_id = payload.get("session_id") if isinstance(payload.get("session_id"), str) else ""
        final_message = payload.get("result") if isinstance(payload.get("result"), str) else ""
        if session_id:
            self.session_id = session_id
        return EngineeringAgentResult(
            returncode=proc.returncode,
            stdout=stdout,
            stderr=stderr,
            final_message=final_message,
            session_id=session_id or (self.session_id or ""),
        )
