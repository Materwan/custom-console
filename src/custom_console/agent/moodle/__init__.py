"""Moodle access for the agent (Playwright is imported lazily, only when used)."""

from .runner import MoodleRunner

__all__ = ["MoodleRunner"]
