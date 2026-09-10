from __future__ import annotations

from pathlib import Path
import time

from resident.telemetry import record_observation


class ObservedChatProvider:
    """Observe actual provider calls without persisting prompts, replies or secrets."""

    def __init__(self, provider, state_dir: Path, *, model: str):
        self.provider = provider
        self.state_dir = Path(state_dir)
        self.model = model
        self.last_success_at = None

    def complete(self, messages):
        return self._observe_call(self.provider.complete, messages)

    def complete_json(self, messages):
        return self._observe_call(getattr(self.provider, "complete_json", self.provider.complete), messages)

    def _observe_call(self, method, messages):
        started = time.monotonic()
        record_observation(self.state_dir, "model", "running", model=self.model,
                           last_success_at=self.last_success_at)
        try:
            result = method(messages)
        except Exception as exc:
            record_observation(self.state_dir, "model", "error", model=self.model,
                               error_type=type(exc).__name__, last_success_at=self.last_success_at,
                               duration_ms=round((time.monotonic() - started) * 1000))
            raise
        self.last_success_at = time.time()
        record_observation(self.state_dir, "model", "healthy", model=self.model,
                           last_success_at=self.last_success_at,
                           duration_ms=round((time.monotonic() - started) * 1000))
        return result
