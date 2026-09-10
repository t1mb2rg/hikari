"""Authenticated dashboard facade over operator-owned policy and capability truth."""
from __future__ import annotations

from capabilities.operator import CapabilityOperatorControls, OperatorPolicyConflict, assert_external_state
from integrations.github.governance import GitHubPolicyStore

from .probes import DashboardProbeConfig


class DashboardOperatorControls:
    """Thin UI adapter; authentication and CSRF checks belong to dashboard.app."""

    def __init__(self, config: DashboardProbeConfig):
        self.config = config
        self.github_policy_path = config.state_dir / "github_policy.json"
        self.capability_operator = CapabilityOperatorControls(config.repository, config.state_dir)
        self.growth_policy_path = self.capability_operator.growth_policy_path
        self.growth_path = self.capability_operator.growth_path

    def get_github_policy(self) -> dict:
        return GitHubPolicyStore(self.github_policy_path).load()

    def save_github_policy(self, document: dict, revision: str) -> dict:
        assert_external_state(self.github_policy_path, self.config.repository)
        try:
            return GitHubPolicyStore(self.github_policy_path).save(
                document, expected_revision=revision, operator=True)
        except ValueError as exc:
            if "正在保存" in str(exc) or "已被修改" in str(exc):
                raise OperatorPolicyConflict(str(exc)) from None
            raise

    def get_growth_policy(self) -> dict:
        return self.capability_operator.get_growth_policy()

    def save_growth_policy(self, document: dict, revision: str) -> dict:
        return self.capability_operator.save_growth_policy(document, revision)

    def growth_snapshot(self) -> dict:
        return self.capability_operator.growth_snapshot()

    def operator_activate_capability(self, request_id: str, digest: str) -> dict:
        return self.capability_operator.operator_activate_capability(request_id, digest)

    def auto_activate_tested_capabilities(self) -> dict:
        return self.capability_operator.auto_activate_tested_capabilities()
