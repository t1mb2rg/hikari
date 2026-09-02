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
    failure_kind: str = "test"

    @property
    def passed(self) -> bool:
        return self.returncode == 0


class ValidationEnvironmentError(RuntimeError):
    """Raised when the Worker cannot provide a trustworthy test environment."""


_NESTED_PROCESS_PROBE = (
    "import subprocess,sys; "
    "result=subprocess.run([sys.executable,'-c','raise SystemExit(0)'],"
    "stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE); "
    "raise SystemExit(result.returncode)"
)


_DEPENDENCY_FAILURE_PATTERNS = (
    re.compile(r"ModuleNotFoundError:\s+No module named", re.IGNORECASE),
    re.compile(r"ImportError:.*requires the .+ package", re.IGNORECASE),
    re.compile(r"requires .+ to be installed", re.IGNORECASE),
)


def _failure_kind(output: str) -> str:
    if any(pattern.search(output) for pattern in _DEPENDENCY_FAILURE_PATTERNS):
        return "dependency_environment"
    return "test"


def assert_nested_process_capability(
    worktree: str | Path,
    *,
    timeout_seconds: float = 30.0,
) -> None:
    """Prove that pytest descendants can create another Python process."""

    root = Path(worktree).expanduser().resolve()
    proc = subprocess.run(
        [sys.executable, "-c", _NESTED_PROCESS_PROBE],
        cwd=root,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=max(1.0, float(timeout_seconds)),
    )
    if proc.returncode != 0:
        detail = "\n".join(
            part.strip() for part in (proc.stdout, proc.stderr) if part.strip()
        )
        if len(detail) > 1800:
            detail = detail[-1800:]
        raise ValidationEnvironmentError(
            "nested subprocess probe failed"
            + (f":\n{detail}" if detail else f" (exit {proc.returncode})")
        )


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
    assert_nested_process_capability(root)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=root,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=max(1.0, float(timeout_seconds)),
    )
    full_output = "\n".join(
        part.strip() for part in (proc.stdout, proc.stderr) if part.strip()
    )
    failure_kind = _failure_kind(full_output)
    visible_output = full_output[-5000:] if len(full_output) > 5000 else full_output
    return ProjectTestResult(proc.returncode, visible_output, failure_kind)


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
