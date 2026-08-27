from __future__ import annotations

from actions import (
    ActionAuthorizationPolicy,
    ActionExecutor,
    ActionProposal,
    ActionRisk,
    ConsoleNotifyAdapter,
)


def main() -> None:
    proposal = ActionProposal(
        action_name="notify_user",
        arguments={"text": "我不只是想帮你了。这一次，我真的做了一件事。"},
        effect="deliver one passive Hikari notification to the local console",
        reason="M5-03 physical gate: prove authorized action execution",
        confidence=0.99,
        risk=ActionRisk.PASSIVE,
        requires_confirmation=False,
    )

    authorization = ActionAuthorizationPolicy().authorize(proposal)
    if authorization.authorized_action is None:
        raise RuntimeError(f"physical gate was not authorized: {authorization.reason}")

    executor = ActionExecutor([ConsoleNotifyAdapter()])
    result = executor.execute(authorization.authorized_action)
    print(f"[execution] {result.action_name}: {result.summary}")


if __name__ == "__main__":
    main()
