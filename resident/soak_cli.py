from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from .paths import default_state_dir
from .windows_host import HostState, _default_process_probe
from .windows_process_tree import snapshot_windows_process_tree


ProcessProbe = Callable[[int], bool]
ProcessTreeResolver = Callable[[int], Sequence[int]]


@dataclass(frozen=True)
class SoakCheckpoint:
    """One read-only snapshot of Hikari's resident persistence and process state."""

    captured_at: str
    state_dir: str
    host_state_present: bool
    host_state_error: str | None
    resident_running: bool
    resident_pid: int | None
    host_started_at: str | None
    process_tree: tuple[int, ...]
    process_count: int
    process_error: str | None
    file_sizes: Mapping[str, int]
    delivery_states: Mapping[str, int]
    qq_spool_states: Mapping[str, int]
    user_model_states: Mapping[str, int]
    conversation_receipts: int | None
    presence_acceptance_count: int | None
    presence_last_accepted_at: float | None
    sqlite_errors: Mapping[str, str]

    def as_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["process_tree"] = list(self.process_tree)
        return payload


def _load_host_state(path: Path) -> tuple[HostState | None, str | None]:
    if not path.is_file():
        return None, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("host state must be a JSON object")
        return HostState.from_mapping(payload), None
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _interesting_file_sizes(root: Path) -> dict[str, int]:
    if not root.is_dir():
        return {}

    sizes: dict[str, int] = {}
    for path in sorted(root.iterdir(), key=lambda item: item.name.casefold()):
        if not path.is_file():
            continue
        name = path.name
        if (
            name == "host.json"
            or name.endswith(".log")
            or name.endswith(".db")
            or name.endswith(".db-wal")
            or name.endswith(".db-shm")
        ):
            try:
                sizes[name] = int(path.stat().st_size)
            except OSError:
                continue
    return sizes


def _query_rows(path: Path, query: str) -> list[tuple[object, ...]]:
    uri = path.expanduser().resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5)
    try:
        connection.execute("PRAGMA query_only = ON")
        rows = connection.execute(query).fetchall()
        return [tuple(row) for row in rows]
    finally:
        connection.close()


def _state_counts(
    path: Path,
    *,
    table: str,
    valid_states: Sequence[str],
    column: str = "state",
) -> tuple[dict[str, int], str | None]:
    counts = {state: 0 for state in valid_states}
    if not path.is_file():
        return counts, None
    try:
        rows = _query_rows(
            path,
            f"SELECT {column}, COUNT(*) FROM {table} GROUP BY {column}",
        )
    except sqlite3.Error as exc:
        return counts, f"{type(exc).__name__}: {exc}"

    for raw_state, raw_count in rows:
        state = str(raw_state)
        if state not in counts:
            counts[state] = 0
        counts[state] += int(raw_count)
    return counts, None


def _scalar_count(path: Path, query: str) -> tuple[int | None, str | None]:
    if not path.is_file():
        return None, None
    try:
        rows = _query_rows(path, query)
    except sqlite3.Error as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if not rows:
        return 0, None
    return int(rows[0][0]), None


def _presence_summary(path: Path) -> tuple[int | None, float | None, str | None]:
    if not path.is_file():
        return None, None, None
    try:
        acceptance_rows = _query_rows(
            path,
            "SELECT COUNT(*) FROM presence_acceptance",
        )
        meta_rows = _query_rows(
            path,
            "SELECT value FROM presence_meta WHERE key = 'last_accepted_at'",
        )
    except sqlite3.Error as exc:
        return None, None, f"{type(exc).__name__}: {exc}"

    acceptance_count = int(acceptance_rows[0][0]) if acceptance_rows else 0
    last_accepted_at = float(meta_rows[0][0]) if meta_rows else None
    return acceptance_count, last_accepted_at, None


def build_checkpoint(
    state_dir: str | Path,
    *,
    process_probe: ProcessProbe | None = None,
    process_tree_resolver: ProcessTreeResolver | None = None,
    now: datetime | None = None,
) -> SoakCheckpoint:
    """Build a checkpoint without mutating resident state or opening transports."""

    root = Path(state_dir).expanduser().resolve()
    state, host_state_error = _load_host_state(root / "host.json")

    resident_running = False
    process_tree: tuple[int, ...] = ()
    process_error: str | None = None
    resident_pid = state.pid if state is not None else None

    if state is not None:
        probe = process_probe or _default_process_probe
        resolver = process_tree_resolver or snapshot_windows_process_tree
        try:
            resident_running = bool(probe(state.pid))
            if resident_running:
                resolved = tuple(
                    int(pid)
                    for pid in resolver(state.pid)
                    if isinstance(pid, int) and not isinstance(pid, bool) and int(pid) > 0
                )
                if state.pid not in resolved:
                    resolved = (state.pid,) + resolved
                process_tree = tuple(dict.fromkeys(resolved))
        except Exception as exc:
            process_error = f"{type(exc).__name__}: {exc}"

    sqlite_errors: dict[str, str] = {}

    delivery_states, error = _state_counts(
        root / "proactive_delivery.db",
        table="proactive_delivery_outbox",
        valid_states=("pending", "sending", "sent", "uncertain"),
    )
    if error:
        sqlite_errors["proactive_delivery.db"] = error

    qq_spool_states, error = _state_counts(
        root / "qq_bridge.db",
        table="qq_bridge_spool",
        valid_states=("pending", "replied", "sent"),
    )
    if error:
        sqlite_errors["qq_bridge.db"] = error

    user_model_states, error = _state_counts(
        root / "user_model.db",
        table="user_facts",
        column="status",
        valid_states=("active", "superseded", "disputed"),
    )
    if error:
        sqlite_errors["user_model.db"] = error

    conversation_receipts, error = _scalar_count(
        root / "conversation_receipts.db",
        "SELECT COUNT(*) FROM conversation_receipts",
    )
    if error:
        sqlite_errors["conversation_receipts.db"] = error

    presence_count, presence_last, error = _presence_summary(root / "presence_policy.db")
    if error:
        sqlite_errors["presence_policy.db"] = error

    captured = now or datetime.now(timezone.utc)
    if captured.tzinfo is None:
        captured = captured.replace(tzinfo=timezone.utc)

    return SoakCheckpoint(
        captured_at=captured.astimezone(timezone.utc).isoformat(),
        state_dir=str(root),
        host_state_present=(root / "host.json").is_file(),
        host_state_error=host_state_error,
        resident_running=resident_running,
        resident_pid=resident_pid,
        host_started_at=state.started_at if state is not None else None,
        process_tree=process_tree,
        process_count=len(process_tree),
        process_error=process_error,
        file_sizes=_interesting_file_sizes(root),
        delivery_states=delivery_states,
        qq_spool_states=qq_spool_states,
        user_model_states=user_model_states,
        conversation_receipts=conversation_receipts,
        presence_acceptance_count=presence_count,
        presence_last_accepted_at=presence_last,
        sqlite_errors=sqlite_errors,
    )


def _print_text(checkpoint: SoakCheckpoint) -> None:
    print(f"captured_at：{checkpoint.captured_at}")
    print(f"state_dir：{checkpoint.state_dir}")
    print(f"host_state_present：{'true' if checkpoint.host_state_present else 'false'}")
    if checkpoint.host_state_error:
        print(f"host_state_error：{checkpoint.host_state_error}")
    print(f"resident_running：{'true' if checkpoint.resident_running else 'false'}")
    print(f"resident_pid：{checkpoint.resident_pid if checkpoint.resident_pid is not None else '-'}")
    print(f"process_count：{checkpoint.process_count}")
    print(
        "process_tree："
        + (",".join(str(pid) for pid in checkpoint.process_tree) if checkpoint.process_tree else "-")
    )
    if checkpoint.process_error:
        print(f"process_error：{checkpoint.process_error}")
    if checkpoint.host_started_at:
        print(f"host_started_at：{checkpoint.host_started_at}")

    delivery = ", ".join(
        f"{state}={count}" for state, count in sorted(checkpoint.delivery_states.items())
    )
    print(f"delivery_states：{delivery}")

    qq_spool = ", ".join(
        f"{state}={count}" for state, count in sorted(checkpoint.qq_spool_states.items())
    )
    print(f"qq_spool_states：{qq_spool}")
    user_model = ", ".join(
        f"{state}={count}"
        for state, count in sorted(checkpoint.user_model_states.items())
    )
    print(f"user_model_states：{user_model}")
    print(
        "conversation_receipts："
        f"{checkpoint.conversation_receipts if checkpoint.conversation_receipts is not None else '-'}"
    )
    print(
        "presence_acceptance_count："
        f"{checkpoint.presence_acceptance_count if checkpoint.presence_acceptance_count is not None else '-'}"
    )
    print(
        "presence_last_accepted_at："
        f"{checkpoint.presence_last_accepted_at if checkpoint.presence_last_accepted_at is not None else '-'}"
    )

    if checkpoint.file_sizes:
        print("files：")
        for name, size in sorted(checkpoint.file_sizes.items()):
            print(f"  {name}：{size}")
    else:
        print("files：-")

    if checkpoint.sqlite_errors:
        print("sqlite_errors：")
        for name, error in sorted(checkpoint.sqlite_errors.items()):
            print(f"  {name}：{error}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hikari-soak",
        description="M6-13 只读 soak checkpoint；不会启动、停止、重试或发送任何东西。",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    checkpoint = subparsers.add_parser(
        "checkpoint",
        help="读取 resident/process/SQLite/log 状态并输出一个可比较快照。",
    )
    checkpoint.add_argument("--state-dir", default=None)
    checkpoint.add_argument("--json", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    root = (
        Path(args.state_dir).expanduser().resolve()
        if args.state_dir
        else default_state_dir().expanduser().resolve()
    )
    checkpoint = build_checkpoint(root)
    if args.json:
        print(json.dumps(checkpoint.as_dict(), ensure_ascii=False, sort_keys=True))
    else:
        _print_text(checkpoint)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
