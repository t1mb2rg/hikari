from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Mapping

from brain.model_reasoner import ChatMessage, ChatProvider
from core.delegation import CapabilityState
from engineering.session import EngineeringSessionState


class EngineeringIntentResolutionError(RuntimeError):
    """Raised when the model cannot provide a trustworthy engineering intent."""


_EFFECT_REQUIREMENTS: dict[str, tuple[str, ...]] = {
    "inspect_project": ("engineering.repository.read",),
    "maintain_project": (
        "engineering.repository.read",
        "engineering.repository.write",
        "engineering.tests.run",
        "engineering.git.commit",
    ),
    "run_project_command": ("engineering.commands.run",),
    "push_engineering_branch": ("engineering.git.push_non_protected",),
    "open_or_update_draft_pr": ("engineering.git.open_or_update_draft_pr",),
    "merge_protected_branch": ("engineering.git.merge_protected",),
    "force_push_shared_history": ("engineering.git.force_push",),
    "modify_or_expose_secrets": ("engineering.secrets.modify",),
    "production_deploy": ("engineering.production.deploy",),
    "destructive_data_migration": ("engineering.data.destructive_migration",),
    "expand_permissions": ("engineering.permissions.expand",),
    "change_project_north_star": ("engineering.project.change_north_star",),
    "incur_material_external_cost": ("engineering.external_cost.material",),
}

_PROJECT_HINTS = (
    "readme",
    "repo",
    "repository",
    "仓库",
    "代码",
    "模块",
    "项目",
    "架构",
    "文件",
    "功能",
    "bug",
    "测试",
    "test",
    "memory",
    "resident",
    "conversation",
    "engineering",
    "hikari",
    "光",
)

_ACTION_HINTS = (
    "修改",
    "更新",
    "修复",
    "实现",
    "添加",
    "新增",
    "重构",
    "改一下",
    "改掉",
    "写代码",
    "检查",
    "分析",
    "看看",
    "看一下",
    "阅读",
    "运行命令",
    "执行命令",
    "跑命令",
    "push",
    "推送",
    "推到",
    "merge",
    "合并",
    "draft pr",
    "pull request",
    "secret",
    "密钥",
    "部署",
    "migration",
    "迁移",
    "权限",
    "北极星",
    "fix",
    "implement",
    "refactor",
    "inspect",
    "run command",
    "execute command",
)

_REMOTE_OR_HIGH_IMPACT_HINTS = (
    "draft pr",
    "pull request",
    "git push",
    "force push",
    "merge main",
    "merge master",
    "生产部署",
    "破坏性迁移",
    "破坏性数据迁移",
    "项目北极星",
)


@dataclass(frozen=True, slots=True)
class EngineeringIntentResolution:
    engineering: bool
    goal: str
    requested_effects: tuple[str, ...]
    required_capabilities: tuple[str, ...]


class EngineeringIntentResolver:
    """Thin semantic translator from user wording to requested engineering effects.

    The model may interpret what effect the user is asking for, but it does not own
    capability truth or authority. Required capability keys are derived deterministically
    from a fixed effect catalog after the model returns its semantic classification.
    """

    def __init__(self, provider: ChatProvider) -> None:
        if not isinstance(provider, ChatProvider):
            raise TypeError("EngineeringIntentResolver requires a ChatProvider")
        self.provider = provider

    @staticmethod
    def is_candidate(text: str, *, bound_session: bool = False) -> bool:
        normalized = str(text).casefold()
        if any(marker in normalized for marker in _REMOTE_OR_HIGH_IMPACT_HINTS):
            return True
        action = any(marker in normalized for marker in _ACTION_HINTS)
        if not action:
            return False
        if bound_session:
            return True
        return any(marker in normalized for marker in _PROJECT_HINTS)

    def resolve(
        self,
        text: str,
        *,
        capabilities: Mapping[str, CapabilityState],
        state: EngineeringSessionState | None = None,
    ) -> EngineeringIntentResolution:
        catalog = {
            effect: {
                "required_capabilities": list(requirements),
                "capabilities": {
                    key: capabilities[key].to_mapping()
                    for key in requirements
                    if key in capabilities
                },
            }
            for effect, requirements in _EFFECT_REQUIREMENTS.items()
        }
        session = {
            "bound": state is not None,
            "status": state.status if state is not None else None,
            "workspace_branch": state.workspace_branch if state is not None else None,
            "has_workspace": bool(state is not None and state.workspace_path),
        }
        payload = {
            "user_message": str(text),
            "current_engineering_session": session,
            "effect_catalog": catalog,
        }
        messages = (
            ChatMessage(
                role="system",
                content=(
                    "You are Hikari's engineering intent resolver. Your only job is to identify the "
                    "external effect the user is actually requesting. Capability availability and authority "
                    "are machine truth shown only as context; never change them and never refuse an effect "
                    "because it is unavailable. Choose only effect names from effect_catalog.\n\n"
                    "Crucial rule: mentioning an engineering concept is not the same as requesting that effect. "
                    "If the user asks to edit README or documentation and the text to be written says that Draft PR "
                    "is unavailable, choose maintain_project only. If the user asks to open a Draft PR, choose "
                    "open_or_update_draft_pr. Likewise, documentation that mentions push, secrets, deployment, or "
                    "other capabilities does not request those effects unless the user actually asks Hikari to do them.\n\n"
                    "Return exactly one JSON object and nothing else with this schema: "
                    '{"engineering": true|false, "goal": "short semantic goal", '
                    '"requested_effects": ["effect_name", ...]}. '
                    "Use engineering=false and an empty requested_effects list for ordinary conversation."
                ),
            ),
            ChatMessage(role="user", content=json.dumps(payload, ensure_ascii=False, sort_keys=True)),
        )
        try:
            raw = self.provider.complete(messages).strip()
        except Exception as exc:
            raise EngineeringIntentResolutionError(
                f"engineering intent model failed: {type(exc).__name__}"
            ) from exc
        if not raw:
            raise EngineeringIntentResolutionError("engineering intent model returned empty output")

        candidate = raw.strip()
        if candidate.startswith("```"):
            lines = candidate.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            candidate = "\n".join(lines).strip()
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as exc:
            raise EngineeringIntentResolutionError("engineering intent output is not valid JSON") from exc
        if not isinstance(parsed, dict):
            raise EngineeringIntentResolutionError("engineering intent output must be an object")

        engineering = parsed.get("engineering")
        goal = parsed.get("goal")
        effects = parsed.get("requested_effects")
        if not isinstance(engineering, bool):
            raise EngineeringIntentResolutionError("engineering intent field must be boolean")
        if not isinstance(goal, str):
            raise EngineeringIntentResolutionError("engineering goal must be a string")
        if not isinstance(effects, list) or not all(isinstance(item, str) for item in effects):
            raise EngineeringIntentResolutionError("requested_effects must be a string list")

        normalized_effects: list[str] = []
        seen: set[str] = set()
        for item in effects:
            effect = item.strip()
            if not effect or effect in seen:
                continue
            if effect not in _EFFECT_REQUIREMENTS:
                raise EngineeringIntentResolutionError(
                    f"unsupported engineering effect from model: {effect}"
                )
            seen.add(effect)
            normalized_effects.append(effect)

        if not engineering:
            if normalized_effects:
                raise EngineeringIntentResolutionError(
                    "non-engineering intent must not request engineering effects"
                )
            return EngineeringIntentResolution(False, goal.strip(), (), ())
        if not normalized_effects:
            raise EngineeringIntentResolutionError(
                "engineering intent must identify at least one requested effect"
            )

        required: list[str] = []
        required_seen: set[str] = set()
        for effect in normalized_effects:
            for key in _EFFECT_REQUIREMENTS[effect]:
                if key not in required_seen:
                    required_seen.add(key)
                    required.append(key)

        return EngineeringIntentResolution(
            engineering=True,
            goal=goal.strip(),
            requested_effects=tuple(normalized_effects),
            required_capabilities=tuple(required),
        )
