"""Provider usage → Decimal cost, plus session/turn accounting.

Port of ``estimate_cost()`` from amplifier-module-hooks-streaming-ui
``cost.py`` with three changes for the TUI:

- **Decimal end to end** (money is never a float here).
- **Offline by default**: the hardcoded fallback pricing table is the
  default; the Helicone live fetch is an explicit opt-in
  (:func:`fetch_live_pricing`) — unit tests and cold starts never touch
  the network.
- **Resume re-seed** from the session's ``events.jsonl`` of normalized
  UIEvents (the ``cost_history.py`` pattern): provider usage events are
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
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .events import ProviderResponseUsage

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
    """

    pricing: PricingTable = field(default_factory=PricingTable)
    _session_cost: Decimal = Decimal("0")
    _turn: TurnUsage = field(default_factory=TurnUsage)

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

    def end_turn(self) -> TurnUsage:
        """Freeze and return the finished turn's usage."""
        finished = self._turn
        self._turn = TurnUsage()
        return finished

    def record(self, usage: ProviderResponseUsage) -> Decimal:
        """Accumulate one usage event; returns its cost (0 if unpriceable)."""
        cost = cost_of(usage, self.pricing) or Decimal("0")
        self._session_cost += cost
        self._turn.cost += cost
        self._turn.input_tokens += usage.input_tokens
        self._turn.output_tokens += usage.output_tokens
        self._turn.cache_read += usage.cache_read
        self._turn.cache_write += usage.cache_write
        return cost


# --------------------------------------------------------------------------
# Resume re-seed from events.jsonl
# --------------------------------------------------------------------------

_USAGE_KIND = "provider_response_usage"


def sum_prior_cost(events_path: Path, pricing: PricingTable | None = None) -> Decimal | None:
    """Sum the cost of every usage event in a session's events.jsonl.

    Reads line-by-line (events files can be large) with a substring
    pre-filter, replaying each ``provider_response_usage`` record
    through the same pricing math. Returns ``None`` when the file is
    missing/unreadable or holds no priceable usage. Never raises.
    """
    if not events_path.is_file():
        return None

    total: Decimal | None = None
    try:
        with events_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if _USAGE_KIND not in line:
                    continue
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if not isinstance(record, dict) or record.get("kind") != _USAGE_KIND:
                    continue
                try:
                    usage = ProviderResponseUsage.model_validate(record)
                except Exception:  # noqa: BLE001 — skip malformed records
                    continue
                cost = cost_of(usage, pricing)
                if cost is None:
                    continue
                total = (total or Decimal("0")) + cost
    except OSError:
        logger.debug("Could not read events for prior cost: %s", events_path, exc_info=True)
        return None
    return total


def restore_session_cost(
    tracker: CostTracker, events_path: Path
) -> Decimal | None:
    """Seed *tracker* with the prior spend found in *events_path*.

    Returns the restored total, or ``None`` when there was nothing to
    restore. Never raises — resume must not break on a bad event log.
    """
    prior = sum_prior_cost(events_path, tracker.pricing)
    if prior is None or prior <= 0:
        return None
    tracker.seed(prior)
    logger.info("Restored prior session cost $%s from %s", prior, events_path)
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
        request = urllib.request.Request(
            _HELICONE_URL, headers={"Accept": "application/json"}
        )
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


__all__ = [
    "FALLBACK_PRICING",
    "CostTracker",
    "ModelPricing",
    "PricingTable",
    "TurnUsage",
    "cost_of",
    "estimate_cost",
    "fetch_live_pricing",
    "infer_provider",
    "restore_session_cost",
    "sum_prior_cost",
]
