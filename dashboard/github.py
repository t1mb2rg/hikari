from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock
import time
import os

from integrations.github import GitHubClient, GitHubError, repository_from_origin
from integrations.github.governance import GitHubEvidenceStore, GitHubMergeGate
from .settings import DashboardSettings


class DashboardGitHub:
    def __init__(self, repository: Path, settings: DashboardSettings, state_dir: Path):
        self.repository = repository
        self.settings = settings
        self.state_dir = Path(state_dir)
        self._lock = Lock()
        self._cache = None
        self._cache_at = 0.0

    def assessment(self, number: int) -> dict:
        name = self.settings.values().get("HIKARI_GITHUB_REPOSITORY", "").strip() or repository_from_origin(self.repository)
        return GitHubMergeGate(
            GitHubClient(name, environment={**self.settings.values(), **os.environ}), GitHubEvidenceStore(self.state_dir / "github_evidence.db", read_only=True),
            self.state_dir / "github_policy.json",
        ).assess(number)

    def snapshot(self) -> dict:
        with self._lock:
            if self._cache is not None and time.monotonic() - self._cache_at < 30:
                return self._cache
            name = ""
            try:
                name = self.settings.values().get("HIKARI_GITHUB_REPOSITORY", "").strip() or repository_from_origin(self.repository)
                client = GitHubClient(name, environment={**self.settings.values(), **os.environ})
                with ThreadPoolExecutor(max_workers=3) as pool:
                    info = pool.submit(client.repository_info)
                    prs = pool.submit(client.pull_requests)
                    runs = pool.submit(client.workflow_runs)
                    result = {"status": "healthy", "repository": name, "info": info.result(),
                              "pull_requests": prs.result(), "runs": runs.result(), "observed_at": time.time(),
                              "message": "已通过本机 gh 认证读取远端；尚未据此授权合并"}
            except (GitHubError, ValueError, OSError) as exc:
                result = {"status": "error", "repository": name, "message": str(exc),
                          "pull_requests": [], "runs": [], "observed_at": time.time()}
            self._cache, self._cache_at = result, time.monotonic()
            return result
