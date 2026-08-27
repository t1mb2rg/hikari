from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Callable, Iterable, Protocol, runtime_checkable

from .authorization import AuthorizedAction


class ActionExecutionError(RuntimeError):
    """Raised when an authorized action cannot be safely dispatched."""


class WindowsNotificationUnavailable(ActionExecutionError):
    """Raised when the native Windows notification transport is unavailable."""


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


def _validated_notify_text(action: AuthorizedAction, *, adapter_name: str) -> str:
    if not isinstance(action, AuthorizedAction):
        raise TypeError(f"{adapter_name} accepts only AuthorizedAction")

    proposal = action.proposal
    if proposal.action_name != "notify_user":
        raise ActionExecutionError(
            f"{adapter_name} cannot execute {proposal.action_name!r}"
        )

    arguments = proposal.arguments
    if set(arguments) != {"text"}:
        raise ActionExecutionError("notify_user requires exactly one `text` argument")
    text = arguments.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ActionExecutionError("notify_user `text` must be a non-empty string")
    return text.strip()


class ConsoleNotifyAdapter:
    """Dependency-free notification adapter for tests and fallback use."""

    action_name = "notify_user"

    def execute(self, action: AuthorizedAction) -> ExecutionResult:
        message = _validated_notify_text(action, adapter_name="ConsoleNotifyAdapter")
        print(message)
        return ExecutionResult(
            action_name=self.action_name,
            success=True,
            summary="console notification delivered",
        )


ToastSender = Callable[[str, str], None]


class WindowsToastNotifyAdapter:
    """Native Windows toast leaf adapter for the existing notify_user action."""

    action_name = "notify_user"

    def __init__(
        self,
        *,
        app_name: str = "Hikari",
        sender: ToastSender | None = None,
    ) -> None:
        normalized = app_name.strip()
        if not normalized:
            raise ValueError("Windows toast app_name must not be empty")
        self.app_name = normalized
        self._sender = sender or _send_windows_toast

    def execute(self, action: AuthorizedAction) -> ExecutionResult:
        message = _validated_notify_text(action, adapter_name="WindowsToastNotifyAdapter")
        self._sender(self.app_name, message)
        return ExecutionResult(
            action_name=self.action_name,
            success=True,
            summary="Windows toast notification delivered",
        )


def _send_windows_toast(app_name: str, message: str) -> None:
    if os.name != "nt":
        raise WindowsNotificationUnavailable(
            "Windows toast notifications are only available on Windows"
        )

    try:
        from windows_toasts import Toast, WindowsToaster
    except ImportError as exc:
        raise WindowsNotificationUnavailable(
            'Windows notification support is not installed; run '
            'python -m pip install -e ".[windows-notify]"'
        ) from exc

    try:
        toaster = WindowsToaster(app_name)
        toast = Toast()
        toast.text_fields = [app_name, message]
        toaster.show_toast(toast)
    except Exception as exc:
        raise WindowsNotificationUnavailable(
            f"Windows toast transport failed: {exc}"
        ) from exc


class CreateLocalNoteAdapter:
    """Create exactly one caller-chosen local note without exposing a path tool.

    The model supplies only note text. The target path is fixed by trusted caller
    code when the adapter is created, and existing files are never overwritten.
    """

    action_name = "create_local_note"

    def __init__(self, target_path: str | Path) -> None:
        path = Path(target_path)
        if not path.name:
            raise ValueError("local note target_path must identify a file")
        self.target_path = path

    def execute(self, action: AuthorizedAction) -> ExecutionResult:
        if not isinstance(action, AuthorizedAction):
            raise TypeError("CreateLocalNoteAdapter accepts only AuthorizedAction")

        proposal = action.proposal
        if proposal.action_name != self.action_name:
            raise ActionExecutionError(
                f"CreateLocalNoteAdapter cannot execute {proposal.action_name!r}"
            )

        arguments = proposal.arguments
        if set(arguments) != {"text"}:
            raise ActionExecutionError(
                "create_local_note requires exactly one `text` argument"
            )
        text = arguments.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ActionExecutionError(
                "create_local_note `text` must be a non-empty string"
            )
        if self.target_path.exists():
            raise ActionExecutionError(
                f"local note target already exists: {self.target_path}"
            )

        parent = self.target_path.parent
        if not parent.exists() or not parent.is_dir():
            raise ActionExecutionError(
                f"local note parent directory does not exist: {parent}"
            )

        message = text.strip()
        try:
            with self.target_path.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(message)
                handle.write("\n")
        except FileExistsError as exc:
            raise ActionExecutionError(
                f"local note target already exists: {self.target_path}"
            ) from exc
        except OSError as exc:
            raise ActionExecutionError(f"local note write failed: {exc}") from exc

        return ExecutionResult(
            action_name=self.action_name,
            success=True,
            summary=f"local note created at {self.target_path}",
        )
