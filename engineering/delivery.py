from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import time

from core.delivery import DeliveryOutbox, DeliveryRequest, DeliveryRouter

from .bindings import EngineeringConversationBindingStore
from .effects import RESTART_REPLAY_SAFE_EFFECTS, turn_effect
from .goal import EngineeringGoalState, EngineeringGoalStore
from .maintainer_loop import PersistentMaintainerLoop
from .session import (
    EngineeringEvent,
    EngineeringProtocolError,
    EngineeringResult,
    EngineeringSessionStore,
)
from .workspace import EngineeringWorkspace, EngineeringWorkspaceError


@dataclass(frozen=True)
class EngineeringCompletionFacts:
    """Machine truth projected from one durable terminal engineering unit.

    ``goal`` may describe a legacy single turn or a persistent multi-step goal. This
    object deliberately contains no user-facing prose. Engineering owns the grounded
    result; a Conversation-owned renderer decides how Jarvis says it.
    """

    status: str
    goal: str
    summary: str
    changed_files: tuple[str, ...] = ()
    branch: str | None = None
    historical: bool = False


EngineeringCompletionRenderer = Callable[[EngineeringCompletionFacts, str, str], str]


class EngineeringCompletionDelivery:
    """Advance goal truth and project terminal facts into Hikari's durable outbox.

    Resident constructs this class with a Conversation-owned renderer. In that mode the
    same one-second pump first advances persistent Engineering Goals, then delivers only
    whole-goal terminal facts. Intermediate goal steps are deliberately suppressed so a
    completed edit turn cannot masquerade as completion of a still-pending push/PR goal.

    Engineering Worker constructs this class without a renderer. The Worker invokes its
    pump only after successfully acquiring the single-worker lease. That ownership
    transition is therefore also the safe place to reconcile a ``running`` session left
    behind by a killed previous Worker. Restart replay is effect-aware: only explicitly
    replay-safe effects are returned to ``pending``. Commands with uncertain side effects
    become a grounded blocked result rather than being executed a second time.
    Existing DeliveryOutbox rows remain immutable historical facts.
    """

    def __init__(
        self,
        sessions: EngineeringSessionStore,
        bindings: EngineeringConversationBindingStore,
        outbox: DeliveryOutbox,
        *,
        renderer: EngineeringCompletionRenderer | None = None,
    ) -> None:
        if not isinstance(sessions, EngineeringSessionStore):
            raise TypeError("EngineeringCompletionDelivery requires EngineeringSessionStore")
        if not isinstance(bindings, EngineeringConversationBindingStore):
            raise TypeError("EngineeringCompletionDelivery requires EngineeringConversationBindingStore")
        if not isinstance(outbox, DeliveryOutbox):
            raise TypeError("EngineeringCompletionDelivery requires DeliveryOutbox")
        if renderer is not None and not callable(renderer):
            raise TypeError("EngineeringCompletionDelivery renderer must be callable or None")
        self.sessions = sessions
        self.bindings = bindings
        self.router = DeliveryRouter(outbox)
        self.renderer = renderer
        self.goals = EngineeringGoalStore(sessions.root.parent / "engineering_goals")
        self.maintainer_loop = (
            PersistentMaintainerLoop(self.goals, sessions) if renderer is not None else None
        )

    def pump(self) -> int:
        """Idempotently reconcile ownership, advance goals, and enqueue terminal facts."""

        if self.renderer is None:
            self._recover_worker_owned_running_turns()

        if self.maintainer_loop is not None:
            self.maintainer_loop.advance_all()

        managed_turn_ids = self._managed_turn_ids()
        submitted = self._pump_single_turns(managed_turn_ids)
        submitted += self._pump_terminal_goals()
        return submitted

    def _recover_worker_owned_running_turns(self) -> None:
        """Reconcile turns orphaned when a previous Worker died while ``running``.

        This method is intentionally reachable only from the renderer-less Worker pump.
        Worker main calls that pump after acquiring the global Engineering Worker lease,
        so no second live Worker is allowed to be executing these sessions concurrently.
        The durable turn id is never replaced here.
        """

        for state in self.sessions.list_states():
            if state.status != "running":
                continue
            turn_id = state.current_turn_id
            if not turn_id:
                self.sessions.update_runtime(
                    state.session_id,
                    status="blocked",
                    latest_summary="Engineering Worker 重启时发现 running session 缺少 current turn",
                )
                continue

            try:
                result = self.sessions.load_result(state.session_id, turn_id)
            except EngineeringProtocolError as exc:
                if not str(exc).startswith("unknown engineering result:"):
                    self._block_recovery(
                        state.session_id,
                        turn_id,
                        "Engineering Worker 重启时无法读取遗留 turn result；不会猜测执行结果",
                    )
                    continue
            else:
                # Crash window: the result file reached disk but state.json did not.
                self.sessions.save_result(state.session_id, result)
                continue

            try:
                turn = self.sessions.load_turn(state.session_id, turn_id)
            except EngineeringProtocolError:
                self._block_recovery(
                    state.session_id,
                    turn_id,
                    "Engineering Worker 重启时发现遗留 running turn 不可读取；不会猜测或重放",
                )
                continue

            effect = turn_effect(turn)
            if effect not in RESTART_REPLAY_SAFE_EFFECTS:
                label = effect or "unknown"
                self._block_recovery(
                    state.session_id,
                    turn_id,
                    (
                        f"Engineering Worker 重启时 `{label}` 的执行结果不确定；"
                        "该 effect 不允许自动重放，以避免重复副作用"
                    ),
                )
                continue

            if effect in {"inspect_project", "maintain_project"}:
                cleanup_error = self._clean_interrupted_local_work(state)
                if cleanup_error is not None:
                    self._block_recovery(
                        state.session_id,
                        turn_id,
                        cleanup_error,
                    )
                    continue

            recovery_summary = (
                "Engineering Worker restart recovered the same durable turn; "
                f"effect={effect}"
            )
            recovered = self.sessions.update_runtime(
                state.session_id,
                status="pending",
                latest_summary=recovery_summary,
            )
            self.sessions.append_event(
                EngineeringEvent(
                    session_id=state.session_id,
                    turn_id=turn_id,
                    sequence=recovered.next_sequence,
                    kind="accepted",
                    summary=recovery_summary,
                    timestamp=time.time(),
                )
            )

    def _clean_interrupted_local_work(self, state) -> str | None:
        if not (state.workspace_path and state.workspace_branch and state.baseline_commit):
            return None
        try:
            workspace = EngineeringWorkspace.resume(
                repository=state.repository,
                workspace_path=state.workspace_path,
                branch=state.workspace_branch,
                baseline_commit=state.baseline_commit,
            )
            discarded = workspace.discard_uncommitted_changes()
        except (EngineeringWorkspaceError, OSError) as exc:
            return (
                "Engineering Worker 重启时无法把隔离 worktree 恢复到可信 committed state："
                f"{type(exc).__name__}；不会在半完成修改上自动重放"
            )
        if discarded:
            return None
        return None

    def _block_recovery(self, session_id: str, turn_id: str, message: str) -> None:
        """Persist one grounded terminal result when restart outcome is not replay-safe."""

        state = self.sessions.load(session_id)
        self.sessions.append_event(
            EngineeringEvent(
                session_id=session_id,
                turn_id=turn_id,
                sequence=state.next_sequence,
                kind="blocked",
                summary=message[:1000],
                timestamp=time.time(),
            )
        )
        self.sessions.save_result(
            session_id,
            EngineeringResult(
                turn_id=turn_id,
                status="blocked",
                message=message,
                completed_at=time.time(),
            ),
        )

    def _managed_turn_ids(self) -> set[str]:
        managed: set[str] = set()
        for goal in self.goals.list_states():
            for step in goal.steps:
                if step.turn_id:
                    managed.add(step.turn_id)
        return managed

    def _pump_single_turns(self, managed_turn_ids: set[str]) -> int:
        if self.renderer is None:
            return 0
        submitted = 0
        for state in self.sessions.list_states():
            if state.status not in {"completed", "failed", "blocked"}:
                continue
            turn_id = state.current_turn_id
            if not turn_id or turn_id in managed_turn_ids:
                continue
            try:
                turn = self.sessions.load_turn(state.session_id, turn_id)
                result = self.sessions.load_result(state.session_id, turn_id)
            except EngineeringProtocolError:
                continue
            binding = self.bindings.get(state.session_id)
            if binding is None:
                continue
            delivery_id = f"engineering:{state.session_id}:{turn_id}"
            facts = EngineeringCompletionFacts(
                status=result.status,
                goal=turn.intent,
                summary=result.message,
                changed_files=result.changed_files,
                branch=state.workspace_branch,
            )
            text = self.renderer(facts, binding.channel, binding.conversation_id)
            request = DeliveryRequest(
                delivery_id=delivery_id,
                channel=binding.channel,
                recipient=binding.conversation_id.removeprefix("private:"),
                text=text,
                kind="engineering_terminal",
                created_at=result.completed_at or time.time(),
                source_id=turn_id,
            )
            if self.router.submit(request):
                submitted += 1
        return submitted

    def _pump_terminal_goals(self) -> int:
        if self.renderer is None:
            return 0
        submitted = 0
        for goal in self.goals.list_states():
            if goal.status not in {"completed", "failed", "blocked"}:
                continue
            binding = self.bindings.get(goal.session_id)
            if binding is None:
                continue
            state = self.sessions.load(goal.session_id)
            changed_files = self._goal_changed_files(goal)
            facts = EngineeringCompletionFacts(
                status=goal.status,
                goal=goal.goal,
                summary=goal.final_summary or goal.current_step.result_message,
                changed_files=changed_files,
                branch=state.workspace_branch,
            )
            text = self.renderer(facts, binding.channel, binding.conversation_id)
            delivery_id = f"engineering-goal:{goal.goal_id}"
            request = DeliveryRequest(
                delivery_id=delivery_id,
                channel=binding.channel,
                recipient=binding.conversation_id.removeprefix("private:"),
                text=text,
                kind="engineering_goal_terminal",
                created_at=goal.updated_at,
                source_id=goal.goal_id,
            )
            if self.router.submit(request):
                submitted += 1
        return submitted

    def _goal_changed_files(self, goal: EngineeringGoalState) -> tuple[str, ...]:
        seen: set[str] = set()
        files: list[str] = []
        for step in goal.steps:
            if not step.turn_id:
                continue
            try:
                result = self.sessions.load_result(goal.session_id, step.turn_id)
            except EngineeringProtocolError:
                continue
            for path in result.changed_files:
                if path not in seen:
                    seen.add(path)
                    files.append(path)
        return tuple(files)
