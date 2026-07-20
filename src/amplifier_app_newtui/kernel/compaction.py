"""Amplifier-native context compaction configuration.

The context module still owns compaction.  newtui only applies validated
settings to the effective bundle mount plan and exposes the resulting policy
to its context/status presentation.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from inspect import isawaitable
from typing import Any, Literal

logger = logging.getLogger(__name__)

DEFAULT_CONTEXT_WINDOW = 200_000
_SETTING_KEYS = ("max_tokens", "compact_threshold", "auto_compact")
AccountingMode = Literal["provider-observed", "estimated"]


@dataclass(frozen=True)
class CompactionConfig:
    """Effective context window and automatic-compaction posture."""

    max_tokens: int = DEFAULT_CONTEXT_WINDOW
    auto_compact: bool | None = None
    compact_threshold: float | None = None
    accounting: AccountingMode = "estimated"

    @property
    def threshold_tokens(self) -> int | None:
        if self.compact_threshold is None:
            return None
        return round(self.max_tokens * self.compact_threshold)


class CompactionRuntimeBinding:
    """Bind NewTUI's effective policy to the context that actually mounted.

    Older ``context-simple`` releases accepted ``auto_compact`` in bundle
    configuration without consuming it. Contexts with an ``auto_compact``
    attribute receive the flag directly; older threshold-only contexts use
    an infinite internal threshold when automatic compaction is disabled.
    Future contexts can consume exact provider usage through one of the
    observer methods detected here.
    """

    _OBSERVERS = ("record_observed_input_tokens", "set_observed_input_tokens")

    def __init__(self, context: Any, config: CompactionConfig) -> None:
        self._context = context
        self._observer = next(
            (
                candidate
                for name in self._OBSERVERS
                if callable(candidate := getattr(context, name, None))
            ),
            None,
        )
        self.config = CompactionConfig(
            max_tokens=config.max_tokens,
            auto_compact=config.auto_compact,
            compact_threshold=config.compact_threshold,
            accounting=(
                "provider-observed" if self._observer is not None else "estimated"
            ),
        )

    def apply(self) -> CompactionConfig:
        auto_compact = self.config.auto_compact
        threshold = self.config.compact_threshold
        if hasattr(self._context, "max_tokens"):
            self._context.max_tokens = self.config.max_tokens
        if auto_compact is not None and hasattr(self._context, "auto_compact"):
            self._context.auto_compact = auto_compact
        if hasattr(self._context, "compact_threshold"):
            if auto_compact is False and not hasattr(self._context, "auto_compact"):
                self._context.compact_threshold = float("inf")
            elif threshold is not None:
                self._context.compact_threshold = threshold
        return self.config

    async def observe_input_tokens(self, input_tokens: int) -> bool:
        """Feed an exact server-observed input count when supported."""
        if self._observer is None or input_tokens <= 0:
            return False
        try:
            result = self._observer(input_tokens)
            if isawaitable(result):
                await result
            return True
        except Exception:  # noqa: BLE001 - optional telemetry must not break a turn
            logger.warning("context rejected provider-observed token usage", exc_info=True)
            return False


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
    "AccountingMode",
    "DEFAULT_CONTEXT_WINDOW",
    "CompactionConfig",
    "CompactionRuntimeBinding",
    "apply_compaction_settings",
    "compaction_config",
]
