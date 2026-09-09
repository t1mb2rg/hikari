from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock
import time

from integrations.github import GitHubClient, GitHubError, repository_from_origin
from .settings import DashboardSettings


class DashboardGitHub:
    def __init__(self, repository: Path, settings: DashboardSettings):
        self.repository = repository
        self.settings = settings
        self._lock = Lock()
        self._cache = None
        self._cache_at = 0.0

    def snapshot(self) -> dict:
        with self._lock:
            if self._cache is not None and time.monotonic() - self._cache_at < 30:
                return self._cache
            name = ""
            try:
                name = self.settings.values().get("HIKARI_GITHUB_REPOSITORY", "").strip() or repository_from_origin(self.repository)
                client = GitHubClient(name)
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
