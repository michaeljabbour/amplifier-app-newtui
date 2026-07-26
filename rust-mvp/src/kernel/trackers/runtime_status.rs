//! Runtime status tracker: turn boundaries + provider usage/notices.
//!
//! Hook-tracker pattern. Kernel `SessionStatus` counters are NOT populated
//! (RESEARCH-BRIEF §2), so this tracker accumulates provider usage itself
//! from `provider:response` payloads (normalized through
//! [`crate::kernel::events::normalize`], absorbing flat/nested usage
//! shapes).
//!
//! Turn boundaries follow the ROOT session's `prompt:submit` /
//! `prompt:complete` / `execution:end`; usage from child sessions still
//! counts toward the running turn and the session totals (the parent pays
//! for its agents). Cost is computed per usage event by an injectable
//! `cost_fn` (kept out of this module so pricing tables live in one place).
//!
//! Port of `src/amplifier_app_newtui/kernel/trackers/runtime_status.py`.
//! Python's `async def handle_event` hook is a synchronous method here (no
//! async runtime in the crate); its `HookResult(action="continue")` return
//! is mirrored minimally as a private struct — this tracker never blocks
//! the hook chain.

use std::sync::{Arc, Mutex, OnceLock};
use std::time::Instant;

use rust_decimal::Decimal;

use crate::kernel::events::{normalize, Payload, ProviderNotice, ProviderResponseUsage, UIEvent};

/// Handle returned by [`RuntimeStatusTracker::add_listener`]; pass it to
/// [`RuntimeStatusTracker::remove_listener`] (the Python original returns
/// a removal closure instead — same semantics, removal is idempotent).
pub type ListenerId = u64;

/// Injectable per-usage-event cost function. Python's `CostFn` may raise;
/// that fallibility is modeled as `Result` — an `Err` is best-effort
/// ignored (cost stays 0) and must never drop the usage update.
pub type CostFn = Box<dyn Fn(&ProviderResponseUsage) -> Result<Decimal, String> + Send + Sync>;

/// One handler registered by [`RuntimeStatusTracker::register_hooks`].
pub type HookHandler = Box<dyn Fn(&str, &Payload) + Send + Sync>;

/// Unregister callback returned by a [`HookRegistry`] registration.
pub type UnregisterFn = Box<dyn FnOnce() + Send>;

/// Python `register_hooks(..., priority=55)` keyword default.
pub const DEFAULT_HOOK_PRIORITY: i32 = 55;

type Clock = Box<dyn Fn() -> f64 + Send + Sync>;
type Listener = Arc<dyn Fn() + Send + Sync>;

/// Monotonic clock in fractional seconds (Python's `time.monotonic`),
/// anchored at first use within this process.
fn monotonic() -> f64 {
    static START: OnceLock<Instant> = OnceLock::new();
    START.get_or_init(Instant::now).elapsed().as_secs_f64()
}

/// (De)serialize `Decimal` the way pydantic's JSON mode does (string on
/// the wire) and accept string/number back on the parse side — the same
/// convention as `kernel::events::decimal_opt`, non-optional here.
mod decimal_str {
    use std::str::FromStr;

    use rust_decimal::Decimal;
    use serde::{Deserialize, Deserializer, Serializer};
    use serde_json::Value;

    pub fn serialize<S: Serializer>(value: &Decimal, serializer: S) -> Result<S::Ok, S::Error> {
        serializer.serialize_str(&value.to_string())
    }

    pub fn deserialize<'de, D: Deserializer<'de>>(deserializer: D) -> Result<Decimal, D::Error> {
        match Value::deserialize(deserializer)? {
            Value::String(s) => Decimal::from_str(&s).map_err(serde::de::Error::custom),
            Value::Number(n) => Decimal::from_str(&n.to_string()).map_err(serde::de::Error::custom),
            other => Err(serde::de::Error::custom(format!(
                "invalid decimal value: {other}"
            ))),
        }
    }
}

/// Minimal mirror of amplifier-core's `HookResult` — this tracker only
/// ever answers `action="continue"` (state-only observer, never blocks).
#[derive(Clone, Debug, PartialEq, Eq)]
struct HookResult {
    action: &'static str,
}

/// The abstract hooks surface `register_hooks` needs (Python duck-types
/// this as `hooks: Any`): register a named handler at a priority, and
/// optionally hand back an unregister callback.
pub trait HookRegistry {
    fn register(
        &mut self,
        event: &str,
        handler: HookHandler,
        priority: i32,
        name: &str,
    ) -> Option<UnregisterFn>;
}

/// Immutable snapshot of accumulated provider usage.
///
/// Python pins every counter `ge=0` (pydantic would reject a negative);
/// `u64` enforces the same bound by construction — [`UsageTotals::adding`]
/// saturates at 0 instead of raising should a normalized payload ever
/// carry a negative token count (recorded divergence: Python raises a
/// `ValidationError` there, an untested pathological shape).
#[derive(Clone, Debug, Default, PartialEq, serde::Serialize, serde::Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct UsageTotals {
    pub requests: u64,
    pub input_tokens: u64,
    pub output_tokens: u64,
    pub cache_read: u64,
    pub cache_write: u64,
    #[serde(with = "decimal_str")]
    pub cost: Decimal,
}

impl UsageTotals {
    /// Percent of prompt tokens served from cache (the `NN% cached` figure).
    pub fn cache_hit_pct(&self) -> u8 {
        let prompt_total = self.input_tokens + self.cache_read;
        if prompt_total == 0 {
            return 0;
        }
        // Python round() is round-half-to-even.
        ((100.0 * self.cache_read as f64) / prompt_total as f64).round_ties_even() as u8
    }

    pub fn adding(&self, usage: &ProviderResponseUsage, cost: Decimal) -> UsageTotals {
        UsageTotals {
            requests: self.requests + 1,
            input_tokens: self.input_tokens.saturating_add_signed(usage.input_tokens),
            output_tokens: self.output_tokens.saturating_add_signed(usage.output_tokens),
            cache_read: self.cache_read.saturating_add_signed(usage.cache_read),
            cache_write: self.cache_write.saturating_add_signed(usage.cache_write),
            cost: self.cost + Decimal::ZERO.max(cost),
        }
    }
}

/// One coherent view for the working line, footer, and turn rules.
#[derive(Clone, Debug, Default, PartialEq, serde::Serialize, serde::Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct RuntimeSnapshot {
    pub running: bool,
    pub turn_elapsed: f64,
    pub turn: UsageTotals,
    pub session: UsageTotals,
    pub last_notice: Option<ProviderNotice>,
}

struct State {
    running: bool,
    turn_started_at: Option<f64>,
    turn: UsageTotals,
    session: UsageTotals,
    last_notice: Option<ProviderNotice>,
}

/// Track turn lifecycle and telemetry; state only, listener-driven.
pub struct RuntimeStatusTracker {
    root_session_id: String,
    cost_fn: Option<CostFn>,
    clock: Clock,
    state: Mutex<State>,
    listeners: Mutex<ListenerState>,
}

struct ListenerState {
    next_id: ListenerId,
    entries: Vec<(ListenerId, Listener)>,
}

/// Python's `str(value)` for the payload scalars encountered as session
/// ids (mirrors the private helper in `kernel::events`).
fn py_str(value: &serde_json::Value) -> String {
    match value {
        serde_json::Value::String(s) => s.clone(),
        serde_json::Value::Bool(true) => "True".to_string(),
        serde_json::Value::Bool(false) => "False".to_string(),
        serde_json::Value::Number(n) => n.to_string(),
        other => other.to_string(),
    }
}

/// Python truthiness for JSON values (mirrors `kernel::events`).
fn is_truthy(value: &serde_json::Value) -> bool {
    match value {
        serde_json::Value::Null => false,
        serde_json::Value::Bool(b) => *b,
        serde_json::Value::Number(n) => n.as_f64().map(|f| f != 0.0).unwrap_or(true),
        serde_json::Value::String(s) => !s.is_empty(),
        serde_json::Value::Array(a) => !a.is_empty(),
        serde_json::Value::Object(o) => !o.is_empty(),
    }
}

impl RuntimeStatusTracker {
    pub const EVENTS: [&'static str; 8] = [
        "prompt:submit",
        "prompt:complete",
        "execution:start",
        "execution:end",
        "provider:response",
        "provider:error",
        "provider:retry",
        "provider:throttle",
    ];

    pub fn new(root_session_id: impl Into<String>) -> Self {
        Self {
            root_session_id: root_session_id.into(),
            cost_fn: None,
            clock: Box::new(monotonic),
            state: Mutex::new(State {
                running: false,
                turn_started_at: None,
                turn: UsageTotals::default(),
                session: UsageTotals::default(),
                last_notice: None,
            }),
            listeners: Mutex::new(ListenerState {
                next_id: 1,
                entries: Vec::new(),
            }),
        }
    }

    /// Python's `cost_fn=` keyword argument (builder style).
    pub fn with_cost_fn(
        mut self,
        cost_fn: impl Fn(&ProviderResponseUsage) -> Result<Decimal, String> + Send + Sync + 'static,
    ) -> Self {
        self.cost_fn = Some(Box::new(cost_fn));
        self
    }

    /// Python's `clock=` keyword argument (builder style; defaults to
    /// the process-monotonic clock).
    pub fn with_clock(mut self, clock: impl Fn() -> f64 + Send + Sync + 'static) -> Self {
        self.clock = Box::new(clock);
        self
    }

    pub fn root_session_id(&self) -> &str {
        &self.root_session_id
    }

    // -- state ---------------------------------------------------------------

    pub fn running(&self) -> bool {
        self.state.lock().unwrap().running
    }

    pub fn turn_elapsed(&self) -> f64 {
        let started = self.state.lock().unwrap().turn_started_at;
        match started {
            None => 0.0,
            Some(started_at) => ((self.clock)() - started_at).max(0.0),
        }
    }

    pub fn snapshot(&self) -> RuntimeSnapshot {
        let state = self.state.lock().unwrap();
        let turn_elapsed = match state.turn_started_at {
            None => 0.0,
            Some(started_at) => ((self.clock)() - started_at).max(0.0),
        };
        RuntimeSnapshot {
            running: state.running,
            turn_elapsed,
            turn: state.turn.clone(),
            session: state.session.clone(),
            last_notice: state.last_notice.clone(),
        }
    }

    /// Re-seed restored spend on resume (ui-events.jsonl replay).
    pub fn seed_session_cost(&self, prior_cost: Decimal) {
        if prior_cost <= Decimal::ZERO {
            return;
        }
        {
            let mut state = self.state.lock().unwrap();
            state.session.cost += prior_cost;
        }
        self.notify();
    }

    /// Register a change callback; remove it via [`Self::remove_listener`].
    pub fn add_listener(&self, listener: impl Fn() + Send + Sync + 'static) -> ListenerId {
        let mut listeners = self.listeners.lock().unwrap();
        let id = listeners.next_id;
        listeners.next_id += 1;
        listeners.entries.push((id, Arc::new(listener)));
        id
    }

    pub fn remove_listener(&self, id: ListenerId) {
        let mut listeners = self.listeners.lock().unwrap();
        listeners.entries.retain(|(entry_id, _)| *entry_id != id);
    }

    // -- hook plumbing ---------------------------------------------------------

    /// Python's `async def handle_event` — synchronous here; always
    /// answers continue (state-only observer).
    fn handle_event(&self, event: &str, data: &Payload) -> HookResult {
        self.consume(event, data);
        HookResult { action: "continue" }
    }

    /// Register this tracker's handler for every consumed event; the
    /// returned callback unregisters them all in reverse order.
    pub fn register_hooks(
        self: &Arc<Self>,
        hooks: &mut dyn HookRegistry,
        priority: i32,
    ) -> UnregisterFn {
        let mut unregister_callbacks: Vec<UnregisterFn> = Vec::new();
        for event in Self::EVENTS {
            let tracker = Arc::clone(self);
            let handler: HookHandler = Box::new(move |event, data| {
                tracker.handle_event(event, data);
            });
            let name = format!("newtui-runtime-status-{}", event.replace(':', "-"));
            if let Some(unregister) = hooks.register(event, handler, priority, &name) {
                unregister_callbacks.push(unregister);
            }
        }
        Box::new(move || {
            for unregister in unregister_callbacks.into_iter().rev() {
                unregister();
            }
        })
    }

    // -- consumption -----------------------------------------------------------

    pub fn consume(&self, event: &str, data: &Payload) {
        // Python: `str(payload.get("session_id") or self.root_session_id)`.
        let session_id = match data.get("session_id") {
            Some(value) if is_truthy(value) => py_str(value),
            _ => self.root_session_id.clone(),
        };
        let is_root = session_id == self.root_session_id;
        if event == "prompt:submit" && is_root {
            {
                let mut state = self.state.lock().unwrap();
                state.running = true;
                state.turn_started_at = Some((self.clock)());
                state.turn = UsageTotals::default();
                state.last_notice = None;
            }
            self.notify();
            return;
        }
        if (event == "prompt:complete" || event == "execution:end") && is_root {
            self.state.lock().unwrap().running = false;
            self.notify();
            return;
        }
        if event == "execution:start" && is_root {
            {
                let mut state = self.state.lock().unwrap();
                state.running = true;
                if state.turn_started_at.is_none() {
                    state.turn_started_at = Some((self.clock)());
                }
            }
            self.notify();
            return;
        }
        if event == "provider:response" {
            if let Some(UIEvent::ProviderResponseUsage(usage)) = normalize(event, Some(data)) {
                self.add_usage(&usage);
            }
            return;
        }
        if matches!(event, "provider:error" | "provider:retry" | "provider:throttle") {
            if let Some(UIEvent::ProviderNotice(notice)) = normalize(event, Some(data)) {
                self.state.lock().unwrap().last_notice = Some(notice);
                self.notify();
            }
        }
    }

    fn add_usage(&self, usage: &ProviderResponseUsage) {
        let mut cost = Decimal::ZERO;
        if let Some(cost_fn) = &self.cost_fn {
            // Best-effort cost calc: a bad cost fn must not drop the
            // usage update (Python swallows the exception at debug log).
            if let Ok(value) = cost_fn(usage) {
                cost = value;
            }
        }
        {
            let mut state = self.state.lock().unwrap();
            state.turn = state.turn.adding(usage, cost);
            state.session = state.session.adding(usage, cost);
        }
        self.notify();
    }

    /// Snapshot then call OUTSIDE the lock (Python `tuple(self._listeners)`).
    ///
    /// Divergence: Python crash-isolates each listener (`except
    /// Exception`); a panicking Rust listener propagates (crate-wide
    /// listener convention, see `model::queues`).
    fn notify(&self) {
        let snapshot: Vec<Listener> = self
            .listeners
            .lock()
            .unwrap()
            .entries
            .iter()
            .map(|(_, listener)| Arc::clone(listener))
            .collect();
        for listener in snapshot {
            listener();
        }
    }
}

// --------------------------------------------------------------------------
// Tests — ports of the RuntimeStatusTracker cases in
// tests/test_kernel_trackers.py (stream/task tracker, queue bridge, and
// display-system cases pin other units).
// --------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use std::str::FromStr;

    use serde_json::{json, Value};

    use super::*;

    const ROOT: &str = "sess-root";

    fn obj(value: Value) -> Payload {
        match value {
            Value::Object(map) => map,
            _ => panic!("payload literal must be a JSON object"),
        }
    }

    fn dec(text: &str) -> Decimal {
        Decimal::from_str(text).unwrap()
    }

    /// Python `usage()` payload helper (nested usage, anthropic cache keys).
    fn usage(session_id: &str) -> Payload {
        obj(json!({
            "session_id": session_id,
            "usage": {
                "input_tokens": 100,
                "output_tokens": 40,
                "cache_read_input_tokens": 300,
                "cache_creation_input_tokens": 10,
            },
            "model": "claude-fable-5",
        }))
    }

    #[test]
    fn test_runtime_tracker_turn_boundaries() {
        let now = Arc::new(Mutex::new(100.0_f64));
        let clock_now = Arc::clone(&now);
        let tracker =
            RuntimeStatusTracker::new(ROOT).with_clock(move || *clock_now.lock().unwrap());
        assert!(!tracker.running());
        tracker.consume(
            "prompt:submit",
            &obj(json!({"session_id": ROOT, "prompt": "go"})),
        );
        assert!(tracker.running());
        *now.lock().unwrap() = 103.5;
        assert!((tracker.turn_elapsed() - 3.5).abs() < 1e-9);
        tracker.consume("provider:response", &usage(ROOT));
        let snap = tracker.snapshot();
        assert_eq!(snap.turn.output_tokens, 40);
        assert_eq!(snap.turn.cache_hit_pct(), 75); // 300 / (100+300)
        tracker.consume("prompt:complete", &obj(json!({"session_id": ROOT})));
        assert!(!tracker.running());
        // New root turn resets turn totals but keeps session totals.
        tracker.consume(
            "prompt:submit",
            &obj(json!({"session_id": ROOT, "prompt": "next"})),
        );
        let snap = tracker.snapshot();
        assert_eq!(snap.turn.requests, 0);
        assert_eq!(snap.session.requests, 1);
    }

    #[test]
    fn test_runtime_tracker_child_usage_counts_toward_turn_and_session() {
        let tracker = RuntimeStatusTracker::new(ROOT);
        tracker.consume(
            "prompt:submit",
            &obj(json!({"session_id": ROOT, "prompt": "go"})),
        );
        tracker.consume("provider:response", &usage("sess-child_worker"));
        let snap = tracker.snapshot();
        assert_eq!(snap.turn.requests, 1);
        assert_eq!(snap.session.requests, 1);
        // …but a CHILD prompt:submit never resets the root turn.
        tracker.consume(
            "prompt:submit",
            &obj(json!({"session_id": "sess-child_worker", "prompt": "x"})),
        );
        assert_eq!(tracker.snapshot().turn.requests, 1);
    }

    #[test]
    fn test_runtime_tracker_cost_fn_and_seed() {
        let tracker = RuntimeStatusTracker::new(ROOT).with_cost_fn(|event| {
            Ok(if event.output_tokens != 0 {
                dec("0.25")
            } else {
                dec("0")
            })
        });
        tracker.consume("prompt:submit", &obj(json!({"session_id": ROOT})));
        tracker.consume("provider:response", &usage(ROOT));
        tracker.consume("provider:response", &usage(ROOT));
        assert_eq!(tracker.snapshot().turn.cost, dec("0.50"));
        tracker.seed_session_cost(dec("1.00"));
        assert_eq!(tracker.snapshot().session.cost, dec("1.50"));
    }

    #[test]
    fn test_runtime_tracker_provider_notices() {
        let tracker = RuntimeStatusTracker::new(ROOT);
        tracker.consume(
            "provider:throttle",
            &obj(json!({"session_id": ROOT, "message": "rate limited"})),
        );
        let notice = tracker.snapshot().last_notice;
        let notice = notice.expect("notice is not None");
        assert_eq!(notice.notice.as_str(), "throttle");
        assert_eq!(notice.message, "rate limited");
        // Cleared at the next root turn.
        tracker.consume("prompt:submit", &obj(json!({"session_id": ROOT})));
        assert!(tracker.snapshot().last_notice.is_none());
    }

    #[test]
    fn test_runtime_tracker_broken_cost_fn_does_not_crash() {
        let tracker = RuntimeStatusTracker::new(ROOT)
            .with_cost_fn(|_| Err("no pricing table".to_string()));
        tracker.consume("provider:response", &usage(ROOT));
        assert_eq!(tracker.snapshot().session.cost, dec("0"));
    }

    /// Rust-only plumbing check (the Python file pins the equivalent
    /// roundtrip on StreamStatusTracker only): every consumed event is
    /// registered under its `newtui-runtime-status-*` name, registered
    /// handlers actually feed `consume`, and the returned callback
    /// unregisters everything in reverse order.
    #[test]
    fn register_hooks_roundtrip_rust_only() {
        #[derive(Default)]
        struct FakeHooks {
            registered: Vec<(String, i32, String)>,
            handlers: Vec<(String, HookHandler)>,
            unregistered: Arc<Mutex<Vec<String>>>,
        }

        impl HookRegistry for FakeHooks {
            fn register(
                &mut self,
                event: &str,
                handler: HookHandler,
                priority: i32,
                name: &str,
            ) -> Option<UnregisterFn> {
                self.registered
                    .push((event.to_string(), priority, name.to_string()));
                self.handlers.push((event.to_string(), handler));
                let sink = Arc::clone(&self.unregistered);
                let name = name.to_string();
                Some(Box::new(move || sink.lock().unwrap().push(name)))
            }
        }

        let tracker = Arc::new(RuntimeStatusTracker::new(ROOT));
        let mut hooks = FakeHooks::default();
        let unregister = tracker.register_hooks(&mut hooks, DEFAULT_HOOK_PRIORITY);
        let registered_events: Vec<&str> = hooks
            .registered
            .iter()
            .map(|(event, _, _)| event.as_str())
            .collect();
        assert_eq!(registered_events, RuntimeStatusTracker::EVENTS);
        assert!(hooks
            .registered
            .iter()
            .all(|(_, priority, _)| *priority == 55));
        assert_eq!(hooks.registered[0].2, "newtui-runtime-status-prompt-submit");

        // Handlers feed consume (handle_event answers continue silently).
        let payload = obj(json!({"session_id": ROOT, "prompt": "go"}));
        for (event, handler) in &hooks.handlers {
            if event == "prompt:submit" {
                handler(event, &payload);
            }
        }
        assert!(tracker.running());

        unregister();
        let unregistered = hooks.unregistered.lock().unwrap();
        assert_eq!(unregistered.len(), RuntimeStatusTracker::EVENTS.len());
        assert_eq!(
            unregistered.last().map(String::as_str),
            Some("newtui-runtime-status-prompt-submit")
        );
    }
}
