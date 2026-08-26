"""Hikari Core runtime lifecycle.

M0-02 keeps the runtime deliberately small: load identity, enter a running
state, remain alive until stopped, and shut down cleanly.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from core.identity import HikariIdentity, load_identity


def heartbeat() -> str:
    """Return the minimal liveness signal used by early M0 checks."""
    return "Hikari is awake."


@dataclass
class HikariRuntime:
    """Minimal lifecycle runtime for Hikari Core."""

    heartbeat_interval: float = 5.0
    sleeper: Callable[[float], None] = time.sleep
    identity: HikariIdentity | None = field(default=None, init=False)
    running: bool = field(default=False, init=False)

    def initialize(self) -> HikariIdentity:
        """Load the stable identity required by the runtime."""
        if self.identity is None:
            self.identity = load_identity()
        return self.identity

    def start(self) -> str:
        """Initialize Hikari and enter the running state."""
        identity = self.initialize()
        self.running = True
        return f"{identity.name} is awake."

    def stop(self) -> None:
        """Leave the running state without discarding loaded identity."""
        self.running = False

    def run_forever(self) -> None:
        """Remain alive until stopped or interrupted by the host process."""
        print(self.start())
        try:
            while self.running:
                self.sleeper(self.heartbeat_interval)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()
            print("Hikari is resting.")


def main() -> None:
    HikariRuntime().run_forever()


if __name__ == "__main__":
    main()
