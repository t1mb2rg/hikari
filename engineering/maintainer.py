from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Mapping
import os
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
    environment: Mapping[str, str] | None = None,
) -> None:
    """Prove that pytest descendants can create another Python process."""

    root = Path(worktree).expanduser().resolve()
    proc = subprocess.run(
        [sys.executable, "-c", _NESTED_PROCESS_PROBE],
        cwd=root,
        env=dict(environment) if environment is not None else None,
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


def project_test_environment(
    environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Remove live Hikari configuration and secrets from project tests."""

    source = os.environ if environment is None else environment
    return {
        str(key): str(value)
        for key, value in source.items()
        if not str(key).upper().startswith("HIKARI_")
    }


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


def project_push_authority() -> EngineeringAuthority:
    """Narrow authority for publishing only the current Hikari engineering branch."""

    return EngineeringAuthority(
        repository_read=True,
        repository_write=False,
        run_commands=False,
        run_tests=False,
        network=True,
        publish=True,
        outside_repo=False,
    )


def project_session_authority_ceiling() -> EngineeringAuthority:
    """Standing session ceiling for delegated maintenance plus non-protected branch push.

    Individual turns still receive a strict subset. Ordinary edit/test turns therefore
    remain offline even though the same durable session may later receive a dedicated
    push turn.
    """

    return EngineeringAuthority(
        repository_read=True,
        repository_write=True,
        run_commands=True,
        run_tests=True,
        network=True,
        publish=True,
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


def is_push_authority(authority: EngineeringAuthority) -> bool:
    return (
        authority.repository_read
        and not authority.repository_write
        and not authority.run_commands
        and not authority.run_tests
        and authority.network
        and authority.publish
        and not authority.outside_repo
    )


def run_project_tests(
    worktree: str | Path,
    *,
    timeout_seconds: float = 300.0,
) -> ProjectTestResult:
    """Run the repository test suite with Hikari's own Python environment."""

    root = Path(worktree).expanduser().resolve()
    test_environment = project_test_environment()
    assert_nested_process_capability(root, environment=test_environment)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
        cwd=root,
        env=test_environment,
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
    first_line = next((line.strip() for line in intent.splitlines() if line.strip()), "")
    text = re.sub(r"\s+", " ", first_line).strip()
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


def push_engineering_branch(
    worktree: str | Path,
    branch: str,
    baseline_commit: str,
    *,
    timeout_seconds: float = 120.0,
) -> str:
    """Push exactly one clean Hikari engineering branch to the configured ``origin``.

    The branch name and destination are not supplied by the model. This helper never
    force-pushes, never pushes a protected branch, and never publishes dirty or empty
    engineering state. It returns the pushed local HEAD commit SHA.
    """

    root = Path(worktree).expanduser().resolve()
    normalized_branch = branch.strip()
    if not normalized_branch.startswith("hikari/engineering/"):
        raise RuntimeError("refusing to push a non-engineering branch")

    current_branch = subprocess.run(
        ["git", "-C", str(root), "branch", "--show-current"],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=True,
    ).stdout.strip()
    if current_branch != normalized_branch:
        raise RuntimeError("engineering worktree branch does not match durable session state")

    dirty = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain"],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=True,
    ).stdout.strip()
    if dirty:
        raise RuntimeError("refusing to push an engineering branch with uncommitted changes")

    head = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=True,
    ).stdout.strip()
    if not baseline_commit.strip() or head == baseline_commit.strip():
        raise RuntimeError("engineering branch has no committed project change to push")

    environment = {
        str(key): str(value)
        for key, value in os.environ.items()
        if not str(key).upper().startswith("HIKARI_")
    }
    environment["GIT_TERMINAL_PROMPT"] = "0"
    proc = subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "push",
            "--set-upstream",
            "origin",
            f"refs/heads/{normalized_branch}:refs/heads/{normalized_branch}",
        ],
        env=environment,
        stdin=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=max(1.0, float(timeout_seconds)),
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        if len(detail) > 1600:
            detail = detail[-1600:]
        raise RuntimeError(detail or "git push failed")
    return head
