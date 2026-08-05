"""Unit tests for :mod:`amplifier_app_tui.kernel.context_meter`.

Pure math over the honest sources (no serve, no runtime): the context-cost meter
folds ``ProviderResponseUsage`` events into a ``context.state`` snapshot — context
tokens used (last response), % of the context window, running $ spent — and stays
truthful when the window is unknown.
"""

from __future__ import annotations

from decimal import Decimal

from amplifier_app_tui.kernel.context_meter import (
    CONTEXT_STATE_TYPE,
    WINDOW_SOURCE_COMPACTION,
    ContextMeter,
)
from amplifier_app_tui.kernel.cost import CostTracker, PricingTable
from amplifier_app_tui.kernel.events import ContextCompacted, ProviderResponseUsage

WINDOW = 200_000


def _meter() -> ContextMeter:
    # Deterministic offline pricing (no live/network swap).
    return ContextMeter(cost=CostTracker(pricing=PricingTable()))


def _usage(**kw: object) -> ProviderResponseUsage:
    base: dict[str, object] = {
        "session_id": "s1",
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read": 0,
        "cache_write": 0,
        "model": "claude-sonnet-4",
    }
    base.update(kw)
    return ProviderResponseUsage(**base)  # type: ignore[arg-type]


def test_no_usage_snapshot_is_all_null_but_cost_zero() -> None:
    snap = _meter().snapshot(session_id="s1", model="m", window=WINDOW)
    assert snap["type"] == CONTEXT_STATE_TYPE
    assert snap["schema_version"] == 1
    assert snap["context_tokens"] is None
    for key in ("input_tokens", "output_tokens", "cache_read", "cache_write"):
        assert snap[key] is None, key
    # Window is known, but with no tokens the percentage stays null (never 0-lie).
    assert snap["context_pct"] is None
    assert snap["cost_usd"] == "0"
    assert snap["cost_estimated"] is False


def test_context_tokens_is_last_response_not_accumulated() -> None:
    meter = _meter()
    meter.record(_usage(input_tokens=1000, output_tokens=200))
    meter.record(_usage(input_tokens=1200, output_tokens=340, cache_read=800, cache_write=100))
    # Canonical input is gross: cache_read is already inside it. Cache
    # creation is separate, so only cache_write is added.
    assert meter.context_tokens == 1200 + 340 + 100
    snap = meter.snapshot(session_id="s1", model="m", window=WINDOW)
    assert snap["context_tokens"] == 1640
    assert snap["input_tokens"] == 1200
    assert snap["output_tokens"] == 340
    assert snap["cache_read"] == 800
    assert snap["cache_write"] == 100


def test_native_compaction_supplies_provider_budget_and_request_view() -> None:
    meter = _meter()
    meter.record_compaction(
        ContextCompacted(
            after_tokens=482_452,
            budget=963_104,
            target_tokens=481_552,
            strategy_level=3,
        )
    )
    snap = meter.snapshot(session_id="s1", model="m", window=300_000)
    assert snap["context_tokens"] == 482_452
    assert snap["context_window"] == 963_104
    assert snap["context_pct"] == 50


def test_pct_is_rounded_ratio_against_window() -> None:
    meter = _meter()
    meter.record(_usage(input_tokens=50_000, output_tokens=10_000))  # 60_000
    snap = meter.snapshot(session_id="s1", model="m", window=WINDOW)
    assert snap["context_window"] == WINDOW
    assert snap["window_source"] == WINDOW_SOURCE_COMPACTION
    assert snap["context_pct"] == round(60_000 / WINDOW * 100)  # 30


def test_null_window_yields_null_pct_and_source() -> None:
    meter = _meter()
    meter.record(_usage(input_tokens=1000, output_tokens=200))
    snap = meter.snapshot(session_id="s1", model="m", window=None)
    assert snap["context_window"] is None
    assert snap["window_source"] is None
    assert snap["context_pct"] is None
    # Tokens themselves are still reported (only the % denominator is unknown).
    assert snap["context_tokens"] == 1200


def test_nonpositive_window_treated_as_unknown() -> None:
    meter = _meter()
    meter.record(_usage(input_tokens=1000, output_tokens=200))
    snap = meter.snapshot(session_id="s1", model="m", window=0)
    assert snap["context_window"] is None
    assert snap["context_pct"] is None


def test_cost_tracks_the_cost_tracker_and_priceable_is_not_estimated() -> None:
    meter = _meter()
    meter.record(_usage(input_tokens=1000, output_tokens=200))
    snap = meter.snapshot(session_id="s1", model="m", window=WINDOW)
    expected = Decimal(1000) * Decimal("0.003") / 1000 + Decimal(200) * Decimal("0.015") / 1000
    assert Decimal(str(snap["cost_usd"])) == expected == meter.cost.session_cost
    assert snap["cost_estimated"] is False


def test_unpriceable_usage_marks_cost_estimated() -> None:
    meter = _meter()
    # Unknown model + no provider cost_usd -> unpriceable (floor).
    meter.record(_usage(input_tokens=1000, output_tokens=200, model="totally-unknown-model"))
    snap = meter.snapshot(session_id="s1", model="totally-unknown-model", window=WINDOW)
    assert snap["cost_estimated"] is True
    assert snap["cost_usd"] == "0"
    # Tokens still reported honestly even when the money is a floor.
    assert snap["context_tokens"] == 1200


def test_reused_tracker_carries_resume_seed() -> None:
    tracker = CostTracker(pricing=PricingTable())
    tracker.seed(Decimal("1.25"))  # prior/resume spend
    meter = ContextMeter(cost=tracker)
    meter.record(_usage(input_tokens=1000, output_tokens=200))
    snap = meter.snapshot(session_id="s1", model="m", window=WINDOW)
    expected = Decimal("1.25") + (
        Decimal(1000) * Decimal("0.003") / 1000 + Decimal(200) * Decimal("0.015") / 1000
    )
    assert Decimal(str(snap["cost_usd"])) == expected


def test_default_tracker_when_none_supplied() -> None:
    # A meter with no runtime tracker still works (fresh CostTracker default).
    meter = ContextMeter()
    meter.record(_usage(input_tokens=10, output_tokens=5, model="claude-sonnet-4"))
    snap = meter.snapshot(session_id="s1", model="m", window=WINDOW)
    assert snap["context_tokens"] == 15
    assert Decimal(str(snap["cost_usd"])) >= 0
