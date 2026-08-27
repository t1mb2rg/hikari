from __future__ import annotations

from pathlib import Path

from actions import (
    ActionAuthorizationPolicy,
    ActionExecutor,
    ActionProposal,
    ActionRisk,
    AuthorizationDecision,
    CreateLocalNoteAdapter,
)


TARGET = Path("m5-05-confirmed-local-action.txt")
TEXT = "光第一次经过你的明确确认，修改了本机状态。"


def main() -> None:
    proposal = ActionProposal(
        action_name="create_local_note",
        arguments={"text": TEXT},
        effect=f"create one new local note at {TARGET.resolve()}",
        reason="M5-05 physical gate for confirmed reversible local action",
        confidence=0.99,
        risk=ActionRisk.REVERSIBLE,
        requires_confirmation=True,
    )

    policy = ActionAuthorizationPolicy()
    initial = policy.authorize(proposal)
    if initial.decision is not AuthorizationDecision.REQUIRE_CONFIRMATION:
        raise RuntimeError("gate expected the local write to require confirmation")

    print("Hikari proposes one reversible local action:")
    print(f"  effect: {proposal.effect}")
    print(f"  text:   {TEXT}")
    print("No file has been created yet.")
    answer = input("Type yes to approve exactly this action: ").strip().lower()

    confirmation = policy.confirm(proposal, approved=answer == "yes")
    if confirmation.authorized_action is None:
        print(f"[authorization] {confirmation.decision.value}: {confirmation.reason}")
        return

    result = ActionExecutor([CreateLocalNoteAdapter(TARGET)]).execute(
        confirmation.authorized_action
    )
    print(f"[execution] {result.action_name}: {result.summary}")
    print(f"[verify] {TARGET.resolve()}")
    print(TARGET.read_text(encoding="utf-8").rstrip())


if __name__ == "__main__":
    main()
