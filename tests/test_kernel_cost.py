"""Tests for kernel/cost.py — Decimal cost math + resume re-seed.

Fully offline: only the fallback pricing table is exercised (live
fetches are injected fakes; the on-disk cache is a tmp file).
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from amplifier_app_newtui.kernel.cost import (
    CostTracker,
    ModelPricing,
    PricingTable,
    active_pricing_table,
    cost_of,
    estimate_cost,
    infer_provider,
    load_cached_pricing,
    pricing_live_enabled,
    restore_session_cost,
    save_pricing_cache,
    set_active_pricing_table,
    start_live_pricing,
    sum_prior_cost,
)
from amplifier_app_newtui.kernel.events import ProviderResponseUsage, normalize
from amplifier_app_newtui.kernel.persistence import SessionStore

# --------------------------------------------------------------------------
# estimate_cost
# --------------------------------------------------------------------------


def test_estimate_cost_known_model_exact_decimal() -> None:
    cost = estimate_cost(input_tokens=1000, output_tokens=1000, model="claude-sonnet-4-5")
    # 1k * $0.003 + 1k * $0.015 — exact Decimal, no float drift
    assert cost == Decimal("0.018")


def test_estimate_cost_cache_heuristics() -> None:
    # cache read = 10% of input price; cache write = input price
    read_only = estimate_cost(
        input_tokens=0, output_tokens=0, cache_read_tokens=1000, model="claude-sonnet-4"
    )
    assert read_only == Decimal("0.0003")
    write_only = estimate_cost(
        input_tokens=0, output_tokens=0, cache_write_tokens=1000, model="claude-sonnet-4"
    )
    assert write_only == Decimal("0.003")


def test_estimate_cost_unknown_model_returns_none() -> None:
    assert estimate_cost(input_tokens=10, output_tokens=10, model="") is None
    assert estimate_cost(input_tokens=10, output_tokens=10, model="mystery-model-9000") is None


def test_infer_provider() -> None:
    assert infer_provider("claude-sonnet-4-5") == "anthropic"
    assert infer_provider("gpt-4o-mini") == "openai"
    assert infer_provider("o1-preview") == "openai"
    assert infer_provider("gemini-2.0-flash-exp") == "google"
    assert infer_provider("llama-3") is None


def test_pricing_table_longest_prefix_wins() -> None:
    table = PricingTable()
    mini = table.lookup("openai", "gpt-4o-mini")
    full = table.lookup("openai", "gpt-4o")
    assert mini is not None and full is not None
    assert mini.input_per_1k == Decimal("0.00015")  # not the gpt-4o rate
    assert full.input_per_1k == Decimal("0.0025")


def test_azure_mirrors_openai() -> None:
    table = PricingTable()
    assert table.lookup("azure", "gpt-4o") == table.lookup("openai", "gpt-4o")


# --------------------------------------------------------------------------
# CostTracker
# --------------------------------------------------------------------------


def _usage(
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read: int = 0,
    cache_write: int = 0,
    model: str = "claude-sonnet-4",
) -> ProviderResponseUsage:
    return ProviderResponseUsage(
        session_id="s1",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read=cache_read,
        cache_write=cache_write,
        model=model,
    )


def test_cost_tracker_accumulates_session_and_turn() -> None:
    tracker = CostTracker()
    tracker.start_turn()
    first = tracker.record(_usage(input_tokens=1000, output_tokens=1000))
    assert first == Decimal("0.018")
    tracker.record(_usage(input_tokens=1000, output_tokens=0))

    assert tracker.turn.cost == Decimal("0.021")
    assert tracker.turn.tokens_down == 1000
    assert tracker.session_cost == Decimal("0.021")

    finished = tracker.end_turn()
    assert finished.cost == Decimal("0.021")
    # turn reset, session total kept
    assert tracker.turn.cost == Decimal("0")
    assert tracker.session_cost == Decimal("0.021")


def test_cached_pct() -> None:
    tracker = CostTracker()
    tracker.start_turn()
    assert tracker.turn.cached_pct is None  # no usage yet
    tracker.record(_usage(input_tokens=250, cache_read=750))
    assert tracker.turn.cached_pct == 75


def test_unpriceable_usage_counts_tokens_but_zero_cost() -> None:
    tracker = CostTracker()
    cost = tracker.record(_usage(output_tokens=500, model="mystery"))
    assert cost == Decimal("0")
    assert tracker.session_cost == Decimal("0")
    assert tracker.turn.tokens_down == 500


def test_seed_adds_prior_spend() -> None:
    tracker = CostTracker()
    tracker.seed(Decimal("1.25"))
    tracker.record(_usage(input_tokens=1000, output_tokens=0))
    assert tracker.session_cost == Decimal("1.253")


# --------------------------------------------------------------------------
# Resume re-seed from events.jsonl
# --------------------------------------------------------------------------


def _events_file_with_usage(tmp_path: Path) -> Path:
    """Write an events.jsonl through the real SessionStore pipeline."""
    store = SessionStore(base_dir=tmp_path / "sessions")
    for _ in range(2):
        event = normalize(
            "provider:response",
            {
                "session_id": "s1",
                "usage": {"input_tokens": 1000, "output_tokens": 1000},
                "model": "claude-sonnet-4",
            },
        )
        assert event is not None
        store.append_event("s1", event)
    # noise the reader must skip
    store.append_event("s1", {"kind": "session_start", "session_id": "s1"})
    events_path = store.events_path("s1")
    with events_path.open("a", encoding="utf-8") as handle:
        handle.write("corrupt line provider_response_usage\n")
    return events_path


def test_sum_prior_cost_replays_usage_events(tmp_path: Path) -> None:
    events_path = _events_file_with_usage(tmp_path)
    total = sum_prior_cost(events_path)
    assert total == Decimal("0.036")  # 2 × $0.018


def test_sum_prior_cost_missing_or_empty(tmp_path: Path) -> None:
    assert sum_prior_cost(tmp_path / "nope.jsonl") is None
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert sum_prior_cost(empty) is None


def test_restore_session_cost_seeds_tracker(tmp_path: Path) -> None:
    events_path = _events_file_with_usage(tmp_path)
    tracker = CostTracker()
    restored = restore_session_cost(tracker, events_path)
    assert restored == Decimal("0.036")
    assert tracker.session_cost == Decimal("0.036")
    # per-turn state untouched by the re-seed
    assert tracker.turn.cost == Decimal("0")


def test_restore_session_cost_no_prior_is_noop(tmp_path: Path) -> None:
    tracker = CostTracker()
    assert restore_session_cost(tracker, tmp_path / "nope.jsonl") is None
    assert tracker.session_cost == Decimal("0")


def test_cost_of_normalized_event_flat_usage_keys() -> None:
    # normalize() absorbs flat usage payloads too
    event = normalize(
        "provider:response",
        {"session_id": "s1", "input_tokens": 1000, "output_tokens": 1000, "model": "claude-opus-4"},
    )
    assert isinstance(event, ProviderResponseUsage)
    assert cost_of(event) == Decimal("0.09")  # 0.015 + 0.075


@pytest.mark.parametrize(
    ("cache_key", "expected"),
    [("cache_read_input_tokens", Decimal("0.0003")), ("cache_read", Decimal("0.0003"))],
)
def test_cache_key_variants_price_identically(cache_key: str, expected: Decimal) -> None:
    event = normalize(
        "provider:response",
        {"session_id": "s1", "usage": {cache_key: 1000}, "model": "claude-sonnet-4"},
    )
    assert isinstance(event, ProviderResponseUsage)
    assert cost_of(event) == expected


# --------------------------------------------------------------------------
# Unpriced counter — never lie in the footer (BACKLOG item 1)
# --------------------------------------------------------------------------


def test_unpriced_counter_counts_records_that_could_not_be_priced() -> None:
    tracker = CostTracker()
    tracker.start_turn()
    tracker.record(_usage(output_tokens=500, model="mystery-model-9000"))
    tracker.record(_usage(input_tokens=1000, output_tokens=1000))  # priceable
    assert tracker.unpriced == 1
    assert tracker.turn.unpriced == 1

    finished = tracker.end_turn()
    assert finished.unpriced == 1
    # per-turn count resets; the session counter is sticky
    assert tracker.turn.unpriced == 0
    assert tracker.unpriced == 1


def test_provider_reported_cost_usd_counts_as_priced() -> None:
    tracker = CostTracker()
    usage = ProviderResponseUsage(
        session_id="s1",
        output_tokens=500,
        model="mystery-model-9000",
        cost_usd=Decimal("0.42"),
    )
    assert tracker.record(usage) == Decimal("0.42")
    assert tracker.unpriced == 0


# --------------------------------------------------------------------------
# Active pricing table — atomic swap, new turns only
# --------------------------------------------------------------------------


_EXPENSIVE = PricingTable(
    {"anthropic": {"claude-sonnet-4": ModelPricing(Decimal("1"), Decimal("1"))}}
)


def test_active_table_defaults_to_fallback_and_none_resets() -> None:
    default = active_pricing_table()
    assert default.lookup("anthropic", "claude-sonnet-4") is not None
    set_active_pricing_table(_EXPENSIVE)
    assert active_pricing_table() is _EXPENSIVE
    set_active_pricing_table(None)
    assert active_pricing_table() is default


def test_table_swap_applies_to_new_turns_only() -> None:
    tracker = CostTracker()
    tracker.start_turn()
    tracker.record(_usage(input_tokens=1000))  # fallback: $0.003
    set_active_pricing_table(_EXPENSIVE)
    # Mid-turn swap: the running turn keeps its snapshot table.
    tracker.record(_usage(input_tokens=1000))  # still $0.003
    assert tracker.session_cost == Decimal("0.006")
    tracker.end_turn()
    tracker.start_turn()
    tracker.record(_usage(input_tokens=1000))  # new turn: $1.00
    assert tracker.session_cost == Decimal("1.006")


def test_explicit_tracker_pricing_wins_over_active_table() -> None:
    set_active_pricing_table(_EXPENSIVE)
    tracker = CostTracker(pricing=PricingTable())
    tracker.start_turn()
    assert tracker.record(_usage(input_tokens=1000)) == Decimal("0.003")


# --------------------------------------------------------------------------
# On-disk pricing cache (24h TTL; never raises)
# --------------------------------------------------------------------------


def test_pricing_cache_roundtrip_preserves_decimal_rates(tmp_path: Path) -> None:
    path = tmp_path / "pricing_cache.json"
    assert save_pricing_cache(_EXPENSIVE, path, now=lambda: 1000.0)
    loaded = load_cached_pricing(path, now=lambda: 1000.0 + 60)
    assert loaded is not None
    entry = loaded.lookup("anthropic", "claude-sonnet-4")
    assert entry == ModelPricing(Decimal("1"), Decimal("1"))


def test_pricing_cache_stale_missing_or_corrupt_returns_none(tmp_path: Path) -> None:
    path = tmp_path / "pricing_cache.json"
    assert load_cached_pricing(path) is None  # missing
    assert save_pricing_cache(_EXPENSIVE, path, now=lambda: 1000.0)
    stale_at = 1000.0 + 24 * 3600 + 1
    assert load_cached_pricing(path, now=lambda: stale_at) is None  # stale
    path.write_text("{not json", encoding="utf-8")
    assert load_cached_pricing(path, now=lambda: 1000.0) is None  # corrupt
    path.write_text('{"fetched_at": "soon"}', encoding="utf-8")
    assert load_cached_pricing(path, now=lambda: 1000.0) is None  # malformed


def test_pricing_cache_write_failure_never_raises(tmp_path: Path) -> None:
    # A directory where the cache file should be → open() fails.
    blocked = tmp_path / "pricing_cache.json"
    blocked.mkdir()
    assert save_pricing_cache(_EXPENSIVE, blocked) is False


# --------------------------------------------------------------------------
# Startup wiring — pricing.live settings gate + background fetch
# --------------------------------------------------------------------------


def test_pricing_live_enabled_defaults_true() -> None:
    assert pricing_live_enabled({}) is True
    assert pricing_live_enabled({"pricing": {}}) is True
    assert pricing_live_enabled({"pricing": "garbage"}) is True
    assert pricing_live_enabled({"pricing": {"live": True}}) is True
    assert pricing_live_enabled({"pricing": {"live": False}}) is False


def test_start_live_pricing_disabled_never_fetches(tmp_path: Path) -> None:
    def _fetch() -> PricingTable | None:
        raise AssertionError("fetch must not run when pricing.live is false")

    default = active_pricing_table()
    thread = start_live_pricing(
        {"pricing": {"live": False}},
        cache_path=tmp_path / "pricing_cache.json",
        fetch=_fetch,
    )
    assert thread is None
    assert active_pricing_table() is default


def test_start_live_pricing_fresh_cache_short_circuits_fetch(tmp_path: Path) -> None:
    path = tmp_path / "pricing_cache.json"
    assert save_pricing_cache(_EXPENSIVE, path)

    def _fetch() -> PricingTable | None:
        raise AssertionError("fresh cache must skip the network fetch")

    thread = start_live_pricing({}, cache_path=path, fetch=_fetch)
    assert thread is None
    assert active_pricing_table().lookup("anthropic", "claude-sonnet-4") == ModelPricing(
        Decimal("1"), Decimal("1")
    )


def test_start_live_pricing_fetch_success_swaps_table_and_writes_cache(
    tmp_path: Path,
) -> None:
    path = tmp_path / "pricing_cache.json"
    thread = start_live_pricing({}, cache_path=path, fetch=lambda: _EXPENSIVE)
    assert thread is not None
    assert thread.daemon
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert active_pricing_table() is _EXPENSIVE
    assert load_cached_pricing(path) is not None


def test_start_live_pricing_fetch_failure_keeps_fallback_silently(
    tmp_path: Path,
) -> None:
    path = tmp_path / "pricing_cache.json"
    default = active_pricing_table()

    thread = start_live_pricing({}, cache_path=path, fetch=lambda: None)
    assert thread is not None
    thread.join(timeout=5)
    assert active_pricing_table() is default
    assert not path.exists()

    def _boom() -> PricingTable | None:
        raise RuntimeError("network exploded")

    thread = start_live_pricing({}, cache_path=path, fetch=_boom)
    assert thread is not None
    thread.join(timeout=5)  # the raise never escapes the daemon worker
    assert active_pricing_table() is default
    assert not path.exists()
