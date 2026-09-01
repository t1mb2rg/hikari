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
from engineering.progress import describe_engineering_progress
from engineering.session import (
    EngineeringAuthority,
    EngineeringProtocolError,
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


_READ_REQUIREMENTS = ("engineering.repository.read",)
_MAINTAIN_REQUIREMENTS = (
    "engineering.repository.read",
    "engineering.repository.write",
    "engineering.tests.run",
    "engineering.git.commit",
)


def engineering_requirements_for_intent(text: str) -> tuple[str, ...] | None:
    """Narrow task-to-capability mapper for the first delegated maintainer slice."""

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


def looks_like_engineering_status_query(text: str) -> bool:
    """Recognize explicit status checks that must bypass generative completion.

    Mutation wording takes precedence because maintenance requests can quote a
    sentence containing phrases such as ``Engineering 现在状态``. Quoted content
    must never hijack a real write task into the status-query path.
    """

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
    """Return whether a terminal session still represents the current source revision."""

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


class ConversationEngineeringBridge(ConversationForgeBridge):
    """Route delegated project work into Hikari EngineeringSession.

    Explicit Engineering status questions are answered directly from durable
    session/result state. They do not ask the Conversation model to infer whether
    a task succeeded.
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
        self.fallback = fallback

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

        state = self._bound_state(turn.channel, turn.conversation_id)
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
