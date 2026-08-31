from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .session import (
    EngineeringAuthority,
    EngineeringSessionState,
    EngineeringSessionStore,
    EngineeringTurn,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage Hikari EngineeringSession state")
    parser.add_argument("--state-dir", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    submit = sub.add_parser("submit", help="create a read-only engineering session")
    submit.add_argument("intent")
    submit.add_argument("--repo", default=".")
    submit.add_argument("--project-id", default="hikari")
    submit.add_argument("--context", default="")

    follow = sub.add_parser("follow", help="append a read-only follow-up turn")
    follow.add_argument("session_id")
    follow.add_argument("intent")
    follow.add_argument("--context", default="")

    status = sub.add_parser("status", help="show durable engineering session state")
    status.add_argument("session_id")

    result = sub.add_parser("result", help="show the latest engineering result")
    result.add_argument("session_id")

    sub.add_parser("list", help="list engineering sessions")
    return parser


def _store(state_dir: str | None) -> EngineeringSessionStore:
    from resident.windows_host import default_state_dir

    root = Path(state_dir).expanduser().resolve() if state_dir else default_state_dir()
    return EngineeringSessionStore(root / "engineering")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = _store(args.state_dir)

    if args.command == "submit":
        authority = EngineeringAuthority.read_only()
        state = EngineeringSessionState.create(
            project_id=args.project_id,
            repository=args.repo,
            authority_ceiling=authority,
        )
        store.create(state)
        turn = EngineeringTurn.create(
            intent=args.intent,
            context=args.context,
            authority=authority,
        )
        store.enqueue_turn(state.session_id, turn)
        print(state.session_id)
        return 0

    if args.command == "follow":
        state = store.load(args.session_id)
        authority = EngineeringAuthority.read_only()
        turn = EngineeringTurn.create(
            intent=args.intent,
            context=args.context,
            authority=authority,
        )
        store.enqueue_turn(state.session_id, turn)
        print(turn.turn_id)
        return 0

    if args.command == "status":
        print(json.dumps(store.load(args.session_id).to_mapping(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "result":
        state = store.load(args.session_id)
        if not state.current_turn_id:
            print("{}")
            return 1
        result = store.load_result(state.session_id, state.current_turn_id)
        print(json.dumps(result.to_mapping(), ensure_ascii=False, indent=2))
        return 0

    if args.command == "list":
        payload = [state.to_mapping() for state in store.list_states()]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
