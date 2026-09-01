from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import subprocess
import sys

from .session import EngineeringAuthority


@dataclass(frozen=True, slots=True)
class ProjectTestResult:
    returncode: int
    output: str

    @property
    def passed(self) -> bool:
        return self.returncode == 0


def project_maintainer_authority() -> EngineeringAuthority:
    """Standing low-level execution envelope for ordinary Hikari-project maintenance.

    Network publication and outside-repository effects remain outside this profile.
    The higher-level ProjectMandate decides that this profile is delegated for the
    Hikari repository; EngineeringAuthority remains the deterministic worker ceiling.
    """

    return EngineeringAuthority(
        repository_read=True,
        repository_write=True,
        run_commands=True,
        run_tests=True,
        network=False,
        publish=False,
        outside_repo=False,
    )


def is_read_only_authority(authority: EngineeringAuthority) -> bool:
    return (
        authority.repository_read
        and not authority.repository_write
        and not authority.run_tests
        and not authority.network
        and not authority.publish
        and not authority.outside_repo
    )


def is_maintainer_authority(authority: EngineeringAuthority) -> bool:
    return (
        authority.repository_read
        and authority.repository_write
        and authority.run_commands
        and authority.run_tests
        and not authority.network
        and not authority.publish
        and not authority.outside_repo
    )


def run_project_tests(
    worktree: str | Path,
    *,
    timeout_seconds: float = 300.0,
) -> ProjectTestResult:
    """Run the repository test suite with Hikari's own Python environment."""

    root = Path(worktree).expanduser().resolve()
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q"],
        cwd=root,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=max(1.0, float(timeout_seconds)),
    )
    combined = "\n".join(part.strip() for part in (proc.stdout, proc.stderr) if part.strip())
    if len(combined) > 5000:
        combined = combined[-5000:]
    return ProjectTestResult(proc.returncode, combined)


def _commit_subject(intent: str) -> str:
    text = re.sub(r"\s+", " ", intent).strip()
    if len(text) > 68:
        text = text[:65].rstrip() + "..."
    return f"hikari: {text or 'maintain project'}"


def commit_project_changes(worktree: str | Path, intent: str) -> str | None:
    """Commit current worktree changes on its isolated engineering branch.

    Returns the new commit SHA, or ``None`` when the task required no repository change.
    """

    root = Path(worktree).expanduser().resolve()
    status = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain"],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=True,
    ).stdout.strip()
    if not status:
        return None

    subprocess.run(
        ["git", "-C", str(root), "add", "-A"],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=True,
    )
    proc = subprocess.run(
        ["git", "-C", str(root), "commit", "-m", _commit_subject(intent)],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise RuntimeError(detail or "git commit failed")
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=True,
    ).stdout.strip()
