"""Tests for kernel/cost.py — Decimal cost math + resume re-seed.

Fully offline: only the fallback pricing table is exercised.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from amplifier_app_newtui.kernel.cost import (
    CostTracker,
    PricingTable,
    cost_of,
    estimate_cost,
    infer_provider,
    restore_session_cost,
    sum_prior_cost,
)
from amplifier_app_newtui.kernel.events import ProviderResponseUsage, normalize
from amplifier_app_newtui.kernel.persistence import SessionStore

# --------------------------------------------------------------------------
# estimate_cost
# --------------------------------------------------------------------------


def test_estimate_cost_known_model_exact_decimal() -> None:
    cost = estimate_cost(
        input_tokens=1000, output_tokens=1000, model="claude-sonnet-4-5"
    )
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
    assert (
        estimate_cost(input_tokens=10, output_tokens=10, model="mystery-model-9000")
        is None
    )


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
