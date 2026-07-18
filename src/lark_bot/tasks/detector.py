"""Backward-compatible task detection import path."""

from lark_bot.modules.task.task_detector import dedupe_tags, detect_output

__all__ = ["dedupe_tags", "detect_output"]
