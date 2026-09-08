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

_INSPECTION_HINTS = (
    "看看",
    "看一下",
    "看一眼",
    "阅读",
    "读一下",
    "检查",
    "分析",
    "了解",
    "理解",
    "查一下",
    "去看",
    "inspect",
)

_WRITE_HINTS = (
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
    "fix",
    "implement",
    "refactor",
)

_COMMAND_HINTS = (
    "运行命令",
    "执行命令",
    "跑命令",
    "run command",
    "run the command",
    "execute command",
)

_PUSH_ACTION_HINTS = (
    "git push",
    "push 分支",
    "push分支",
    "分支 push",
    "分支push",
    "push branch",
    "branch push",
    "push 到远端",
    "push到远端",
    "push 到 github",
    "push到 github",
    "推送分支",
    "推到远端",
    "推送到远端",
    "推到 github",
    "推送到 github",
)

_DRAFT_PR_ACTION_HINTS = (
    "开 draft pr",
    "开一个 draft pr",
    "创建 draft pr",
    "创建一个 draft pr",
    "新建 draft pr",
    "新建一个 draft pr",
    "更新 draft pr",
    "开草稿 pr",
    "开一个草稿 pr",
    "创建草稿 pr",
    "新建草稿 pr",
    "open draft pr",
    "create draft pr",
    "update draft pr",
    "open pull request",
    "create pull request",
    "update pull request",
)

_READ_UPDATE_QUERY_HINTS = (
    "最近更新了什么",
    "更新了什么",
    "有哪些更新",
    "有什么更新",
    "最近改了什么",
    "改了什么",
    "最近有什么变化",
    "有什么变化",
)

_FORCE_PUSH_HINTS = (
    "force push",
    "force-push",
    "强制 push",
    "强制push",
    "强推",
    "强制推送",
)

_PROTECTED_MERGE_HINTS = (
    "merge main",
    "merge master",
    "merge protected branch",
    "merge into main",
    "merge into master",
    "合并 main",
    "合并main",
    "合并 master",
    "合并master",
    "合并到 main",
    "合并到main",
    "合并到 master",
    "合并到master",
    "合并进 main",
    "合并进main",
    "合并进 master",
    "合并进master",
    "合并保护分支",
    "合并到保护分支",
)

_SECRET_NOUN_HINTS = (
    "secret",
    "secrets",
    "密钥",
    "api key",
    "api_key",
    "access token",
    "auth token",
    "api token",
    "访问令牌",
    "认证令牌",
)

_SECRET_ACTION_HINTS = (
    "修改",
    "更新",
    "更换",
    "替换",
    "轮换",
    "暴露",
    "显示",
    "输出",
    "打印",
    "发我",
    "change",
    "update",
    "replace",
    "rotate",
    "expose",
    "reveal",
    "show",
    "print",
    "send me",
)

_PRODUCTION_DEPLOY_HINTS = (
    "生产部署",
    "部署到生产",
    "部署进生产",
    "部署上线",
    "上线生产",
    "production deploy",
    "deploy production",
    "deploy to production",
)

_DESTRUCTIVE_MIGRATION_HINTS = (
    "破坏性数据迁移",
    "破坏性迁移",
    "destructive data migration",
    "destructive migration",
)

_PERMISSION_NOUN_HINTS = (
    "权限边界",
    "permission boundary",
)

_PERMISSION_EXPANSION_HINTS = (
    "扩展",
    "扩大",
    "提升",
    "增加",
    "expand",
    "widen",
    "elevate",
    "increase",
)

_NORTH_STAR_CHANGE_HINTS = (
    "改变项目北极星",
    "修改项目北极星",
    "调整项目北极星",
    "project north star change",
    "change project north star",
    "change the project north star",
)

_MATERIAL_COST_HINTS = (
    "显著外部成本",
    "重大外部成本",
    "material external cost",
    "material paid resource cost",
)


@dataclass(frozen=True, slots=True)
class EngineeringIntentResolution:
    engineering: bool
    goal: str
    requested_effects: tuple[str, ...]
    required_capabilities: tuple[str, ...]


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def _resolution_for_effects(
    goal: str,
    effects: tuple[str, ...],
) -> EngineeringIntentResolution:
    required: list[str] = []
    seen: set[str] = set()
    for effect in effects:
        for key in _EFFECT_REQUIREMENTS[effect]:
            if key not in seen:
                seen.add(key)
                required.append(key)
    return EngineeringIntentResolution(
        engineering=True,
        goal=goal.strip(),
        requested_effects=effects,
        required_capabilities=tuple(required),
    )


def _explicit_high_impact_effect(text: str) -> str | None:
    """Deterministic sentry for explicit effects that must never depend on model routing."""

    if _contains_any(text, _FORCE_PUSH_HINTS):
        return "force_push_shared_history"
    if _contains_any(text, _PROTECTED_MERGE_HINTS):
        return "merge_protected_branch"
    if _contains_any(text, _SECRET_NOUN_HINTS) and _contains_any(
        text,
        _SECRET_ACTION_HINTS,
    ):
        return "modify_or_expose_secrets"
    if _contains_any(text, _PRODUCTION_DEPLOY_HINTS):
        return "production_deploy"
    if _contains_any(text, _DESTRUCTIVE_MIGRATION_HINTS):
        return "destructive_data_migration"
    if _contains_any(text, _PERMISSION_NOUN_HINTS) and _contains_any(
        text,
        _PERMISSION_EXPANSION_HINTS,
    ):
        return "expand_permissions"
    if _contains_any(text, _NORTH_STAR_CHANGE_HINTS):
        return "change_project_north_star"
    if _contains_any(text, _MATERIAL_COST_HINTS):
        return "incur_material_external_cost"
    return None


def _fallback_resolution(text: str) -> EngineeringIntentResolution:
    """Small degraded-mode classifier used only when semantic model output is unusable."""

    normalized = str(text).casefold()
    high_impact = _explicit_high_impact_effect(normalized)
    if high_impact is not None:
        return _resolution_for_effects("explicit high-impact engineering effect", (high_impact,))

    if _contains_any(normalized, _COMMAND_HINTS):
        return _resolution_for_effects("run project command", ("run_project_command",))
    if _contains_any(normalized, _DRAFT_PR_ACTION_HINTS):
        return _resolution_for_effects("open or update Draft PR", ("open_or_update_draft_pr",))
    if _contains_any(normalized, _PUSH_ACTION_HINTS):
        return _resolution_for_effects("push engineering branch", ("push_engineering_branch",))

    project_context = _contains_any(normalized, _PROJECT_HINTS)
    if project_context:
        if _contains_any(normalized, _INSPECTION_HINTS) and _contains_any(
            normalized,
            _READ_UPDATE_QUERY_HINTS,
        ):
            return _resolution_for_effects("inspect recent project changes", ("inspect_project",))
        if _contains_any(normalized, _WRITE_HINTS):
            return _resolution_for_effects("maintain project", ("maintain_project",))
        if _contains_any(normalized, _INSPECTION_HINTS):
            return _resolution_for_effects("inspect project", ("inspect_project",))

    return EngineeringIntentResolution(
        engineering=False,
        goal="semantic resolver unavailable and no clear engineering effect",
        requested_effects=(),
        required_capabilities=(),
    )


class EngineeringIntentResolver:
    """Thin semantic translator from user wording to requested engineering effects.

    The model may interpret what effect the user is asking for, but it does not own
    capability truth or authority. Required capability keys are derived deterministically
    from a fixed effect catalog after the model returns its semantic classification.
    Explicit high-impact effects are intercepted deterministically before any model call.
    """

    def __init__(self, provider: ChatProvider) -> None:
        if not isinstance(provider, ChatProvider):
            raise TypeError("EngineeringIntentResolver requires a ChatProvider")
        self.provider = provider

    @staticmethod
    def is_candidate(text: str, *, bound_session: bool = False) -> bool:
        normalized = str(text).casefold()
        if _explicit_high_impact_effect(normalized) is not None:
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
        normalized_text = str(text).casefold()
        high_impact = _explicit_high_impact_effect(normalized_text)
        if high_impact is not None:
            return _resolution_for_effects(
                "explicit high-impact engineering effect",
                (high_impact,),
            )

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
        except Exception:
            return _fallback_resolution(text)
        if not raw:
            return _fallback_resolution(text)

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
        except json.JSONDecodeError:
            return _fallback_resolution(text)
        if not isinstance(parsed, dict):
            return _fallback_resolution(text)

        engineering = parsed.get("engineering")
        goal = parsed.get("goal")
        effects = parsed.get("requested_effects")
        if not isinstance(engineering, bool):
            return _fallback_resolution(text)
        if not isinstance(goal, str):
            return _fallback_resolution(text)
        if not isinstance(effects, list) or not all(isinstance(item, str) for item in effects):
            return _fallback_resolution(text)

        normalized_effects: list[str] = []
        seen: set[str] = set()
        for item in effects:
            effect = item.strip()
            if not effect or effect in seen:
                continue
            if effect not in _EFFECT_REQUIREMENTS:
                return _fallback_resolution(text)
            seen.add(effect)
            normalized_effects.append(effect)

        if not engineering:
            if normalized_effects:
                return _fallback_resolution(text)
            return EngineeringIntentResolution(False, goal.strip(), (), ())
        if not normalized_effects:
            return _fallback_resolution(text)

        return _resolution_for_effects(goal, tuple(normalized_effects))
