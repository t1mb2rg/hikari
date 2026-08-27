from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable

from .contract import ActionProposal, ActionRisk


class AuthorizationDecision(StrEnum):
    """Trusted outcome between an action proposal and any future executor."""

    AUTHORIZE = "authorize"
    REQUIRE_CONFIRMATION = "require_confirmation"
    DENY = "deny"


class AuthorizedAction:
    """Immutable grant produced only by the authorization boundary.

    Callers should never construct this directly. The private factory is used by
    ActionAuthorizationPolicy after all deterministic checks have passed.
    """

    __slots__ = ("_proposal", "_sealed")

    def __init__(self, proposal: ActionProposal, *, _seal: object | None = None) -> None:
        if _seal is not _AUTHORIZATION_SEAL:
            raise TypeError("AuthorizedAction can only be created by authorization policy")
        object.__setattr__(self, "_proposal", proposal)
        object.__setattr__(self, "_sealed", True)

    def __setattr__(self, name: str, value: object) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError("AuthorizedAction is immutable")
        object.__setattr__(self, name, value)

    @property
    def proposal(self) -> ActionProposal:
        return self._proposal

    @classmethod
    def _from_policy(cls, proposal: ActionProposal) -> "AuthorizedAction":
        return cls(proposal, _seal=_AUTHORIZATION_SEAL)


_AUTHORIZATION_SEAL = object()


@dataclass(frozen=True)
class AuthorizationResult:
    decision: AuthorizationDecision
    proposal: ActionProposal
    reason: str
    authorized_action: AuthorizedAction | None = None

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError("authorization reason must not be empty")
        if self.decision is AuthorizationDecision.AUTHORIZE:
            if self.authorized_action is None:
                raise ValueError("authorized decision requires AuthorizedAction")
            if self.authorized_action.proposal is not self.proposal:
                raise ValueError("AuthorizedAction must wrap the reviewed proposal")
        elif self.authorized_action is not None:
            raise ValueError("non-authorized decision cannot carry AuthorizedAction")


class ActionAuthorizationPolicy:
    """Side-effect-free trusted policy from proposal to permission.

    The model never participates here. Risk and confirmation requirements have
    already been bound to the trusted ActionSpec by ModelActionPlanner.
    """

    def __init__(
        self,
        *,
        min_confidence: float = 0.75,
        denied_actions: Iterable[str] = (),
    ) -> None:
        threshold = float(min_confidence)
        if not 0.0 <= threshold <= 1.0:
            raise ValueError("min_confidence must be between 0.0 and 1.0")

        denied: set[str] = set()
        for action_name in denied_actions:
            normalized = str(action_name).strip()
            if not normalized:
                raise ValueError("denied action names must not be empty")
            denied.add(normalized)

        self.min_confidence = threshold
        self.denied_actions = frozenset(denied)

    def authorize(self, proposal: ActionProposal) -> AuthorizationResult:
        if proposal.action_name in self.denied_actions:
            return AuthorizationResult(
                decision=AuthorizationDecision.DENY,
                proposal=proposal,
                reason=f"action {proposal.action_name!r} is explicitly denied by policy",
            )

        # Trusted confirmation requirements always win over model confidence.
        if proposal.requires_confirmation or proposal.risk is not ActionRisk.PASSIVE:
            return AuthorizationResult(
                decision=AuthorizationDecision.REQUIRE_CONFIRMATION,
                proposal=proposal,
                reason=(
                    f"{proposal.risk.value} action requires external confirmation "
                    "before execution"
                ),
            )

        if proposal.confidence < self.min_confidence:
            return AuthorizationResult(
                decision=AuthorizationDecision.DENY,
                proposal=proposal,
                reason=(
                    f"proposal confidence {proposal.confidence:.2f} is below "
                    f"authorization threshold {self.min_confidence:.2f}"
                ),
            )

        authorized = AuthorizedAction._from_policy(proposal)
        return AuthorizationResult(
            decision=AuthorizationDecision.AUTHORIZE,
            proposal=proposal,
            reason=(
                f"passive action passed deterministic authorization at confidence "
                f"{proposal.confidence:.2f}"
            ),
            authorized_action=authorized,
        )
