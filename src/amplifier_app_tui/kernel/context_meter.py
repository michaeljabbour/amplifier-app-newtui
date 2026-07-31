"""Live context/cost telemetry for the ``serve`` protocol (the HUD meter row).

Backend counterpart of the opencode sidebar-context panel: it packages the three
numbers a context/cost HUD renders — context **tokens used**, **% of the context
window**, and running **$ spent** — into ONE ``context.state`` snapshot a protocol
client can consume without re-deriving them from the raw usage stream.

Every number comes from a source the in-process TUI already trusts; none is
fabricated (mirrors ``.ai/oc_donor.md``):

- **tokens (context occupancy)** — the MOST RECENT
  :class:`~amplifier_app_tui.kernel.events.ProviderResponseUsage` event's
  summed tokens (``input + output + cache_read + cache_write``). This is the donor
  panel's ``last.tokens.*`` sum: a snapshot of how full the context is *after the
  latest provider response*, NOT a session-wide accumulation.
- **cost** — a :class:`~amplifier_app_tui.kernel.cost.CostTracker` over the same
  usage events, i.e. the exact pricing math the footer/reducer use. Its
  ``session_cost`` already includes any resume-seeded prior spend and its
  ``unpriced`` count drives the ``~$`` floor marker (never lie in the footer).
- **window (the ``%`` denominator)** — supplied by the caller from
  ``compaction.max_tokens`` (the app's effective context window — the same source
  ``ui/app.context_usage()`` and the ``/context`` command use for ``NN% of 200k``).
  This app has NO provider per-model context-limit registry, so when no window is
  known the percentage is ``null`` — never guessed (donor
  ``model.limit.context ? … : null`` parity).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .cost import CostTracker
from .events import ProviderResponseUsage

CONTEXT_STATE_TYPE = "context.state"
WINDOW_SOURCE_COMPACTION = "compaction"


@dataclass
class ContextMeter:
    """Fold provider usage into a renderable context/cost snapshot.

    Feed every :class:`ProviderResponseUsage` to :meth:`record` (typically from
    the ``serve`` event pump); call :meth:`snapshot` to package the current
    ``context.state`` record. The tracker is reused from the live runtime when one
    exists, so the running ``$`` inherits the resume-seeded session total.
    """

    cost: CostTracker = field(default_factory=CostTracker)
    _last: ProviderResponseUsage | None = None

    def record(self, usage: ProviderResponseUsage) -> None:
        """Accumulate one provider response into cost + last-usage occupancy."""
        self.cost.record(usage)
        self._last = usage

    @property
    def last_usage(self) -> ProviderResponseUsage | None:
        return self._last

    @property
    def context_tokens(self) -> int | None:
        """Context occupancy = the latest response's summed tokens (or ``None``).

        Matches the donor's ``last.tokens.input + output + reasoning +
        cache.read + cache.write``; our normalized event folds reasoning into
        ``output_tokens``.
        """
        usage = self._last
        if usage is None:
            return None
        return usage.input_tokens + usage.output_tokens + usage.cache_read + usage.cache_write

    def snapshot(
        self,
        *,
        session_id: str,
        model: str,
        window: int | None,
        window_source: str = WINDOW_SOURCE_COMPACTION,
    ) -> dict[str, object]:
        """Build the ``context.state`` record (a json-safe dict).

        ``window`` is the context-window size (``compaction.max_tokens``); a
        non-positive or ``None`` window yields ``context_window``/``window_source``/
        ``context_pct`` all ``null`` — the honest "window unknowable" case. The
        percentage is whole-number ``round(tokens / window * 100)``, computed only
        when both a token figure and a positive window exist.
        """
        tokens = self.context_tokens
        usable_window = window if (window is not None and window > 0) else None
        pct: int | None = None
        if tokens is not None and usable_window is not None:
            pct = round(tokens / usable_window * 100)
        last = self._last
        return {
            "schema_version": 1,
            "type": CONTEXT_STATE_TYPE,
            "session_id": session_id,
            "model": model,
            "context_tokens": tokens,
            "input_tokens": last.input_tokens if last is not None else None,
            "output_tokens": last.output_tokens if last is not None else None,
            "cache_read": last.cache_read if last is not None else None,
            "cache_write": last.cache_write if last is not None else None,
            "context_window": usable_window,
            "window_source": window_source if usable_window is not None else None,
            "context_pct": pct,
            "cost_usd": str(self.cost.session_cost),
            "cost_estimated": self.cost.unpriced > 0,
        }


__all__ = [
    "CONTEXT_STATE_TYPE",
    "WINDOW_SOURCE_COMPACTION",
    "ContextMeter",
]
