from __future__ import annotations

import os

from actions import (
    ActionAuthorizationPolicy,
    ActionExecutor,
    ActionProposal,
    ActionRisk,
    WindowsToastNotifyAdapter,
)


def main() -> None:
    if os.name != "nt":
        raise SystemExit("This physical gate must be run on Windows.")

    proposal = ActionProposal(
        action_name="notify_user",
        arguments={
            "text": "光现在真的能从终端外找到你了。",
        },
        effect="show one native Windows desktop notification",
        reason="M5-04 physical gate",
        confidence=0.99,
        risk=ActionRisk.PASSIVE,
        requires_confirmation=False,
    )

    authorization = ActionAuthorizationPolicy().authorize(proposal)
    action = authorization.authorized_action
    if action is None:
        raise RuntimeError(f"notification was not authorized: {authorization.reason}")

    result = ActionExecutor([WindowsToastNotifyAdapter()]).execute(action)
    print(f"[execution] {result.action_name}: {result.summary}")
    print("If a Hikari toast appeared in Windows, M5-04 Physical Gate PASS.")


if __name__ == "__main__":
    main()
