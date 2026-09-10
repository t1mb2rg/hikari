"""Hikari-owned GitHub adapter. Authentication remains with the local gh client."""
from .client import GitHubClient, GitHubError, GitHubOutcomeUnknown, repository_from_origin
from .actions import GitHubActionService
from .governance import GitHubPolicyStore

__all__ = ["GitHubClient", "GitHubError", "GitHubOutcomeUnknown", "repository_from_origin", "GitHubActionService", "GitHubPolicyStore"]
