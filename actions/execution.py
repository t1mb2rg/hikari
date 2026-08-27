from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Protocol, runtime_checkable

from .authorization import AuthorizedAction


class ActionExecutionError(RuntimeError):
    """Raised when an authorized action cannot be safely dispatched."""


@dataclass(frozen=True)
class ExecutionResult:
    action_name: str
    success: bool
    summary: str

    def __post_init__(self) -> None:
        if not self.action_name.strip():
            raise ValueError("execution result action_name must not be empty")
        if not isinstance(self.success, bool):
            raise TypeError("execution result success must be a bool")
        if not self.summary.strip():
            raise ValueError("execution result summary must not be empty")


@runtime_checkable
class ActionAdapter(Protocol):
    """One explicit execution capability behind the authorization boundary."""

    @property
    def action_name(self) -> str:
        ...

    def execute(self, action: AuthorizedAction) -> ExecutionResult:
        ...


class ActionExecutor:
    """Trusted dispatcher from AuthorizedAction to an explicit adapter registry."""

    def __init__(self, adapters: Iterable[ActionAdapter]) -> None:
        indexed: dict[str, ActionAdapter] = {}
        for adapter in adapters:
            name = str(adapter.action_name).strip()
            if not name:
                raise ValueError("adapter action_name must not be empty")
            if name in indexed:
                raise ValueError(f"duplicate action adapter: {name}")
            indexed[name] = adapter
        if not indexed:
            raise ValueError("action executor requires at least one adapter")
        self._adapters = indexed

    def execute(self, action: AuthorizedAction) -> ExecutionResult:
        if not isinstance(action, AuthorizedAction):
            raise TypeError("ActionExecutor accepts only AuthorizedAction")

        proposal = action.proposal
        adapter = self._adapters.get(proposal.action_name)
        if adapter is None:
            raise ActionExecutionError(
                f"no registered adapter for action {proposal.action_name!r}"
            )

        result = adapter.execute(action)
        if result.action_name != proposal.action_name:
            raise ActionExecutionError(
                "adapter returned an ExecutionResult for a different action"
            )
        return result


class ConsoleNotifyAdapter:
    """First concrete M5 side effect: one validated console notification."""

    action_name = "notify_user"

    def execute(self, action: AuthorizedAction) -> ExecutionResult:
        if not isinstance(action, AuthorizedAction):
            raise TypeError("ConsoleNotifyAdapter accepts only AuthorizedAction")

        proposal = action.proposal
        if proposal.action_name != self.action_name:
            raise ActionExecutionError(
                f"ConsoleNotifyAdapter cannot execute {proposal.action_name!r}"
            )

        arguments = proposal.arguments
        if set(arguments) != {"text"}:
            raise ActionExecutionError("notify_user requires exactly one `text` argument")
        text = arguments.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ActionExecutionError("notify_user `text` must be a non-empty string")

        message = text.strip()
        print(message)
        return ExecutionResult(
            action_name=self.action_name,
            success=True,
            summary="console notification delivered",
        )
