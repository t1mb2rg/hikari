from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence


ASSESSMENT_EXECUTABLE = "executable"
ASSESSMENT_CAPABILITY_GAP = "capability_gap"
ASSESSMENT_ESCALATION_REQUIRED = "escalation_required"


@dataclass(frozen=True, slots=True)
class CapabilityState:
    """Grounded state for one capability Hikari may need to perform a task.

    ``available`` is implementation truth. ``delegated`` is standing-authority truth.
    These are deliberately independent: a project mandate may delegate an outcome that
    the current runtime has not learned how to perform yet.
    """

    key: str
    available: bool
    delegated: bool
    scope: str
    escalation_required: bool = False
    gap: str | None = None

    def __post_init__(self) -> None:
        key = self.key.strip()
        scope = self.scope.strip()
        gap = self.gap.strip() if isinstance(self.gap, str) and self.gap.strip() else None
        if not key:
            raise ValueError("capability key must not be empty")
        if not scope:
            raise ValueError("capability scope must not be empty")
        object.__setattr__(self, "key", key)
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "gap", gap)

    def to_mapping(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "available": self.available,
            "delegated": self.delegated,
            "scope": self.scope,
        }
        if self.escalation_required:
            payload["escalation_required"] = True
        if self.gap is not None:
            payload["gap"] = self.gap
        return payload


@dataclass(frozen=True, slots=True)
class ProjectMandate:
    """Standing delegation for one project rather than per-action approval."""

    project_id: str
    role: str
    scope: str
    active: bool
    delegated_outcomes: tuple[str, ...]
    escalation_outcomes: tuple[str, ...]
    principle: str

    def __post_init__(self) -> None:
        project_id = self.project_id.strip()
        role = self.role.strip()
        scope = self.scope.strip()
        principle = self.principle.strip()
        if not project_id:
            raise ValueError("project mandate project_id must not be empty")
        if not role:
            raise ValueError("project mandate role must not be empty")
        if not scope:
            raise ValueError("project mandate scope must not be empty")
        if not principle:
            raise ValueError("project mandate principle must not be empty")
        object.__setattr__(self, "project_id", project_id)
        object.__setattr__(self, "role", role)
        object.__setattr__(self, "scope", scope)
        object.__setattr__(self, "principle", principle)
        object.__setattr__(
            self,
            "delegated_outcomes",
            tuple(item.strip() for item in self.delegated_outcomes if item.strip()),
        )
        object.__setattr__(
            self,
            "escalation_outcomes",
            tuple(item.strip() for item in self.escalation_outcomes if item.strip()),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "project_id": self.project_id,
            "role": self.role,
            "active": self.active,
            "scope": self.scope,
            "delegated_outcomes": self.delegated_outcomes,
            "escalate": self.escalation_outcomes,
            "principle": self.principle,
        }


@dataclass(frozen=True, slots=True)
class TaskCapabilityAssessment:
    """Deterministic assessment after cognition identifies required capability keys."""

    status: str
    required: tuple[str, ...]
    available: tuple[str, ...]
    missing: tuple[str, ...]
    escalation: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.status not in {
            ASSESSMENT_EXECUTABLE,
            ASSESSMENT_CAPABILITY_GAP,
            ASSESSMENT_ESCALATION_REQUIRED,
        }:
            raise ValueError(f"unsupported task capability assessment: {self.status}")

    def to_mapping(self) -> dict[str, object]:
        return {
            "status": self.status,
            "required": self.required,
            "available": self.available,
            "missing": self.missing,
            "escalation": self.escalation,
        }


def assess_task_capabilities(
    required: Sequence[str],
    capabilities: Mapping[str, CapabilityState],
) -> TaskCapabilityAssessment:
    """Assess requirements against implemented capability + standing delegation.

    Precedence is intentional:
    - an explicitly non-delegated/high-impact capability requires escalation;
    - a delegated capability that is not implemented is a capability gap;
    - only implemented + delegated requirements are executable.

    Unknown capability keys are treated as capability gaps rather than fabricated ability.
    """

    normalized: list[str] = []
    seen: set[str] = set()
    for item in required:
        key = str(item).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        normalized.append(key)

    available: list[str] = []
    missing: list[str] = []
    escalation: list[str] = []

    for key in normalized:
        capability = capabilities.get(key)
        if capability is None:
            missing.append(key)
            continue
        if capability.escalation_required or not capability.delegated:
            escalation.append(key)
            continue
        if not capability.available:
            missing.append(key)
            continue
        available.append(key)

    if escalation:
        status = ASSESSMENT_ESCALATION_REQUIRED
    elif missing:
        status = ASSESSMENT_CAPABILITY_GAP
    else:
        status = ASSESSMENT_EXECUTABLE

    return TaskCapabilityAssessment(
        status=status,
        required=tuple(normalized),
        available=tuple(available),
        missing=tuple(missing),
        escalation=tuple(escalation),
    )


def hikari_engineering_capabilities(engineering_enabled: bool) -> dict[str, CapabilityState]:
    """Current implementation truth plus the standing Hikari-project mandate."""

    return {
        "engineering.repository.read": CapabilityState(
            "engineering.repository.read",
            available=engineering_enabled,
            delegated=engineering_enabled,
            scope="project_repository",
        ),
        "engineering.repository.write": CapabilityState(
            "engineering.repository.write",
            available=engineering_enabled,
            delegated=engineering_enabled,
            scope="isolated_project_worktree",
        ),
        "engineering.commands.run": CapabilityState(
            "engineering.commands.run",
            available=False,
            delegated=engineering_enabled,
            scope="project_worktree",
            gap="generic_project_command_execution_not_implemented_yet",
        ),
        "engineering.tests.run": CapabilityState(
            "engineering.tests.run",
            available=engineering_enabled,
            delegated=engineering_enabled,
            scope="project_worktree",
        ),
        "engineering.git.commit": CapabilityState(
            "engineering.git.commit",
            available=engineering_enabled,
            delegated=engineering_enabled,
            scope="isolated_engineering_branch",
        ),
        "engineering.git.push_non_protected": CapabilityState(
            "engineering.git.push_non_protected",
            available=False,
            delegated=engineering_enabled,
            scope="engineering_branch",
            gap="push_execution_not_implemented_yet",
        ),
        "engineering.git.open_or_update_draft_pr": CapabilityState(
            "engineering.git.open_or_update_draft_pr",
            available=False,
            delegated=engineering_enabled,
            scope="engineering_branch",
            gap="github_publish_execution_not_implemented_yet",
        ),
        "engineering.git.merge_protected": CapabilityState(
            "engineering.git.merge_protected",
            available=False,
            delegated=False,
            scope="protected_branch",
            escalation_required=True,
        ),
        "engineering.git.force_push": CapabilityState(
            "engineering.git.force_push",
            available=False,
            delegated=False,
            scope="protected_or_shared_history",
            escalation_required=True,
        ),
        "engineering.secrets.modify": CapabilityState(
            "engineering.secrets.modify",
            available=False,
            delegated=False,
            scope="secret_configuration",
            escalation_required=True,
        ),
        "engineering.production.deploy": CapabilityState(
            "engineering.production.deploy",
            available=False,
            delegated=False,
            scope="external_or_production_system",
            escalation_required=True,
        ),
    }


def hikari_project_mandate(engineering_enabled: bool) -> ProjectMandate:
    return ProjectMandate(
        project_id="hikari",
        role="maintainer",
        active=engineering_enabled,
        scope="configured_hikari_repository",
        delegated_outcomes=(
            "inspect",
            "edit_project_files",
            "run_project_commands",
            "run_tests",
            "create_engineering_branch",
            "commit_engineering_changes",
            "push_non_protected_engineering_branch",
            "open_or_update_draft_pr",
            "diagnose_and_retry_project_failures",
        ),
        escalation_outcomes=(
            "merge_protected_branch",
            "force_push_shared_history",
            "change_or_expose_secrets",
            "production_or_external_deployment",
            "destructive_data_migration",
            "permission_boundary_expansion",
            "project_north_star_change",
            "material_external_cost",
        ),
        principle=(
            "Within the delegated project scope, Hikari should complete ordinary engineering work "
            "without asking for approval at every step. Escalation is for boundary changes or "
            "high-impact external effects, not routine edits, tests, commits, or draft PR upkeep."
        ),
    )
