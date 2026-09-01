from __future__ import annotations

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
from engineering.maintainer import project_maintainer_authority
from engineering.session import (
    EngineeringAuthority,
    EngineeringSessionState,
    EngineeringSessionStore,
    EngineeringTurn,
)
from engineering.workspace import EngineeringWorkspace, EngineeringWorkspaceError

from .action_bridge import ConversationForgeBridge, _remember_control_exchange
from .engine import ConversationEngine
from .models import AssistantReply, UserTurn


_PROJECT_NOUNS = (
    "readme",
    "仓库",
    "代码",
    "模块",
    "项目",
    "架构",
    "文件",
    "实现",
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


_READ_REQUIREMENTS = ("engineering.repository.read",)
_MAINTAIN_REQUIREMENTS = (
    "engineering.repository.read",
    "engineering.repository.write",
    "engineering.commands.run",
    "engineering.tests.run",
    "engineering.git.commit",
)


def engineering_requirements_for_intent(text: str) -> tuple[str, ...] | None:
    """Narrow task-to-capability mapper for the first delegated maintainer slice.

    Cognition can become richer later, but the capability decision itself remains
    machine-readable. Unknown everyday conversation is not accidentally routed to
    Engineering merely because a generic verb appears.
    """

    normalized = text.casefold()
    project_context = any(noun in normalized for noun in _PROJECT_NOUNS)
    if not project_context:
        return None
    if any(verb in normalized for verb in _WRITE_VERBS):
        return _MAINTAIN_REQUIREMENTS
    if any(verb in normalized for verb in _INSPECTION_VERBS):
        return _READ_REQUIREMENTS
    return None


def looks_like_read_only_engineering_intent(text: str) -> bool:
    return engineering_requirements_for_intent(text) == _READ_REQUIREMENTS


def engineering_session_matches_repository_head(
    state: EngineeringSessionState,
    repository_head: str,
) -> bool:
    """Return whether a terminal session still represents the current source revision.

    A session without a workspace baseline has not inspected the repository yet,
    so it can still be reused and will bind to HEAD when the Worker first opens it.
    Once a baseline exists it is immutable for that EngineeringSession. If the
    source repository advances, Conversation must create a new session rather
    than asking an old worktree to describe the new world.
    """

    baseline = (state.baseline_commit or "").strip()
    if not baseline:
        return True
    return baseline == repository_head.strip()


class ConversationEngineeringBridge(ConversationForgeBridge):
    """Route delegated project work into Hikari EngineeringSession.

    Conversation identifies the task class. The machine-readable capability model
    decides whether the task is executable, an implementation gap, or outside the
    standing mandate. Routine work inside the Hikari maintainer mandate does not ask
    the user for approval at each file/command/test step.
    """

    def __init__(
        self,
        store: EngineeringSessionStore,
        bindings: EngineeringConversationBindingStore,
        *,
        repository: str | Path,
        fallback: ConversationForgeBridge | None = None,
    ) -> None:
        if not isinstance(store, EngineeringSessionStore):
            raise TypeError("ConversationEngineeringBridge requires EngineeringSessionStore")
        if not isinstance(bindings, EngineeringConversationBindingStore):
            raise TypeError("ConversationEngineeringBridge requires EngineeringConversationBindingStore")
        if fallback is not None and not isinstance(fallback, ConversationForgeBridge):
            raise TypeError("engineering fallback must be ConversationForgeBridge or None")
        repository_path = Path(repository).expanduser().resolve()
        if not repository_path.is_dir():
            raise ValueError(f"engineering repository must exist: {repository_path}")
        self.store = store
        self.bindings = bindings
        self.repository = repository_path

    def respond(
        self,
        engine: ConversationEngine,
        turn: UserTurn,
        *,
        source_ref: str | None = None,
    ) -> AssistantReply:
        requirements = engineering_requirements_for_intent(turn.text)
        if requirements is None:
            if self.fallback is not None:
                return self.fallback.respond(engine, turn, source_ref=source_ref)
            return engine.respond(turn, source_ref=source_ref)

        capabilities = hikari_engineering_capabilities(True)
        assessment = assess_task_capabilities(requirements, capabilities)
        if assessment.status == ASSESSMENT_CAPABILITY_GAP:
            missing = ", ".join(assessment.missing)
            reply = AssistantReply(
                channel=turn.channel,
                conversation_id=turn.conversation_id,
                text=(
                    "这个需求在我当前的项目维护职责里，但 Engineering Runtime 还缺少实际执行能力："
                    f"{missing}。这是能力缺口，不是需要你逐个动作给我授权。"
                    "我会保留这个原始需求作为后续能力迭代的目标。"
                ),
            )
            _remember_control_exchange(engine, turn, reply)
            return reply
        if assessment.status == ASSESSMENT_ESCALATION_REQUIRED:
            escalation = ", ".join(assessment.escalation)
            reply = AssistantReply(
                channel=turn.channel,
                conversation_id=turn.conversation_id,
                text=(
                    "这个需求触及当前项目 mandate 之外的影响边界，需要你决定是否扩展这次授权："
                    f"{escalation}。"
                ),
            )
            _remember_control_exchange(engine, turn, reply)
            return reply

        turn_authority = (
            EngineeringAuthority.read_only()
            if requirements == _READ_REQUIREMENTS
            else project_maintainer_authority()
        )
        session_ceiling = project_maintainer_authority()

        binding = self.bindings.for_conversation(turn.channel, turn.conversation_id)
        state: EngineeringSessionState | None = None
        if binding is not None:
            try:
                state = self.store.load(binding.session_id)
            except Exception:
                state = None

        if state is not None and state.status in {"pending", "running"}:
            reply = AssistantReply(
                channel=turn.channel,
                conversation_id=turn.conversation_id,
                text=(
                    "我这边已经有一个工程会话在处理了。它完成后我会把实际结果发回来，"
                    "不会假装已经完成。"
                ),
            )
            _remember_control_exchange(engine, turn, reply)
            return reply

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

        engineering_turn = EngineeringTurn.create(
            intent=turn.text,
            context=(
                "This request came from Hikari's explicit conversation channel. "
                "The Hikari repository has a standing maintainer mandate. Complete routine project "
                "work autonomously inside that mandate and return the grounded result."
            ),
            authority=turn_authority,
        )
        self.store.enqueue_turn(state.session_id, engineering_turn)

        if requirements == _READ_REQUIREMENTS:
            text = "我去看。已经开始一个只读工程会话，完成后我会把实际检查结果发回来。"
        else:
            text = (
                "我来处理。这个任务在我的项目维护职责内，我已经交给 Engineering Runtime。"
                "我会在隔离工程分支里完成修改、测试和提交，完成后把实际结果发回来。"
            )
        reply = AssistantReply(
            channel=turn.channel,
            conversation_id=turn.conversation_id,
            text=text,
        )
        _remember_control_exchange(engine, turn, reply)
        return reply
