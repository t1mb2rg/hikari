from __future__ import annotations

from dataclasses import dataclass

from .effects import SUPPORTED_ENGINEERING_EFFECTS
from .session import EngineeringProtocolError


_EFFECT_CAPABILITIES: dict[str, tuple[str, ...]] = {
    "inspect_project": ("engineering.repository.read",),
    "run_project_command": ("engineering.commands.run",),
    "maintain_project": (
        "engineering.repository.read",
        "engineering.repository.write",
        "engineering.tests.run",
        "engineering.git.commit",
    ),
    "push_engineering_branch": ("engineering.git.push_non_protected",),
    "open_or_update_draft_pr": ("engineering.git.open_or_update_draft_pr",),
}


@dataclass(frozen=True, slots=True)
class PlannedEngineeringStep:
    effect: str
    instruction: str


@dataclass(frozen=True, slots=True)
class EngineeringGoalPlan:
    goal: str
    steps: tuple[PlannedEngineeringStep, ...]
    required_capabilities: tuple[str, ...]


def _step_instruction(effect: str, goal: str, original_request: str) -> str:
    if effect == "maintain_project":
        # Keep the human goal first. Worker commit metadata is derived from the turn
        # intent, while scope enforcement still sees the full original request below.
        return (
            f"{goal}\n"
            "完成这个持久工程目标需要的仓库修改、必要验证和提交。"
            "只完成维护步骤，不进行 push、PR、merge 或 force push。"
            f"\n持久目标：{goal}\n原始请求：{original_request}"
        )
    if effect == "push_engineering_branch":
        return (
            "推送当前 durable EngineeringSession 的非保护 engineering 分支到 origin。"
            "不得 force push、merge 或选择其他分支。"
            f"\n持久目标：{goal}"
        )
    if effect == "open_or_update_draft_pr":
        return (
            "为当前 durable EngineeringSession 的已推送 engineering 分支创建或更新 Draft PR。"
            "不得 merge、force push，也不得把 ready-for-review PR 改回草稿。"
            f"\n持久目标：{goal}"
        )
    if effect == "inspect_project":
        return f"只读检查项目以服务这个持久目标，不修改仓库。\n持久目标：{goal}"
    if effect == "run_project_command":
        return f"在项目隔离 worktree 中执行原始请求明确要求的命令。\n原始请求：{original_request}"
    raise EngineeringProtocolError(f"unsupported planned engineering effect: {effect!r}")


def build_engineering_goal_plan(
    *,
    goal: str,
    requested_effects: tuple[str, ...],
    original_request: str,
) -> EngineeringGoalPlan:
    """Build the deterministic execution skeleton for one already-resolved goal.

    Semantic resolution decides which effects the user requested. This planner may only
    order supported effects and add safe implementation prerequisites. In particular,
    Draft PR publication requires a pushed engineering branch, so a missing push step is
    inserted before the Draft PR step. The planner never adds merge, force-push, deploy,
    secret, permission, cost, or other high-impact effects.
    """

    normalized_goal = goal.strip() or original_request.strip()
    if not normalized_goal:
        raise EngineeringProtocolError("persistent engineering goal must not be empty")

    effects: list[str] = []
    seen: set[str] = set()
    for raw in requested_effects:
        effect = raw.strip()
        if effect not in SUPPORTED_ENGINEERING_EFFECTS:
            raise EngineeringProtocolError(f"unsupported persistent engineering effect: {effect!r}")
        if effect not in seen:
            seen.add(effect)
            effects.append(effect)
    if not effects:
        raise EngineeringProtocolError("persistent engineering goal requires at least one effect")

    if any(effect in effects for effect in {"inspect_project", "run_project_command"}) and len(effects) > 1:
        raise EngineeringProtocolError(
            "read-only inspection or explicit command execution cannot be mixed into a persistent publish plan"
        )

    if set(effects).issubset(
        {"maintain_project", "push_engineering_branch", "open_or_update_draft_pr"}
    ):
        ordered: list[str] = []
        if "maintain_project" in effects:
            ordered.append("maintain_project")
        if "push_engineering_branch" in effects or "open_or_update_draft_pr" in effects:
            ordered.append("push_engineering_branch")
        if "open_or_update_draft_pr" in effects:
            ordered.append("open_or_update_draft_pr")
        effects = ordered

    required: list[str] = []
    required_seen: set[str] = set()
    for effect in effects:
        for capability in _EFFECT_CAPABILITIES[effect]:
            if capability not in required_seen:
                required_seen.add(capability)
                required.append(capability)

    return EngineeringGoalPlan(
        goal=normalized_goal,
        steps=tuple(
            PlannedEngineeringStep(
                effect=effect,
                instruction=_step_instruction(effect, normalized_goal, original_request),
            )
            for effect in effects
        ),
        required_capabilities=tuple(required),
    )
