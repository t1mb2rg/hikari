from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import time
from typing import Callable, Sequence

from core.delivery import DeliveryOutbox

from .backend import ClaudeEngineeringBackend, EngineeringAgentResult
from .bindings import EngineeringConversationBindingStore
from .delivery import EngineeringCompletionDelivery
from .heartbeat import (
    EngineeringWorkerHeartbeatEmitter,
    EngineeringWorkerHeartbeatStore,
    EngineeringWorkerLease,
)
from .maintainer import (
    commit_project_changes,
    is_maintainer_authority,
    is_read_only_authority,
    run_project_tests,
)
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
        "Hikari's Worker will run the project test suite after your edit, so focus on making a coherent implementation.",
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
            "After editing, summarize what you changed and any important design decision. Do not claim tests passed; the Worker validates them separately.",
        ]
    )
    return "\n".join(lines) + "\n"


def _prompt_for_test_repair(turn: EngineeringTurn, test_output: str, attempt: int) -> str:
    detail = test_output[-3500:] if test_output else "pytest returned a non-zero status without output"
    return (
        "# Hikari Engineering Validation Repair\n"
        "The Hikari Worker ran the project test suite after your implementation and it failed.\n"
        f"Repair attempt: {attempt}.\n"
        "Inspect the current worktree, fix the implementation, and do not stage/commit/push/publish.\n"
        "Stay inside the repository and do not use the network.\n\n"
        f"# Original intent\n{turn.intent}\n\n"
        f"# Test failure\n{detail}\n\n"
        "# Response\nApply the necessary repository edits and briefly summarize the repair.\n"
    )


class EngineeringWorker:
    """Separate fault-domain worker that advances Hikari EngineeringSession state."""

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
        self.max_repair_attempts = int(max_repair_attempts)

    @staticmethod
    def _default_backend(state: EngineeringSessionState, turn: EngineeringTurn):
        return ClaudeEngineeringBackend(
            permission_mode="acceptEdits" if turn.authority.repository_write else "plan",
            session_id=state.backend_session_id,
        )

    def run_once(self) -> WorkerOutcome | None:
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
        if not read_only and not maintainer:
            return self._finish(
                state,
                turn,
                status="blocked",
                message=(
                    "这个工程 turn 超出了当前项目 mandate 的已实现执行配置。"
                    "外部网络、发布、部署或仓库外操作不会被普通 maintainer turn 自动获得。"
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
        if read_only:
            return self._run_read_only(state, turn, workspace)
        return self._run_maintainer(state, turn, workspace)

    def _run_backend(
        self,
        state: EngineeringSessionState,
        turn: EngineeringTurn,
        workspace: EngineeringWorkspace,
        prompt: str,
        *,
        backend: object | None = None,
    ) -> tuple[object, EngineeringAgentResult] | WorkerOutcome:
        try:
            active_backend = backend or self.backend_factory(state, turn)
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

        changed = workspace.changed_files()
        if changed:
            return self._finish(
                state,
                turn,
                status="blocked",
                message="只读工程会话检测到仓库发生变化，结果已拒绝。",
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
        backend, result = backend_result
        if result.returncode != 0:
            return self._backend_failure(state, turn, result)

        latest_result = result
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

        self._event(state.session_id, turn.turn_id, "progress", "正在运行项目测试")
        try:
            tests = run_project_tests(workspace.path)
        except (OSError, subprocess.SubprocessError) as exc:
            return self._finish(
                state,
                turn,
                status="failed",
                message=f"项目测试没有完成：{type(exc).__name__}",
                backend_session_id=result.session_id or None,
                changed_files=changed,
            )

        for attempt in range(1, self.max_repair_attempts + 1):
            if tests.passed:
                break
            self._event(
                state.session_id,
                turn.turn_id,
                "progress",
                f"测试失败，正在进行自动修复 {attempt}/{self.max_repair_attempts}",
            )
            repair_result = self._run_backend(
                state,
                turn,
                workspace,
                _prompt_for_test_repair(turn, tests.output, attempt),
                backend=backend,
            )
            if isinstance(repair_result, WorkerOutcome):
                return repair_result
            backend, latest_result = repair_result
            if latest_result.returncode != 0:
                return self._backend_failure(
                    state,
                    turn,
                    latest_result,
                    changed_files=workspace.changed_files(),
                )
            try:
                tests = run_project_tests(workspace.path)
            except (OSError, subprocess.SubprocessError) as exc:
                return self._finish(
                    state,
                    turn,
                    status="failed",
                    message=f"自动修复后的项目测试没有完成：{type(exc).__name__}",
                    backend_session_id=latest_result.session_id or None,
                    changed_files=workspace.changed_files(),
                )

        changed = workspace.changed_files()
        if not tests.passed:
            detail = tests.output[-1800:] if tests.output else "pytest failed without output"
            return self._finish(
                state,
                turn,
                status="failed",
                message=f"自动修复后测试仍未通过：\n{detail}",
                backend_session_id=latest_result.session_id or None,
                changed_files=changed,
            )

        self._event(state.session_id, turn.turn_id, "progress", "测试通过，正在提交工程分支")
        try:
            commit_sha = commit_project_changes(workspace.path, turn.intent)
        except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
            return self._finish(
                state,
                turn,
                status="failed",
                message=f"测试通过，但工程分支提交失败：{type(exc).__name__}",
                backend_session_id=latest_result.session_id or None,
                changed_files=changed,
            )

        summary = latest_result.final_message.strip() or result.final_message.strip()
        if not summary:
            summary = "工程修改已完成。"
        if commit_sha:
            summary += (
                f"\n\nHikari Worker 已运行项目测试并通过，修改已提交到隔离工程分支 "
                f"`{workspace.branch}`，commit `{commit_sha[:12]}`。"
            )
        else:
            summary += "\n\nHikari Worker 已运行项目测试并通过，当前没有需要提交的剩余变更。"
        return self._finish(
            state,
            turn,
            status="completed",
            message=summary,
            backend_session_id=latest_result.session_id or None,
            changed_files=changed,
        )

    def _backend_failure(
        self,
        state: EngineeringSessionState,
        turn: EngineeringTurn,
        result: EngineeringAgentResult,
        *,
        changed_files: tuple[str, ...] = (),
    ) -> WorkerOutcome:
        detail = (result.stderr or result.stdout).strip()
        if len(detail) > 1200:
            detail = detail[-1200:]
        message = "Engineering backend 执行失败"
        if detail:
            message += f"：{detail}"
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
