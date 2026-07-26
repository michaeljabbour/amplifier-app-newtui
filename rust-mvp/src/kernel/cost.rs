//! Provider usage → Decimal cost, plus session/turn accounting.
//!
//! Port of `src/amplifier_app_newtui/kernel/cost.py`:
//!
//! - **Decimal end to end** (money is never a float here).
//! - **Offline by default**: the fallback pricing table is the default;
//!   the Helicone live fetch is an explicit opt-in
//!   ([`fetch_live_pricing`]) — unit tests and cold starts never touch
//!   the network.
//! - **Resume re-seed** from the session's `ui-events.jsonl` of
//!   normalized UIEvents (legacy pre-rename `events.jsonl` files are
//!   read too): provider usage events are replayed through the same
//!   pricing math, so a resumed session's footer cost continues from
//!   the prior total.
//!
//! Divergences from the Python source (all recorded, none behavioral
//! for the pinned tests):
//!
//! - Python hardcodes `FALLBACK_PRICING` as a dict literal; here the
//!   same table is embedded as JSON (`cost_fallback_pricing.json`,
//!   `include_str!`) and parsed once. A drift-canary test compares the
//!   embedded values against the literal in the Python source file.
//! - Python's module-global `_active_table` swap relies on the GIL; here
//!   it is an `RwLock<Arc<PricingTable>>` with the same swap semantics
//!   (new turns only — [`CostTracker::start_turn`] snapshots the Arc).
//! - Python's `logger.debug(...)` breadcrumbs are dropped (silent
//!   degradation, same control flow).
//! - `start_live_pricing` spawns a `std::thread` (detached-on-drop is
//!   the Rust default, matching Python's `daemon=True` intent) and the
//!   injected fetch's failure mode is a panic caught with
//!   `catch_unwind` (Python catches `Exception` in the worker).

use std::fs;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::str::FromStr;
use std::sync::{Arc, OnceLock, RwLock};
use std::thread::JoinHandle;
use std::time::{SystemTime, UNIX_EPOCH};

use rust_decimal::Decimal;
use serde_json::{json, Map, Value};

use super::events::{parse_event, usage_from_content_block_end, ProviderResponseUsage, UIEvent};

/// USD per 1K tokens for one model (0 cache prices ⇒ use heuristics).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ModelPricing {
    pub input_per_1k: Decimal,
    pub output_per_1k: Decimal,
    pub cache_read_per_1k: Decimal,
    pub cache_write_per_1k: Decimal,
}

impl ModelPricing {
    /// Python `ModelPricing(input, output)` — cache rates default to 0.
    pub fn new(input_per_1k: Decimal, output_per_1k: Decimal) -> Self {
        Self {
            input_per_1k,
            output_per_1k,
            cache_read_per_1k: Decimal::ZERO,
            cache_write_per_1k: Decimal::ZERO,
        }
    }

    pub fn with_cache(
        input_per_1k: Decimal,
        output_per_1k: Decimal,
        cache_read_per_1k: Decimal,
        cache_write_per_1k: Decimal,
    ) -> Self {
        Self {
            input_per_1k,
            output_per_1k,
            cache_read_per_1k,
            cache_write_per_1k,
        }
    }
}

/// Insertion-ordered `provider → model → pricing` entries (Python uses
/// insertion-ordered dicts; order is the tie-break for equal-length
/// prefix matches).
pub type ProviderEntries = Vec<(String, Vec<(String, ModelPricing)>)>;

/// The Python fallback table, embedded as JSON so a canary test can
/// diff it against the source of truth in the Python package.
const FALLBACK_PRICING_JSON: &str = include_str!("cost_fallback_pricing.json");

/// heuristic: cache read = 10% of input price
fn cache_read_discount() -> Decimal {
    Decimal::new(1, 1) // 0.1
}

fn parse_decimal(text: &str) -> Option<Decimal> {
    let trimmed = text.trim();
    Decimal::from_str(trimmed)
        .ok()
        .or_else(|| Decimal::from_scientific(trimmed).ok())
}

/// Python's `str(value)` for the payload scalars we encounter.
fn py_str(value: &Value) -> String {
    match value {
        Value::String(s) => s.clone(),
        Value::Bool(true) => "True".to_string(),
        Value::Bool(false) => "False".to_string(),
        Value::Number(n) => n.to_string(),
        other => other.to_string(),
    }
}

/// Python's `float(value)` for the cache-payload scalars we encounter.
fn py_float(value: &Value) -> Option<f64> {
    match value {
        Value::Number(n) => n.as_f64(),
        Value::String(s) => s.trim().parse::<f64>().ok(),
        Value::Bool(b) => Some(if *b { 1.0 } else { 0.0 }),
        _ => None,
    }
}

/// Python truthiness for JSON values (the `or 0` fallbacks).
fn is_truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().map(|f| f != 0.0).unwrap_or(true),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

/// `entries.setdefault(provider, {})` over the ordered representation.
fn provider_slot<'a>(
    entries: &'a mut ProviderEntries,
    provider: &str,
) -> &'a mut Vec<(String, ModelPricing)> {
    if let Some(index) = entries.iter().position(|(name, _)| name == provider) {
        &mut entries[index].1
    } else {
        entries.push((provider.to_string(), Vec::new()));
        &mut entries.last_mut().expect("just pushed").1
    }
}

/// `models[model] = pricing` — overwrite keeps the original position.
fn insert_model(models: &mut Vec<(String, ModelPricing)>, model: &str, pricing: ModelPricing) {
    if let Some(slot) = models.iter_mut().find(|(name, _)| name == model) {
        slot.1 = pricing;
    } else {
        models.push((model.to_string(), pricing));
    }
}

/// Minimal hardcoded pricing (per 1K tokens) — offline default, mirrors
/// the streaming-ui module's fallback table (`FALLBACK_PRICING` +
/// the `azure = dict(openai)` mirror).
pub fn fallback_pricing() -> &'static ProviderEntries {
    static ENTRIES: OnceLock<ProviderEntries> = OnceLock::new();
    ENTRIES.get_or_init(|| {
        let payload: Value = serde_json::from_str(FALLBACK_PRICING_JSON)
            .expect("embedded fallback pricing JSON is valid");
        let providers = payload
            .as_object()
            .expect("embedded fallback pricing JSON is an object");
        let mut entries: ProviderEntries = Vec::new();
        for (provider, models) in providers {
            let models = models
                .as_object()
                .expect("embedded fallback pricing providers map models");
            let slot = provider_slot(&mut entries, provider);
            for (model, rates) in models {
                let input = rates
                    .get("input_per_1k")
                    .and_then(|v| v.as_str())
                    .and_then(parse_decimal)
                    .expect("embedded fallback pricing input rate");
                let output = rates
                    .get("output_per_1k")
                    .and_then(|v| v.as_str())
                    .and_then(parse_decimal)
                    .expect("embedded fallback pricing output rate");
                insert_model(slot, model, ModelPricing::new(input, output));
            }
        }
        // Python: FALLBACK_PRICING["azure"] = dict(FALLBACK_PRICING["openai"])
        if let Some(openai) = entries
            .iter()
            .find(|(provider, _)| provider == "openai")
            .map(|(_, models)| models.clone())
        {
            entries.push(("azure".to_string(), openai));
        }
        entries
    })
}

/// Model-pricing lookup with prefix matching (longest prefix wins).
#[derive(Clone, Debug, PartialEq)]
pub struct PricingTable {
    entries: ProviderEntries,
}

impl PricingTable {
    pub fn new(entries: ProviderEntries) -> Self {
        Self { entries }
    }

    pub fn lookup(&self, provider: &str, model: &str) -> Option<&ModelPricing> {
        let provider = provider.to_lowercase();
        let (_, models) = self.entries.iter().find(|(name, _)| *name == provider)?;
        if let Some((_, pricing)) = models.iter().find(|(name, _)| name == model) {
            return Some(pricing);
        }
        let mut best: Option<(usize, &ModelPricing)> = None;
        for (pattern, pricing) in models {
            if model.starts_with(pattern.as_str()) || pattern.starts_with(model) {
                let score = pattern.len();
                if best.is_none_or(|(top, _)| score > top) {
                    best = Some((score, pricing));
                }
            }
        }
        best.map(|(_, pricing)| pricing)
    }
}

impl Default for PricingTable {
    /// Python `PricingTable()` — the fallback entries.
    fn default() -> Self {
        Self::new(fallback_pricing().clone())
    }
}

fn default_pricing_table() -> &'static Arc<PricingTable> {
    static TABLE: OnceLock<Arc<PricingTable>> = OnceLock::new();
    TABLE.get_or_init(|| Arc::new(PricingTable::default()))
}

/// The process-wide pricing table used for NEW turns (Python's
/// `_active_table` module global; the GIL-atomic swap becomes an RwLock).
fn active_table_cell() -> &'static RwLock<Arc<PricingTable>> {
    static ACTIVE: OnceLock<RwLock<Arc<PricingTable>>> = OnceLock::new();
    ACTIVE.get_or_init(|| RwLock::new(default_pricing_table().clone()))
}

/// The pricing table new turns should price against.
pub fn active_pricing_table() -> Arc<PricingTable> {
    active_table_cell()
        .read()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
        .clone()
}

/// Atomically swap the active table (`None` restores the fallback).
pub fn set_active_pricing_table(table: Option<Arc<PricingTable>>) {
    let mut cell = active_table_cell()
        .write()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    *cell = table.unwrap_or_else(|| default_pricing_table().clone());
}

/// Best-effort provider inference from a model name.
pub fn infer_provider(model: &str) -> Option<&'static str> {
    let lowered = model.to_lowercase();
    if lowered.starts_with("claude") {
        return Some("anthropic");
    }
    if ["gpt", "o1", "o3", "o4"]
        .iter()
        .any(|prefix| lowered.starts_with(prefix))
    {
        return Some("openai");
    }
    if lowered.starts_with("gemini") {
        return Some("google");
    }
    None
}

/// Estimate USD cost for one provider response.
///
/// Cache pricing: explicit table rates when present, otherwise cache
/// read = 10% of input price, cache write = input price (the
/// streaming-ui heuristics). Returns `None` when the model is unknown —
/// callers must treat unknown as "no figure", never 0.
pub fn estimate_cost(
    input_tokens: i64,
    output_tokens: i64,
    cache_read_tokens: i64,
    cache_write_tokens: i64,
    provider: Option<&str>,
    model: Option<&str>,
    pricing: Option<&PricingTable>,
) -> Option<Decimal> {
    // Python: `if not model` — None and "" both bail.
    let model = model.filter(|value| !value.is_empty())?;
    // Python: `provider = provider or infer_provider(model)`.
    let inferred;
    let provider = match provider.filter(|value| !value.is_empty()) {
        Some(value) => value,
        None => {
            inferred = infer_provider(model)?;
            inferred
        }
    };
    let table = pricing.unwrap_or_else(|| default_pricing_table());
    let entry = table.lookup(provider, model)?;

    let k = Decimal::ONE_THOUSAND;
    let input_cost = Decimal::from(input_tokens) * entry.input_per_1k / k;
    let output_cost = Decimal::from(output_tokens) * entry.output_per_1k / k;

    let read_rate = if entry.cache_read_per_1k.is_zero() {
        entry.input_per_1k * cache_read_discount()
    } else {
        entry.cache_read_per_1k
    };
    let write_rate = if entry.cache_write_per_1k.is_zero() {
        entry.input_per_1k
    } else {
        entry.cache_write_per_1k
    };
    let cache_read_cost = Decimal::from(cache_read_tokens) * read_rate / k;
    let cache_write_cost = Decimal::from(cache_write_tokens) * write_rate / k;

    Some(input_cost + output_cost + cache_read_cost + cache_write_cost)
}

/// Cost of one normalized `provider_response_usage` event.
///
/// A provider-reported `cost_usd` (loop-streaming's content-block usage
/// payload) is authoritative over the local table estimate.
pub fn cost_of(usage: &ProviderResponseUsage, pricing: Option<&PricingTable>) -> Option<Decimal> {
    if let Some(cost) = usage.cost_usd {
        return Some(cost);
    }
    estimate_cost(
        usage.input_tokens,
        usage.output_tokens,
        usage.cache_read,
        usage.cache_write,
        None,
        Some(&usage.model),
        pricing,
    )
}

// --------------------------------------------------------------------------
// Session / turn accounting
// --------------------------------------------------------------------------

/// Accumulated usage for the turn in flight.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct TurnUsage {
    pub cost: Decimal,
    pub input_tokens: i64,
    pub output_tokens: i64,
    pub cache_read: i64,
    pub cache_write: i64,
    /// Usage records this turn that could not be priced (no table entry
    /// and no provider `cost_usd`) — their $0 makes `cost` a floor, so
    /// renderers must mark the figure (`~$`) instead of lying.
    pub unpriced: i64,
}

impl TurnUsage {
    /// Output tokens — the `↓ X.Xk tok` figure.
    pub fn tokens_down(&self) -> i64 {
        self.output_tokens
    }

    /// % of prompt tokens served from cache (None before any usage).
    /// Python `round()` is banker's rounding — ties go to even.
    pub fn cached_pct(&self) -> Option<i64> {
        let denominator = self.input_tokens + self.cache_read + self.cache_write;
        if denominator <= 0 {
            return None;
        }
        Some((self.cache_read as f64 * 100.0 / denominator as f64).round_ties_even() as i64)
    }
}

/// Running session + per-turn cost from provider usage events.
///
/// Feed every [`ProviderResponseUsage`] to [`CostTracker::record`]; call
/// [`CostTracker::start_turn`] at `prompt:submit` and
/// [`CostTracker::end_turn`] at the turn boundary. `session_cost`
/// includes any resume-seeded prior spend ([`CostTracker::seed`]).
///
/// Pricing table selection: an explicit `pricing` always wins (unit
/// tests, fixed-table callers). Otherwise the tracker snapshots the
/// process-wide [`active_pricing_table`] at `start_turn`, so a
/// live-pricing swap landing mid-session applies to NEW turns only —
/// already-recorded turn costs and the running session total never
/// change retroactively.
#[derive(Debug, Default)]
pub struct CostTracker {
    pub pricing: Option<Arc<PricingTable>>,
    /// Session-total count of usage records that could not be priced.
    pub unpriced: i64,
    session_cost: Decimal,
    turn: TurnUsage,
    turn_pricing: Option<Arc<PricingTable>>,
}

impl CostTracker {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn with_pricing(pricing: Arc<PricingTable>) -> Self {
        Self {
            pricing: Some(pricing),
            ..Self::default()
        }
    }

    pub fn session_cost(&self) -> Decimal {
        self.session_cost
    }

    pub fn turn(&self) -> &TurnUsage {
        &self.turn
    }

    /// Re-seed the session total with pre-resume spend.
    pub fn seed(&mut self, prior_total: Decimal) {
        if prior_total > Decimal::ZERO {
            self.session_cost += prior_total;
        }
    }

    pub fn start_turn(&mut self) {
        self.turn = TurnUsage::default();
        // Snapshot the table for the whole turn (see type docs).
        self.turn_pricing = Some(match &self.pricing {
            Some(pricing) => pricing.clone(),
            None => active_pricing_table(),
        });
    }

    /// Freeze and return the finished turn's usage.
    pub fn end_turn(&mut self) -> TurnUsage {
        let finished = std::mem::take(&mut self.turn);
        self.turn_pricing = None;
        finished
    }

    fn table(&self) -> Arc<PricingTable> {
        if let Some(pricing) = &self.pricing {
            return pricing.clone();
        }
        if let Some(pricing) = &self.turn_pricing {
            return pricing.clone();
        }
        active_pricing_table()
    }

    /// Accumulate one usage event; returns its cost (0 if unpriceable).
    ///
    /// Unpriceable records (unknown model, no provider `cost_usd`)
    /// contribute $0 to the totals but increment `unpriced` — session
    /// and per-turn — so the UI can mark the figures as a floor.
    pub fn record(&mut self, usage: &ProviderResponseUsage) -> Decimal {
        let table = self.table();
        let cost = match cost_of(usage, Some(&table)) {
            Some(cost) => cost,
            None => {
                self.unpriced += 1;
                self.turn.unpriced += 1;
                Decimal::ZERO
            }
        };
        self.session_cost += cost;
        self.turn.cost += cost;
        self.turn.input_tokens += usage.input_tokens;
        self.turn.output_tokens += usage.output_tokens;
        self.turn.cache_read += usage.cache_read;
        self.turn.cache_write += usage.cache_write;
        cost
    }
}

// --------------------------------------------------------------------------
// Resume re-seed from ui-events.jsonl (legacy events.jsonl fallback)
// --------------------------------------------------------------------------

const USAGE_KIND: &str = "provider_response_usage";
const CONTENT_BLOCK_KIND: &str = "content_block_end";

fn add_cost(total: &mut Option<Decimal>, usage: &ProviderResponseUsage, pricing: Option<&PricingTable>) {
    if let Some(cost) = cost_of(usage, pricing) {
        *total = Some(total.unwrap_or(Decimal::ZERO) + cost);
    }
}

/// Sum provider responses in one UIEvent log file exactly once.
///
/// `events_path` is a `ui-events.jsonl` (or pre-rename `events.jsonl`).
/// Reads line-by-line (events files can be large) with a substring
/// pre-filter; foreign records (hooks-logging's colon-named hook events)
/// carry no `kind` and are skipped. Older NewTUI logs wrote the same
/// usage record before every block in one response; the following
/// `content_block_end` identifies whether that record belongs to the
/// response's final block. Standalone provider usage records retain
/// their original behavior. Returns `None` when the file is missing/
/// unreadable or holds no priceable usage. Never fails.
pub fn sum_prior_cost(events_path: &Path, pricing: Option<&PricingTable>) -> Option<Decimal> {
    if !events_path.is_file() {
        return None;
    }
    let file = fs::File::open(events_path).ok()?;
    let reader = BufReader::new(file);

    let mut total: Option<Decimal> = None;
    let mut pending: Option<ProviderResponseUsage> = None;

    for line in reader.lines() {
        // Python catches OSError around the whole read → None.
        let line = line.ok()?;
        if !line.contains(USAGE_KIND) && !line.contains(CONTENT_BLOCK_KIND) {
            if let Some(usage) = pending.take() {
                add_cost(&mut total, &usage, pricing);
            }
            continue;
        }
        let Ok(record) = serde_json::from_str::<Value>(&line) else {
            continue; // malformed JSON: pending survives (Python `continue`)
        };
        if !record.is_object() {
            continue;
        }

        let kind = record.get("kind").and_then(Value::as_str);
        if kind == Some(USAGE_KIND) {
            if let Some(usage) = pending.take() {
                add_cost(&mut total, &usage, pricing);
            }
            // Python `model_validate` (extra="forbid") — parse via the
            // tagged UIEvent enum; malformed records leave pending None.
            pending = match parse_event(&record) {
                Some(UIEvent::ProviderResponseUsage(usage)) => Some(usage),
                _ => None,
            };
            continue;
        }

        if kind != Some(CONTENT_BLOCK_KIND) {
            if let Some(usage) = pending.take() {
                add_cost(&mut total, &usage, pricing);
            }
            continue;
        }

        let block = match parse_event(&record) {
            Some(UIEvent::ContentBlockEnd(block)) => block,
            // Preserve an adjacent usage record on malformed blocks.
            _ => {
                if let Some(usage) = pending.take() {
                    add_cost(&mut total, &usage, pricing);
                }
                continue;
            }
        };

        let final_block = block.total_blocks <= 0 || block.block_index == block.total_blocks - 1;
        if !block.usage.is_empty() {
            let usage = pending.take().or_else(|| usage_from_content_block_end(&block));
            if final_block {
                if let Some(usage) = usage {
                    add_cost(&mut total, &usage, pricing);
                }
            }
        } else if let Some(usage) = pending.take() {
            add_cost(&mut total, &usage, pricing);
        }
        pending = None;
    }
    if let Some(usage) = pending.take() {
        add_cost(&mut total, &usage, pricing);
    }
    total
}

/// Seed `tracker` with the prior spend found across `events_paths`.
///
/// A rename-straddling session splits its UIEvents between the legacy
/// `events.jsonl` and `ui-events.jsonl`, so every file is summed.
/// Returns the restored total, or `None` when there was nothing to
/// restore. Never fails — resume must not break on a bad event log.
pub fn restore_session_cost(tracker: &mut CostTracker, events_paths: &[&Path]) -> Option<Decimal> {
    let totals: Vec<Decimal> = events_paths
        .iter()
        .filter_map(|path| sum_prior_cost(path, tracker.pricing.as_deref()))
        .collect();
    if totals.is_empty() {
        return None;
    }
    let prior: Decimal = totals.iter().sum();
    if prior <= Decimal::ZERO {
        return None;
    }
    tracker.seed(prior);
    Some(prior)
}

// --------------------------------------------------------------------------
// Optional live pricing (explicit opt-in; never called implicitly)
// --------------------------------------------------------------------------

const HELICONE_URL: &str = "https://www.helicone.ai/api/llm-costs";

/// Build a [`PricingTable`] from a Helicone `/api/llm-costs` payload.
/// The pure half of [`fetch_live_pricing`].
pub fn parse_helicone_payload(payload: &Value) -> Option<PricingTable> {
    let k = Decimal::ONE_THOUSAND;
    let rate = |item: &Map<String, Value>, key: &str| -> Option<Decimal> {
        // Python: Decimal(str(item.get(key) or 0)) / _K
        let text = match item.get(key).filter(|value| is_truthy(value)) {
            Some(value) => py_str(value),
            None => "0".to_string(),
        };
        parse_decimal(&text).map(|value| value / k)
    };

    let mut entries: ProviderEntries = Vec::new();
    if let Some(data) = payload.get("data").and_then(Value::as_array) {
        for item in data {
            let Some(item) = item.as_object() else {
                continue;
            };
            let provider = item
                .get("provider")
                .filter(|value| is_truthy(value))
                .map(|value| py_str(value).to_lowercase())
                .unwrap_or_default();
            let model = item
                .get("model")
                .filter(|value| is_truthy(value))
                .map(py_str)
                .unwrap_or_default();
            if provider.is_empty() || model.is_empty() {
                continue;
            }
            let Some(pricing) = (|| {
                Some(ModelPricing::with_cache(
                    rate(item, "input_cost_per_1m")?,
                    rate(item, "output_cost_per_1m")?,
                    rate(item, "prompt_cache_read_per_1m")?,
                    rate(item, "prompt_cache_write_per_1m")?,
                ))
            })() else {
                continue; // InvalidOperation/ValueError → skip the item
            };
            insert_model(provider_slot(&mut entries, &provider), &model, pricing);
        }
    }
    if entries.iter().any(|(provider, _)| provider == "openai")
        && !entries.iter().any(|(provider, _)| provider == "azure")
    {
        let openai = entries
            .iter()
            .find(|(provider, _)| provider == "openai")
            .map(|(_, models)| models.clone())
            .unwrap_or_default();
        entries.push(("azure".to_string(), openai));
    }
    if entries.is_empty() {
        None
    } else {
        Some(PricingTable::new(entries))
    }
}

/// Fetch live pricing from Helicone (explicit opt-in).
///
/// Returns `None` on any failure; callers keep the offline table.
pub fn fetch_live_pricing(timeout: f64) -> Option<PricingTable> {
    let agent = ureq::AgentBuilder::new()
        .timeout(std::time::Duration::from_secs_f64(timeout))
        .build();
    let response = agent
        .get(HELICONE_URL)
        .set("Accept", "application/json")
        .call()
        .ok()?;
    let payload: Value = serde_json::from_reader(response.into_reader()).ok()?;
    parse_helicone_payload(&payload)
}

// --------------------------------------------------------------------------
// On-disk pricing cache + startup wiring (BACKLOG item 1)
// --------------------------------------------------------------------------

/// Fetched-table cache at `~/.amplifier/pricing_cache.json` (the
/// `~/.amplifier` JSON-cache-file convention; Python's
/// `PRICING_CACHE_PATH` module constant becomes a function).
pub fn pricing_cache_path() -> PathBuf {
    let home = std::env::var_os("HOME")
        .or_else(|| std::env::var_os("USERPROFILE"))
        .map(PathBuf::from)
        .unwrap_or_default();
    home.join(".amplifier").join("pricing_cache.json")
}

/// Cache freshness window (24 h — the backlog's default TTL).
pub const PRICING_CACHE_TTL_SECONDS: f64 = 24.0 * 3600.0;

const RATE_FIELDS: [&str; 4] = [
    "input_per_1k",
    "output_per_1k",
    "cache_read_per_1k",
    "cache_write_per_1k",
];

fn system_now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// The cached pricing table, or `None` when missing/stale/corrupt.
///
/// Never fails — a bad cache file simply means "no cache" (callers fall
/// back and refetch).
pub fn load_cached_pricing(
    path: Option<&Path>,
    ttl: Option<f64>,
    now: Option<&dyn Fn() -> f64>,
) -> Option<PricingTable> {
    let cache_path = path.map(Path::to_path_buf).unwrap_or_else(pricing_cache_path);
    let clock = match now {
        Some(clock) => clock(),
        None => system_now(),
    };
    let text = fs::read_to_string(&cache_path).ok()?;
    let payload: Value = serde_json::from_str(&text).ok()?;
    let fetched_at = py_float(payload.get("fetched_at")?)?;
    if clock - fetched_at > ttl.unwrap_or(PRICING_CACHE_TTL_SECONDS) {
        return None;
    }
    let mut entries: ProviderEntries = Vec::new();
    for (provider, models) in payload.get("entries")?.as_object()? {
        for (model, rates) in models.as_object()? {
            let rates = rates.as_object()?;
            let mut values = [Decimal::ZERO; 4];
            for (index, field_name) in RATE_FIELDS.iter().enumerate() {
                values[index] = parse_decimal(&py_str(rates.get(*field_name)?))?;
            }
            insert_model(
                provider_slot(&mut entries, provider),
                model,
                ModelPricing::with_cache(values[0], values[1], values[2], values[3]),
            );
        }
    }
    if entries.is_empty() {
        None
    } else {
        Some(PricingTable::new(entries))
    }
}

/// Persist `table` (Decimal rates as strings). Never fails.
pub fn save_pricing_cache(
    table: &PricingTable,
    path: Option<&Path>,
    now: Option<&dyn Fn() -> f64>,
) -> bool {
    let cache_path = path.map(Path::to_path_buf).unwrap_or_else(pricing_cache_path);
    let clock = match now {
        Some(clock) => clock(),
        None => system_now(),
    };
    let mut entries = Map::new();
    for (provider, models) in &table.entries {
        let mut models_json = Map::new();
        for (model, pricing) in models {
            let mut rates = Map::new();
            rates.insert(
                "input_per_1k".to_string(),
                Value::String(pricing.input_per_1k.to_string()),
            );
            rates.insert(
                "output_per_1k".to_string(),
                Value::String(pricing.output_per_1k.to_string()),
            );
            rates.insert(
                "cache_read_per_1k".to_string(),
                Value::String(pricing.cache_read_per_1k.to_string()),
            );
            rates.insert(
                "cache_write_per_1k".to_string(),
                Value::String(pricing.cache_write_per_1k.to_string()),
            );
            models_json.insert(model.clone(), Value::Object(rates));
        }
        entries.insert(provider.clone(), Value::Object(models_json));
    }
    let payload = json!({"fetched_at": clock, "entries": entries});
    let Ok(text) = serde_json::to_string(&payload) else {
        return false;
    };
    let write = || -> std::io::Result<()> {
        if let Some(parent) = cache_path.parent() {
            if !parent.as_os_str().is_empty() {
                fs::create_dir_all(parent)?;
            }
        }
        let tmp_path = cache_path.with_extension("json.tmp");
        fs::write(&tmp_path, &text)?;
        fs::rename(&tmp_path, &cache_path)
    };
    write().is_ok()
}

/// The `pricing.live` settings key (default: enabled).
pub fn pricing_live_enabled(settings: &Value) -> bool {
    if let Some(Value::Object(section)) = settings.get("pricing") {
        if let Some(Value::Bool(value)) = section.get("live") {
            return *value;
        }
    }
    true
}

/// Injected fetch for [`start_live_pricing`] (tests use fakes; the
/// default is [`fetch_live_pricing`] with the Python 5 s timeout).
pub type PricingFetch = Box<dyn FnOnce() -> Option<Arc<PricingTable>> + Send + 'static>;

/// Wire live pricing at app startup (behind `pricing.live`).
///
/// - `pricing.live: false` → nothing happens; the fallback table stays.
/// - Fresh on-disk cache → activated immediately, no fetch needed.
/// - Stale/missing cache → fallback now, plus a background thread that
///   fetches Helicone, atomically swaps the active table (new turns
///   only — see [`set_active_pricing_table`]) and writes the cache on
///   success.
///
/// Returns the fetch thread's handle when one was started (tests join
/// it); `None` otherwise. Never fails — any failure degrades silently
/// to the fallback table (an injected fetch's panic is caught).
pub fn start_live_pricing(
    settings: &Value,
    cache_path: Option<&Path>,
    fetch: Option<PricingFetch>,
    now: Option<fn() -> f64>,
) -> Option<JoinHandle<()>> {
    if !pricing_live_enabled(settings) {
        return None;
    }
    {
        let now_dyn: Option<&dyn Fn() -> f64> = now.as_ref().map(|f| f as &dyn Fn() -> f64);
        if let Some(cached) = load_cached_pricing(cache_path, None, now_dyn) {
            set_active_pricing_table(Some(Arc::new(cached)));
            return None;
        }
    }
    let fetch_fn: PricingFetch =
        fetch.unwrap_or_else(|| Box::new(|| fetch_live_pricing(5.0).map(Arc::new)));
    let cache_path = cache_path.map(Path::to_path_buf);
    std::thread::Builder::new()
        .name("pricing-fetch".to_string())
        .spawn(move || {
            // Python's worker catches Exception; a panicking injected
            // fetch must never take the app down.
            let _ = std::panic::catch_unwind(std::panic::AssertUnwindSafe(move || {
                let Some(table) = fetch_fn() else {
                    return; // fetch failed/timed out → keep the fallback silently
                };
                set_active_pricing_table(Some(table.clone()));
                let now_dyn: Option<&dyn Fn() -> f64> =
                    now.as_ref().map(|f| f as &dyn Fn() -> f64);
                save_pricing_cache(&table, cache_path.as_deref(), now_dyn);
            }));
        })
        .ok()
}

// --------------------------------------------------------------------------
// Tests — ports of tests/test_kernel_cost.py and
// tests/test_cost_parity_appcli.py, plus the embedded-table drift canary.
// --------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use crate::kernel::events::{normalize, ContentBlockEnd, Payload};
    use serde_json::json;
    use std::collections::BTreeMap;
    use std::sync::{Mutex, MutexGuard};

    fn d(text: &str) -> Decimal {
        Decimal::from_str(text).unwrap()
    }

    fn obj(value: Value) -> Payload {
        match value {
            Value::Object(map) => map,
            _ => panic!("payload literal must be a JSON object"),
        }
    }

    /// Serialize the active-table tests (Python runs pytest serially;
    /// Rust tests share the process-wide table across threads). Every
    /// caller starts from the pristine fallback.
    fn active_table_guard() -> MutexGuard<'static, ()> {
        static LOCK: Mutex<()> = Mutex::new(());
        let guard = LOCK.lock().unwrap_or_else(|poisoned| poisoned.into_inner());
        set_active_pricing_table(None);
        guard
    }

    /// Python's module-level `_EXPENSIVE` table.
    fn expensive_table() -> Arc<PricingTable> {
        Arc::new(PricingTable::new(vec![(
            "anthropic".to_string(),
            vec![(
                "claude-sonnet-4".to_string(),
                ModelPricing::new(Decimal::ONE, Decimal::ONE),
            )],
        )]))
    }

    fn usage_with_model(
        input_tokens: i64,
        output_tokens: i64,
        cache_read: i64,
        cache_write: i64,
        model: &str,
    ) -> ProviderResponseUsage {
        ProviderResponseUsage {
            session_id: "s1".to_string(),
            input_tokens,
            output_tokens,
            cache_read,
            cache_write,
            model: model.to_string(),
            ..ProviderResponseUsage::default()
        }
    }

    fn usage(input_tokens: i64, output_tokens: i64) -> ProviderResponseUsage {
        usage_with_model(input_tokens, output_tokens, 0, 0, "claude-sonnet-4")
    }

    // -- estimate_cost ------------------------------------------------------

    #[test]
    fn test_estimate_cost_known_model_exact_decimal() {
        let cost = estimate_cost(1000, 1000, 0, 0, None, Some("claude-sonnet-4-5"), None);
        // 1k * $0.003 + 1k * $0.015 — exact Decimal, no float drift
        assert_eq!(cost, Some(d("0.018")));
    }

    #[test]
    fn test_estimate_cost_cache_heuristics() {
        // cache read = 10% of input price; cache write = input price
        let read_only = estimate_cost(0, 0, 1000, 0, None, Some("claude-sonnet-4"), None);
        assert_eq!(read_only, Some(d("0.0003")));
        let write_only = estimate_cost(0, 0, 0, 1000, None, Some("claude-sonnet-4"), None);
        assert_eq!(write_only, Some(d("0.003")));
    }

    #[test]
    fn test_estimate_cost_unknown_model_returns_none() {
        assert_eq!(estimate_cost(10, 10, 0, 0, None, Some(""), None), None);
        assert_eq!(
            estimate_cost(10, 10, 0, 0, None, Some("mystery-model-9000"), None),
            None
        );
    }

    #[test]
    fn test_infer_provider() {
        assert_eq!(infer_provider("claude-sonnet-4-5"), Some("anthropic"));
        assert_eq!(infer_provider("gpt-4o-mini"), Some("openai"));
        assert_eq!(infer_provider("o1-preview"), Some("openai"));
        assert_eq!(infer_provider("gemini-2.0-flash-exp"), Some("google"));
        assert_eq!(infer_provider("llama-3"), None);
    }

    #[test]
    fn test_pricing_table_longest_prefix_wins() {
        let table = PricingTable::default();
        let mini = table.lookup("openai", "gpt-4o-mini").unwrap();
        let full = table.lookup("openai", "gpt-4o").unwrap();
        assert_eq!(mini.input_per_1k, d("0.00015")); // not the gpt-4o rate
        assert_eq!(full.input_per_1k, d("0.0025"));
    }

    #[test]
    fn test_azure_mirrors_openai() {
        let table = PricingTable::default();
        assert_eq!(table.lookup("azure", "gpt-4o"), table.lookup("openai", "gpt-4o"));
    }

    // -- CostTracker ---------------------------------------------------------

    #[test]
    fn test_cost_tracker_accumulates_session_and_turn() {
        let _guard = active_table_guard();
        let mut tracker = CostTracker::new();
        tracker.start_turn();
        let first = tracker.record(&usage(1000, 1000));
        assert_eq!(first, d("0.018"));
        tracker.record(&usage(1000, 0));

        assert_eq!(tracker.turn().cost, d("0.021"));
        assert_eq!(tracker.turn().tokens_down(), 1000);
        assert_eq!(tracker.session_cost(), d("0.021"));

        let finished = tracker.end_turn();
        assert_eq!(finished.cost, d("0.021"));
        // turn reset, session total kept
        assert_eq!(tracker.turn().cost, Decimal::ZERO);
        assert_eq!(tracker.session_cost(), d("0.021"));
    }

    #[test]
    fn test_cached_pct() {
        let _guard = active_table_guard();
        let mut tracker = CostTracker::new();
        tracker.start_turn();
        assert_eq!(tracker.turn().cached_pct(), None); // no usage yet
        tracker.record(&usage_with_model(250, 0, 750, 0, "claude-sonnet-4"));
        assert_eq!(tracker.turn().cached_pct(), Some(75));
    }

    #[test]
    fn test_unpriceable_usage_counts_tokens_but_zero_cost() {
        let _guard = active_table_guard();
        let mut tracker = CostTracker::new();
        let cost = tracker.record(&usage_with_model(0, 500, 0, 0, "mystery"));
        assert_eq!(cost, Decimal::ZERO);
        assert_eq!(tracker.session_cost(), Decimal::ZERO);
        assert_eq!(tracker.turn().tokens_down(), 500);
    }

    #[test]
    fn test_seed_adds_prior_spend() {
        let _guard = active_table_guard();
        let mut tracker = CostTracker::new();
        tracker.seed(d("1.25"));
        tracker.record(&usage(1000, 0));
        assert_eq!(tracker.session_cost(), d("1.253"));
    }

    // -- Resume re-seed from ui-events.jsonl ---------------------------------

    /// Python's `_events_file_with_usage` writes through the real
    /// SessionStore (not ported); the same `ui-events.jsonl` lines are
    /// written directly here — `append_event` is one
    /// `model_dump(mode="json")` JSON object per line, which is exactly
    /// `serde_json::to_string(&UIEvent)`.
    fn events_file_with_usage(dir: &Path) -> PathBuf {
        let path = dir.join("ui-events.jsonl");
        let mut lines: Vec<String> = Vec::new();
        for _ in 0..2 {
            let payload = obj(json!({
                "session_id": "s1",
                "usage": {"input_tokens": 1000, "output_tokens": 1000},
                "model": "claude-sonnet-4",
            }));
            let event = normalize("provider:response", Some(&payload)).unwrap();
            lines.push(serde_json::to_string(&event).unwrap());
        }
        // noise the reader must skip
        lines.push(r#"{"kind": "session_start", "session_id": "s1"}"#.to_string());
        lines.push("corrupt line provider_response_usage".to_string());
        fs::write(&path, lines.join("\n") + "\n").unwrap();
        path
    }

    #[test]
    fn test_sum_prior_cost_replays_usage_events() {
        let tmp = tempfile::tempdir().unwrap();
        let events_path = events_file_with_usage(tmp.path());
        let total = sum_prior_cost(&events_path, None);
        assert_eq!(total, Some(d("0.036"))); // 2 × $0.018
    }

    #[test]
    fn test_sum_prior_cost_repairs_legacy_per_block_usage_duplication() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("ui-events.jsonl");
        let usage_event = ProviderResponseUsage {
            session_id: "s1".to_string(),
            input_tokens: 1000,
            output_tokens: 1000,
            model: "claude-sonnet-4".to_string(),
            cost_usd: Some(d("0.42")),
            ..ProviderResponseUsage::default()
        };
        let raw_usage = obj(json!({
            "input_tokens": 1000,
            "output_tokens": 1000,
            "model": "claude-sonnet-4",
            "cost_usd": "0.42",
        }));
        let mut lines: Vec<String> = Vec::new();
        for (index, block_type) in ["thinking", "text", "tool_use"].iter().enumerate() {
            lines.push(
                serde_json::to_string(&UIEvent::ProviderResponseUsage(usage_event.clone()))
                    .unwrap(),
            );
            lines.push(
                serde_json::to_string(&UIEvent::ContentBlockEnd(ContentBlockEnd {
                    session_id: "s1".to_string(),
                    block_type: block_type.to_string(),
                    block_index: index as i64,
                    total_blocks: 3,
                    block: obj(json!({"type": block_type})),
                    usage: raw_usage.clone(),
                    ..ContentBlockEnd::default()
                }))
                .unwrap(),
            );
        }
        fs::write(&path, lines.join("\n") + "\n").unwrap();

        assert_eq!(sum_prior_cost(&path, None), Some(d("0.42")));
    }

    #[test]
    fn test_sum_prior_cost_reads_final_block_usage_without_synthetic_record() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("ui-events.jsonl");
        let line = serde_json::to_string(&UIEvent::ContentBlockEnd(ContentBlockEnd {
            session_id: "s1".to_string(),
            block_type: "text".to_string(),
            block_index: 0,
            total_blocks: 1,
            block: obj(json!({"type": "text", "text": "done"})),
            usage: obj(json!({"cost_usd": "0.55"})),
            ..ContentBlockEnd::default()
        }))
        .unwrap();
        fs::write(&path, line + "\n").unwrap();

        assert_eq!(sum_prior_cost(&path, None), Some(d("0.55")));
    }

    #[test]
    fn test_sum_prior_cost_missing_or_empty() {
        let tmp = tempfile::tempdir().unwrap();
        assert_eq!(sum_prior_cost(&tmp.path().join("nope.jsonl"), None), None);
        let empty = tmp.path().join("empty.jsonl");
        fs::write(&empty, "").unwrap();
        assert_eq!(sum_prior_cost(&empty, None), None);
    }

    #[test]
    fn test_restore_session_cost_seeds_tracker() {
        let tmp = tempfile::tempdir().unwrap();
        let events_path = events_file_with_usage(tmp.path());
        let mut tracker = CostTracker::new();
        let restored = restore_session_cost(&mut tracker, &[&events_path]);
        assert_eq!(restored, Some(d("0.036")));
        assert_eq!(tracker.session_cost(), d("0.036"));
        // per-turn state untouched by the re-seed
        assert_eq!(tracker.turn().cost, Decimal::ZERO);
    }

    #[test]
    fn test_restore_session_cost_no_prior_is_noop() {
        let tmp = tempfile::tempdir().unwrap();
        let mut tracker = CostTracker::new();
        assert_eq!(
            restore_session_cost(&mut tracker, &[&tmp.path().join("nope.jsonl")]),
            None
        );
    }

    /// A session written before the ui-events.jsonl rename re-seeds from
    /// its events.jsonl (mixed foreign hook records and corrupt lines
    /// skipped). Python's SessionStore `events_path` legacy-name
    /// resolution is that unit's behavior — the legacy file path is
    /// passed directly here.
    #[test]
    fn test_restore_from_legacy_only_session_dir() {
        let tmp = tempfile::tempdir().unwrap();
        let legacy_path = tmp.path().join("events.jsonl");
        let usage_event = ProviderResponseUsage {
            session_id: "s1".to_string(),
            model: "claude-sonnet-4".to_string(),
            cost_usd: Some(d("0.42")),
            ..ProviderResponseUsage::default()
        };
        let content = format!(
            "{}\n{}\ncorrupt line provider_response_usage\n",
            r#"{"ts": "2026-07-21T00:00:00Z", "event": "provider:response", "data": {}}"#,
            serde_json::to_string(&UIEvent::ProviderResponseUsage(usage_event)).unwrap(),
        );
        fs::write(&legacy_path, content).unwrap();

        let mut tracker = CostTracker::new();
        assert_eq!(
            restore_session_cost(&mut tracker, &[&legacy_path]),
            Some(d("0.42"))
        );
        assert_eq!(tracker.session_cost(), d("0.42"));
    }

    /// A legacy session resumed under this build splits usage across
    /// events.jsonl and ui-events.jsonl — both halves count once.
    #[test]
    fn test_restore_sums_split_legacy_and_current_files() {
        let tmp = tempfile::tempdir().unwrap();
        let legacy_path = tmp.path().join("events.jsonl");
        let current_path = tmp.path().join("ui-events.jsonl");
        let legacy_usage = ProviderResponseUsage {
            session_id: "s1".to_string(),
            model: "claude-sonnet-4".to_string(),
            cost_usd: Some(d("0.42")),
            ..ProviderResponseUsage::default()
        };
        fs::write(
            &legacy_path,
            serde_json::to_string(&UIEvent::ProviderResponseUsage(legacy_usage)).unwrap() + "\n",
        )
        .unwrap();
        let current_usage = ProviderResponseUsage {
            session_id: "s1".to_string(),
            model: "claude-sonnet-4".to_string(),
            cost_usd: Some(d("0.55")),
            ..ProviderResponseUsage::default()
        };
        fs::write(
            &current_path,
            serde_json::to_string(&UIEvent::ProviderResponseUsage(current_usage)).unwrap() + "\n",
        )
        .unwrap();

        let mut tracker = CostTracker::new();
        assert_eq!(
            restore_session_cost(&mut tracker, &[&legacy_path, &current_path]),
            Some(d("0.97"))
        );
        assert_eq!(tracker.session_cost(), d("0.97"));
    }

    #[test]
    fn test_cost_of_normalized_event_flat_usage_keys() {
        // normalize() absorbs flat usage payloads too
        let payload = obj(json!({
            "session_id": "s1",
            "input_tokens": 1000,
            "output_tokens": 1000,
            "model": "claude-opus-4",
        }));
        let event = normalize("provider:response", Some(&payload)).unwrap();
        let UIEvent::ProviderResponseUsage(event) = event else {
            panic!("expected ProviderResponseUsage, got {event:?}");
        };
        assert_eq!(cost_of(&event, None), Some(d("0.09"))); // 0.015 + 0.075
    }

    #[test]
    fn test_cache_key_variants_price_identically() {
        for cache_key in ["cache_read_input_tokens", "cache_read"] {
            let payload = obj(json!({
                "session_id": "s1",
                "usage": {cache_key: 1000},
                "model": "claude-sonnet-4",
            }));
            let event = normalize("provider:response", Some(&payload)).unwrap();
            let UIEvent::ProviderResponseUsage(event) = event else {
                panic!("expected ProviderResponseUsage for {cache_key}");
            };
            assert_eq!(cost_of(&event, None), Some(d("0.0003")), "{cache_key}");
        }
    }

    // -- Unpriced counter — never lie in the footer (BACKLOG item 1) ---------

    #[test]
    fn test_unpriced_counter_counts_records_that_could_not_be_priced() {
        let _guard = active_table_guard();
        let mut tracker = CostTracker::new();
        tracker.start_turn();
        tracker.record(&usage_with_model(0, 500, 0, 0, "mystery-model-9000"));
        tracker.record(&usage(1000, 1000)); // priceable
        assert_eq!(tracker.unpriced, 1);
        assert_eq!(tracker.turn().unpriced, 1);

        let finished = tracker.end_turn();
        assert_eq!(finished.unpriced, 1);
        // per-turn count resets; the session counter is sticky
        assert_eq!(tracker.turn().unpriced, 0);
        assert_eq!(tracker.unpriced, 1);
    }

    #[test]
    fn test_provider_reported_cost_usd_counts_as_priced() {
        let _guard = active_table_guard();
        let mut tracker = CostTracker::new();
        let usage = ProviderResponseUsage {
            session_id: "s1".to_string(),
            output_tokens: 500,
            model: "mystery-model-9000".to_string(),
            cost_usd: Some(d("0.42")),
            ..ProviderResponseUsage::default()
        };
        assert_eq!(tracker.record(&usage), d("0.42"));
        assert_eq!(tracker.unpriced, 0);
    }

    // -- Active pricing table — atomic swap, new turns only -------------------

    #[test]
    fn test_active_table_defaults_to_fallback_and_none_resets() {
        let _guard = active_table_guard();
        let default = active_pricing_table();
        assert!(default.lookup("anthropic", "claude-sonnet-4").is_some());
        let expensive = expensive_table();
        set_active_pricing_table(Some(expensive.clone()));
        assert!(Arc::ptr_eq(&active_pricing_table(), &expensive));
        set_active_pricing_table(None);
        assert!(Arc::ptr_eq(&active_pricing_table(), &default));
    }

    #[test]
    fn test_table_swap_applies_to_new_turns_only() {
        let _guard = active_table_guard();
        let mut tracker = CostTracker::new();
        tracker.start_turn();
        tracker.record(&usage(1000, 0)); // fallback: $0.003
        set_active_pricing_table(Some(expensive_table()));
        // Mid-turn swap: the running turn keeps its snapshot table.
        tracker.record(&usage(1000, 0)); // still $0.003
        assert_eq!(tracker.session_cost(), d("0.006"));
        tracker.end_turn();
        tracker.start_turn();
        tracker.record(&usage(1000, 0)); // new turn: $1.00
        assert_eq!(tracker.session_cost(), d("1.006"));
        set_active_pricing_table(None);
    }

    #[test]
    fn test_explicit_tracker_pricing_wins_over_active_table() {
        let _guard = active_table_guard();
        set_active_pricing_table(Some(expensive_table()));
        let mut tracker = CostTracker::with_pricing(Arc::new(PricingTable::default()));
        tracker.start_turn();
        assert_eq!(tracker.record(&usage(1000, 0)), d("0.003"));
        set_active_pricing_table(None);
    }

    // -- On-disk pricing cache (24h TTL; never raises) ------------------------

    #[test]
    fn test_pricing_cache_roundtrip_preserves_decimal_rates() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("pricing_cache.json");
        assert!(save_pricing_cache(&expensive_table(), Some(&path), Some(&|| 1000.0)));
        let loaded = load_cached_pricing(Some(&path), None, Some(&|| 1000.0 + 60.0)).unwrap();
        let entry = loaded.lookup("anthropic", "claude-sonnet-4").unwrap();
        assert_eq!(*entry, ModelPricing::new(Decimal::ONE, Decimal::ONE));
    }

    #[test]
    fn test_pricing_cache_stale_missing_or_corrupt_returns_none() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("pricing_cache.json");
        assert!(load_cached_pricing(Some(&path), None, None).is_none()); // missing
        assert!(save_pricing_cache(&expensive_table(), Some(&path), Some(&|| 1000.0)));
        let stale_at = 1000.0 + 24.0 * 3600.0 + 1.0;
        assert!(load_cached_pricing(Some(&path), None, Some(&move || stale_at)).is_none()); // stale
        fs::write(&path, "{not json").unwrap();
        assert!(load_cached_pricing(Some(&path), None, Some(&|| 1000.0)).is_none()); // corrupt
        fs::write(&path, r#"{"fetched_at": "soon"}"#).unwrap();
        assert!(load_cached_pricing(Some(&path), None, Some(&|| 1000.0)).is_none()); // malformed
    }

    #[test]
    fn test_pricing_cache_write_failure_never_raises() {
        // A directory where the cache file should be → the rename fails.
        let tmp = tempfile::tempdir().unwrap();
        let blocked = tmp.path().join("pricing_cache.json");
        fs::create_dir(&blocked).unwrap();
        assert!(!save_pricing_cache(&expensive_table(), Some(&blocked), None));
    }

    // -- Startup wiring — pricing.live settings gate + background fetch -------

    #[test]
    fn test_pricing_live_enabled_defaults_true() {
        assert!(pricing_live_enabled(&json!({})));
        assert!(pricing_live_enabled(&json!({"pricing": {}})));
        assert!(pricing_live_enabled(&json!({"pricing": "garbage"})));
        assert!(pricing_live_enabled(&json!({"pricing": {"live": true}})));
        assert!(!pricing_live_enabled(&json!({"pricing": {"live": false}})));
    }

    #[test]
    fn test_start_live_pricing_disabled_never_fetches() {
        let _guard = active_table_guard();
        let tmp = tempfile::tempdir().unwrap();
        let default = active_pricing_table();
        let fetch: PricingFetch =
            Box::new(|| panic!("fetch must not run when pricing.live is false"));
        let thread = start_live_pricing(
            &json!({"pricing": {"live": false}}),
            Some(&tmp.path().join("pricing_cache.json")),
            Some(fetch),
            None,
        );
        assert!(thread.is_none());
        assert!(Arc::ptr_eq(&active_pricing_table(), &default));
    }

    #[test]
    fn test_start_live_pricing_fresh_cache_short_circuits_fetch() {
        let _guard = active_table_guard();
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("pricing_cache.json");
        assert!(save_pricing_cache(&expensive_table(), Some(&path), None));

        let fetch: PricingFetch = Box::new(|| panic!("fresh cache must skip the network fetch"));
        let thread = start_live_pricing(&json!({}), Some(&path), Some(fetch), None);
        assert!(thread.is_none());
        assert_eq!(
            active_pricing_table().lookup("anthropic", "claude-sonnet-4"),
            Some(&ModelPricing::new(Decimal::ONE, Decimal::ONE))
        );
        set_active_pricing_table(None);
    }

    #[test]
    fn test_start_live_pricing_fetch_success_swaps_table_and_writes_cache() {
        let _guard = active_table_guard();
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("pricing_cache.json");
        let expensive = expensive_table();
        let fetched = expensive.clone();
        let thread = start_live_pricing(
            &json!({}),
            Some(&path),
            Some(Box::new(move || Some(fetched))),
            None,
        );
        // Python also asserts `thread.daemon`; Rust threads never block
        // process exit, so daemon-ness holds by construction.
        let thread = thread.expect("a fetch thread must start");
        thread.join().unwrap();
        assert!(Arc::ptr_eq(&active_pricing_table(), &expensive));
        assert!(load_cached_pricing(Some(&path), None, None).is_some());
        set_active_pricing_table(None);
    }

    #[test]
    fn test_start_live_pricing_fetch_failure_keeps_fallback_silently() {
        let _guard = active_table_guard();
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("pricing_cache.json");
        let default = active_pricing_table();

        let thread = start_live_pricing(&json!({}), Some(&path), Some(Box::new(|| None)), None);
        thread.expect("a fetch thread must start").join().unwrap();
        assert!(Arc::ptr_eq(&active_pricing_table(), &default));
        assert!(!path.exists());

        // The panic never escapes the worker (Python: raise inside the
        // daemon thread is swallowed by its except Exception).
        let boom: PricingFetch = Box::new(|| panic!("network exploded"));
        let thread = start_live_pricing(&json!({}), Some(&path), Some(boom), None);
        thread.expect("a fetch thread must start").join().unwrap();
        assert!(Arc::ptr_eq(&active_pricing_table(), &default));
        assert!(!path.exists());
    }

    // -- Parity with amplifier-app-cli's estimator (test_cost_parity_appcli.py)

    /// (provider, model, input, output, cache_read, cache_write, app-cli
    /// total) — the reference estimator's float results, verbatim (repr).
    const PARITY_FIXTURES: [(&str, &str, i64, i64, i64, i64, &str); 9] = [
        ("anthropic", "claude-sonnet-4-5", 12_345, 6_789, 100_000, 2_048, "0.175014"),
        ("anthropic", "claude-opus-4-1", 50_000, 10_000, 0, 25_000, "1.875"),
        ("anthropic", "claude-3-5-haiku-20241022", 400_000, 100_000, 350_000, 0, "0.748"),
        ("openai", "gpt-4o", 100_000, 50_000, 0, 10_000, "0.775"),
        ("openai", "o3-mini", 8_192, 4_096, 0, 0, "0.0270336"),
        ("openai", "o1-preview", 5_000, 15_000, 0, 0, "0.9749999999999999"),
        ("google", "gemini-2.0-flash-exp", 2_000_000, 500_000, 1_000_000, 0, "0.41000000000000003"),
        ("google", "gemini-1.5-pro", 123_456, 65_432, 0, 0, "0.48148"),
        ("azure", "gpt-4o", 250_000, 125_000, 80_000, 0, "1.895"),
    ];

    #[test]
    fn test_estimate_cost_matches_appcli_estimator() {
        let tolerance = d("0.000001");
        for (provider, model, input_tokens, output_tokens, cache_read, cache_write, expected) in
            PARITY_FIXTURES
        {
            let cost = estimate_cost(
                input_tokens,
                output_tokens,
                cache_read,
                cache_write,
                Some(provider),
                Some(model),
                None,
            );
            let cost = cost
                .unwrap_or_else(|| panic!("{provider}/{model} must be priceable on the fallback table"));
            let delta = (cost - d(expected)).abs();
            assert!(
                delta <= tolerance,
                "{provider}/{model}: newtui ${cost} vs app-cli ${expected} (Δ={delta})"
            );
        }
    }

    /// app-cli passes provider explicitly; newtui may infer it from the
    /// model name — both paths must price identically for the fixtures.
    #[test]
    fn test_provider_inference_matches_explicit_provider() {
        for (provider, model, inp, out, cread, cwrite, _expected) in PARITY_FIXTURES {
            if provider == "azure" {
                continue; // azure is never inferable from a model name
            }
            let explicit =
                estimate_cost(inp, out, cread, cwrite, Some(provider), Some(model), None);
            let inferred = estimate_cost(inp, out, cread, cwrite, None, Some(model), None);
            assert_eq!(explicit, inferred);
        }
    }

    // -- Drift canary: embedded table vs the Python source of truth -----------

    /// The Python package keeps `FALLBACK_PRICING` as a hardcoded dict
    /// literal in `kernel/cost.py` (there is no JSON data file to
    /// byte-compare against), so this canary extracts the literal's
    /// `ModelPricing(_p("..."), _p("..."))` rates from the source and
    /// diffs them against the embedded `cost_fallback_pricing.json`.
    /// Skips gracefully when the Python source is absent.
    #[test]
    fn test_embedded_fallback_pricing_matches_python_source() {
        let py_path = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("..")
            .join("src/amplifier_app_newtui/kernel/cost.py");
        if !py_path.is_file() {
            eprintln!("skipping drift canary: {} not present", py_path.display());
            return;
        }
        let source = fs::read_to_string(&py_path).unwrap();

        let header = "FALLBACK_PRICING: dict[str, dict[str, ModelPricing]] = {";
        let start = source
            .find(header)
            .expect("FALLBACK_PRICING literal present in Python cost.py");
        let end = source[start..]
            .find("\n}\n")
            .expect("FALLBACK_PRICING literal terminator")
            + start;
        let block = &source[start..end];

        let provider_re = regex::Regex::new(r#"^    "([^"]+)": \{$"#).unwrap();
        let entry_re = regex::Regex::new(
            r#"^        "([^"]+)": ModelPricing\(_p\("([^"]+)"\), _p\("([^"]+)"\)\),"#,
        )
        .unwrap();
        let mut python_table: BTreeMap<String, BTreeMap<String, (Decimal, Decimal)>> =
            BTreeMap::new();
        let mut current: Option<String> = None;
        for line in block.lines() {
            if let Some(captures) = provider_re.captures(line) {
                current = Some(captures[1].to_string());
                python_table.entry(captures[1].to_string()).or_default();
            } else if let Some(captures) = entry_re.captures(line) {
                let provider = current.clone().expect("entry line inside a provider block");
                python_table.entry(provider).or_default().insert(
                    captures[1].to_string(),
                    (d(&captures[2]), d(&captures[3])),
                );
            }
        }
        assert!(
            !python_table.is_empty()
                && python_table.values().map(|models| models.len()).sum::<usize>() > 0,
            "canary regex extracted nothing — the Python literal's shape drifted"
        );

        let embedded: Value = serde_json::from_str(FALLBACK_PRICING_JSON).unwrap();
        let mut embedded_table: BTreeMap<String, BTreeMap<String, (Decimal, Decimal)>> =
            BTreeMap::new();
        for (provider, models) in embedded.as_object().unwrap() {
            let slot = embedded_table.entry(provider.clone()).or_default();
            for (model, rates) in models.as_object().unwrap() {
                slot.insert(
                    model.clone(),
                    (
                        d(rates["input_per_1k"].as_str().unwrap()),
                        d(rates["output_per_1k"].as_str().unwrap()),
                    ),
                );
            }
        }

        assert_eq!(
            embedded_table, python_table,
            "embedded cost_fallback_pricing.json drifted from FALLBACK_PRICING in cost.py"
        );
        // The azure mirror is code, not data, on both sides.
        assert!(
            source.contains(r#"FALLBACK_PRICING["azure"] = dict(FALLBACK_PRICING["openai"])"#),
            "Python dropped/changed its azure = openai mirror; update fallback_pricing()"
        );
        let table = PricingTable::default();
        assert_eq!(table.lookup("azure", "gpt-4o"), table.lookup("openai", "gpt-4o"));
    }
}
