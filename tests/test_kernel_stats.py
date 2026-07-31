"""kernel/stats.py — cross-session cost/usage aggregation (donor: opencode `stats`).

Every test runs against a tmp-dir :class:`SessionStore` seeded with
``provider_response_usage`` UIEvents; nothing touches the real ``~/.amplifier``.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from amplifier_app_tui.kernel import stats
from amplifier_app_tui.kernel.persistence import SessionStore

# claude-sonnet-4 offline fallback price: in 0.003/1k, out 0.015/1k.
# 1000 in + 500 out -> 0.003 + 0.0075 = 0.0105 per record.
_SONNET_ONE = Decimal("0.0105")


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(base_dir=tmp_path / "sessions")


def _seed(
    store: SessionStore,
    session_id: str,
    *,
    usages: list[dict[str, object]],
    messages: int = 0,
    bundle: str = "tui",
    mtime: float | None = None,
) -> None:
    transcript = [{"role": "user", "content": f"m{i}"} for i in range(messages)]
    store.save(session_id, transcript, {"session_id": session_id, "bundle": bundle})
    for usage in usages:
        store.append_event(session_id, {"kind": "provider_response_usage", **usage})
    if mtime is not None:
        os.utime(store.session_dir(session_id), (mtime, mtime))


def _u(model: str = "claude-sonnet-4", **kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "input_tokens": 1000,
        "output_tokens": 500,
        "cache_read": 0,
        "cache_write": 0,
        "model": model,
    }
    base.update(kw)
    return base


# -- totals + per-model rollup ----------------------------------------------


def test_aggregate_totals_and_model_rollup(store: SessionStore) -> None:
    _seed(store, "sessa", usages=[_u(), _u()], messages=4)
    _seed(store, "sessb", usages=[_u()], messages=2)

    report = stats.aggregate([("proj", store)])

    assert report.total_sessions == 2
    assert report.total_messages == 6
    assert report.total_responses == 3
    assert report.input_tokens == 3000
    assert report.output_tokens == 1500
    assert report.total_cost == _SONNET_ONE * 3
    assert report.unpriced == 0
    model = report.by_model["claude-sonnet-4"]
    assert model.responses == 3
    assert model.cost == _SONNET_ONE * 3
    assert report.tokens_per_session == round(4500 / 2)


def test_unpriced_usage_is_marked_not_zeroed(store: SessionStore) -> None:
    # Unknown model + no provider cost_usd -> cannot be priced.
    _seed(store, "sess", usages=[_u(model="mystery-model-9000"), _u()])

    report = stats.aggregate([("proj", store)])

    assert report.total_responses == 2
    assert report.unpriced == 1
    # Only the priceable record contributes to cost.
    assert report.total_cost == _SONNET_ONE
    assert report.by_model["mystery-model-9000"].unpriced == 1


def test_provider_cost_usd_wins_over_table(store: SessionStore) -> None:
    _seed(store, "sess", usages=[_u(cost_usd="0.50")])

    report = stats.aggregate([("proj", store)])

    assert report.total_cost == Decimal("0.50")
    assert report.unpriced == 0


def test_cache_tokens_accumulate(store: SessionStore) -> None:
    _seed(store, "sess", usages=[_u(cache_read=200, cache_write=50)])

    report = stats.aggregate([("proj", store)])

    assert report.cache_read == 200
    assert report.cache_write == 50


# -- window (--days) semantics ----------------------------------------------


def test_days_cutoff_excludes_old_sessions(store: SessionStore) -> None:
    now = datetime(2026, 1, 20, 12, 0, tzinfo=UTC)
    recent = (now - timedelta(days=1)).timestamp()
    old = (now - timedelta(days=10)).timestamp()
    _seed(store, "recent", usages=[_u()], mtime=recent)
    _seed(store, "old", usages=[_u()], mtime=old)

    report = stats.aggregate([("proj", store)], days=7, now=now)

    assert report.total_sessions == 1
    assert report.total_responses == 1
    assert report.days == 7


def test_days_zero_is_today_only_window_one(store: SessionStore) -> None:
    now = datetime(2026, 1, 20, 12, 0, tzinfo=UTC)
    today = (now - timedelta(hours=2)).timestamp()
    yesterday = (now - timedelta(days=1)).timestamp()
    _seed(store, "today", usages=[_u()], mtime=today)
    _seed(store, "yesterday", usages=[_u()], mtime=yesterday)

    report = stats.aggregate([("proj", store)], days=0, now=now)

    assert report.total_sessions == 1
    assert report.days == 1
    assert report.cost_per_day == _SONNET_ONE  # divided by a 1-day window


def test_all_time_window_uses_observed_span(store: SessionStore) -> None:
    now = datetime(2026, 1, 20, 12, 0, tzinfo=UTC)
    _seed(store, "a", usages=[_u()], mtime=(now - timedelta(days=4)).timestamp())
    _seed(store, "b", usages=[_u()], mtime=now.timestamp())

    report = stats.aggregate([("proj", store)], now=now)

    assert report.window_label == "all time"
    assert report.days == 4  # ceil(span)


# -- by-day + by-project rollups --------------------------------------------


def test_by_day_bucketing(store: SessionStore) -> None:
    now = datetime(2026, 1, 20, 12, 0, tzinfo=UTC)
    _seed(store, "d1", usages=[_u()], mtime=datetime(2026, 1, 18, 9, tzinfo=UTC).timestamp())
    _seed(store, "d2", usages=[_u(), _u()], mtime=datetime(2026, 1, 19, 9, tzinfo=UTC).timestamp())

    report = stats.aggregate([("proj", store)], now=now)

    assert set(report.by_day) == {"2026-01-18", "2026-01-19"}
    assert report.by_day["2026-01-19"].responses == 2


def test_multi_project_rollup(tmp_path: Path) -> None:
    store_a = SessionStore(base_dir=tmp_path / "a" / "sessions")
    store_b = SessionStore(base_dir=tmp_path / "b" / "sessions")
    _seed(store_a, "a1", usages=[_u()])
    _seed(store_b, "b1", usages=[_u(), _u()])

    report = stats.aggregate([("proj-a", store_a), ("proj-b", store_b)], multi_project=True)

    assert report.total_responses == 3
    assert report.by_project["proj-a"].responses == 1
    assert report.by_project["proj-b"].responses == 2


# -- empty / robustness ------------------------------------------------------


def test_empty_report_is_zeroed_with_window_days(store: SessionStore) -> None:
    report = stats.aggregate([("proj", store)], days=7)

    assert report.total_sessions == 0
    assert report.total_cost == Decimal("0")
    assert report.days == 7
    assert report.cost_per_day == Decimal("0")  # no divide-by-zero


def test_malformed_usage_record_skipped(store: SessionStore) -> None:
    store.save("sess", [], {"session_id": "sess", "bundle": "tui"})
    store.append_event("sess", {"kind": "provider_response_usage", "input_tokens": "not-an-int"})
    store.append_event("sess", {"kind": "provider_response_usage", **_u()})

    report = stats.aggregate([("proj", store)])

    assert report.total_responses == 1  # the valid record only


# -- resolve_sources ---------------------------------------------------------


def test_resolve_sources_current_project(tmp_path: Path) -> None:
    sources, scope = stats.resolve_sources(None, project_dir=tmp_path)
    assert len(sources) == 1
    assert "current project" in scope


def test_resolve_sources_all_scans_home(tmp_path: Path) -> None:
    home = tmp_path / ".amplifier"
    for name in ("projA", "projB"):
        (home / "projects" / name / "sessions").mkdir(parents=True)
    sources, scope = stats.resolve_sources("all", amplifier_home=home)
    assert {label for label, _ in sources} == {"projA", "projB"}
    assert scope == "all projects"


def test_resolve_sources_named_slug(tmp_path: Path) -> None:
    home = tmp_path / ".amplifier"
    sources, scope = stats.resolve_sources("-Users-me-dev-x", amplifier_home=home)
    assert len(sources) == 1
    assert scope == "project -Users-me-dev-x"


# -- render ------------------------------------------------------------------


def test_render_contains_section_markers(store: SessionStore) -> None:
    _seed(store, "sess", usages=[_u()])
    report = stats.aggregate([("proj", store)])
    text = stats.render(report, models="all")
    assert "AMPLIFIER USAGE STATS" in text
    assert "OVERVIEW" in text
    assert "COST & TOKENS" in text
    assert "BY DAY" in text
    assert "BY MODEL" in text
    assert "claude-sonnet-4" in text
    assert "$0.01" in text


def test_render_empty_note(store: SessionStore) -> None:
    report = stats.aggregate([("proj", store)], days=7)
    text = stats.render(report)
    assert "OVERVIEW" in text
    assert "No usage" in text


def test_render_model_section_hidden_by_default(store: SessionStore) -> None:
    _seed(store, "sess", usages=[_u()])
    report = stats.aggregate([("proj", store)])
    assert "BY MODEL" not in stats.render(report)  # models=None hides it
    assert "BY MODEL" in stats.render(report, models="all")


def test_render_json_surface(store: SessionStore) -> None:
    import json

    _seed(store, "sess", usages=[_u()])
    report = stats.aggregate([("proj", store)])
    payload = json.loads(stats.render(report, json_output=True))
    assert payload["total_responses"] == 1
    assert payload["total_cost"] == str(_SONNET_ONE)
    assert "claude-sonnet-4" in payload["by_model"]


def test_render_unpriced_marker(store: SessionStore) -> None:
    _seed(store, "sess", usages=[_u(model="mystery-x")])
    report = stats.aggregate([("proj", store)])
    text = stats.render(report)
    assert "~$" in text  # cost is a floor when unpriced
