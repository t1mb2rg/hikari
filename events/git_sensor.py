"""Compatibility import for the Git sensor adapter.

New code should import from ``events.sensors.git`` or ``events.sensors``.
"""

from .sensors.git import GitSensor, GitSensorError

__all__ = ["GitSensor", "GitSensorError"]
