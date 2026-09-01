from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import json
import shutil
import subprocess


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
    """

    def __init__(
        self,
        *,
        executable: str = "claude",
        max_turns: int = 30,
        permission_mode: str = "plan",
        session_id: str | None = None,
        model: str | None = None,
    ) -> None:
        if max_turns < 1:
            raise ValueError("engineering max_turns must be >= 1")
        if permission_mode not in {"plan", "auto", "acceptEdits", "manual", "dontAsk"}:
            raise ValueError(f"unsupported engineering permission mode: {permission_mode!r}")
        self.executable = executable
        self.max_turns = int(max_turns)
        self.permission_mode = permission_mode
        self.session_id = session_id.strip() if isinstance(session_id, str) and session_id.strip() else None
        self.model = model.strip() if isinstance(model, str) and model.strip() else None

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
        ]
        if self.model:
            argv.extend(["--model", self.model])
        if self.session_id:
            argv.extend(["--resume", self.session_id])

        proc = subprocess.run(
            argv,
            cwd=root,
            input=prompt,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
        )
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
