from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
import json
import os
import time
from typing import Mapping
from uuid import uuid4


_SESSION_STATUSES = frozenset({"idle", "pending", "running", "completed", "failed", "blocked"})
_EVENT_KINDS = frozenset({"accepted", "started", "progress", "completed", "failed", "blocked"})
_RESULT_STATUSES = frozenset({"completed", "failed", "blocked"})


class EngineeringProtocolError(ValueError):
    """Raised when durable engineering state is malformed or unsafe."""


@dataclass(frozen=True, slots=True)
class EngineeringAuthority:
    """Machine-readable outer boundary for one engineering session/turn.

    The task itself stays natural language. These flags are never inferred from
    model wording and a turn may only use a subset of its session ceiling.
    """

    repository_read: bool = False
    repository_write: bool = False
    run_commands: bool = False
    run_tests: bool = False
    network: bool = False
    publish: bool = False
    outside_repo: bool = False

    _FIELDS = (
        "repository_read",
        "repository_write",
        "run_commands",
        "run_tests",
        "network",
        "publish",
        "outside_repo",
    )

    def __post_init__(self) -> None:
        for field in self._FIELDS:
            if not isinstance(getattr(self, field), bool):
                raise TypeError(f"engineering authority {field} must be bool")
        if self.repository_write and not self.repository_read:
            raise EngineeringProtocolError("repository_write requires repository_read")
        if self.run_tests and not self.run_commands:
            raise EngineeringProtocolError("run_tests requires run_commands")
        if self.publish and not self.network:
            raise EngineeringProtocolError("publish requires network")

    @classmethod
    def read_only(cls) -> "EngineeringAuthority":
        return cls(repository_read=True)

    def is_subset_of(self, ceiling: "EngineeringAuthority") -> bool:
        if not isinstance(ceiling, EngineeringAuthority):
            raise TypeError("engineering authority ceiling must be EngineeringAuthority")
        return all(not getattr(self, field) or getattr(ceiling, field) for field in self._FIELDS)

    def to_mapping(self) -> dict[str, bool]:
        return {field: getattr(self, field) for field in self._FIELDS}

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "EngineeringAuthority":
        if not isinstance(payload, Mapping):
            raise TypeError("engineering authority must be an object")
        unexpected = sorted(str(key) for key in payload if key not in cls._FIELDS)
        if unexpected:
            raise EngineeringProtocolError(
                "unsupported engineering authority field(s): " + ", ".join(unexpected)
            )
        values: dict[str, bool] = {}
        for field in cls._FIELDS:
            value = payload.get(field, False)
            if not isinstance(value, bool):
                raise TypeError(f"engineering authority {field} must be bool")
            values[field] = value
        return cls(**values)


@dataclass(frozen=True, slots=True)
class EngineeringTurn:
    turn_id: str
    intent: str
    context: str
    authority: EngineeringAuthority
    created_at: float

    def __post_init__(self) -> None:
        turn_id = self.turn_id.strip()
        intent = self.intent.strip()
        context = self.context.strip()
        if not turn_id:
            raise EngineeringProtocolError("engineering turn_id must not be empty")
        if not intent:
            raise EngineeringProtocolError("engineering intent must not be empty")
        if not isinstance(self.authority, EngineeringAuthority):
            raise TypeError("engineering turn authority must be EngineeringAuthority")
        object.__setattr__(self, "turn_id", turn_id)
        object.__setattr__(self, "intent", intent)
        object.__setattr__(self, "context", context)
        object.__setattr__(self, "created_at", float(self.created_at))

    @classmethod
    def create(
        cls,
        *,
        intent: str,
        authority: EngineeringAuthority,
        context: str = "",
    ) -> "EngineeringTurn":
        return cls(
            turn_id=uuid4().hex,
            intent=intent,
            context=context,
            authority=authority,
            created_at=time.time(),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "version": 1,
            "turn_id": self.turn_id,
            "intent": self.intent,
            "context": self.context,
            "authority": self.authority.to_mapping(),
            "created_at": self.created_at,
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "EngineeringTurn":
        if payload.get("version") != 1:
            raise EngineeringProtocolError("unsupported engineering turn version")
        authority = payload.get("authority")
        if not isinstance(authority, Mapping):
            raise TypeError("engineering turn authority must be an object")
        return cls(
            turn_id=str(payload.get("turn_id", "")),
            intent=str(payload.get("intent", "")),
            context=str(payload.get("context", "")),
            authority=EngineeringAuthority.from_mapping(authority),
            created_at=float(payload.get("created_at", 0.0)),
        )


@dataclass(frozen=True, slots=True)
class EngineeringResult:
    turn_id: str
    status: str
    message: str
    backend_session_id: str | None = None
    changed_files: tuple[str, ...] = ()
    completed_at: float = 0.0

    def __post_init__(self) -> None:
        turn_id = self.turn_id.strip()
        status = self.status.strip().lower()
        message = self.message.strip()
        if not turn_id:
            raise EngineeringProtocolError("engineering result turn_id must not be empty")
        if status not in _RESULT_STATUSES:
            raise EngineeringProtocolError(f"unsupported engineering result status: {status!r}")
        if not message:
            raise EngineeringProtocolError("engineering result message must not be empty")
        changed_files = tuple(str(item).strip() for item in self.changed_files if str(item).strip())
        backend_session_id = (
            self.backend_session_id.strip()
            if isinstance(self.backend_session_id, str) and self.backend_session_id.strip()
            else None
        )
        object.__setattr__(self, "turn_id", turn_id)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "message", message)
        object.__setattr__(self, "backend_session_id", backend_session_id)
        object.__setattr__(self, "changed_files", changed_files)
        object.__setattr__(self, "completed_at", float(self.completed_at or time.time()))

    def to_mapping(self) -> dict[str, object]:
        return {
            "version": 1,
            "turn_id": self.turn_id,
            "status": self.status,
            "message": self.message,
            "backend_session_id": self.backend_session_id,
            "changed_files": list(self.changed_files),
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "EngineeringResult":
        if payload.get("version") != 1:
            raise EngineeringProtocolError("unsupported engineering result version")
        changed = payload.get("changed_files") or []
        if not isinstance(changed, list):
            raise TypeError("engineering changed_files must be a list")
        return cls(
            turn_id=str(payload.get("turn_id", "")),
            status=str(payload.get("status", "")),
            message=str(payload.get("message", "")),
            backend_session_id=(
                str(payload["backend_session_id"])
                if payload.get("backend_session_id") is not None
                else None
            ),
            changed_files=tuple(str(item) for item in changed),
            completed_at=float(payload.get("completed_at", 0.0)),
        )


@dataclass(frozen=True, slots=True)
class EngineeringEvent:
    session_id: str
    turn_id: str
    sequence: int
    kind: str
    summary: str
    timestamp: float

    def __post_init__(self) -> None:
        session_id = self.session_id.strip()
        turn_id = self.turn_id.strip()
        kind = self.kind.strip().lower()
        summary = self.summary.strip()
        if not session_id:
            raise EngineeringProtocolError("engineering event session_id must not be empty")
        if not turn_id:
            raise EngineeringProtocolError("engineering event turn_id must not be empty")
        if not isinstance(self.sequence, int) or isinstance(self.sequence, bool) or self.sequence < 1:
            raise EngineeringProtocolError("engineering event sequence must be an integer >= 1")
        if kind not in _EVENT_KINDS:
            raise EngineeringProtocolError(f"unsupported engineering event kind: {kind!r}")
        if not summary:
            raise EngineeringProtocolError("engineering event summary must not be empty")
        object.__setattr__(self, "session_id", session_id)
        object.__setattr__(self, "turn_id", turn_id)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "summary", summary)
        object.__setattr__(self, "timestamp", float(self.timestamp))

    def to_mapping(self) -> dict[str, object]:
        return {
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "sequence": self.sequence,
            "kind": self.kind,
            "summary": self.summary,
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True, slots=True)
class EngineeringSessionState:
    session_id: str
    project_id: str
    repository: str
    authority_ceiling: EngineeringAuthority
    status: str = "idle"
    current_turn_id: str | None = None
    backend_session_id: str | None = None
    workspace_path: str | None = None
    workspace_branch: str | None = None
    baseline_commit: str | None = None
    latest_summary: str = ""
    next_sequence: int = 1
    updated_at: float = 0.0

    def __post_init__(self) -> None:
        session_id = self.session_id.strip()
        project_id = self.project_id.strip()
        repository = self.repository.strip()
        status = self.status.strip().lower()
        if not session_id:
            raise EngineeringProtocolError("engineering session_id must not be empty")
        if not project_id:
            raise EngineeringProtocolError("engineering project_id must not be empty")
        if not repository:
            raise EngineeringProtocolError("engineering repository must not be empty")
        if status not in _SESSION_STATUSES:
            raise EngineeringProtocolError(f"unsupported engineering session status: {status!r}")
        if not isinstance(self.authority_ceiling, EngineeringAuthority):
            raise TypeError("engineering authority_ceiling must be EngineeringAuthority")
        if not isinstance(self.next_sequence, int) or isinstance(self.next_sequence, bool) or self.next_sequence < 1:
            raise EngineeringProtocolError("engineering next_sequence must be an integer >= 1")
        object.__setattr__(self, "session_id", session_id)
        object.__setattr__(self, "project_id", project_id)
        object.__setattr__(self, "repository", repository)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "updated_at", float(self.updated_at or time.time()))

    @classmethod
    def create(
        cls,
        *,
        project_id: str,
        repository: str | Path,
        authority_ceiling: EngineeringAuthority,
        session_id: str | None = None,
    ) -> "EngineeringSessionState":
        return cls(
            session_id=session_id or uuid4().hex,
            project_id=project_id,
            repository=str(Path(repository).expanduser().resolve()),
            authority_ceiling=authority_ceiling,
            updated_at=time.time(),
        )

    def authorize(self, turn: EngineeringTurn) -> None:
        if not turn.authority.is_subset_of(self.authority_ceiling):
            raise EngineeringProtocolError("engineering turn exceeds the session authority ceiling")

    def to_mapping(self) -> dict[str, object]:
        return {
            "version": 1,
            "session_id": self.session_id,
            "project_id": self.project_id,
            "repository": self.repository,
            "authority_ceiling": self.authority_ceiling.to_mapping(),
            "status": self.status,
            "current_turn_id": self.current_turn_id,
            "backend_session_id": self.backend_session_id,
            "workspace_path": self.workspace_path,
            "workspace_branch": self.workspace_branch,
            "baseline_commit": self.baseline_commit,
            "latest_summary": self.latest_summary,
            "next_sequence": self.next_sequence,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "EngineeringSessionState":
        if payload.get("version") != 1:
            raise EngineeringProtocolError("unsupported engineering session state version")
        authority = payload.get("authority_ceiling")
        if not isinstance(authority, Mapping):
            raise TypeError("engineering authority_ceiling must be an object")
        return cls(
            session_id=str(payload.get("session_id", "")),
            project_id=str(payload.get("project_id", "")),
            repository=str(payload.get("repository", "")),
            authority_ceiling=EngineeringAuthority.from_mapping(authority),
            status=str(payload.get("status", "")),
            current_turn_id=(
                str(payload["current_turn_id"])
                if payload.get("current_turn_id") is not None
                else None
            ),
            backend_session_id=(
                str(payload["backend_session_id"])
                if payload.get("backend_session_id") is not None
                else None
            ),
            workspace_path=(
                str(payload["workspace_path"])
                if payload.get("workspace_path") is not None
                else None
            ),
            workspace_branch=(
                str(payload["workspace_branch"])
                if payload.get("workspace_branch") is not None
                else None
            ),
            baseline_commit=(
                str(payload["baseline_commit"])
                if payload.get("baseline_commit") is not None
                else None
            ),
            latest_summary=str(payload.get("latest_summary", "")),
            next_sequence=int(payload.get("next_sequence", 1)),
            updated_at=float(payload.get("updated_at", 0.0)),
        )


class EngineeringSessionStore:
    """Hikari-owned durable state shared by Resident and engineering worker."""

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()

    def create(self, state: EngineeringSessionState) -> EngineeringSessionState:
        session_dir = self._session_dir(state.session_id)
        if session_dir.exists():
            raise EngineeringProtocolError(f"engineering session already exists: {state.session_id}")
        (session_dir / "turns").mkdir(parents=True, exist_ok=False)
        (session_dir / "results").mkdir(parents=True, exist_ok=False)
        self.save(state)
        return state

    def load(self, session_id: str) -> EngineeringSessionState:
        path = self._state_path(session_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise EngineeringProtocolError(f"unknown engineering session: {session_id}") from None
        except (OSError, json.JSONDecodeError):
            raise EngineeringProtocolError(f"engineering session state is unreadable: {session_id}") from None
        if not isinstance(payload, Mapping):
            raise EngineeringProtocolError("engineering session state must be an object")
        return EngineeringSessionState.from_mapping(payload)

    def save(self, state: EngineeringSessionState) -> None:
        session_dir = self._session_dir(state.session_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        path = self._state_path(state.session_id)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(state.to_mapping(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, path)

    def list_states(self) -> list[EngineeringSessionState]:
        if not self.root.is_dir():
            return []
        states: list[EngineeringSessionState] = []
        for path in self.root.iterdir():
            if not path.is_dir():
                continue
            try:
                states.append(self.load(path.name))
            except EngineeringProtocolError:
                continue
        return sorted(states, key=lambda item: item.updated_at)

    def enqueue_turn(self, session_id: str, turn: EngineeringTurn) -> EngineeringSessionState:
        state = self.load(session_id)
        state.authorize(turn)
        if state.status in {"pending", "running"}:
            raise EngineeringProtocolError("engineering session already has an active turn")
        path = self._turn_path(session_id, turn.turn_id)
        _atomic_json(path, turn.to_mapping())
        updated = replace(
            state,
            status="pending",
            current_turn_id=turn.turn_id,
            latest_summary="等待 Engineering Worker",
            updated_at=time.time(),
        )
        self.save(updated)
        self.append_event(
            EngineeringEvent(
                session_id=session_id,
                turn_id=turn.turn_id,
                sequence=updated.next_sequence,
                kind="accepted",
                summary="工程请求已进入 Hikari Engineering Runtime",
                timestamp=time.time(),
            )
        )
        return self.load(session_id)

    def load_turn(self, session_id: str, turn_id: str) -> EngineeringTurn:
        path = self._turn_path(session_id, turn_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise EngineeringProtocolError(f"unknown engineering turn: {turn_id}") from None
        except (OSError, json.JSONDecodeError):
            raise EngineeringProtocolError(f"engineering turn is unreadable: {turn_id}") from None
        if not isinstance(payload, Mapping):
            raise EngineeringProtocolError("engineering turn must be an object")
        return EngineeringTurn.from_mapping(payload)

    def save_result(self, session_id: str, result: EngineeringResult) -> EngineeringSessionState:
        state = self.load(session_id)
        if state.current_turn_id != result.turn_id:
            raise EngineeringProtocolError("engineering result does not match the active turn")
        _atomic_json(self._result_path(session_id, result.turn_id), result.to_mapping())
        updated = replace(
            state,
            status=result.status,
            backend_session_id=result.backend_session_id or state.backend_session_id,
            latest_summary=result.message[:1000],
            updated_at=result.completed_at,
        )
        self.save(updated)
        return updated

    def load_result(self, session_id: str, turn_id: str) -> EngineeringResult:
        path = self._result_path(session_id, turn_id)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise EngineeringProtocolError(f"unknown engineering result: {turn_id}") from None
        except (OSError, json.JSONDecodeError):
            raise EngineeringProtocolError(f"engineering result is unreadable: {turn_id}") from None
        if not isinstance(payload, Mapping):
            raise EngineeringProtocolError("engineering result must be an object")
        return EngineeringResult.from_mapping(payload)

    def update_runtime(
        self,
        session_id: str,
        *,
        status: str | None = None,
        backend_session_id: str | None = None,
        workspace_path: str | None = None,
        workspace_branch: str | None = None,
        baseline_commit: str | None = None,
        latest_summary: str | None = None,
    ) -> EngineeringSessionState:
        state = self.load(session_id)
        updated = replace(
            state,
            status=status or state.status,
            backend_session_id=(
                backend_session_id if backend_session_id is not None else state.backend_session_id
            ),
            workspace_path=(workspace_path if workspace_path is not None else state.workspace_path),
            workspace_branch=(workspace_branch if workspace_branch is not None else state.workspace_branch),
            baseline_commit=(baseline_commit if baseline_commit is not None else state.baseline_commit),
            latest_summary=(latest_summary if latest_summary is not None else state.latest_summary),
            updated_at=time.time(),
        )
        self.save(updated)
        return updated

    def append_event(self, event: EngineeringEvent) -> EngineeringSessionState:
        state = self.load(event.session_id)
        if event.sequence != state.next_sequence:
            raise EngineeringProtocolError(
                f"engineering event sequence mismatch: expected {state.next_sequence}, got {event.sequence}"
            )
        path = self._events_path(event.session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(event.to_mapping(), ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")
        updated = replace(
            state,
            status=_status_for_event(event.kind, state.status),
            latest_summary=event.summary,
            next_sequence=state.next_sequence + 1,
            updated_at=event.timestamp,
        )
        self.save(updated)
        return updated

    def events(self, session_id: str, *, after_sequence: int = 0) -> list[EngineeringEvent]:
        self.load(session_id)
        path = self._events_path(session_id)
        if not path.exists():
            return []
        events: list[EngineeringEvent] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            raise EngineeringProtocolError(f"engineering event log is unreadable: {session_id}") from None
        for line in lines:
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                raise EngineeringProtocolError(f"engineering event log is malformed: {session_id}") from None
            if not isinstance(payload, Mapping):
                raise EngineeringProtocolError("engineering event must be an object")
            sequence = int(payload.get("sequence", 0))
            if sequence <= after_sequence:
                continue
            events.append(
                EngineeringEvent(
                    session_id=str(payload.get("session_id", "")),
                    turn_id=str(payload.get("turn_id", "")),
                    sequence=sequence,
                    kind=str(payload.get("kind", "")),
                    summary=str(payload.get("summary", "")),
                    timestamp=float(payload.get("timestamp", 0.0)),
                )
            )
        return events

    def _session_dir(self, session_id: str) -> Path:
        normalized = session_id.strip()
        if not normalized or any(
            ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
            for ch in normalized
        ):
            raise EngineeringProtocolError("engineering session_id contains unsupported characters")
        return self.root / normalized

    def _state_path(self, session_id: str) -> Path:
        return self._session_dir(session_id) / "state.json"

    def _events_path(self, session_id: str) -> Path:
        return self._session_dir(session_id) / "events.jsonl"

    def _turn_path(self, session_id: str, turn_id: str) -> Path:
        _validate_leaf_id(turn_id, "turn_id")
        return self._session_dir(session_id) / "turns" / f"{turn_id}.json"

    def _result_path(self, session_id: str, turn_id: str) -> Path:
        _validate_leaf_id(turn_id, "turn_id")
        return self._session_dir(session_id) / "results" / f"{turn_id}.json"


def _validate_leaf_id(value: str, label: str) -> None:
    normalized = value.strip()
    if not normalized or any(
        ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
        for ch in normalized
    ):
        raise EngineeringProtocolError(f"engineering {label} contains unsupported characters")


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(dict(payload), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _status_for_event(kind: str, current: str) -> str:
    if kind == "accepted":
        return "pending"
    if kind in {"started", "progress"}:
        return "running"
    if kind == "completed":
        return "completed"
    if kind == "failed":
        return "failed"
    if kind == "blocked":
        return "blocked"
    return current
