from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
import time
from typing import Callable, Sequence

from core.delivery import DeliveryOutbox

from .backend import ClaudeEngineeringBackend, EngineeringAgentResult
from .bindings import EngineeringConversationBindingStore
from .delivery import EngineeringCompletionDelivery
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


def _read_only_supported(authority: EngineeringAuthority) -> bool:
    return (
        authority.repository_read
        and not authority.repository_write
        and not authority.run_tests
        and not authority.network
        and not authority.publish
        and not authority.outside_repo
    )


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


class EngineeringWorker:
    """Separate fault-domain worker that advances Hikari EngineeringSession state."""

    def __init__(
        self,
        store: EngineeringSessionStore,
        *,
        backend_factory: BackendFactory | None = None,
    ) -> None:
        if not isinstance(store, EngineeringSessionStore):
            raise TypeError("EngineeringWorker requires EngineeringSessionStore")
        self.store = store
        self.backend_factory = backend_factory or self._default_backend

    @staticmethod
    def _default_backend(state: EngineeringSessionState, turn: EngineeringTurn):
        return ClaudeEngineeringBackend(
            permission_mode="plan",
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

        if not _read_only_supported(turn.authority):
            return self._finish(
                state,
                turn,
                status="blocked",
                message="当前 M7-04 竖切只开放只读工程会话；写入、测试、网络或发布权限尚未启用。",
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
        self._event(state.session_id, turn.turn_id, "progress", "正在只读理解项目")
        prompt = _prompt_for_read_only_turn(state, turn)
        try:
            backend = self.backend_factory(state, turn)
            result = backend.run(workspace.path, prompt)
        except Exception as exc:
            return self._finish(
                state,
                turn,
                status="failed",
                message=f"Engineering backend 没有完成：{type(exc).__name__}",
            )

        if not isinstance(result, EngineeringAgentResult):
            raise TypeError("engineering backend must return EngineeringAgentResult")

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
            )

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
    return parser


def _completion_delivery(root: Path, store: EngineeringSessionStore) -> EngineeringCompletionDelivery:
    return EngineeringCompletionDelivery(
        store,
        EngineeringConversationBindingStore(root / "engineering_bindings.json"),
        DeliveryOutbox(root / "proactive_delivery.db"),
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    from resident.windows_host import default_state_dir

    root = Path(args.state_dir).expanduser().resolve() if args.state_dir else default_state_dir()
    store = EngineeringSessionStore(root / "engineering")
    worker = EngineeringWorker(store)
    completion = _completion_delivery(root, store)

    # Recover any terminal result that was persisted before a previous worker
    # stopped but had not yet reached the M6 delivery outbox.
    completion.pump()

    if args.once:
        outcome = worker.run_once()
        completion.pump()
        if outcome is None:
            print("[engineering] idle")
            return 0
        print(f"[engineering] {outcome.status} session={outcome.session_id} turn={outcome.turn_id}")
        return 0 if outcome.status == "completed" else 1

    poll = max(0.2, float(args.poll_seconds))
    print(f"[engineering] worker started state={store.root}")
    while True:
        outcome = worker.run_once()
        completion.pump()
        if outcome is None:
            time.sleep(poll)


if __name__ == "__main__":
    raise SystemExit(main())