from __future__ import annotations

from pathlib import Path
import subprocess

from .models import Event


class GitSensorError(RuntimeError):
    """Raised when a repository cannot be inspected."""


class GitSensor:
    """Detect new HEAD commits in a local Git repository.

    The first poll establishes a baseline and emits nothing. A later HEAD
    change produces one normalized ``git.commit`` event.
    """

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
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise GitSensorError(f"Unable to inspect Git repository: {self.repository}") from exc

        return result.stdout.strip()

    def _validate_repository(self) -> None:
        if self._git("rev-parse", "--is-inside-work-tree") != "true":
            raise GitSensorError(f"Not a Git working tree: {self.repository}")

    def _snapshot(self) -> dict[str, str]:
        return {
            "sha": self._git("rev-parse", "HEAD"),
            "subject": self._git("log", "-1", "--format=%s"),
            "author": self._git("log", "-1", "--format=%an"),
            "authored_at": self._git("log", "-1", "--format=%aI"),
        }

    def poll(self) -> Event | None:
        current = self._snapshot()

        if self._last_sha is None:
            self._last_sha = current["sha"]
            return None

        if current["sha"] == self._last_sha:
            return None

        previous_sha = self._last_sha
        self._last_sha = current["sha"]

        return Event(
            event_type="git.commit",
            source="git",
            content=current["subject"],
            context={
                "repository": str(self.repository),
                "sha": current["sha"],
                "previous_sha": previous_sha,
                "author": current["author"],
                "authored_at": current["authored_at"],
            },
        )
