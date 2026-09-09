"""Hikari-owned GitHub adapter. Authentication remains with the local gh client."""
from .client import GitHubClient, GitHubError, repository_from_origin

__all__ = ["GitHubClient", "GitHubError", "repository_from_origin"]
