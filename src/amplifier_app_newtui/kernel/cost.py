"""Provider usage → Decimal cost, plus session/turn accounting.

Port of ``estimate_cost()`` from amplifier-module-hooks-streaming-ui
``cost.py`` with three changes for the TUI:

- **Decimal end to end** (money is never a float here).
- **Offline by default**: the hardcoded fallback pricing table is the
  default; the Helicone live fetch is an explicit opt-in
  (:func:`fetch_live_pricing`) — unit tests and cold starts never touch
  the network.
- **Resume re-seed** from the session's ``ui-events.jsonl`` of normalized
  UIEvents (the ``cost_history.py`` pattern; legacy pre-rename
  ``events.jsonl`` files are read too): provider usage events are
  replayed through the same pricing math, so a resumed session's footer
  cost continues from the prior total.

Kernel ``SessionStatus`` counters are NOT populated by the engine — the
app computes cost from ``provider:response`` usage itself (RESEARCH-BRIEF
§2). :class:`CostTracker` consumes normalized
:class:`~amplifier_app_newtui.kernel.events.ProviderResponseUsage` events
and exposes session spend, per-turn cost/tokens and cache-hit %.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from decimal import Decimal, InvalidOperation
from typing import Any

from .events import ContentBlockEnd, ProviderResponseUsage, usage_from_content_block_end

logger = logging.getLogger(__name__)

_K = Decimal(1000)
_CACHE_READ_DISCOUNT = Decimal("0.1")  # heuristic: cache read = 10% of input price


@dataclass(frozen=True)
class ModelPricing:
    """USD per 1K tokens for one model (0 cache prices ⇒ use heuristics)."""

    input_per_1k: Decimal
    output_per_1k: Decimal
    cache_read_per_1k: Decimal = Decimal("0")
    cache_write_per_1k: Decimal = Decimal("0")


def _p(value: str) -> Decimal:
    return Decimal(value)


# Minimal hardcoded pricing (per 1K tokens) — offline default, mirrors the
# streaming-ui module's fallback table.
FALLBACK_PRICING: dict[str, dict[str, ModelPricing]] = {
    "anthropic": {
        "claude-sonnet-4": ModelPricing(_p("0.003"), _p("0.015")),
        "claude-opus-4": ModelPricing(_p("0.015"), _p("0.075")),
        "claude-3-5-sonnet": ModelPricing(_p("0.003"), _p("0.015")),
        "claude-3-5-haiku": ModelPricing(_p("0.0008"), _p("0.004")),
        "claude": ModelPricing(_p("0.003"), _p("0.015")),  # family fallback
    },
    "openai": {
        "gpt-4o-mini": ModelPricing(_p("0.00015"), _p("0.0006")),
        "gpt-4o": ModelPricing(_p("0.0025"), _p("0.01")),
        "o3-mini": ModelPricing(_p("0.0011"), _p("0.0044")),
        "o1": ModelPricing(_p("0.015"), _p("0.06")),
    },
    "google": {
        "gemini-2.0-flash": ModelPricing(_p("0.0001"), _p("0.0004")),
        "gemini-1.5-pro": ModelPricing(_p("0.00125"), _p("0.005")),
    },
}
FALLBACK_PRICING["azure"] = dict(FALLBACK_PRICING["openai"])


class PricingTable:
    """Model-pricing lookup with prefix matching (longest prefix wins)."""

    def __init__(self, entries: dict[str, dict[str, ModelPricing]] | None = None) -> None:
        self._entries = entries if entries is not None else FALLBACK_PRICING

    def lookup(self, provider: str, model: str) -> ModelPricing | None:
        models = self._entries.get(provider.lower())
        if not models:
            return None
        if model in models:
            return models[model]
        best: tuple[int, ModelPricing] | None = None
        for pattern, pricing in models.items():
            if model.startswith(pattern) or pattern.startswith(model):
                score = len(pattern)
                if best is None or score > best[0]:
                    best = (score, pricing)
        return best[1] if best else None


_DEFAULT_TABLE = PricingTable()

_active_table: PricingTable = _DEFAULT_TABLE
"""The process-wide pricing table used for NEW turns.

A plain module-level reference: reads and the swap in
:func:`set_active_pricing_table` are single attribute operations —
atomic under the GIL — so the background fetch thread never needs a
lock. :class:`CostTracker` snapshots this reference at ``start_turn``,
which is what keeps already-recorded turn costs (and the running
session total) immune to a mid-session swap.
"""


def active_pricing_table() -> PricingTable:
    """The pricing table new turns should price against."""
    return _active_table


def set_active_pricing_table(table: PricingTable | None) -> None:
    """Atomically swap the active table (``None`` restores the fallback)."""
    global _active_table
    _active_table = table if table is not None else _DEFAULT_TABLE


def infer_provider(model: str) -> str | None:
    """Best-effort provider inference from a model name."""
    lowered = model.lower()
    if lowered.startswith("claude"):
        return "anthropic"
    if lowered.startswith(("gpt", "o1", "o3", "o4")):
        return "openai"
    if lowered.startswith("gemini"):
        return "google"
    return None


def estimate_cost(
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    provider: str | None = None,
    model: str | None = None,
    pricing: PricingTable | None = None,
) -> Decimal | None:
    """Estimate USD cost for one provider response.

    Cache pricing: explicit table rates when present, otherwise
    cache read = 10% of input price, cache write = input price
    (the streaming-ui heuristics). Returns ``None`` when the model is
    unknown — callers must treat unknown as "no figure", never 0.
    """
    if not model:
        return None
    provider = provider or infer_provider(model)
    if not provider:
        return None
    entry = (pricing or _DEFAULT_TABLE).lookup(provider, model)
    if entry is None:
        return None

    input_cost = Decimal(input_tokens) * entry.input_per_1k / _K
    output_cost = Decimal(output_tokens) * entry.output_per_1k / _K

    read_rate = entry.cache_read_per_1k or entry.input_per_1k * _CACHE_READ_DISCOUNT
    write_rate = entry.cache_write_per_1k or entry.input_per_1k
    cache_read_cost = Decimal(cache_read_tokens) * read_rate / _K
    cache_write_cost = Decimal(cache_write_tokens) * write_rate / _K

    return input_cost + output_cost + cache_read_cost + cache_write_cost


def cost_of(usage: ProviderResponseUsage, pricing: PricingTable | None = None) -> Decimal | None:
    """Cost of one normalized ``provider_response_usage`` event.

    A provider-reported ``cost_usd`` (loop-streaming's content-block
    usage payload) is authoritative over the local table estimate."""
    if usage.cost_usd is not None:
        return usage.cost_usd
    return estimate_cost(
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read,
        cache_write_tokens=usage.cache_write,
        model=usage.model,
        pricing=pricing,
    )


# --------------------------------------------------------------------------
# Session / turn accounting
# --------------------------------------------------------------------------


@dataclass
class TurnUsage:
    """Accumulated usage for the turn in flight."""

    cost: Decimal = Decimal("0")
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    unpriced: int = 0
    """Usage records this turn that could not be priced (no table entry
    and no provider ``cost_usd``) — their $0 makes ``cost`` a floor, so
    renderers must mark the figure (``~$``) instead of lying."""

    @property
    def tokens_down(self) -> int:
        """Output tokens — the ``↓ X.Xk tok`` figure."""
        return self.output_tokens

    @property
    def cached_pct(self) -> int | None:
        """% of prompt tokens served from cache (None before any usage)."""
        denominator = self.input_tokens + self.cache_read + self.cache_write
        if denominator <= 0:
            return None
        return round(self.cache_read * 100 / denominator)


@dataclass
class CostTracker:
    """Running session + per-turn cost from provider usage events.

    Feed every :class:`ProviderResponseUsage` to :meth:`record`; call
    :meth:`start_turn` at ``prompt:submit`` and :meth:`end_turn` at the
    turn boundary. ``session_cost`` includes any resume-seeded prior
    spend (:meth:`seed`).

    Pricing table selection: an explicit ``pricing`` always wins (unit
    tests, fixed-table callers). Otherwise the tracker snapshots the
    process-wide :func:`active_pricing_table` at :meth:`start_turn`, so
    a live-pricing swap landing mid-session applies to NEW turns only —
    already-recorded turn costs and the running session total never
    change retroactively.
    """

    pricing: PricingTable | None = None
    unpriced: int = 0
    """Session-total count of usage records that could not be priced."""
    _session_cost: Decimal = Decimal("0")
    _turn: TurnUsage = field(default_factory=TurnUsage)
    _turn_pricing: PricingTable | None = None

    @property
    def session_cost(self) -> Decimal:
        return self._session_cost

    @property
    def turn(self) -> TurnUsage:
        return self._turn

    def seed(self, prior_total: Decimal) -> None:
        """Re-seed the session total with pre-resume spend."""
        if prior_total > 0:
            self._session_cost += prior_total

    def start_turn(self) -> None:
        self._turn = TurnUsage()
        # Snapshot the table for the whole turn (see class docstring).
        self._turn_pricing = self.pricing if self.pricing is not None else active_pricing_table()

    def end_turn(self) -> TurnUsage:
        """Freeze and return the finished turn's usage."""
        finished = self._turn
        self._turn = TurnUsage()
        self._turn_pricing = None
        return finished

    def _table(self) -> PricingTable:
        if self.pricing is not None:
            return self.pricing
        if self._turn_pricing is not None:
            return self._turn_pricing
        return active_pricing_table()

    def record(self, usage: ProviderResponseUsage) -> Decimal:
        """Accumulate one usage event; returns its cost (0 if unpriceable).

        Unpriceable records (unknown model, no provider ``cost_usd``)
        contribute $0 to the totals but increment ``unpriced`` — session
        and per-turn — so the UI can mark the figures as a floor.
        """
        cost = cost_of(usage, self._table())
        if cost is None:
            cost = Decimal("0")
            self.unpriced += 1
            self._turn.unpriced += 1
        self._session_cost += cost
        self._turn.cost += cost
        self._turn.input_tokens += usage.input_tokens
        self._turn.output_tokens += usage.output_tokens
        self._turn.cache_read += usage.cache_read
        self._turn.cache_write += usage.cache_write
        return cost


# --------------------------------------------------------------------------
# Resume re-seed from ui-events.jsonl (legacy events.jsonl fallback)
# --------------------------------------------------------------------------

_USAGE_KIND = "provider_response_usage"
_CONTENT_BLOCK_KIND = "content_block_end"


def sum_prior_cost(events_path: Path, pricing: PricingTable | None = None) -> Decimal | None:
    """Sum provider responses in one UIEvent log file exactly once.

    *events_path* is a ``ui-events.jsonl`` (or pre-rename ``events.jsonl``)
    from :meth:`SessionStore.events_path` / ``events_read_paths``.
    Reads line-by-line (events files can be large) with a substring
    pre-filter; foreign records (hooks-logging's colon-named hook events)
    carry no ``kind`` and are skipped. Older NewTUI logs wrote the same
    usage record before every block in one response; the following
    ``content_block_end`` identifies whether that record belongs to the
    response's final block. Standalone provider usage records retain their
    original behavior. Returns ``None`` when the file is missing/unreadable
    or holds no priceable usage. Never raises.
    """
    if not events_path.is_file():
        return None

    total: Decimal | None = None
    pending: ProviderResponseUsage | None = None

    def add(usage: ProviderResponseUsage) -> None:
        nonlocal total
        cost = cost_of(usage, pricing)
        if cost is not None:
            total = (total or Decimal("0")) + cost

    try:
        with events_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if _USAGE_KIND not in line and _CONTENT_BLOCK_KIND not in line:
                    if pending is not None:
                        add(pending)
                        pending = None
                    continue
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(record, dict):
                    continue

                kind = record.get("kind")
                if kind == _USAGE_KIND:
                    if pending is not None:
                        add(pending)
                    try:
                        pending = ProviderResponseUsage.model_validate(record)
                    except Exception:  # noqa: BLE001 — skip malformed records
                        pending = None
                    continue

                if kind != _CONTENT_BLOCK_KIND:
                    if pending is not None:
                        add(pending)
                        pending = None
                    continue

                try:
                    block = ContentBlockEnd.model_validate(record)
                except Exception:  # noqa: BLE001 — preserve an adjacent usage record
                    if pending is not None:
                        add(pending)
                        pending = None
                    continue

                final_block = block.total_blocks <= 0 or block.block_index == block.total_blocks - 1
                if block.usage:
                    usage = pending or usage_from_content_block_end(block)
                    if final_block and usage is not None:
                        add(usage)
                elif pending is not None:
                    add(pending)
                pending = None
        if pending is not None:
            add(pending)
    except OSError:
        logger.debug("Could not read events for prior cost: %s", events_path, exc_info=True)
        return None
    return total


def restore_session_cost(tracker: CostTracker, *events_paths: Path) -> Decimal | None:
    """Seed *tracker* with the prior spend found across *events_paths*.

    A rename-straddling session splits its UIEvents between the legacy
    ``events.jsonl`` and ``ui-events.jsonl``
    (:meth:`SessionStore.events_read_paths`), so every file is summed.
    Returns the restored total, or ``None`` when there was nothing to
    restore. Never raises — resume must not break on a bad event log.
    """
    totals = [
        total
        for total in (sum_prior_cost(path, tracker.pricing) for path in events_paths)
        if total is not None
    ]
    prior = sum(totals, Decimal("0")) if totals else None
    if prior is None or prior <= 0:
        return None
    tracker.seed(prior)
    logger.info("Restored prior session cost $%s from %s", prior, list(map(str, events_paths)))
    return prior


# --------------------------------------------------------------------------
# Optional live pricing (explicit opt-in; never called implicitly)
# --------------------------------------------------------------------------

_HELICONE_URL = "https://www.helicone.ai/api/llm-costs"


def fetch_live_pricing(timeout: float = 5.0) -> PricingTable | None:
    """Fetch live pricing from Helicone (explicit opt-in, stdlib only).

    Returns ``None`` on any failure; callers keep the offline table.
    """
    import urllib.error
    import urllib.request

    try:
        request = urllib.request.Request(_HELICONE_URL, headers={"Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            payload = json.loads(response.read().decode())
    except (urllib.error.URLError, OSError, ValueError):
        logger.debug("Helicone pricing unavailable; keeping fallback table")
        return None

    entries: dict[str, dict[str, ModelPricing]] = {}
    for item in payload.get("data", []):
        provider = str(item.get("provider") or "").lower()
        model = str(item.get("model") or "")
        if not provider or not model:
            continue
        try:
            entries.setdefault(provider, {})[model] = ModelPricing(
                input_per_1k=Decimal(str(item.get("input_cost_per_1m") or 0)) / _K,
                output_per_1k=Decimal(str(item.get("output_cost_per_1m") or 0)) / _K,
                cache_read_per_1k=Decimal(str(item.get("prompt_cache_read_per_1m") or 0)) / _K,
                cache_write_per_1k=Decimal(str(item.get("prompt_cache_write_per_1m") or 0)) / _K,
            )
        except (InvalidOperation, ValueError):
            continue
    if "openai" in entries and "azure" not in entries:
        entries["azure"] = dict(entries["openai"])
    return PricingTable(entries) if entries else None


# --------------------------------------------------------------------------
# On-disk pricing cache + startup wiring (BACKLOG item 1)
# --------------------------------------------------------------------------

PRICING_CACHE_PATH = Path.home() / ".amplifier" / "pricing_cache.json"
"""Fetched-table cache. amplifier-app-cli keeps no on-disk pricing cache
(its estimator in amplifier-module-hooks-streaming-ui caches in memory
only), so this follows its ``~/.amplifier`` JSON-cache-file convention
(cf. app-cli's ``.update_cache.json``) at the backlog's default location."""

PRICING_CACHE_TTL_SECONDS = 24 * 3600.0
"""Cache freshness window (24 h — the backlog's default TTL)."""

_RATE_FIELDS = ("input_per_1k", "output_per_1k", "cache_read_per_1k", "cache_write_per_1k")


def load_cached_pricing(
    path: Path | None = None,
    *,
    ttl: float = PRICING_CACHE_TTL_SECONDS,
    now: Callable[[], float] | None = None,
) -> PricingTable | None:
    """The cached pricing table, or ``None`` when missing/stale/corrupt.

    Never raises — a bad cache file simply means "no cache" (callers
    fall back and refetch).
    """
    cache_path = path if path is not None else PRICING_CACHE_PATH
    clock = now or time.time
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        fetched_at = float(payload["fetched_at"])
        if clock() - fetched_at > ttl:
            return None
        entries: dict[str, dict[str, ModelPricing]] = {}
        for provider, models in payload["entries"].items():
            for model, rates in models.items():
                entries.setdefault(str(provider), {})[str(model)] = ModelPricing(
                    **{field_name: Decimal(str(rates[field_name])) for field_name in _RATE_FIELDS}
                )
        return PricingTable(entries) if entries else None
    except Exception:  # noqa: BLE001 — corrupt/missing cache is "no cache"
        logger.debug("Pricing cache unusable: %s", cache_path, exc_info=True)
        return None


def save_pricing_cache(
    table: PricingTable,
    path: Path | None = None,
    *,
    now: Callable[[], float] | None = None,
) -> bool:
    """Persist *table* (Decimal rates as strings). Never raises."""
    cache_path = path if path is not None else PRICING_CACHE_PATH
    clock = now or time.time
    try:
        entries = {
            provider: {
                model: {
                    field_name: str(getattr(pricing, field_name)) for field_name in _RATE_FIELDS
                }
                for model, pricing in models.items()
            }
            for provider, models in table._entries.items()  # noqa: SLF001 — same module
        }
        payload = json.dumps({"fetched_at": clock(), "entries": entries})
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = cache_path.with_suffix(".json.tmp")
        tmp_path.write_text(payload, encoding="utf-8")
        tmp_path.replace(cache_path)
        return True
    except Exception:  # noqa: BLE001 — the cache is an optimization only
        logger.debug("Could not write pricing cache: %s", cache_path, exc_info=True)
        return False


def pricing_live_enabled(settings: Mapping[str, Any]) -> bool:
    """The ``pricing.live`` settings key (default: enabled)."""
    section = settings.get("pricing")
    if isinstance(section, Mapping):
        value = section.get("live")
        if isinstance(value, bool):
            return value
    return True


def start_live_pricing(
    settings: Mapping[str, Any],
    *,
    cache_path: Path | None = None,
    fetch: Callable[[], PricingTable | None] | None = None,
    now: Callable[[], float] | None = None,
) -> threading.Thread | None:
    """Wire live pricing at app startup (behind ``pricing.live``).

    - ``pricing.live: false`` → nothing happens; the fallback table stays.
    - Fresh on-disk cache → activated immediately, no fetch needed.
    - Stale/missing cache → fallback now, plus a **daemon** background
      thread that fetches Helicone, atomically swaps the active table
      (new turns only — see :func:`set_active_pricing_table`) and writes
      the cache on success.

    Returns the fetch thread when one was started (tests ``join()`` it);
    ``None`` otherwise. Never raises — any failure degrades silently to
    the fallback table.
    """
    try:
        if not pricing_live_enabled(settings or {}):
            return None
        cached = load_cached_pricing(cache_path, now=now)
        if cached is not None:
            set_active_pricing_table(cached)
            return None
        fetch_fn = fetch if fetch is not None else fetch_live_pricing

        def _worker() -> None:
            try:
                table = fetch_fn()
                if table is None:
                    return  # fetch failed/timed out → keep the fallback silently
                set_active_pricing_table(table)
                save_pricing_cache(table, cache_path, now=now)
            except Exception:  # noqa: BLE001 — background work never raises into the app
                logger.debug("Live pricing fetch failed; keeping fallback", exc_info=True)

        thread = threading.Thread(target=_worker, name="pricing-fetch", daemon=True)
        thread.start()
        return thread
    except Exception:  # noqa: BLE001 — startup must never break on pricing
        logger.debug("Live pricing startup wiring failed", exc_info=True)
        return None


__all__ = [
    "FALLBACK_PRICING",
    "PRICING_CACHE_PATH",
    "PRICING_CACHE_TTL_SECONDS",
    "CostTracker",
    "ModelPricing",
    "PricingTable",
    "TurnUsage",
    "active_pricing_table",
    "cost_of",
    "estimate_cost",
    "fetch_live_pricing",
    "infer_provider",
    "load_cached_pricing",
    "pricing_live_enabled",
    "restore_session_cost",
    "save_pricing_cache",
    "set_active_pricing_table",
    "start_live_pricing",
    "sum_prior_cost",
]
