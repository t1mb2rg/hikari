from __future__ import annotations

import os
from pathlib import Path
import subprocess

from ..models import Event


_WINDOWS_CREATE_NO_WINDOW = 0x08000000


def _git_creationflags(platform_name: str | None = None) -> int:
    """Keep Git probes invisible when the resident is hosted by pythonw."""

    name = os.name if platform_name is None else platform_name
    if name != "nt":
        return 0
    return int(getattr(subprocess, "CREATE_NO_WINDOW", _WINDOWS_CREATE_NO_WINDOW))


class GitSensorError(RuntimeError):
    """Raised when a repository cannot be inspected."""


class GitSensor:
    """Detect new HEAD commits in a local Git repository.

    The first poll establishes a baseline and emits nothing. A later HEAD
    change produces one normalized ``git.commit`` event.
    """

    name = "git"

    def __init__(self, repository: str | Path):
        self.repository = Path(repository).resolve()
        self._last_sha: str | None = None
        self._validate_repository()

    def _git(self, *args: str) -> str:
        try:
            result = subprocess.run(
                ["git", "-C", str(self.repository), *args],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                creationflags=_git_creationflags(),
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise GitSensorError(f"Unable to inspect Git repository: {self.repository}") from exc

        return result.stdout.strip()

    def _validate_repository(self) -> None:
        if self._git("rev-parse", "--is-inside-work-tree") != "true":
            raise GitSensorError(f"Not a Git working tree: {self.repository}")

    def _commit_metadata(self) -> dict[str, str]:
        metadata = self._git("log", "-1", "--format=%s%x00%an%x00%aI").split("\x00")
        if len(metadata) != 3:
            raise GitSensorError(f"Unable to read commit metadata: {self.repository}")
        subject, author, authored_at = metadata
        return {
            "subject": subject,
            "author": author,
            "authored_at": authored_at,
        }

    def poll(self) -> list[Event]:
        # Quiet cycles need only one Git process. Metadata is fetched only after
        # HEAD changes, which keeps a resident polling loop cheap and silent.
        current_sha = self._git("rev-parse", "HEAD")

        if self._last_sha is None:
            self._last_sha = current_sha
            return []

        if current_sha == self._last_sha:
            return []

        previous_sha = self._last_sha
        self._last_sha = current_sha
        metadata = self._commit_metadata()

        return [
            Event(
                event_type="git.commit",
                source=self.name,
                content=metadata["subject"],
                context={
                    "repository": str(self.repository),
                    "sha": current_sha,
                    "previous_sha": previous_sha,
                    "author": metadata["author"],
                    "authored_at": metadata["authored_at"],
                },
            )
        ]
