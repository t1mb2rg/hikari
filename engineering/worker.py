from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import time
from typing import Callable, Sequence

from core.delivery import DeliveryOutbox

from .backend import ClaudeEngineeringBackend, EngineeringAgentEvent, EngineeringAgentResult
from .bindings import EngineeringConversationBindingStore
from .delivery import EngineeringCompletionDelivery
from .github_publish import open_or_update_draft_pr
from .goal import EngineeringGoalStore
from .heartbeat import (
    EngineeringWorkerHeartbeatEmitter,
    EngineeringWorkerHeartbeatStore,
    EngineeringWorkerLease,
)
from .maintainer import (
    commit_project_changes,
    is_maintainer_authority,
    is_push_authority,
    is_read_only_authority,
    push_engineering_branch,
)
from .validation_policy import change_policy_violations
from .session import (
    EngineeringAuthority,
    EngineeringEvent,
    EngineeringProtocolError,
    EngineeringResult,
    EngineeringSessionState,
    EngineeringSessionStore,
    EngineeringTurn,
)
from .workspace import EngineeringWorkspace, EngineeringWorkspaceError


@dataclass(frozen=True, slots=True)
class WorkerOutcome:
    session_id: str
    turn_id: str
    status: str
    message: str


BackendFactory = Callable[
    [EngineeringSessionState, EngineeringTurn],
    object,
]


_VALIDATION_COMMAND_MARKERS = (
    "pytest",
    "python -m unittest",
    "python -m doctest",
    "tox",
    "nox",
    "ruff",
    "mypy",
    "pyright",
    "npm test",
    "npm run test",
    "pnpm test",
    "yarn test",
    "cargo test",
    "go test",
    "dotnet test",
)
_EFFECT_PREFIX = "Requested effect: "


def _turn_effect(turn: EngineeringTurn) -> str:
    """Read the deterministic effect written by ConversationEngineeringBridge.

    This deliberately does not interpret the user's natural-language intent. The bridge
    writes its machine field after the semantic goal, so the last marker is authoritative.
    Older publish turns without the field remain compatible with the original push path.
    """

    _, marker, tail = turn.context.rpartition(_EFFECT_PREFIX)
    if not marker:
        return ""
    return tail.split(".", 1)[0].strip()


def _prompt_for_read_only_turn(state: EngineeringSessionState, turn: EngineeringTurn) -> str:
    command_note = (
        "You may use ordinary local read-only shell inspection commands inside the repository."
        if turn.authority.run_commands
        else "Do not use shell commands; rely on repository read tools only."
    )
    context = turn.context.strip()
    lines = [
        "# Hikari Engineering Session",
        "You are the engineering reasoning component inside Hikari.",
        "This turn is READ-ONLY. Inspect the repository and answer the engineering intent.",
        "Do not edit, create, delete, rename, stage, commit, publish, or deploy files.",
        "Do not access paths outside this repository and do not use the network.",
        command_note,
        "",
        "# Intent",
        turn.intent,
    ]
    if context:
        lines.extend(["", "# Hikari Context", context])
    if state.backend_session_id:
        lines.extend(
            [
                "",
                "# Continuity",
                "This is a follow-up in the same Hikari engineering session. Preserve prior engineering context.",
            ]
        )
    lines.extend(
        [
            "",
            "# Response",
            "Return a concise but useful engineering conclusion for Hikari. Ground it in what you actually inspected.",
        ]
    )
    return "\n".join(lines) + "\n"


def _prompt_for_maintainer_turn(state: EngineeringSessionState, turn: EngineeringTurn) -> str:
    context = turn.context.strip()
    lines = [
        "# Hikari Engineering Maintainer Session",
        "You are the engineering reasoning and editing component inside Hikari.",
        "The user has delegated ordinary maintenance of this project to Hikari.",
        "Complete the requested repository change inside this isolated engineering worktree.",
        "You may inspect and edit/create/delete project files needed for the task.",
        "Stay inside this repository. Do not use the network or access external secret locations.",
        "Do not stage, commit, push, merge, publish, deploy, or alter Git history; Hikari's Worker owns those steps.",
        "You own task-appropriate validation inside this turn. Choose validation proportionate to the actual change.",
        "Documentation-only changes do not need a meaningless full project test suite. For code/config/test changes, run the relevant checks needed to support completion.",
        "If a validation command fails because of your change, continue diagnosing and repairing inside this same agent loop before finishing.",
        "Do not weaken, skip, or rewrite validation merely to obtain a pass.",
        "",
        "# Intent",
        turn.intent,
    ]
    if context:
        lines.extend(["", "# Hikari Context", context])
    if state.backend_session_id:
        lines.extend(
            [
                "",
                "# Continuity",
                "This is a follow-up in the same Hikari engineering session. Preserve prior engineering context.",
            ]
        )
    lines.extend(
        [
            "",
            "# Response",
            "After editing and any task-appropriate validation, summarize what you changed and what you actually validated. Do not claim commands or tests you did not run.",
        ]
    )
    return "\n".join(lines) + "\n"


class EngineeringWorker:
    """Separate fault-domain worker that advances Hikari EngineeringSession state.

    Claude Code owns the inner engineering agent loop. The Worker owns durable
    state, deterministic authority/scope checks, Git commit/publication, and
    delivery boundaries.
    """

    def __init__(
        self,
        store: EngineeringSessionStore,
        *,
        backend_factory: BackendFactory | None = None,
        max_repair_attempts: int = 2,
    ) -> None:
        if not isinstance(store, EngineeringSessionStore):
            raise TypeError("EngineeringWorker requires EngineeringSessionStore")
        if max_repair_attempts < 0:
            raise ValueError("max_repair_attempts must be >= 0")
        self.store = store
        self.backend_factory = backend_factory or self._default_backend
        # Kept for source compatibility with older callers. Repair now belongs
        # to Claude Code's own agent loop instead of a second Worker loop.
        self.max_repair_attempts = int(max_repair_attempts)

    @staticmethod
    def _default_backend(state: EngineeringSessionState, turn: EngineeringTurn):
        return ClaudeEngineeringBackend(
            permission_mode="acceptEdits" if turn.authority.repository_write else "plan",
            session_id=state.backend_session_id,
        )

    def run_once(self) -> WorkerOutcome | None:
        # Do not execute pending work when its possible Goal ownership is unreadable.
        EngineeringGoalStore(self.store.root.parent / "engineering_goals").list_states()
        pending = [state for state in self.store.list_states() if state.status == "pending"]
        if not pending:
            return None
        state = pending[0]
        if not state.current_turn_id:
            self.store.update_runtime(
                state.session_id,
                status="blocked",
                latest_summary="EngineeringSession 缺少当前 turn",
            )
            return WorkerOutcome(state.session_id, "", "blocked", "session missing current turn")

        turn = self.store.load_turn(state.session_id, state.current_turn_id)
        try:
            state.authorize(turn)
        except EngineeringProtocolError as exc:
            return self._finish(
                state,
                turn,
                status="blocked",
                message=f"工程权限边界拒绝了这个 turn：{exc}",
            )

        read_only = is_read_only_authority(turn.authority)
        maintainer = is_maintainer_authority(turn.authority)
        publish = is_push_authority(turn.authority)
        if not read_only and not maintainer and not publish:
            return self._finish(
                state,
                turn,
                status="blocked",
                message=(
                    "这个工程 turn 超出了当前项目 mandate 的已实现执行配置。"
                    "外部部署、保护分支修改或仓库外操作不会被普通 maintainer turn 自动获得。"
                ),
            )

        self._event(state.session_id, turn.turn_id, "started", "Engineering Worker 已开始处理")
        state = self.store.update_runtime(
            state.session_id,
            status="running",
            latest_summary="正在准备工程工作区",
        )

        try:
            workspace = self._workspace_for(state)
        except EngineeringWorkspaceError as exc:
            return self._finish(
                state,
                turn,
                status="failed",
                message=f"无法准备工程工作区：{exc}",
            )

        state = self.store.load(state.session_id)
        if publish:
            effect = _turn_effect(turn)
            if effect in {"", "push_engineering_branch"}:
                return self._run_push(state, turn, workspace)
            if effect == "open_or_update_draft_pr":
                return self._run_draft_pr(state, turn, workspace)
            return self._finish(
                state,
                turn,
                status="blocked",
                message=f"publish turn 包含未实现的确定性 effect：{effect}",
            )
        if read_only:
            return self._run_read_only(state, turn, workspace)
        return self._run_maintainer(state, turn, workspace)

    def _run_push(
        self,
        state: EngineeringSessionState,
        turn: EngineeringTurn,
        workspace: EngineeringWorkspace,
    ) -> WorkerOutcome:
        self._event(state.session_id, turn.turn_id, "progress", "正在推送非保护 engineering 分支")
        if workspace.uncommitted_files():
            return self._finish(
                state,
                turn,
                status="blocked",
                message="engineering 分支仍有未提交变更，Worker 拒绝 push。",
            )
        try:
            commit_sha = push_engineering_branch(
                workspace.path,
                workspace.branch,
                workspace.baseline_commit,
            )
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            detail = str(exc).strip()
            if len(detail) > 1200:
                detail = detail[-1200:]
            message = "engineering 分支 push 失败"
            if detail:
                message += f"：{detail}"
            return self._finish(
                state,
                turn,
                status="failed",
                message=message,
            )
        return self._finish(
            state,
            turn,
            status="completed",
            message=(
                f"已将非保护工程分支 `{workspace.branch}` 推送到 `origin`。\n"
                f"提交：`{commit_sha[:12]}`。"
            ),
        )

    def _run_draft_pr(
        self,
        state: EngineeringSessionState,
        turn: EngineeringTurn,
        workspace: EngineeringWorkspace,
    ) -> WorkerOutcome:
        self._event(state.session_id, turn.turn_id, "progress", "正在创建或维护 Draft PR")
        if workspace.uncommitted_files():
            return self._finish(
                state,
                turn,
                status="blocked",
                message="engineering 分支仍有未提交变更，Worker 拒绝发布 Draft PR。",
            )
        try:
            result = open_or_update_draft_pr(
                source_repo=state.repository,
                worktree=workspace.path,
                branch=workspace.branch,
                baseline_commit=workspace.baseline_commit,
            )
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            detail = str(exc).strip()
            if len(detail) > 1200:
                detail = detail[-1200:]
            message = "Draft PR 发布失败"
            if detail:
                message += f"：{detail}"
            return self._finish(
                state,
                turn,
                status="failed",
                message=message,
            )

        verb = {
            "created": "已创建",
            "updated": "已更新",
            "existing": "已确认已有",
        }[result.action]
        return self._finish(
            state,
            turn,
            status="completed",
            message=(
                f"{verb} Draft PR #{result.number}：{result.url}\n"
                f"Head：`{result.head}`；Base：`{result.base}`。"
            ),
        )

    def _run_backend(
        self,
        state: EngineeringSessionState,
        turn: EngineeringTurn,
        workspace: EngineeringWorkspace,
        prompt: str,
    ) -> tuple[object, EngineeringAgentResult] | WorkerOutcome:
        try:
            active_backend = self.backend_factory(state, turn)
            set_event_sink = getattr(active_backend, "set_event_sink", None)
            if callable(set_event_sink):
                set_event_sink(
                    lambda event: self._backend_event(state.session_id, turn.turn_id, event)
                )
            result = active_backend.run(workspace.path, prompt)
        except Exception as exc:
            return self._finish(
                state,
                turn,
                status="failed",
                message=f"Engineering backend 没有完成：{type(exc).__name__}",
            )
        if not isinstance(result, EngineeringAgentResult):
            raise TypeError("engineering backend must return EngineeringAgentResult")
        return active_backend, result

    def _backend_event(
        self,
        session_id: str,
        turn_id: str,
        event: EngineeringAgentEvent,
    ) -> None:
        if not isinstance(event, EngineeringAgentEvent) or not event.summary.strip():
            return
        self._event(
            session_id,
            turn_id,
            "progress",
            f"Claude Code: {event.summary.strip()}"[:1000],
        )

    def _run_read_only(
        self,
        state: EngineeringSessionState,
        turn: EngineeringTurn,
        workspace: EngineeringWorkspace,
    ) -> WorkerOutcome:
        self._event(state.session_id, turn.turn_id, "progress", "正在只读理解项目")
        backend_result = self._run_backend(
            state,
            turn,
            workspace,
            _prompt_for_read_only_turn(state, turn),
        )
        if isinstance(backend_result, WorkerOutcome):
            return backend_result
        _, result = backend_result

        changed = workspace.uncommitted_files()
        if changed:
            return self._finish(
                state,
                turn,
                status="blocked",
                message="只读工程会话检测到当前 turn 产生未提交仓库变化，结果已拒绝。",
                backend_session_id=result.session_id or None,
                changed_files=changed,
            )
        if result.returncode != 0:
            return self._backend_failure(state, turn, result)
        message = result.final_message.strip()
        if not message:
            return self._finish(
                state,
                turn,
                status="failed",
                message="Engineering backend 没有返回可消费的工程结论。",
                backend_session_id=result.session_id or None,
            )
        return self._finish(
            state,
            turn,
            status="completed",
            message=message,
            backend_session_id=result.session_id or None,
        )

    def _run_maintainer(
        self,
        state: EngineeringSessionState,
        turn: EngineeringTurn,
        workspace: EngineeringWorkspace,
    ) -> WorkerOutcome:
        self._event(state.session_id, turn.turn_id, "progress", "正在维护项目")
        backend_result = self._run_backend(
            state,
            turn,
            workspace,
            _prompt_for_maintainer_turn(state, turn),
        )
        if isinstance(backend_result, WorkerOutcome):
            return backend_result
        _, result = backend_result
        if result.returncode != 0:
            return self._backend_failure(
                state,
                turn,
                result,
                changed_files=workspace.changed_files(),
            )

        changed = workspace.changed_files()
        if not changed:
            message = result.final_message.strip() or "检查完成，当前任务不需要修改仓库。"
            return self._finish(
                state,
                turn,
                status="completed",
                message=message,
                backend_session_id=result.session_id or None,
            )

        violation = self._change_policy_violation(turn, workspace, changed)
        if violation:
            return self._finish(
                state,
                turn,
                status="blocked",
                message=f"工程修改超出任务范围或改变了验证标准：{violation}",
                backend_session_id=result.session_id or None,
                changed_files=changed,
            )

        self._event(
            state.session_id,
            turn.turn_id,
            "progress",
            "工程执行完成，范围检查通过，正在提交 engineering 分支",
        )
        try:
            commit_sha = commit_project_changes(workspace.path, turn.intent)
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            return self._finish(
                state,
                turn,
                status="failed",
                message=f"工程执行完成，但 engineering 分支提交失败：{type(exc).__name__}",
                backend_session_id=result.session_id or None,
                changed_files=changed,
            )

        file_summary = "、".join(changed) if changed else "无"
        summary = (
            f"任务已完成。\n修改：{file_summary}\n"
            f"验证：{self._validation_summary(result.events)}"
        )
        backend_summary = result.final_message.strip()
        if backend_summary:
            summary += f"\n工程结论：{backend_summary[:1200]}"
        if commit_sha:
            summary += f"\n提交：`{workspace.branch}` / `{commit_sha[:12]}`。"
        else:
            summary += "\n提交：当前没有需要提交的剩余变更。"
        return self._finish(
            state,
            turn,
            status="completed",
            message=summary,
            backend_session_id=result.session_id or None,
            changed_files=changed,
        )

    @staticmethod
    def _validation_summary(events: tuple[EngineeringAgentEvent, ...]) -> str:
        commands: list[str] = []
        for event in events:
            if event.kind != "tool" or not event.summary.startswith("Bash:"):
                continue
            command = event.summary.split(":", 1)[1].strip()
            normalized = command.casefold()
            if any(marker in normalized for marker in _VALIDATION_COMMAND_MARKERS):
                commands.append(command)
        if commands:
            visible = "；".join(f"`{command[:220]}`" for command in commands[:4])
            if len(commands) > 4:
                visible += f"；另有 {len(commands) - 4} 条"
            return f"Claude Code 已执行任务内验证：{visible}"
        return "Claude Code 按任务范围自主管理验证；未记录到项目测试命令，Hikari 已完成改动范围检查。"

    @staticmethod
    def _change_policy_violation(
        turn: EngineeringTurn,
        workspace: EngineeringWorkspace,
        changed_files: tuple[str, ...],
    ) -> str:
        violations = change_policy_violations(
            turn.intent,
            changed_files,
            workspace.diff_text(),
        )
        return "; ".join(violations)

    def _backend_failure(
        self,
        state: EngineeringSessionState,
        turn: EngineeringTurn,
        result: EngineeringAgentResult,
        *,
        changed_files: tuple[str, ...] = (),
    ) -> WorkerOutcome:
        detail = (result.stderr or "").strip()
        if len(detail) > 1200:
            detail = detail[-1200:]
        message = "Engineering backend 执行失败"
        if detail:
            message += f"：{detail}"
        if result.events:
            recent = " | ".join(event.summary for event in result.events[-4:])
            message += f"\n最后活动：{recent[:1000]}"
        return self._finish(
            state,
            turn,
            status="failed",
            message=message,
            backend_session_id=result.session_id or None,
            changed_files=changed_files,
        )

    def _workspace_for(self, state: EngineeringSessionState) -> EngineeringWorkspace:
        if state.workspace_path and state.workspace_branch and state.baseline_commit:
            return EngineeringWorkspace.resume(
                repository=state.repository,
                workspace_path=state.workspace_path,
                branch=state.workspace_branch,
                baseline_commit=state.baseline_commit,
            )
        workspace = EngineeringWorkspace.create(state.repository, state.session_id)
        self.store.update_runtime(
            state.session_id,
            workspace_path=str(workspace.path),
            workspace_branch=workspace.branch,
            baseline_commit=workspace.baseline_commit,
            latest_summary="工程工作区已准备",
        )
        return workspace

    def _event(self, session_id: str, turn_id: str, kind: str, summary: str) -> None:
        state = self.store.load(session_id)
        self.store.append_event(
            EngineeringEvent(
                session_id=session_id,
                turn_id=turn_id,
                sequence=state.next_sequence,
                kind=kind,
                summary=summary,
                timestamp=time.time(),
            )
        )

    def _finish(
        self,
        state: EngineeringSessionState,
        turn: EngineeringTurn,
        *,
        status: str,
        message: str,
        backend_session_id: str | None = None,
        changed_files: tuple[str, ...] = (),
    ) -> WorkerOutcome:
        event_kind = status if status in {"completed", "failed", "blocked"} else "failed"
        self._event(state.session_id, turn.turn_id, event_kind, message[:1000])
        result = EngineeringResult(
            turn_id=turn.turn_id,
            status=event_kind,
            message=message,
            backend_session_id=backend_session_id,
            changed_files=changed_files,
            completed_at=time.time(),
        )
        self.store.save_result(state.session_id, result)
        return WorkerOutcome(state.session_id, turn.turn_id, event_kind, message)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run Hikari's isolated engineering worker")
    parser.add_argument("--state-dir", default=None)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--heartbeat-seconds", type=float, default=1.0)
    parser.add_argument("--owner", default="manual")
    return parser


def _completion_delivery(root: Path, store: EngineeringSessionStore) -> EngineeringCompletionDelivery:
    return EngineeringCompletionDelivery(
        store,
        EngineeringConversationBindingStore(root / "engineering_bindings.json"),
        DeliveryOutbox(root / "proactive_delivery.db"),
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    from resident.paths import default_state_dir

    root = Path(args.state_dir).expanduser().resolve() if args.state_dir else default_state_dir()
    store = EngineeringSessionStore(root / "engineering")
    worker = EngineeringWorker(store)
    completion = _completion_delivery(root, store)
    heartbeat_store = EngineeringWorkerHeartbeatStore(root / "engineering_worker.json")
    lease = EngineeringWorkerLease(root / "engineering_worker.lock", heartbeat_store)
    pid = os.getpid()
    started_at = time.time()

    try:
        lease.acquire(pid=pid, owner=args.owner, started_at=started_at)
    except RuntimeError as exc:
        print(f"[engineering] {exc}")
        return 2

    heartbeat = EngineeringWorkerHeartbeatEmitter(
        heartbeat_store,
        owner=args.owner,
        interval_seconds=max(0.2, float(args.heartbeat_seconds)),
        pid=pid,
    )

    try:
        with heartbeat:
            completion.pump()

            if args.once:
                outcome = worker.run_once()
                completion.pump()
                if outcome is None:
                    print("[engineering] idle")
                    return 0
                print(
                    f"[engineering] {outcome.status} session={outcome.session_id} turn={outcome.turn_id}"
                )
                return 0 if outcome.status == "completed" else 1

            poll = max(0.2, float(args.poll_seconds))
            print(f"[engineering] worker started state={store.root} owner={args.owner}")
            while True:
                outcome = worker.run_once()
                completion.pump()
                if outcome is None:
                    time.sleep(poll)
    finally:
        lease.release()


if __name__ == "__main__":
    raise SystemExit(main())
