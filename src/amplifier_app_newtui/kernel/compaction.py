"""Amplifier-native context compaction configuration.

The context module still owns compaction.  newtui only applies validated
settings to the effective bundle mount plan and exposes the resulting policy
to its context/status presentation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_CONTEXT_WINDOW = 200_000
_SETTING_KEYS = ("max_tokens", "compact_threshold", "auto_compact")


@dataclass(frozen=True)
class CompactionConfig:
    """Effective context window and automatic-compaction posture."""

    max_tokens: int = DEFAULT_CONTEXT_WINDOW
    auto_compact: bool | None = None
    compact_threshold: float | None = None

    @property
    def threshold_tokens(self) -> int | None:
        if self.compact_threshold is None:
            return None
        return round(self.max_tokens * self.compact_threshold)


def _context_config(mount_plan: dict[str, Any]) -> dict[str, Any] | None:
    context = mount_plan.get("context")
    if not isinstance(context, dict):
        return None
    config = context.get("config")
    if not isinstance(config, dict):
        config = {}
        context["config"] = config
    return config


def _valid_value(key: str, value: object) -> bool:
    if key == "auto_compact":
        return isinstance(value, bool)
    if key == "max_tokens":
        return isinstance(value, int) and not isinstance(value, bool) and value > 0
    if key == "compact_threshold":
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and 0 < float(value) <= 1
        )
    return False


def apply_compaction_settings(
    mount_plan: dict[str, Any], settings: dict[str, Any]
) -> dict[str, Any]:
    """Apply ``settings.context`` onto the mounted context config in place.

    Only the three stable context-simple knobs are accepted.  Invalid values
    are skipped with a warning, preserving the repo's rule that settings must
    never prevent startup.  A bundle without a context module is left alone.
    """

    requested = settings.get("context")
    if not isinstance(requested, dict) or not any(
        key in requested for key in _SETTING_KEYS
    ):
        return mount_plan
    context = mount_plan.get("context")
    if isinstance(context, dict) and context.get("module") != "context-simple":
        logger.warning(
            "Ignoring context settings: module %r is not context-simple",
            context.get("module"),
        )
        return mount_plan
    config = _context_config(mount_plan)
    if config is None:
        logger.warning("Ignoring context settings: the active bundle mounts no context")
        return mount_plan
    for key in _SETTING_KEYS:
        if key not in requested:
            continue
        value = requested[key]
        if _valid_value(key, value):
            config[key] = value
        else:
            logger.warning("Ignoring invalid context.%s setting: %r", key, value)
    return mount_plan


def compaction_config(mount_plan: dict[str, Any]) -> CompactionConfig:
    """Read the effective compaction posture without mutating the plan."""

    context = mount_plan.get("context")
    raw = context.get("config") if isinstance(context, dict) else None
    config = raw if isinstance(raw, dict) else {}

    max_tokens = config.get("max_tokens", DEFAULT_CONTEXT_WINDOW)
    if not _valid_value("max_tokens", max_tokens):
        max_tokens = DEFAULT_CONTEXT_WINDOW
    auto_compact = config.get("auto_compact")
    if not _valid_value("auto_compact", auto_compact):
        auto_compact = None
    compact_threshold = config.get("compact_threshold")
    if not _valid_value("compact_threshold", compact_threshold):
        compact_threshold = None
    return CompactionConfig(
        max_tokens=int(max_tokens),
        auto_compact=auto_compact,
        compact_threshold=(
            float(compact_threshold) if compact_threshold is not None else None
        ),
    )


__all__ = [
    "DEFAULT_CONTEXT_WINDOW",
    "CompactionConfig",
    "apply_compaction_settings",
    "compaction_config",
]
