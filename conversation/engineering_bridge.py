from __future__ import annotations

import logging
from pathlib import Path

from core.delegation import (
    ASSESSMENT_CAPABILITY_GAP,
    ASSESSMENT_ESCALATION_REQUIRED,
    assess_task_capabilities,
    hikari_engineering_capabilities,
)
from engineering.bindings import (
    EngineeringConversationBinding,
    EngineeringConversationBindingStore,
)
from engineering.maintainer import (
    project_maintainer_authority,
    project_push_authority,
    project_session_authority_ceiling,
)
from engineering.progress import describe_engineering_progress
from engineering.session import (
    EngineeringAuthority,
    EngineeringProtocolError,
    EngineeringSessionState,
    EngineeringSessionStore,
    EngineeringTurn,
)
from engineering.workspace import EngineeringWorkspace, EngineeringWorkspaceError

from .engine import ASSISTANT_EVENT_TYPE, USER_EVENT_TYPE, ConversationEngine
from .engineering_intent import (
    EngineeringIntentResolution,
    EngineeringIntentResolutionError,
    EngineeringIntentResolver,
)
from .engineering_voice import EngineeringVoiceFacts, EngineeringVoiceRenderer
from .models import AssistantReply, UserTurn


logger = logging.getLogger(__name__)


_PROJECT_NOUNS = (
    "readme",
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
_INSPECTION_VERBS = (
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
)
_WRITE_VERBS = (
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
    "处理这个bug",
    "fix",
    "implement",
    "refactor",
)
_STATUS_SUBJECTS = (
    "engineering",
    "工程任务",
    "工程会话",
    "工程运行时",
    "engineering worker",
    "worker",
)
_STATUS_QUESTIONS = (
    "什么状态",
    "现在状态",
    "进度",
    "怎么样了",
    "做到哪",
    "完成了吗",
    "结束了吗",
    "还在跑",
    "还在处理",
)

_FORCE_PUSH_MARKERS = (
    "force push",
    "force-push",
    "强制 push",
    "强制push",
    "强推",
    "强制推送",
)
_PROTECTED_MERGE_MARKERS = (
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
_PRODUCTION_DEPLOY_MARKERS = (
    "生产部署",
    "部署到生产",
    "部署进生产",
    "部署上线",
    "上线生产",
    "production deploy",
    "deploy production",
    "deploy to production",
)
_DESTRUCTIVE_MIGRATION_MARKERS = (
    "破坏性数据迁移",
    "破坏性迁移",
    "destructive data migration",
    "destructive migration",
)
_PERMISSION_NOUN_MARKERS = (
    "权限边界",
    "permission boundary",
)
_PERMISSION_EXPANSION_ACTION_MARKERS = (
    "扩展",
    "扩大",
    "提升",
    "增加",
    "expand",
    "widen",
    "elevate",
    "increase",
)
_NORTH_STAR_CHANGE_MARKERS = (
    "改变项目北极星",
    "修改项目北极星",
    "调整项目北极星",
    "project north star change",
    "change project north star",
    "change the project north star",
)
_MATERIAL_COST_MARKERS = (
    "显著外部成本",
    "重大外部成本",
    "material external cost",
    "material paid resource cost",
)
_SECRET_NOUN_MARKERS = (
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
_SECRET_ACTION_MARKERS = (
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
_PUSH_MARKERS = (
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
_DRAFT_PR_ACTION_MARKERS = (
    "开 draft pr",
    "创建 draft pr",
    "新建 draft pr",
    "更新 draft pr",
    "开草稿 pr",
    "创建草稿 pr",
    "新建草稿 pr",
    "更新草稿 pr",
    "开 pr",
    "创建 pr",
    "新建 pr",
    "提交 pr",
    "更新 pr",
    "open pr",
    "create pr",
    "update pr",
    "open pull request",
    "create pull request",
    "update pull request",
)
_COMMAND_RUN_MARKERS = (
    "运行命令",
    "执行命令",
    "跑命令",
    "run command",
    "run the command",
    "execute command",
)

_READ_REQUIREMENTS = ("engineering.repository.read",)
_COMMAND_REQUIREMENTS = ("engineering.commands.run",)
_PUSH_REQUIREMENTS = ("engineering.git.push_non_protected",)
_MAINTAIN_REQUIREMENTS = (
    "engineering.repository.read",
    "engineering.repository.write",
    "engineering.tests.run",
    "engineering.git.commit",
)


def _remember_control_exchange(
    engine: ConversationEngine,
    turn: UserTurn,
    reply: AssistantReply,
) -> None:
    try:
        engine.memory.remember_event(
            USER_EVENT_TYPE,
            turn.text,
            context={
                "channel": turn.channel,
                "conversation_id": turn.conversation_id,
                "role": "user",
            },
            importance=1.0,
        )
        engine.memory.remember_event(
            ASSISTANT_EVENT_TYPE,
            reply.text,
            context={
                "channel": turn.channel,
                "conversation_id": turn.conversation_id,
                "role": "assistant",
            },
            importance=1.0,
        )
    except Exception as exc:
        logger.warning(
            "Hikari Engineering control-memory write degraded: %s",
            type(exc).__name__,
        )


def _voice_reply(
    engine: ConversationEngine,
    turn: UserTurn,
    facts: EngineeringVoiceFacts,
) -> AssistantReply:
    text = EngineeringVoiceRenderer(engine).render(
        facts,
        channel=turn.channel,
        conversation_id=turn.conversation_id,
    )
    return AssistantReply(
        channel=turn.channel,
        conversation_id=turn.conversation_id,
        text=text,
    )


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def _boundary_requirements_for_intent(text: str) -> tuple[str, ...] | None:
    """Compatibility fallback for explicit high-impact wording.

    Production routing first uses EngineeringIntentResolver. This helper exists only as
    a degraded fallback if semantic resolution is unavailable.
    """

    if _contains_any(text, _FORCE_PUSH_MARKERS):
        return ("engineering.git.force_push",)
    if _contains_any(text, _PROTECTED_MERGE_MARKERS):
        return ("engineering.git.merge_protected",)
    if _contains_any(text, _PRODUCTION_DEPLOY_MARKERS):
        return ("engineering.production.deploy",)
    if _contains_any(text, _DESTRUCTIVE_MIGRATION_MARKERS):
        return ("engineering.data.destructive_migration",)
    if _contains_any(text, _PERMISSION_NOUN_MARKERS) and _contains_any(
        text,
        _PERMISSION_EXPANSION_ACTION_MARKERS,
    ):
        return ("engineering.permissions.expand",)
    if _contains_any(text, _NORTH_STAR_CHANGE_MARKERS):
        return ("engineering.project.change_north_star",)
    if _contains_any(text, _MATERIAL_COST_MARKERS):
        return ("engineering.external_cost.material",)
    if _contains_any(text, _SECRET_NOUN_MARKERS) and _contains_any(
        text,
        _SECRET_ACTION_MARKERS,
    ):
        return ("engineering.secrets.modify",)
    return None


def engineering_requirements_for_intent(text: str) -> tuple[str, ...] | None:
    """Conservative deterministic fallback, not the production semantic resolver.

    Routine project mutation wins before remote-action words so documentation such as
    "update README to say Draft PR is still unavailable" remains a documentation task.
    """

    normalized = text.casefold()
    boundary = _boundary_requirements_for_intent(normalized)
    if boundary is not None:
        return boundary

    project_context = any(noun in normalized for noun in _PROJECT_NOUNS)
    if project_context:
        if _contains_any(normalized, _COMMAND_RUN_MARKERS):
            return _COMMAND_REQUIREMENTS
        if any(verb in normalized for verb in _WRITE_VERBS):
            return _MAINTAIN_REQUIREMENTS
        if any(verb in normalized for verb in _INSPECTION_VERBS):
            return _READ_REQUIREMENTS

    if _contains_any(normalized, _DRAFT_PR_ACTION_MARKERS):
        return ("engineering.git.open_or_update_draft_pr",)
    if _contains_any(normalized, _PUSH_MARKERS):
        return _PUSH_REQUIREMENTS
    return None


def looks_like_read_only_engineering_intent(text: str) -> bool:
    return engineering_requirements_for_intent(text) == _READ_REQUIREMENTS


def looks_like_engineering_status_query(text: str) -> bool:
    normalized = text.casefold()
    if any(verb in normalized for verb in _WRITE_VERBS):
        return False
    return any(subject in normalized for subject in _STATUS_SUBJECTS) and any(
        question in normalized for question in _STATUS_QUESTIONS
    )


def engineering_session_matches_repository_head(
    state: EngineeringSessionState,
    repository_head: str,
) -> bool:
    baseline = (state.baseline_commit or "").strip()
    if not baseline:
        return True
    return baseline == repository_head.strip()


def _task_label(turn: EngineeringTurn | None) -> str:
    if turn is None:
        return "当前绑定的工程任务"
    text = " ".join(turn.intent.split())
    if len(text) > 120:
        text = text[:117].rstrip() + "..."
    return text or "当前绑定的工程任务"


def _resolution_from_fallback(text: str) -> EngineeringIntentResolution | None:
    requirements = engineering_requirements_for_intent(text)
    if requirements is None:
        return None
    if requirements == _READ_REQUIREMENTS:
        effects = ("inspect_project",)
    elif requirements == _COMMAND_REQUIREMENTS:
        effects = ("run_project_command",)
    elif requirements == _PUSH_REQUIREMENTS:
        effects = ("push_engineering_branch",)
    elif requirements == _MAINTAIN_REQUIREMENTS:
        effects = ("maintain_project",)
    elif requirements == ("engineering.git.open_or_update_draft_pr",):
        effects = ("open_or_update_draft_pr",)
    else:
        effects = ("high_impact_engineering_action",)
    return EngineeringIntentResolution(
        engineering=True,
        goal="deterministic fallback",
        requested_effects=effects,
        required_capabilities=requirements,
    )


class ConversationEngineeringBridge:
    """Route semantic engineering intent into durable Hikari EngineeringSession state."""

    def __init__(
        self,
        store: EngineeringSessionStore,
        bindings: EngineeringConversationBindingStore,
        *,
        repository: str | Path,
        intent_resolver: object | None = None,
    ) -> None:
        if not isinstance(store, EngineeringSessionStore):
            raise TypeError("ConversationEngineeringBridge requires EngineeringSessionStore")
        if not isinstance(bindings, EngineeringConversationBindingStore):
            raise TypeError("ConversationEngineeringBridge requires EngineeringConversationBindingStore")
        repository_path = Path(repository).expanduser().resolve()
        if not repository_path.is_dir():
            raise ValueError(f"engineering repository must exist: {repository_path}")
        self.store = store
        self.bindings = bindings
        self.repository = repository_path
        self.intent_resolver = intent_resolver

    def _bound_state(
        self,
        channel: str,
        conversation_id: str,
    ) -> EngineeringSessionState | None:
        binding = self.bindings.for_conversation(channel, conversation_id)
        if binding is None:
            return None
        try:
            return self.store.load(binding.session_id)
        except EngineeringProtocolError:
            return None

    def _status_reply(self, turn: UserTurn) -> AssistantReply:
        state = self._bound_state(turn.channel, turn.conversation_id)
        if state is None:
            text = "这个会话当前没有可读取的 Engineering 任务状态。"
        else:
            progress = describe_engineering_progress(state)
            engineering_turn: EngineeringTurn | None = None
            if state.current_turn_id:
                try:
                    engineering_turn = self.store.load_turn(state.session_id, state.current_turn_id)
                except EngineeringProtocolError:
                    engineering_turn = None
            label = _task_label(engineering_turn)

            if state.status in {"pending", "running"}:
                text = (
                    f"当前 Engineering 任务是 `{state.status}`，阶段 `{progress.phase}`。\n"
                    f"任务：{label}\n"
                    f"最后一次持久进度：{state.latest_summary or '暂无更细的阶段信息'}。"
                )
            elif state.status in {"completed", "failed", "blocked"}:
                if not state.current_turn_id:
                    text = (
                        f"EngineeringSession 标记为 `{state.status}`，但缺少 current turn。"
                        "我不能据此宣称任务实际完成。"
                    )
                else:
                    try:
                        result = self.store.load_result(state.session_id, state.current_turn_id)
                    except EngineeringProtocolError:
                        text = (
                            f"EngineeringSession 标记为 `{state.status}`，但 terminal result 不可读取。"
                            "我不能据此宣称任务实际完成。"
                        )
                    else:
                        text = (
                            f"当前 Engineering 任务状态是 `{result.status}`。\n"
                            f"任务：{label}\n"
                            f"实际结果：{result.message}"
                        )
            else:
                text = (
                    f"当前 EngineeringSession 状态是 `{state.status}`，阶段 `{progress.phase}`。"
                    "没有 terminal result 时我不会宣称任务已经完成。"
                )

        return AssistantReply(
            channel=turn.channel,
            conversation_id=turn.conversation_id,
            text=text,
        )

    def _resolve_intent(
        self,
        engine: ConversationEngine,
        turn: UserTurn,
        state: EngineeringSessionState | None,
        capabilities,
    ) -> EngineeringIntentResolution | None:
        resolver = self.intent_resolver or EngineeringIntentResolver(engine.provider)
        if not resolver.is_candidate(turn.text, bound_session=state is not None):
            return None
        try:
            return resolver.resolve(
                turn.text,
                capabilities=capabilities,
                state=state,
            )
        except (EngineeringIntentResolutionError, Exception) as exc:
            logger.warning(
                "Hikari Engineering semantic intent resolution degraded: %s",
                type(exc).__name__,
            )
            return _resolution_from_fallback(turn.text)

    def respond(
        self,
        engine: ConversationEngine,
        turn: UserTurn,
        *,
        source_ref: str | None = None,
    ) -> AssistantReply:
        if looks_like_engineering_status_query(turn.text):
            reply = self._status_reply(turn)
            _remember_control_exchange(engine, turn, reply)
            return reply

        state = self._bound_state(turn.channel, turn.conversation_id)
        capabilities = hikari_engineering_capabilities(True)
        resolution = self._resolve_intent(engine, turn, state, capabilities)
        if resolution is None or not resolution.engineering:
            return engine.respond(turn, source_ref=source_ref)

        requirements = resolution.required_capabilities
        assessment = assess_task_capabilities(requirements, capabilities)
        if assessment.status == ASSESSMENT_CAPABILITY_GAP:
            reply = _voice_reply(
                engine,
                turn,
                EngineeringVoiceFacts(
                    kind="capability_gap",
                    goal=resolution.goal or turn.text,
                    capabilities=tuple(assessment.missing),
                    details=(
                        "The requested effect is inside the standing project mandate; the missing item is implementation capability, not per-action authorization.",
                    ),
                ),
            )
            _remember_control_exchange(engine, turn, reply)
            return reply
        if assessment.status == ASSESSMENT_ESCALATION_REQUIRED:
            reply = _voice_reply(
                engine,
                turn,
                EngineeringVoiceFacts(
                    kind="escalation",
                    goal=resolution.goal or turn.text,
                    capabilities=tuple(assessment.escalation),
                    details=(
                        "The authority decision has already been made deterministically: this effect is outside the standing project mandate and requires a human decision before execution.",
                    ),
                ),
            )
            _remember_control_exchange(engine, turn, reply)
            return reply

        effects = resolution.requested_effects
        if len(effects) != 1:
            reply = AssistantReply(
                channel=turn.channel,
                conversation_id=turn.conversation_id,
                text=(
                    "我已经理解到这次请求包含多个工程效果，但当前 Engineering bridge 还没有"
                    "把多个 effect 串成一个持久计划。我不会把它们粗暴合并成一个权限 turn。"
                ),
            )
            _remember_control_exchange(engine, turn, reply)
            return reply

        effect = effects[0]
        if effect == "inspect_project":
            turn_authority = EngineeringAuthority.read_only()
        elif effect == "run_project_command":
            turn_authority = EngineeringAuthority(
                repository_read=True,
                run_commands=True,
            )
        elif effect == "push_engineering_branch":
            turn_authority = project_push_authority()
        elif effect == "maintain_project":
            turn_authority = project_maintainer_authority()
        else:
            reply = AssistantReply(
                channel=turn.channel,
                conversation_id=turn.conversation_id,
                text="这个工程效果目前没有可执行的 Worker turn 类型，我不会假装已经执行。",
            )
            _remember_control_exchange(engine, turn, reply)
            return reply

        session_ceiling = project_session_authority_ceiling()
        if state is not None and state.status in {"pending", "running"}:
            progress = describe_engineering_progress(state)
            reply = AssistantReply(
                channel=turn.channel,
                conversation_id=turn.conversation_id,
                text=(
                    "我这边已经有一个工程会话在处理了。"
                    f"当前阶段是 `{progress.phase}`。它完成后我会把实际结果发回来，"
                    "不会假装已经完成。"
                ),
            )
            _remember_control_exchange(engine, turn, reply)
            return reply

        if effect == "push_engineering_branch":
            if state is None or not (
                state.workspace_path and state.workspace_branch and state.baseline_commit
            ):
                reply = AssistantReply(
                    channel=turn.channel,
                    conversation_id=turn.conversation_id,
                    text=(
                        "这个会话当前没有已经提交的 Engineering 分支可以推送。"
                        "我不会为了满足 push 请求临时创建一个空远端分支。"
                    ),
                )
                _remember_control_exchange(engine, turn, reply)
                return reply
            if not turn_authority.is_subset_of(state.authority_ceiling):
                reply = AssistantReply(
                    channel=turn.channel,
                    conversation_id=turn.conversation_id,
                    text=(
                        "当前绑定的 EngineeringSession 建立时还没有远端发布 ceiling。"
                        "我不会临时扩大一个旧会话的权限；新的 maintainer 会话会直接具备"
                        "非保护 engineering 分支 push 的 standing ceiling。"
                    ),
                )
                _remember_control_exchange(engine, turn, reply)
                return reply
        else:
            if state is not None and not turn_authority.is_subset_of(state.authority_ceiling):
                state = None

            if state is not None and state.baseline_commit:
                try:
                    repository_head = EngineeringWorkspace.source_head(self.repository)
                except EngineeringWorkspaceError:
                    reply = AssistantReply(
                        channel=turn.channel,
                        conversation_id=turn.conversation_id,
                        text=(
                            "我现在没法为这个仓库建立可信的工程版本快照。"
                            "如果源码仓库存在未提交改动，我不会拿旧 worktree 冒充最新状态。"
                        ),
                    )
                    _remember_control_exchange(engine, turn, reply)
                    return reply
                if not engineering_session_matches_repository_head(state, repository_head):
                    state = None

            if state is None:
                state = EngineeringSessionState.create(
                    project_id="hikari",
                    repository=self.repository,
                    authority_ceiling=session_ceiling,
                )
                self.store.create(state)
                self.bindings.bind(
                    EngineeringConversationBinding(
                        session_id=state.session_id,
                        channel=turn.channel,
                        conversation_id=turn.conversation_id,
                    )
                )

        assert state is not None
        engineering_turn = EngineeringTurn.create(
            intent=turn.text,
            context=(
                "This request came from Hikari's explicit conversation channel. "
                f"Semantic engineering goal: {resolution.goal or turn.text}. "
                f"Requested effect: {effect}. "
                "The Hikari repository has a standing maintainer mandate. Complete routine project "
                "work autonomously inside that mandate and return the grounded result."
            ),
            authority=turn_authority,
        )
        self.store.enqueue_turn(state.session_id, engineering_turn)

        if effect == "inspect_project":
            details = (
                "已经开始一个只读工程会话，完成后会返回实际检查结果。",
            )
        elif effect == "run_project_command":
            details = (
                "已经开始一个项目内命令工程会话；命令会在隔离 worktree 中执行，不会获得仓库写入、网络或发布权限。",
            )
        elif effect == "push_engineering_branch":
            details = (
                "当前非保护 engineering 分支已经进入 push turn；只会推送这个分支到 origin，不会 force push 或 merge。",
            )
        else:
            details = (
                "这个任务在项目维护职责内；已经进入持久工程会话，可在隔离工程分支完成修改、测试和提交。",
            )
        reply = _voice_reply(
            engine,
            turn,
            EngineeringVoiceFacts(
                kind="accepted",
                goal=resolution.goal or turn.text,
                status="accepted",
                branch=state.workspace_branch,
                details=details,
            ),
        )
        _remember_control_exchange(engine, turn, reply)
        return reply
