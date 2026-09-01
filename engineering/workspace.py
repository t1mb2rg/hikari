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


def _clean_file_list(items: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    changed: list[str] = []
    for item in items:
        normalized = item.strip()
        if not normalized or normalized in seen:
            continue
        if "__pycache__/" in normalized or normalized.endswith((".pyc", ".pyo")):
            continue
        seen.add(normalized)
        changed.append(normalized)
    return tuple(changed)


@dataclass(frozen=True, slots=True)
class EngineeringWorkspace:
    source_repo: Path
    path: Path
    branch: str
    baseline_commit: str

    @classmethod
    def source_head(cls, repository: str | Path) -> str:
        """Resolve one trustworthy committed source snapshot.

        EngineeringSession worktrees intentionally refuse an ambiguous dirty source.
        A task operates on a committed repository snapshot rather than silently mixing
        old committed files with arbitrary local working-copy edits.
        """

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
        return baseline

    @classmethod
    def create(cls, repository: str | Path, session_id: str) -> "EngineeringWorkspace":
        source = Path(repository).expanduser().resolve()
        baseline = cls.source_head(source)

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
        """Return all files changed by this EngineeringSession since its immutable baseline."""

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
        return _clean_file_list([*tracked, *untracked])

    def uncommitted_files(self) -> tuple[str, ...]:
        """Return only current dirty worktree/index files relative to this branch HEAD.

        This deliberately excludes earlier authorized commits in the same EngineeringSession.
        It is therefore the correct mutation check for a read-only follow-up turn after a prior
        maintainer turn has already committed legitimate session history.
        """

        tracked = _git(
            self.path,
            "diff",
            "--name-only",
            "-z",
            "HEAD",
            check=False,
        ).stdout.split("\0")
        staged = _git(
            self.path,
            "diff",
            "--cached",
            "--name-only",
            "-z",
            "HEAD",
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
        return _clean_file_list([*tracked, *staged, *untracked])

    def diff_text(self) -> str:
        """Return the session diff, including readable untracked text files."""

        tracked = _git(
            self.path,
            "diff",
            "--no-ext-diff",
            "--unified=0",
            self.baseline_commit,
            check=False,
        ).stdout
        chunks = [tracked]
        for relative in _git(
            self.path,
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
            check=False,
        ).stdout.split("\0"):
            if not relative:
                continue
            path = self.path / relative
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            chunks.append(
                f"diff --git a/{relative} b/{relative}\n"
                f"--- /dev/null\n+++ b/{relative}\n"
                + "".join(f"+{line}\n" for line in content.splitlines())
            )
        return "\n".join(chunks)
