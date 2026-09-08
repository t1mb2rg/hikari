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
from engineering.effects import authority_for_effect
from engineering.goal import EngineeringGoalState, EngineeringGoalStep, EngineeringGoalStore
from engineering.goal_index import active_goal_for_session, latest_goal_for_session
from engineering.maintainer import project_session_authority_ceiling
from engineering.planning import EngineeringGoalPlan, build_engineering_goal_plan
from engineering.progress import describe_engineering_progress
from engineering.session import (
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
    _COMMAND_HINTS,
    _DRAFT_PR_ACTION_HINTS,
    _EFFECT_REQUIREMENTS,
    _INSPECTION_HINTS,
    _PROJECT_HINTS,
    _PUSH_ACTION_HINTS,
    _WRITE_HINTS,
    _explicit_high_impact_effect,
)
from .engineering_voice import EngineeringVoiceFacts, EngineeringVoiceRenderer
from .models import AssistantReply, UserTurn


logger = logging.getLogger(__name__)

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


def engineering_requirements_for_intent(text: str) -> tuple[str, ...] | None:
    """Conservative deterministic fallback, not the production semantic resolver.

    Explicit high-impact effects are checked first. For ordinary project wording, a
    requested repository mutation wins before words such as Draft PR or push so a README
    sentence describing those capabilities remains a documentation task.
    """

    normalized = text.casefold()
    high_impact = _explicit_high_impact_effect(normalized)
    if high_impact is not None:
        return _EFFECT_REQUIREMENTS[high_impact]

    project_context = _contains_any(normalized, _PROJECT_HINTS)
    if project_context:
        if _contains_any(normalized, _COMMAND_HINTS):
            return _COMMAND_REQUIREMENTS
        if _contains_any(normalized, _WRITE_HINTS):
            return _MAINTAIN_REQUIREMENTS
        if _contains_any(normalized, _INSPECTION_HINTS):
            return _READ_REQUIREMENTS

    if _contains_any(normalized, _DRAFT_PR_ACTION_HINTS):
        return ("engineering.git.open_or_update_draft_pr",)
    if _contains_any(normalized, _PUSH_ACTION_HINTS):
        return _PUSH_REQUIREMENTS
    return None


def looks_like_read_only_engineering_intent(text: str) -> bool:
    return engineering_requirements_for_intent(text) == _READ_REQUIREMENTS


def looks_like_engineering_status_query(text: str) -> bool:
    normalized = text.casefold()
    if _contains_any(normalized, _WRITE_HINTS):
        return False
    return _contains_any(normalized, _STATUS_SUBJECTS) and _contains_any(
        normalized,
        _STATUS_QUESTIONS,
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
        high_impact = _explicit_high_impact_effect(text.casefold())
        effects = (high_impact,) if high_impact else ("high_impact_engineering_action",)
    return EngineeringIntentResolution(
        engineering=True,
        goal="deterministic fallback",
        requested_effects=effects,
        required_capabilities=requirements,
    )


class ConversationEngineeringBridge:
    """Route engineering intent into single turns or durable persistent goals.

    Single-effect requests keep the proven M7-A turn path. Genuine multi-effect requests
    are persisted as an EngineeringGoal only; Conversation does not enqueue the first
    step. Resident owns deterministic work selection and continuation.
    """

    def __init__(
        self,
        store: EngineeringSessionStore,
        bindings: EngineeringConversationBindingStore,
        *,
        repository: str | Path,
        intent_resolver: object | None = None,
        goals: EngineeringGoalStore | None = None,
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
        self.goals = goals or EngineeringGoalStore(store.root.parent / "engineering_goals")

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
            goal = latest_goal_for_session(self.goals, state.session_id)
            if goal is not None:
                step = goal.current_step
                position = goal.current_step_index + 1
                if goal.status == "active":
                    progress = describe_engineering_progress(state)
                    text = (
                        f"当前持久 Engineering 目标是 `active`，第 {position}/{len(goal.steps)} 步。\n"
                        f"目标：{goal.goal}\n"
                        f"当前步骤：`{step.effect}` / `{step.status}`，工程阶段 `{progress.phase}`。\n"
                        f"最后一次持久进度：{state.latest_summary or '暂无更细的阶段信息'}。"
                    )
                else:
                    text = (
                        f"当前持久 Engineering 目标状态是 `{goal.status}`。\n"
                        f"目标：{goal.goal}\n"
                        f"实际结果：{goal.final_summary or step.result_message or '没有可读取的 terminal summary'}"
                    )
            else:
                progress = describe_engineering_progress(state)
                engineering_turn: EngineeringTurn | None = None
                if state.current_turn_id:
                    try:
                        engineering_turn = self.store.load_turn(
                            state.session_id,
                            state.current_turn_id,
                        )
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
                            result = self.store.load_result(
                                state.session_id,
                                state.current_turn_id,
                            )
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

    def _create_session(self, turn: UserTurn) -> EngineeringSessionState:
        state = EngineeringSessionState.create(
            project_id="hikari",
            repository=self.repository,
            authority_ceiling=project_session_authority_ceiling(),
        )
        self.store.create(state)
        self.bindings.bind(
            EngineeringConversationBinding(
                session_id=state.session_id,
                channel=turn.channel,
                conversation_id=turn.conversation_id,
            )
        )
        return state

    def _state_for_local_work(
        self,
        state: EngineeringSessionState | None,
        turn_authority,
        turn: UserTurn,
    ) -> tuple[EngineeringSessionState | None, AssistantReply | None]:
        if state is not None and not turn_authority.is_subset_of(state.authority_ceiling):
            state = None
        if state is not None and state.baseline_commit:
            try:
                repository_head = EngineeringWorkspace.source_head(self.repository)
            except EngineeringWorkspaceError:
                return None, AssistantReply(
                    channel=turn.channel,
                    conversation_id=turn.conversation_id,
                    text=(
                        "我现在没法为这个仓库建立可信的工程版本快照。"
                        "如果源码仓库存在未提交改动，我不会拿旧 worktree 冒充最新状态。"
                    ),
                )
            if not engineering_session_matches_repository_head(state, repository_head):
                state = None
        return state, None

    def _publish_state_error(
        self,
        state: EngineeringSessionState | None,
        effect: str,
        turn: UserTurn,
    ) -> AssistantReply | None:
        if state is None or not (
            state.workspace_path and state.workspace_branch and state.baseline_commit
        ):
            if effect == "open_or_update_draft_pr":
                text = (
                    "这个会话当前没有已经提交的 Engineering 分支可以开 Draft PR。"
                    "我不会为没有工程结果的会话创建空 PR。"
                )
            else:
                text = (
                    "这个会话当前没有已经提交的 Engineering 分支可以推送。"
                    "我不会为了满足 push 请求临时创建一个空远端分支。"
                )
            return AssistantReply(turn.channel, turn.conversation_id, text)
        authority = authority_for_effect(effect)
        if not authority.is_subset_of(state.authority_ceiling):
            return AssistantReply(
                channel=turn.channel,
                conversation_id=turn.conversation_id,
                text=(
                    "当前绑定的 EngineeringSession 建立时还没有远端发布 ceiling。"
                    "我不会临时扩大一个旧会话的权限；新的 maintainer 会话会直接具备"
                    "非保护 engineering 分支发布的 standing ceiling。"
                ),
            )
        return None

    def _start_persistent_goal(
        self,
        engine: ConversationEngine,
        turn: UserTurn,
        state: EngineeringSessionState | None,
        plan: EngineeringGoalPlan,
    ) -> AssistantReply:
        if state is not None:
            active = active_goal_for_session(self.goals, state.session_id)
            if active is not None:
                step = active.current_step
                return AssistantReply(
                    turn.channel,
                    turn.conversation_id,
                    (
                        "我已经在持续推进一个工程目标了。"
                        f"当前是第 {active.current_step_index + 1}/{len(active.steps)} 步 `{step.effect}`；"
                        "我不会把第二个工程目标插进同一个会话里。"
                    ),
                )
            if state.status in {"pending", "running"}:
                progress = describe_engineering_progress(state)
                return AssistantReply(
                    turn.channel,
                    turn.conversation_id,
                    f"我这边已经有一个工程 turn 在处理，当前阶段是 `{progress.phase}`。完成后再接新的持久目标。",
                )

        first_effect = plan.steps[0].effect
        if first_effect in {"push_engineering_branch", "open_or_update_draft_pr"}:
            error = self._publish_state_error(state, first_effect, turn)
            if error is not None:
                return error
        else:
            state, error = self._state_for_local_work(
                state,
                authority_for_effect(first_effect),
                turn,
            )
            if error is not None:
                return error
            if state is None:
                state = self._create_session(turn)

        assert state is not None
        if self.bindings.for_conversation(turn.channel, turn.conversation_id) is None:
            self.bindings.bind(
                EngineeringConversationBinding(
                    session_id=state.session_id,
                    channel=turn.channel,
                    conversation_id=turn.conversation_id,
                )
            )

        goal_state = EngineeringGoalState.create(
            project_id="hikari",
            session_id=state.session_id,
            goal=plan.goal,
            steps=(
                EngineeringGoalStep.create(
                    effect=step.effect,
                    instruction=step.instruction,
                    step_id=f"step-{index + 1}",
                )
                for index, step in enumerate(plan.steps)
            ),
            source_channel=turn.channel,
            source_conversation_id=turn.conversation_id,
        )
        self.goals.create(goal_state)
        return _voice_reply(
            engine,
            turn,
            EngineeringVoiceFacts(
                kind="accepted",
                goal=plan.goal,
                status="accepted",
                details=(
                    "这个目标已经持久化；Resident 会从 durable state 选择并推进已授权步骤，不需要逐步确认。",
                ),
            ),
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

        state = self._bound_state(turn.channel, turn.conversation_id)
        capabilities = hikari_engineering_capabilities(True)
        resolution = self._resolve_intent(engine, turn, state, capabilities)
        if resolution is None or not resolution.engineering:
            return engine.respond(turn, source_ref=source_ref)

        assessment = assess_task_capabilities(resolution.required_capabilities, capabilities)
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
        if len(effects) > 1:
            try:
                plan = build_engineering_goal_plan(
                    goal=resolution.goal or turn.text,
                    requested_effects=effects,
                    original_request=turn.text,
                )
            except EngineeringProtocolError as exc:
                reply = _voice_reply(
                    engine,
                    turn,
                    EngineeringVoiceFacts(
                        kind="blocked",
                        goal=resolution.goal or turn.text,
                        status="blocked",
                        summary=str(exc),
                    ),
                )
                _remember_control_exchange(engine, turn, reply)
                return reply
            plan_assessment = assess_task_capabilities(plan.required_capabilities, capabilities)
            if plan_assessment.status == ASSESSMENT_CAPABILITY_GAP:
                reply = _voice_reply(
                    engine,
                    turn,
                    EngineeringVoiceFacts(
                        kind="capability_gap",
                        goal=plan.goal,
                        capabilities=tuple(plan_assessment.missing),
                    ),
                )
            elif plan_assessment.status == ASSESSMENT_ESCALATION_REQUIRED:
                reply = _voice_reply(
                    engine,
                    turn,
                    EngineeringVoiceFacts(
                        kind="escalation",
                        goal=plan.goal,
                        capabilities=tuple(plan_assessment.escalation),
                    ),
                )
            else:
                reply = self._start_persistent_goal(engine, turn, state, plan)
            _remember_control_exchange(engine, turn, reply)
            return reply

        if len(effects) != 1:
            return engine.respond(turn, source_ref=source_ref)

        effect = effects[0]
        try:
            turn_authority = authority_for_effect(effect)
        except EngineeringProtocolError:
            reply = AssistantReply(
                channel=turn.channel,
                conversation_id=turn.conversation_id,
                text="这个工程效果目前没有可执行的 Worker turn 类型，我不会假装已经执行。",
            )
            _remember_control_exchange(engine, turn, reply)
            return reply

        if state is not None:
            active = active_goal_for_session(self.goals, state.session_id)
            if active is not None:
                reply = AssistantReply(
                    turn.channel,
                    turn.conversation_id,
                    (
                        "我正在持续推进当前工程目标，暂时不会往同一个 EngineeringSession 里插入另一个 turn。"
                        f"当前步骤是 `{active.current_step.effect}`。"
                    ),
                )
                _remember_control_exchange(engine, turn, reply)
                return reply
            if state.status in {"pending", "running"}:
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

        if effect in {"push_engineering_branch", "open_or_update_draft_pr"}:
            error = self._publish_state_error(state, effect, turn)
            if error is not None:
                _remember_control_exchange(engine, turn, error)
                return error
        else:
            state, error = self._state_for_local_work(state, turn_authority, turn)
            if error is not None:
                _remember_control_exchange(engine, turn, error)
                return error
            if state is None:
                state = self._create_session(turn)

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
            details = ("已经开始一个只读工程会话，完成后会返回实际检查结果。",)
        elif effect == "run_project_command":
            details = (
                "已经开始一个项目内命令工程会话；命令会在隔离 worktree 中执行，不会获得仓库写入、网络或发布权限。",
            )
        elif effect == "push_engineering_branch":
            details = (
                "当前非保护 engineering 分支已经进入 push turn；只会推送这个分支到 origin，不会 force push 或 merge。",
            )
        elif effect == "open_or_update_draft_pr":
            details = (
                "当前 engineering 分支已经进入 Draft PR 发布 turn；只会为这个非保护分支创建或维护草稿 PR，不会 merge、force push 或改变 ready-for-review PR 的评审状态。",
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
