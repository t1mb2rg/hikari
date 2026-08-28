from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
import tempfile
import time
from typing import Callable, Iterable

import yaml

from .authorization import AuthorizedAction
from .contract import ActionRisk, ActionSpec
from .execution import ActionExecutionError, ExecutionResult


FORGE_RUN_ACTION = "run_forge_task"

_ALLOWED_FORGE_ARGUMENTS = frozenset({"project_id", "goal", "constraints", "acceptance"})
_FORGE_BACKENDS = frozenset({"claude", "codex"})
_FORGE_REVIEW_MODES = frozenset({"off", "evidence", "always"})

# Values track the current Forge CLI (`forge run --claude-permission-mode`).
# `bypassPermissions` is intentionally absent: Forge does not expose a mode
# that skips the permission boundary it relies on.
_FORGE_CLAUDE_PERMISSION_MODES = ("auto", "acceptEdits", "manual", "dontAsk", "plan")


@dataclass(frozen=True)
class ForgeSupervisorSettings:
    """Caller-owned optional FlexiWeb supervisor wiring for one Forge project."""

    url: str
    site: str = "chatgpt"
    session: str = "forge-supervisor"
    conversation_url: str | None = None
    after_attempt: int = 2

    def __post_init__(self) -> None:
        url = self.url.strip()
        site = self.site.strip()
        session = self.session.strip()
        if not url:
            raise ValueError("Forge supervisor url must not be empty")
        if not site:
            raise ValueError("Forge supervisor site must not be empty")
        if not session:
            raise ValueError("Forge supervisor session must not be empty")
        if self.conversation_url is not None and not isinstance(self.conversation_url, str):
            raise TypeError("Forge supervisor conversation_url must be a string or None")
        if (
            not isinstance(self.after_attempt, int)
            or isinstance(self.after_attempt, bool)
            or self.after_attempt < 1
        ):
            raise ValueError("Forge supervisor after_attempt must be an integer >= 1")

        conversation_url = (
            self.conversation_url.strip() if isinstance(self.conversation_url, str) else None
        )
        if not conversation_url:
            conversation_url = None

        object.__setattr__(self, "url", url)
        object.__setattr__(self, "site", site)
        object.__setattr__(self, "session", session)
        object.__setattr__(self, "conversation_url", conversation_url)
        object.__setattr__(self, "after_attempt", int(self.after_attempt))

    def argv_flags(self) -> list[str]:
        """Trusted supervisor flags matching the current Forge run CLI."""

        flags = [
            "--supervisor-url", self.url,
            "--supervisor-site", self.site,
            "--supervisor-session", self.session,
        ]
        if self.conversation_url is not None:
            flags += ["--supervisor-conversation-url", self.conversation_url]
        flags += ["--supervisor-after-attempt", str(self.after_attempt)]
        return flags


def _optional_trusted_string(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"Forge {label} must be a string or None")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"Forge {label} must not be empty")
    return normalized


@dataclass(frozen=True)
class ForgeProjectProfile:
    """Caller-owned trusted execution profile for one Forge project.

    Everything Forge needs to run lives here: repository and verification,
    executable/backend limits, optional path scope, Claude permission settings,
    M2 worker/strong/reviewer routing, semantic-review policy, and supervisor
    wiring. The model never supplies any of it.

    Hikari's current `run_forge_task` action intentionally keeps Forge delivery
    at Forge's documented `none` default. Publish/integrate authority is not
    represented by this profile and cannot be smuggled through task content.
    """

    project_id: str
    repository: str | Path
    verification: Iterable[str]
    executable: str = "forge"
    backend: str = "claude"
    max_attempts: int = 3
    claude_permission_mode: str = "auto"
    claude_max_turns: int = 30
    allowed_paths: Iterable[str] = ()
    worker_model: str | None = None
    strong_model: str | None = None
    reviewer_model: str | None = None
    review_mode: str = "off"
    escalation_threshold: int = 2
    broad_change_threshold: int = 10
    supervisor: ForgeSupervisorSettings | None = None

    def __post_init__(self) -> None:
        project_id = self.project_id.strip()
        if not project_id:
            raise ValueError("Forge project_id must not be empty")
        if self.backend not in _FORGE_BACKENDS:
            raise ValueError(f"Forge backend must be one of claude, codex; got {self.backend!r}")
        executable = self.executable.strip()
        if not executable:
            raise ValueError("Forge executable must not be empty")
        if (
            not isinstance(self.max_attempts, int)
            or isinstance(self.max_attempts, bool)
            or self.max_attempts < 1
        ):
            raise ValueError("Forge max_attempts must be an integer >= 1")

        if not isinstance(self.claude_permission_mode, str):
            raise TypeError("Forge claude_permission_mode must be a string")
        claude_permission_mode = self.claude_permission_mode.strip()
        if claude_permission_mode not in _FORGE_CLAUDE_PERMISSION_MODES:
            raise ValueError(
                "Forge claude_permission_mode must be one of "
                + ", ".join(_FORGE_CLAUDE_PERMISSION_MODES)
                + f"; got {claude_permission_mode!r}"
            )
        if (
            not isinstance(self.claude_max_turns, int)
            or isinstance(self.claude_max_turns, bool)
            or self.claude_max_turns < 1
        ):
            raise ValueError("Forge claude_max_turns must be an integer >= 1")

        repository = Path(self.repository).expanduser()
        if not repository.is_dir():
            raise ValueError(f"Forge repository must be an existing directory: {repository}")

        if isinstance(self.verification, (str, bytes)):
            raise TypeError(
                "Forge verification must be an iterable of command strings, not str/bytes"
            )
        verification = tuple(str(item).strip() for item in self.verification)
        if not verification or any(not item for item in verification):
            raise ValueError("Forge verification must be a non-empty list of non-empty commands")

        if isinstance(self.allowed_paths, (str, bytes)):
            raise TypeError("Forge allowed_paths must be an iterable of path strings, not str/bytes")
        allowed_paths = tuple(str(item).strip() for item in self.allowed_paths)
        if any(not item for item in allowed_paths):
            raise ValueError("Forge allowed_paths entries must not be empty")

        worker_model = _optional_trusted_string(self.worker_model, "worker_model")
        strong_model = _optional_trusted_string(self.strong_model, "strong_model")
        reviewer_model = _optional_trusted_string(self.reviewer_model, "reviewer_model")

        if not isinstance(self.review_mode, str):
            raise TypeError("Forge review_mode must be a string")
        review_mode = self.review_mode.strip()
        if review_mode not in _FORGE_REVIEW_MODES:
            raise ValueError(
                "Forge review_mode must be one of off, evidence, always; "
                f"got {review_mode!r}"
            )
        if (
            not isinstance(self.escalation_threshold, int)
            or isinstance(self.escalation_threshold, bool)
            or self.escalation_threshold < 1
        ):
            raise ValueError("Forge escalation_threshold must be an integer >= 1")
        if (
            not isinstance(self.broad_change_threshold, int)
            or isinstance(self.broad_change_threshold, bool)
            or self.broad_change_threshold < 1
        ):
            raise ValueError("Forge broad_change_threshold must be an integer >= 1")

        has_m2_model_policy = any(
            value is not None for value in (worker_model, strong_model, reviewer_model)
        ) or review_mode != "off"
        if self.backend != "claude" and has_m2_model_policy:
            raise ValueError("Forge M2 model routing/review requires the claude backend")
        if review_mode != "off" and reviewer_model is None:
            raise ValueError("Forge reviewer_model is required when review_mode is enabled")

        if self.supervisor is not None and not isinstance(self.supervisor, ForgeSupervisorSettings):
            raise TypeError("Forge supervisor must be ForgeSupervisorSettings or None")

        object.__setattr__(self, "project_id", project_id)
        object.__setattr__(self, "repository", repository.resolve())
        object.__setattr__(self, "verification", verification)
        object.__setattr__(self, "executable", executable)
        object.__setattr__(self, "backend", self.backend)
        object.__setattr__(self, "max_attempts", int(self.max_attempts))
        object.__setattr__(self, "claude_permission_mode", claude_permission_mode)
        object.__setattr__(self, "claude_max_turns", int(self.claude_max_turns))
        object.__setattr__(self, "allowed_paths", allowed_paths)
        object.__setattr__(self, "worker_model", worker_model)
        object.__setattr__(self, "strong_model", strong_model)
        object.__setattr__(self, "reviewer_model", reviewer_model)
        object.__setattr__(self, "review_mode", review_mode)
        object.__setattr__(self, "escalation_threshold", int(self.escalation_threshold))
        object.__setattr__(self, "broad_change_threshold", int(self.broad_change_threshold))
        object.__setattr__(self, "supervisor", self.supervisor)


class ForgeProjectRegistry:
    """Caller-owned trusted mapping from project_id to one Forge execution profile.

    The model only ever supplies a project_id. Repository path, verification,
    Forge executable/backend, scope, attempt limits, model policy, review
    policy, permission settings, and supervisor settings are resolved here.
    """

    def __init__(self, profiles: Iterable[ForgeProjectProfile] = ()) -> None:
        self._profiles: dict[str, ForgeProjectProfile] = {}
        for profile in profiles:
            self.register(profile)

    def register(self, profile: ForgeProjectProfile) -> None:
        if not isinstance(profile, ForgeProjectProfile):
            raise TypeError("ForgeProjectRegistry accepts only ForgeProjectProfile")
        if profile.project_id in self._profiles:
            raise ValueError(f"duplicate Forge project: {profile.project_id}")
        self._profiles[profile.project_id] = profile

    def resolve(self, project_id: str) -> ForgeProjectProfile:
        if not isinstance(project_id, str) or not project_id.strip():
            raise ActionExecutionError("run_forge_task project_id must be a non-empty string")
        profile = self._profiles.get(project_id.strip())
        if profile is None:
            raise ActionExecutionError(f"unknown Forge project: {project_id!r}")
        return profile

    def __contains__(self, project_id: object) -> bool:
        return isinstance(project_id, str) and project_id.strip() in self._profiles


def forge_task_action_spec() -> ActionSpec:
    """Expose run_forge_task as one REVERSIBLE action that requires confirmation.

    The description is the only model-visible part of this spec, so it doubles
    as the argument contract: every required JSON type is spelled out there.
    Trusted execution settings never appear in it.
    """

    return ActionSpec(
        name=FORGE_RUN_ACTION,
        description=(
            "Dispatch one bounded engineering task to Forge for a pre-registered "
            "trusted project; Forge implements and verifies the change in its own "
            "worktree. Arguments are exactly: "
            "project_id: non-empty string; "
            "goal: non-empty string; "
            "constraints: JSON array of strings, may be empty; "
            "acceptance: non-empty JSON array of strings. "
            "No other arguments are allowed."
        ),
        risk=ActionRisk.REVERSIBLE,
        requires_confirmation=True,
    )


def build_forge_task_yaml(
    *,
    goal: str,
    constraints: Iterable[str],
    acceptance: Iterable[str],
    verification: Iterable[str],
) -> str:
    """Render one task file in the YAML shape Forge's current Task.load consumes.

    goal is a plain string; constraints, acceptance, and verification are YAML
    lists. Verification is accepted here only from a trusted profile.
    """

    payload = {
        "goal": goal,
        "constraints": list(constraints),
        "acceptance": list(acceptance),
        "verification": list(verification),
    }
    return yaml.safe_dump(
        payload,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )


def build_forge_argv(
    *,
    executable: str,
    task_file: Path,
    profile: ForgeProjectProfile,
) -> list[str]:
    """Build the current Forge CLI argument list from trusted profile state.

    The historical/default profile intentionally preserves the existing argv
    shape. M2 flags are emitted only when caller-owned M2 policy is configured.
    Forge's delivery mode stays at its documented default `none`; Hikari exposes
    no remote, target-branch, publish, or integrate configuration here.
    """

    argv = [
        executable,
        "run",
        str(task_file),
        "--repo", str(profile.repository),
        "--backend", profile.backend,
        "--max-attempts", str(profile.max_attempts),
    ]
    if profile.backend == "claude":
        argv += [
            "--claude-permission-mode", profile.claude_permission_mode,
            "--claude-max-turns", str(profile.claude_max_turns),
        ]
        if profile.worker_model is not None:
            argv += ["--worker-model", profile.worker_model]
        if profile.strong_model is not None:
            argv += [
                "--strong-model", profile.strong_model,
                "--escalation-threshold", str(profile.escalation_threshold),
            ]
        if profile.reviewer_model is not None:
            argv += ["--reviewer-model", profile.reviewer_model]
        if profile.review_mode != "off":
            argv += [
                "--review-mode", profile.review_mode,
                "--broad-change-threshold", str(profile.broad_change_threshold),
            ]
    for allowed_path in profile.allowed_paths:
        argv += ["--allowed-path", allowed_path]
    if profile.supervisor is not None:
        argv.extend(profile.supervisor.argv_flags())
    return argv


ForgeRunner = Callable[[list[str]], int]


def _default_forge_runner(argv: list[str]) -> int:
    """Run Forge from an argument list only. shell=True is never used."""

    completed = subprocess.run(argv, shell=False)
    return completed.returncode


def _validated_forge_arguments(arguments: dict[str, object]) -> dict[str, object]:
    unexpected = sorted(str(name) for name in arguments if name not in _ALLOWED_FORGE_ARGUMENTS)
    if unexpected:
        raise ActionExecutionError(
            "run_forge_task arguments are limited to project_id, goal, constraints, "
            f"acceptance; rejected: {', '.join(unexpected)}"
        )
    missing = sorted(_ALLOWED_FORGE_ARGUMENTS - set(arguments))
    if missing:
        raise ActionExecutionError(f"run_forge_task missing argument(s): {', '.join(missing)}")

    project_id = arguments["project_id"]
    if not isinstance(project_id, str) or not project_id.strip():
        raise ActionExecutionError("run_forge_task project_id must be a non-empty string")

    goal = arguments["goal"]
    if not isinstance(goal, str) or not goal.strip():
        raise ActionExecutionError("run_forge_task goal must be a non-empty string")

    return {
        "project_id": project_id.strip(),
        "goal": goal.strip(),
        "constraints": _forge_string_list(arguments["constraints"], "constraints", allow_empty=True),
        "acceptance": _forge_string_list(arguments["acceptance"], "acceptance", allow_empty=False),
    }


def _forge_string_list(value: object, label: str, *, allow_empty: bool) -> list[str]:
    if not isinstance(value, list):
        raise ActionExecutionError(f"run_forge_task {label} must be a list of strings")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ActionExecutionError(
                f"run_forge_task {label} entries must be non-empty strings"
            )
        items.append(item.strip())
    if not allow_empty and not items:
        raise ActionExecutionError(f"run_forge_task {label} must contain at least one entry")
    return items


class ForgeTaskAdapter:
    """Dispatch one confirmed run_forge_task through Forge's real CLI contract.

    The model supplies only project_id, goal, constraints, and acceptance.
    Repository, verification, executable/backend, scope, attempt limits,
    Claude/M2 model policy, permission settings, and supervisor settings are
    resolved from the caller-owned registry. Forge is invoked without a shell.
    """

    action_name = FORGE_RUN_ACTION

    def __init__(
        self,
        registry: ForgeProjectRegistry,
        *,
        work_dir: str | Path | None = None,
        runner: ForgeRunner | None = None,
    ) -> None:
        if not isinstance(registry, ForgeProjectRegistry):
            raise TypeError("ForgeTaskAdapter requires a ForgeProjectRegistry")
        self.registry = registry
        self.work_dir = (
            Path(work_dir)
            if work_dir is not None
            else Path(tempfile.gettempdir()) / "hikari-forge-tasks"
        )
        self._runner = runner or _default_forge_runner

    def execute(self, action: AuthorizedAction) -> ExecutionResult:
        if not isinstance(action, AuthorizedAction):
            raise TypeError("ForgeTaskAdapter accepts only AuthorizedAction")

        proposal = action.proposal
        if proposal.action_name != self.action_name:
            raise ActionExecutionError(
                f"ForgeTaskAdapter cannot execute {proposal.action_name!r}"
            )

        arguments = _validated_forge_arguments(proposal.arguments)
        profile = self.registry.resolve(arguments["project_id"])

        task_yaml = build_forge_task_yaml(
            goal=arguments["goal"],
            constraints=arguments["constraints"],
            acceptance=arguments["acceptance"],
            verification=profile.verification,
        )
        task_file = self._write_task_file(profile.project_id, task_yaml)

        argv = build_forge_argv(
            executable=profile.executable,
            task_file=task_file,
            profile=profile,
        )
        try:
            try:
                returncode = self._runner(argv)
            except OSError as exc:
                raise ActionExecutionError(f"Forge executable failed to start: {exc}") from exc
        finally:
            self._remove_task_file(task_file)
        return ExecutionResult(
            action_name=self.action_name,
            success=returncode == 0,
            summary=(
                f"Forge run for project {profile.project_id!r} "
                f"finished with exit code {returncode}"
            ),
        )

    @staticmethod
    def _remove_task_file(task_file: Path) -> None:
        """Best-effort removal of the generated task YAML after the Forge run.

        The task file exists only so the Forge CLI can read it; Forge streams
        its own report and evidence elsewhere. Removal failure must never mask
        the Forge result, so this swallows removal errors.
        """

        try:
            task_file.unlink(missing_ok=True)
        except OSError:
            pass

    def _write_task_file(self, project_id: str, task_yaml: str) -> Path:
        try:
            self.work_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ActionExecutionError(
                f"Forge task directory unavailable: {self.work_dir}"
            ) from exc

        safe_id = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in project_id)
        task_file = self.work_dir / f"hikari-forge-{safe_id}-{time.time_ns()}.yaml"
        try:
            task_file.write_text(task_yaml, encoding="utf-8", newline="\n")
        except OSError as exc:
            raise ActionExecutionError(f"Forge task file write failed: {task_file}") from exc
        return task_file
