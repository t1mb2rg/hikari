from __future__ import annotations

from pathlib import Path

from actions import (
    ActionAuthorizationPolicy,
    ActionExecutor,
    ActionProposal,
    ActionRisk,
    AuthorizationDecision,
    ForgeProjectProfile,
    ForgeProjectRegistry,
    ForgeTaskAdapter,
)

REPOSITORY = Path(__file__).resolve().parents[1]
GOAL = (
    "Add one missing docstring to the smallest module in the hikari package "
    "without changing any behavior."
)


def main() -> None:
    registry = ForgeProjectRegistry(
        [
            ForgeProjectProfile(
                project_id="hikari",
                repository=REPOSITORY,
                verification=["python -m pytest -q"],
                executable="forge",
                backend="claude",
                max_attempts=3,
            )
        ]
    )
    proposal = ActionProposal(
        action_name="run_forge_task",
        arguments={
            "project_id": "hikari",
            "goal": GOAL,
            "constraints": ["Do not change public behavior."],
            "acceptance": ["The full hikari test suite passes."],
        },
        effect=f"dispatch one bounded engineering task to Forge for {REPOSITORY}",
        reason="M5-06 physical gate for the confirmed trusted Forge action",
        confidence=0.99,
        risk=ActionRisk.REVERSIBLE,
        requires_confirmation=True,
    )

    policy = ActionAuthorizationPolicy()
    initial = policy.authorize(proposal)
    if initial.decision is not AuthorizationDecision.REQUIRE_CONFIRMATION:
        raise RuntimeError("gate expected the Forge dispatch to require confirmation")

    print("Hikari proposes one reversible Forge engineering task:")
    print(f"  project: {proposal.arguments['project_id']}")
    print(f"  goal:    {proposal.arguments['goal']}")
    print("No Forge process has started yet.")
    answer = input("Type yes to approve exactly this action: ").strip().lower()

    confirmation = policy.confirm(proposal, approved=answer == "yes")
    if confirmation.authorized_action is None:
        print(f"[authorization] {confirmation.decision.value}: {confirmation.reason}")
        return

    try:
        result = ActionExecutor([ForgeTaskAdapter(registry)]).execute(
            confirmation.authorized_action
        )
    except Exception as exc:
        print(f"[execution] dispatch failed: {exc}")
        return
    print(f"[execution] {result.action_name}: {result.summary}")


if __name__ == "__main__":
    main()
