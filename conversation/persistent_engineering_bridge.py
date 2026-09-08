from __future__ import annotations

from pathlib import Path

from core.delegation import (
    ASSESSMENT_EXECUTABLE,
    assess_task_capabilities,
    hikari_engineering_capabilities,
)
from engineering.bindings import (
    EngineeringConversationBinding,
    EngineeringConversationBindingStore,
)
from engineering.effects import SUPPORTED_ENGINEERING_EFFECTS, authority_for_effect
from engineering.goal import (
    EngineeringGoalCoordinator,
    EngineeringGoalState,
    EngineeringGoalStep,
    EngineeringGoalStore,
)
from engineering.maintainer import project_session_authority_ceiling
from engineering.progress import describe_engineering_progress
from engineering.session import (
    EngineeringProtocolError,
    EngineeringSessionState,
    EngineeringSessionStore,
)
from engineering.workspace import EngineeringWorkspace, EngineeringWorkspaceError

from .engine import ConversationEngine
from .engineering_bridge import (
    ConversationEngineeringBridge,
    _remember_control_exchange,
    _voice_reply,
    engineering_session_matches_repository_head,
    looks_like_engineering_status_query,
)
from .engineering_voice import EngineeringVoiceFacts
from .models import AssistantReply, UserTurn


class PersistentConversationEngineeringBridge(ConversationEngineeringBridge):
    """Add durable multi-effect goal planning above the proven single-turn bridge.

    Semantic resolution may return an ordered list of requested effects. Capability and
    authority remain deterministic. Single-effect requests keep the existing M7-A path;
    only genuine multi-effect requests become persistent Engineering Goals.
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
        super().__init__(
            store,
            bindings,
            repository=repository,
            intent_resolver=intent_resolver,
        )
        self.goals = goals or EngineeringGoalStore(store.root.parent / "engineering_goals")
        self.goal_coordinator = EngineeringGoalCoordinator(self.goals, store)

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
        if (
            resolution is None
            or not resolution.engineering
            or len(resolution.requested_effects) <= 1
        ):
            return super().respond(engine, turn, source_ref=source_ref)

        assessment = assess_task_capabilities(
            resolution.required_capabilities,
            capabilities,
        )
        if assessment.status != ASSESSMENT_EXECUTABLE:
            # Reuse the established capability-gap / escalation voice and memory path.
            return super().respond(engine, turn, source_ref=source_ref)

        effects = tuple(resolution.requested_effects)
        if any(effect not in SUPPORTED_ENGINEERING_EFFECTS for effect in effects):
            return super().respond(engine, turn, source_ref=source_ref)

        active_goal = self._active_goal_for_conversation(turn.channel, turn.conversation_id)
        if active_goal is not None:
            step = active_goal.current_step
            reply = AssistantReply(
                channel=turn.channel,
                conversation_id=turn.conversation_id,
                text=(
                    "我已经有一个持久工程目标在推进。"
                    f"当前是第 {active_goal.current_step_index + 1}/{len(active_goal.steps)} 步 "
                    f"`{step.effect}`，状态 `{step.status}`。"
                    "我会先把它推进到终态，不会把两个目标交错执行。"
                ),
            )
            _remember_control_exchange(engine, turn, reply)
            return reply

        state = self._prepare_session_for_goal(
            state,
            effects,
            turn.channel,
            turn.conversation_id,
        )
        if isinstance(state, AssistantReply):
            _remember_control_exchange(engine, turn, state)
            return state

        goal_text = resolution.goal.strip() or turn.text.strip()
        steps = tuple(
            EngineeringGoalStep.create(
                step_id=f"step-{index:02d}-{effect}",
                effect=effect,
                instruction=self._instruction_for_effect(
                    effect,
                    original_request=turn.text,
                    goal=goal_text,
                ),
            )
            for index, effect in enumerate(effects, start=1)
        )
        goal = EngineeringGoalState.create(
            project_id="hikari",
            session_id=state.session_id,
            goal=goal_text,
            steps=steps,
            source_channel=turn.channel,
            source_conversation_id=turn.conversation_id,
        )
        self.goals.create(goal)
        outcome = self.goal_coordinator.advance_once(goal.goal_id)
        if outcome.status in {"failed", "blocked"}:
            reply = _voice_reply(
                engine,
                turn,
                EngineeringVoiceFacts(
                    kind=outcome.status,
                    goal=goal.goal,
                    status=outcome.status,
                    summary=outcome.message,
                ),
            )
            _remember_control_exchange(engine, turn, reply)
            return reply

        reply = _voice_reply(
            engine,
            turn,
            EngineeringVoiceFacts(
                kind="accepted",
                goal=goal.goal,
                status="accepted",
                branch=state.workspace_branch,
                details=(
                    f"Persistent engineering goal created with {len(goal.steps)} ordered steps. "
                    "Hikari will continue them from durable state without asking for each routine step.",
                ),
            ),
        )
        _remember_control_exchange(engine, turn, reply)
        return reply

    def _status_reply(self, turn: UserTurn) -> AssistantReply:
        goal = self._latest_goal_for_conversation(turn.channel, turn.conversation_id)
        if goal is None:
            return super()._status_reply(turn)
        step = goal.current_step
        if goal.status == "active":
            text = (
                f"当前持久 Engineering 目标是 `active`，"
                f"第 {goal.current_step_index + 1}/{len(goal.steps)} 步 `{step.effect}` "
                f"状态 `{step.status}`。\n"
                f"目标：{goal.goal}\n"
                f"当前步骤：{step.instruction}"
            )
        else:
            text = (
                f"当前持久 Engineering 目标状态是 `{goal.status}`。\n"
                f"目标：{goal.goal}\n"
                f"终态依据：{goal.final_summary or step.result_message or '无可用终态摘要'}"
            )
        return AssistantReply(
            channel=turn.channel,
            conversation_id=turn.conversation_id,
            text=text,
        )

    def _active_goal_for_conversation(
        self,
        channel: str,
        conversation_id: str,
    ) -> EngineeringGoalState | None:
        matches = [
            goal
            for goal in self.goals.list_states()
            if goal.status == "active"
            and goal.source_channel == channel
            and goal.source_conversation_id == conversation_id
        ]
        return matches[-1] if matches else None

    def _latest_goal_for_conversation(
        self,
        channel: str,
        conversation_id: str,
    ) -> EngineeringGoalState | None:
        matches = [
            goal
            for goal in self.goals.list_states()
            if goal.source_channel == channel
            and goal.source_conversation_id == conversation_id
        ]
        return matches[-1] if matches else None

    def _prepare_session_for_goal(
        self,
        state: EngineeringSessionState | None,
        effects: tuple[str, ...],
        channel: str,
        conversation_id: str,
    ) -> EngineeringSessionState | AssistantReply:
        if state is not None and state.status in {"pending", "running"}:
            progress = describe_engineering_progress(state)
            return AssistantReply(
                channel=channel,
                conversation_id=conversation_id,
                text=(
                    "我这边已经有一个工程 turn 在处理。"
                    f"当前阶段是 `{progress.phase}`。"
                    "它进入终态前我不会创建会与它交错的持久目标。"
                ),
            )

        first_effect = effects[0]
        first_authority = authority_for_effect(first_effect)
        if state is not None and not first_authority.is_subset_of(state.authority_ceiling):
            state = None

        if state is not None and first_effect not in {
            "push_engineering_branch",
            "open_or_update_draft_pr",
        }:
            if state.baseline_commit:
                try:
                    repository_head = EngineeringWorkspace.source_head(self.repository)
                except EngineeringWorkspaceError:
                    return AssistantReply(
                        channel=channel,
                        conversation_id=conversation_id,
                        text=(
                            "我现在没法建立可信的工程版本快照。"
                            "源码仓库状态不明确时，我不会拿旧 worktree 继续新的持久目标。"
                        ),
                    )
                if not engineering_session_matches_repository_head(state, repository_head):
                    state = None

        if first_effect in {"push_engineering_branch", "open_or_update_draft_pr"}:
            if state is None or not (
                state.workspace_path and state.workspace_branch and state.baseline_commit
            ):
                return AssistantReply(
                    channel=channel,
                    conversation_id=conversation_id,
                    text=(
                        "这个持久目标从发布步骤开始，但当前会话没有可信的已提交 "
                        "Engineering 分支。我不会创建空远端状态来凑步骤。"
                    ),
                )

        if state is None:
            state = EngineeringSessionState.create(
                project_id="hikari",
                repository=self.repository,
                authority_ceiling=project_session_authority_ceiling(),
            )
            self.store.create(state)
            self.bindings.bind(
                EngineeringConversationBinding(
                    session_id=state.session_id,
                    channel=channel,
                    conversation_id=conversation_id,
                )
            )
        return state

    @staticmethod
    def _instruction_for_effect(
        effect: str,
        *,
        original_request: str,
        goal: str,
    ) -> str:
        if effect == "maintain_project":
            return (
                "Complete only the repository maintenance, task-appropriate validation, and "
                "engineering-branch commit portion of this persistent goal. Do not publish. "
                f"Original request: {original_request}"
            )
        if effect == "push_engineering_branch":
            return (
                "Push the clean committed engineering branch produced for this persistent goal "
                f"to origin without force push. Goal: {goal}"
            )
        if effect == "open_or_update_draft_pr":
            return (
                "Open or update the Draft PR for the already-pushed engineering branch produced "
                f"for this persistent goal. Do not merge it. Goal: {goal}"
            )
        if effect == "inspect_project":
            return f"Inspect the repository for this persistent goal only. Goal: {goal}"
        if effect == "run_project_command":
            return original_request
        raise EngineeringProtocolError(f"unsupported persistent engineering effect: {effect!r}")
