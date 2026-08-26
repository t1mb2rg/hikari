"""Concrete sensor adapters for Hikari."""

from .git import GitSensor, GitSensorError

__all__ = ["GitSensor", "GitSensorError"]
