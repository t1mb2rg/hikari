from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from actions import (
    ActionAuthorizationPolicy,
    ActionCatalog,
    ActionExecutor,
    AuthorizationDecision,
    ForgeProjectProfile,
    ForgeProjectRegistry,
    ForgeTaskAdapter,
    ModelActionPlanner,
    forge_task_action_spec,
)
from attention import AttentionDecision
from brain import ChatProvider
from brain.providers import OpenAICompatibleProvider
from events import Event

REPOSITORY = Path(__file__).resolve().parents[1]

# One concrete piece of repository maintenance that Hikari's maintenance
# review has identified and that is explicitly required to complete. The live
# model only decides whether any available action is appropriate for it; it
# never authors or controls the execution of that action.
EVENT = Event(
    event_type="hikari.maintenance",
    source="maintenance-review",
    content=(
        "A maintenance review of the Hikari repository identified one "
        "specific missing module docstring: events/models.py, the smallest "
        "module in the events package lacking one. Completing that repository "
        "change is required. Runtime behavior must remain unchanged, and the "
        "change must stay narrowly scoped to that single docstring. The "
        "repository test suite must pass afterward. Determine whether any "
        "available action is appropriate for completing this requirement."
    ),
    context={"project": "hikari"},
)

DECISION = AttentionDecision(
    should_intervene=True,
    importance=0.95,
    reason="explicit live-model physical gate",
)


def _ask_yes() -> bool:
    answer = input("Type yes to approve exactly this action: ").strip().lower()
    return answer == "yes"


def run_gate(
    provider: ChatProvider,
    *,
    confirm: Callable[[], bool] = _ask_yes,
    runner: Callable[[list[str]], int] | None = None,
    work_dir: str | Path | None = None,
) -> int:
    """Run one live-model plan -> human confirmation -> Forge dispatch cycle.

    The proposal is produced only by ModelActionPlanner parsing the real model
    response; this gate never authors a proposal itself. Forge is invoked only
    after explicit human confirmation, through the trusted registry-backed
    adapter, always without a shell.
    """

    registry = ForgeProjectRegistry(
        [
            ForgeProjectProfile(
                project_id="hikari",
                repository=REPOSITORY,
                verification=["python -m pytest -q"],
                executable="forge",
                backend="claude",
                max_attempts=3,
                claude_permission_mode="auto",
                claude_max_turns=30,
            )
        ]
    )
    planner = ModelActionPlanner(provider, ActionCatalog([forge_task_action_spec()]))
    proposal = planner.plan(EVENT, DECISION)

    if proposal is None:
        print("The model proposed no action. Exiting cleanly without touching Forge.")
        return 0

    arguments = proposal.arguments
    print("Hikari's live model proposes one reversible Forge engineering task:")
    print(f"  action:      {proposal.action_name}")
    print(f"  project_id:  {arguments['project_id']}")
    print(f"  goal:        {arguments['goal']}")
    print(f"  constraints: {arguments['constraints']}")
    print(f"  acceptance:  {arguments['acceptance']}")
    print(f"  effect:      {proposal.effect}")
    print(f"  reason:      {proposal.reason}")
    print(f"  confidence:  {proposal.confidence:.2f}")
    print(f"  risk:        {proposal.risk.value}")

    policy = ActionAuthorizationPolicy()
    initial = policy.authorize(proposal)
    if initial.decision is not AuthorizationDecision.REQUIRE_CONFIRMATION:
        raise RuntimeError("gate expected the Forge dispatch to require confirmation")

    print("No Forge process has started yet.")

    confirmation = policy.confirm(proposal, approved=confirm())
    if confirmation.authorized_action is None:
        print(f"[authorization] {confirmation.decision.value}: {confirmation.reason}")
        return 0

    try:
        result = ActionExecutor(
            [ForgeTaskAdapter(registry, work_dir=work_dir, runner=runner)]
        ).execute(confirmation.authorized_action)
    except Exception as exc:
        print(f"[execution] dispatch failed: {exc}")
        return 1
    print(f"[execution] {result.action_name}: {result.summary}")
    return 0


def main() -> None:
    base_url = os.environ.get("HIKARI_MODEL_BASE_URL")
    model = os.environ.get("HIKARI_MODEL_NAME")
    api_key = os.environ.get("HIKARI_MODEL_API_KEY")

    if not base_url or not model:
        raise SystemExit(
            "Set HIKARI_MODEL_BASE_URL and HIKARI_MODEL_NAME before running the gate."
        )

    provider = OpenAICompatibleProvider(base_url=base_url, model=model, api_key=api_key)
    raise SystemExit(run_gate(provider))


if __name__ == "__main__":
    main()
