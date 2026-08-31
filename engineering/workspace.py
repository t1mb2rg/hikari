from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
import subprocess


class EngineeringWorkspaceError(RuntimeError):
    pass


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    if check and proc.returncode != 0:
        raise EngineeringWorkspaceError(proc.stderr.strip() or proc.stdout.strip())
    return proc


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", value).strip("-")
    return (slug[:48] or "session").lower()


@dataclass(frozen=True, slots=True)
class EngineeringWorkspace:
    source_repo: Path
    path: Path
    branch: str
    baseline_commit: str

    @classmethod
    def create(cls, repository: str | Path, session_id: str) -> "EngineeringWorkspace":
        source = Path(repository).expanduser().resolve()
        if _git(source, "rev-parse", "--is-inside-work-tree", check=False).stdout.strip() != "true":
            raise EngineeringWorkspaceError(f"not a git repository: {source}")
        if _git(source, "status", "--porcelain", check=False).stdout.strip():
            raise EngineeringWorkspaceError(
                "source repository has uncommitted changes; refusing ambiguous engineering baseline"
            )
        baseline = _git(source, "rev-parse", "HEAD").stdout.strip()
        if not baseline:
            raise EngineeringWorkspaceError("could not resolve engineering repository HEAD")

        token = _safe_slug(session_id)
        root = source.parent / ".hikari-engineering-worktrees"
        root.mkdir(parents=True, exist_ok=True)
        path = root / token
        branch = f"hikari/engineering/{token}"
        if path.exists():
            raise EngineeringWorkspaceError(f"engineering workspace already exists: {path}")

        proc = subprocess.run(
            [
                "git",
                "-C",
                str(source),
                "worktree",
                "add",
                "-b",
                branch,
                str(path),
                baseline,
            ],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
        )
        if proc.returncode != 0:
            raise EngineeringWorkspaceError(proc.stderr.strip() or proc.stdout.strip())
        return cls(source, path, branch, baseline)

    @classmethod
    def resume(
        cls,
        *,
        repository: str | Path,
        workspace_path: str | Path,
        branch: str,
        baseline_commit: str,
    ) -> "EngineeringWorkspace":
        source = Path(repository).expanduser().resolve()
        path = Path(workspace_path).expanduser().resolve()
        if not path.is_dir():
            raise EngineeringWorkspaceError(f"engineering workspace is missing: {path}")
        if _git(path, "rev-parse", "--is-inside-work-tree", check=False).stdout.strip() != "true":
            raise EngineeringWorkspaceError(f"engineering workspace is not a git worktree: {path}")
        return cls(source, path, branch.strip(), baseline_commit.strip())

    def changed_files(self) -> tuple[str, ...]:
        tracked = _git(
            self.path,
            "diff",
            "--name-only",
            "-z",
            self.baseline_commit,
            check=False,
        ).stdout.split("\0")
        untracked = _git(
            self.path,
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
            check=False,
        ).stdout.split("\0")
        seen: set[str] = set()
        changed: list[str] = []
        for item in [*tracked, *untracked]:
            normalized = item.strip()
            if not normalized or normalized in seen:
                continue
            if "__pycache__/" in normalized or normalized.endswith((".pyc", ".pyo")):
                continue
            seen.add(normalized)
            changed.append(normalized)
        return tuple(changed)
