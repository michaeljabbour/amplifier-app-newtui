//! Channel-A stream tracker: live text/thinking tail state.
//!
//! Hook-tracker pattern (`EVENTS` list, `handle_event -> HookResult`,
//! `register_hooks -> unregister`, `add_listener`). Pure state — the app
//! wires listeners to UI notification.
//!
//! Consumes the ad-hoc provider streaming events (`llm:stream_block_*`)
//! through [`crate::kernel::events::normalize`], so provider payload
//! variance (`delta` | `text` | `content` keys) is absorbed at the one
//! boundary. Root session only: child streams stay dark by design (lanes
//! summarize them). Blocks are keyed `(request_id, block_index)`;
//! non-text/thinking block types (and thinking when `show_thinking` is
//! off) are hidden.
//!
//! Port of `src/amplifier_app_newtui/kernel/trackers/stream_status.py`.
//! Divergences from the Python original, recorded honestly:
//!
//! - Python's async `handle_event` becomes a synchronous method (no async
//!   runtime in the crate); the `HookResult` mirror is crate-private.
//! - Python `register_hooks` passes the bound `handle_event` coroutine to
//!   `hooks.register`; the Rust [`HookRegistry`] trait registers only the
//!   `(event, priority, name)` interest — the runtime that owns both the
//!   registry and the tracker routes matching events to [`StreamStatusTracker::consume`]
//!   itself, since a stored `&mut self` callback cannot coexist with the
//!   tracker's other users under the borrow checker.
//! - Python `add_listener` returns a remove-closure over the live list;
//!   here it returns a [`ListenerId`] token consumed by
//!   [`StreamStatusTracker::remove_listener`] (same idempotent semantics).
//! - Python `_hidden.pop()` evicts an arbitrary set element when the
//!   hidden set is full; the `HashSet` here likewise evicts an arbitrary
//!   element (`iter().next()`), matching the unordered intent.

use std::collections::{HashMap, HashSet};
use std::panic::{catch_unwind, AssertUnwindSafe};
use std::sync::OnceLock;
use std::time::Instant;

use serde_json::Value;

use crate::kernel::events::{
    normalize, Payload, StreamBlockDelta, StreamBlockEnd, StreamBlockStart, UIEvent,
};

const MAX_ACTIVE_BLOCKS: usize = 8;
const MAX_STREAM_CHARS: usize = 16_384;
const DELTA_NOTIFY_SECONDS: f64 = 0.05;
const VISIBLE_KINDS: [&str; 3] = ["text", "thinking", "reasoning"];

/// Python's keyword-only `priority: int = 60` default on `register_hooks`.
pub const DEFAULT_HOOK_PRIORITY: i64 = 60;

/// `(request_id, block_index)` — the streaming block identity.
type BlockKey = (String, i64);

/// One notification callback (Python `Listener = Callable[[], None]`).
pub type Listener = Box<dyn FnMut()>;

/// Handle returned by [`StreamStatusTracker::add_listener`]; pass it to
/// [`StreamStatusTracker::remove_listener`] to detach (the Rust shape of
/// Python's returned remove-closure).
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ListenerId(u64);

/// Minimal mirror of amplifier-core's `HookResult` — this tracker only
/// ever answers `continue`.
// Exercised by this module's tests only until the queue-bridge/runtime
// hook-dispatch units port over and call `handle_event` for real.
#[allow(dead_code)]
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct HookResult {
    pub(crate) action: &'static str,
}

/// Minimal mirror of the amplifier hooks registry surface this tracker
/// uses: register interest in an event and get back an unregister
/// callback when the registry supports one (Python's
/// `if callable(unregister)` check maps to the `Option`).
pub trait HookRegistry {
    fn register(&mut self, event: &str, priority: i64, name: &str) -> Option<Box<dyn FnMut()>>;
}

/// key value: (kind, accumulated text, monotonic sequence) in Python.
struct BlockState {
    kind: String,
    text: String,
    sequence: u64,
}

/// Process-wide monotonic clock, mirroring Python `time.monotonic` (one
/// shared time domain across trackers, seconds as `f64`).
fn monotonic_clock() -> Box<dyn FnMut() -> f64> {
    static ORIGIN: OnceLock<Instant> = OnceLock::new();
    let origin = *ORIGIN.get_or_init(Instant::now);
    Box::new(move || origin.elapsed().as_secs_f64())
}

/// Python `str(data.get("session_id") or <fallback>)` — the truthiness
/// check and scalar stringification for the one key this tracker reads
/// (the equivalents in `events.rs` are module-private by design).
fn truthy_session_id(data: &Payload) -> Option<String> {
    let value = data.get("session_id")?;
    let truthy = match value {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().map(|f| f != 0.0).unwrap_or(true),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    };
    if !truthy {
        return None;
    }
    Some(match value {
        Value::String(s) => s.clone(),
        Value::Bool(_) => "True".to_string(), // only `true` is truthy
        Value::Number(n) => n.to_string(),
        // Containers: JSON text stands in for Python's repr (session ids
        // are strings in practice).
        other => other.to_string(),
    })
}

/// Python `text[-max_chars:]` — a character-based (not byte-based) tail.
fn tail_chars(text: String, max_chars: usize) -> String {
    let count = text.chars().count();
    if count <= max_chars {
        return text;
    }
    let start = text
        .char_indices()
        .nth(count - max_chars)
        .map(|(index, _)| index)
        .unwrap_or(0);
    text[start..].to_string()
}

/// Track the active root-session stream without touching the terminal.
pub struct StreamStatusTracker {
    pub root_session_id: String,
    pub show_thinking: bool,
    clock: Box<dyn FnMut() -> f64>,
    blocks: HashMap<BlockKey, BlockState>,
    hidden: HashSet<BlockKey>,
    listeners: Vec<(ListenerId, Listener)>,
    listener_counter: u64,
    sequence: u64,
    last_delta_notify: f64,
}

impl StreamStatusTracker {
    pub const EVENTS: [&'static str; 9] = [
        "llm:stream_block_start",
        "llm:stream_block_delta",
        "llm:stream_block_end",
        "llm:stream_aborted",
        "provider:error",
        "provider:retry",
        "orchestrator:complete",
        "execution:end",
        "prompt:submit",
    ];

    const RESET_EVENTS: [&'static str; 6] = [
        "llm:stream_aborted",
        "provider:error",
        "provider:retry",
        "orchestrator:complete",
        "execution:end",
        "prompt:submit",
    ];

    /// Python `StreamStatusTracker(root_session_id)` with the keyword
    /// defaults `show_thinking=False, clock=monotonic`.
    pub fn new(root_session_id: impl Into<String>) -> Self {
        Self {
            root_session_id: root_session_id.into(),
            show_thinking: false,
            clock: monotonic_clock(),
            blocks: HashMap::new(),
            hidden: HashSet::new(),
            listeners: Vec::new(),
            listener_counter: 0,
            sequence: 0,
            last_delta_notify: 0.0,
        }
    }

    /// Builder for the Python `show_thinking=` keyword.
    pub fn with_show_thinking(mut self, show_thinking: bool) -> Self {
        self.show_thinking = show_thinking;
        self
    }

    /// Builder for the Python `clock=` keyword (injectable time source).
    pub fn with_clock(mut self, clock: impl FnMut() -> f64 + 'static) -> Self {
        self.clock = Box::new(clock);
        self
    }

    // -- state ---------------------------------------------------------------

    /// `(kind, text)` of the most recently touched visible block.
    pub fn preview(&self) -> Option<(String, String)> {
        self.blocks
            .values()
            .max_by_key(|block| block.sequence)
            .map(|block| (block.kind.clone(), block.text.clone()))
    }

    pub fn active_block_count(&self) -> usize {
        self.blocks.len()
    }

    /// Rough live token count before provider usage arrives (~4 chars/tok).
    ///
    /// Python's `max(0, (characters + 3) // 4)` — the `max(0, ...)` guard
    /// is enforced by `usize` arithmetic here.
    pub fn estimated_tokens(&self) -> usize {
        let characters: usize = self
            .blocks
            .values()
            .filter(|block| block.kind == "text")
            .map(|block| block.text.chars().count())
            .sum();
        // Python `(characters + 3) // 4` == ceil(characters / 4).
        characters.div_ceil(4)
    }

    pub fn add_listener(&mut self, listener: impl FnMut() + 'static) -> ListenerId {
        self.listener_counter += 1;
        let id = ListenerId(self.listener_counter);
        self.listeners.push((id, Box::new(listener)));
        id
    }

    /// Detach a listener; idempotent, like Python's returned remove-closure.
    pub fn remove_listener(&mut self, id: ListenerId) {
        self.listeners.retain(|(listener_id, _)| *listener_id != id);
    }

    // -- hook plumbing ---------------------------------------------------------

    /// Synchronous port of the async hook entrypoint: consume, then
    /// always answer `continue`.
    // Exercised by this module's tests only until the hook-dispatch
    // units port over (see `HookResult`).
    #[allow(dead_code)]
    pub(crate) fn handle_event(&mut self, event: &str, data: &Payload) -> HookResult {
        self.consume(event, data);
        HookResult { action: "continue" }
    }

    /// Register this tracker's event interests at the default priority
    /// (60); returns an unregister-all callback.
    pub fn register_hooks(&self, hooks: &mut dyn HookRegistry) -> Box<dyn FnOnce()> {
        self.register_hooks_with_priority(hooks, DEFAULT_HOOK_PRIORITY)
    }

    /// Python `register_hooks(hooks, *, priority=60)`; the returned
    /// closure unregisters everything in reverse registration order.
    pub fn register_hooks_with_priority(
        &self,
        hooks: &mut dyn HookRegistry,
        priority: i64,
    ) -> Box<dyn FnOnce()> {
        let mut unregister_callbacks: Vec<Box<dyn FnMut()>> = Vec::new();
        for event in Self::EVENTS {
            let name = format!("newtui-stream-status-{}", event.replace(':', "-"));
            if let Some(unregister) = hooks.register(event, priority, &name) {
                unregister_callbacks.push(unregister);
            }
        }
        Box::new(move || {
            for mut unregister in unregister_callbacks.into_iter().rev() {
                unregister();
            }
        })
    }

    // -- consumption -----------------------------------------------------------

    pub fn consume(&mut self, event: &str, data: &Payload) {
        let session_id =
            truthy_session_id(data).unwrap_or_else(|| self.root_session_id.clone());
        if session_id != self.root_session_id {
            return;
        }
        if Self::RESET_EVENTS.contains(&event) {
            self.blocks.clear();
            self.hidden.clear();
            self.notify();
            return;
        }
        match normalize(event, Some(data)) {
            Some(UIEvent::StreamBlockStart(normalized)) => self.on_start(normalized),
            Some(UIEvent::StreamBlockDelta(normalized)) => self.on_delta(normalized),
            Some(UIEvent::StreamBlockEnd(normalized)) => self.on_end(normalized),
            _ => {}
        }
    }

    fn on_start(&mut self, event: StreamBlockStart) {
        let key = (event.request_id.clone(), event.block_index);
        self.hidden.remove(&key);
        if !self.visible(&event.block_type) {
            self.hide(key);
            return;
        }
        self.store(key, event.block_type, String::new());
        self.notify();
    }

    fn on_delta(&mut self, event: StreamBlockDelta) {
        let key = (event.request_id.clone(), event.block_index);
        if self.hidden.contains(&key) {
            return;
        }
        let (current_kind, current_text) = match self.blocks.get(&key) {
            Some(block) => (block.kind.clone(), block.text.clone()),
            None => (event.block_type.clone(), String::new()),
        };
        // Python `event.block_type or current_kind`.
        let kind = if event.block_type.is_empty() {
            current_kind
        } else {
            event.block_type.clone()
        };
        if !self.visible(&kind) {
            self.blocks.remove(&key);
            self.hide(key);
            return;
        }
        let text = tail_chars(current_text + &event.text, MAX_STREAM_CHARS);
        self.store(key, kind, text);
        let now = (self.clock)();
        if now - self.last_delta_notify < DELTA_NOTIFY_SECONDS {
            return;
        }
        self.last_delta_notify = now;
        self.notify();
    }

    fn on_end(&mut self, event: StreamBlockEnd) {
        let key = (event.request_id.clone(), event.block_index);
        self.blocks.remove(&key);
        self.hidden.remove(&key);
        self.notify();
    }

    // -- helpers ----------------------------------------------------------------

    fn visible(&self, kind: &str) -> bool {
        if !VISIBLE_KINDS.contains(&kind) {
            return false;
        }
        if (kind == "thinking" || kind == "reasoning") && !self.show_thinking {
            return false;
        }
        true
    }

    fn hide(&mut self, key: BlockKey) {
        if self.hidden.len() >= MAX_ACTIVE_BLOCKS {
            // Python `set.pop()`: evict an arbitrary element to keep the bound.
            if let Some(evicted) = self.hidden.iter().next().cloned() {
                self.hidden.remove(&evicted);
            }
        }
        self.hidden.insert(key);
    }

    fn store(&mut self, key: BlockKey, kind: String, text: String) {
        if !self.blocks.contains_key(&key) && self.blocks.len() >= MAX_ACTIVE_BLOCKS {
            let oldest = self
                .blocks
                .iter()
                .min_by_key(|(_, block)| block.sequence)
                .map(|(oldest_key, _)| oldest_key.clone());
            if let Some(oldest) = oldest {
                self.blocks.remove(&oldest);
            }
        }
        self.sequence += 1;
        self.blocks.insert(
            key,
            BlockState {
                kind,
                text,
                sequence: self.sequence,
            },
        );
    }

    fn notify(&mut self) {
        for (_, listener) in self.listeners.iter_mut() {
            // Crash-isolate listener callbacks: one bad listener must not
            // stop notification (Python swallows Exception at debug level).
            let _ = catch_unwind(AssertUnwindSafe(listener));
        }
    }
}

// --------------------------------------------------------------------------
// Tests — ports of the StreamStatusTracker cases in
// tests/test_kernel_trackers.py. The RuntimeStatusTracker, TaskStatusTracker,
// QueueBridge, and DisplaySystem cases in that file pin units that are not
// ported yet.
// --------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{json, Map};
    use std::cell::{Cell, RefCell};
    use std::rc::Rc;

    const ROOT: &str = "sess-root";

    /// Python module-level `delta(text_key, text, index=0)` helper.
    fn delta(text_key: &str, text: &str) -> Payload {
        let mut map = Map::new();
        map.insert("session_id".to_string(), json!(ROOT));
        map.insert("request_id".to_string(), json!("req-1"));
        map.insert("block_index".to_string(), json!(0));
        map.insert("block_type".to_string(), json!("text"));
        map.insert(text_key.to_string(), json!(text));
        map
    }

    fn obj(value: serde_json::Value) -> Payload {
        match value {
            serde_json::Value::Object(map) => map,
            _ => panic!("payload literal must be a JSON object"),
        }
    }

    /// Python test-file `FakeHooks`, restricted to the registry surface
    /// the stream tracker exercises (register bookkeeping + unregister
    /// callbacks; handler dispatch is runtime wiring, see module docs).
    struct FakeHooks {
        registered: Vec<(String, i64, String)>,
        unregistered: Rc<RefCell<Vec<String>>>,
    }

    impl FakeHooks {
        fn new() -> Self {
            Self {
                registered: Vec::new(),
                unregistered: Rc::new(RefCell::new(Vec::new())),
            }
        }
    }

    impl HookRegistry for FakeHooks {
        fn register(
            &mut self,
            event: &str,
            priority: i64,
            name: &str,
        ) -> Option<Box<dyn FnMut()>> {
            self.registered
                .push((event.to_string(), priority, name.to_string()));
            let sink = Rc::clone(&self.unregistered);
            let name = name.to_string();
            Some(Box::new(move || sink.borrow_mut().push(name.clone())))
        }
    }

    #[test]
    fn test_stream_tracker_accumulates_and_consolidates() {
        let mut tracker = StreamStatusTracker::new(ROOT).with_clock(|| 0.0);
        tracker.consume("llm:stream_block_start", &delta("delta", ""));
        tracker.consume("llm:stream_block_delta", &delta("delta", "Hello "));
        tracker.consume("llm:stream_block_delta", &delta("text", "wor"));
        tracker.consume("llm:stream_block_delta", &delta("content", "ld"));
        let preview = tracker.preview();
        assert_eq!(
            preview,
            Some(("text".to_string(), "Hello world".to_string()))
        );
        assert_eq!(tracker.estimated_tokens(), 3); // ceil(11 / 4)
        tracker.consume(
            "llm:stream_block_end",
            &obj(json!({"session_id": ROOT, "request_id": "req-1", "block_index": 0})),
        );
        assert_eq!(tracker.preview(), None);
        assert_eq!(tracker.active_block_count(), 0);
    }

    #[test]
    fn test_stream_tracker_ignores_child_sessions() {
        let mut tracker = StreamStatusTracker::new(ROOT);
        let mut payload = delta("delta", "child text");
        payload.insert("session_id".to_string(), json!("kid"));
        tracker.consume("llm:stream_block_delta", &payload);
        assert_eq!(tracker.preview(), None);
    }

    #[test]
    fn test_stream_tracker_hides_thinking_unless_enabled() {
        let mut payload = delta("delta", "hmm");
        payload.insert("block_type".to_string(), json!("thinking"));

        let mut tracker = StreamStatusTracker::new(ROOT);
        tracker.consume("llm:stream_block_start", &payload);
        tracker.consume("llm:stream_block_delta", &payload);
        assert_eq!(tracker.preview(), None);

        let mut showing = StreamStatusTracker::new(ROOT)
            .with_show_thinking(true)
            .with_clock(|| 0.0);
        showing.consume("llm:stream_block_start", &payload);
        showing.consume("llm:stream_block_delta", &payload);
        assert_eq!(
            showing.preview(),
            Some(("thinking".to_string(), "hmm".to_string()))
        );
    }

    #[test]
    fn test_stream_tracker_resets_on_lifecycle_events() {
        for reset_event in ["prompt:submit", "orchestrator:complete", "llm:stream_aborted"] {
            let mut tracker = StreamStatusTracker::new(ROOT).with_clock(|| 0.0);
            tracker.consume("llm:stream_block_delta", &delta("delta", "abc"));
            assert!(tracker.preview().is_some());
            tracker.consume(reset_event, &obj(json!({"session_id": ROOT})));
            assert_eq!(tracker.preview(), None);
        }
    }

    #[test]
    fn test_stream_tracker_throttles_delta_notifications() {
        let now = Rc::new(Cell::new(0.0_f64));
        let clock_now = Rc::clone(&now);
        let mut tracker = StreamStatusTracker::new(ROOT).with_clock(move || clock_now.get());
        let calls = Rc::new(Cell::new(0_usize));
        let listener_calls = Rc::clone(&calls);
        tracker.add_listener(move || listener_calls.set(listener_calls.get() + 1));
        now.set(1.0);
        tracker.consume("llm:stream_block_delta", &delta("delta", "a"));
        let first = calls.get();
        now.set(1.01); // within the 50ms window — suppressed
        tracker.consume("llm:stream_block_delta", &delta("delta", "b"));
        assert_eq!(calls.get(), first);
        now.set(1.2);
        tracker.consume("llm:stream_block_delta", &delta("delta", "c"));
        assert_eq!(calls.get(), first + 1);
    }

    #[test]
    fn test_stream_tracker_register_hooks_roundtrip() {
        let mut hooks = FakeHooks::new();
        let tracker = StreamStatusTracker::new(ROOT);
        let unregister = tracker.register_hooks(&mut hooks);
        let registered_events: Vec<&str> = hooks
            .registered
            .iter()
            .map(|(event, _, _)| event.as_str())
            .collect();
        assert_eq!(registered_events, StreamStatusTracker::EVENTS.to_vec());
        let unregistered = Rc::clone(&hooks.unregistered);
        unregister();
        assert_eq!(
            unregistered.borrow().len(),
            StreamStatusTracker::EVENTS.len()
        );
    }

    /// Not a Python pin by itself: the FakeHooks `emit` path in the Python
    /// file exercises `handle_event` only through QueueBridge tests.
    /// This keeps the tracker's hook entrypoint (`consume` + the constant
    /// `continue` HookResult) covered until that unit ports.
    #[test]
    fn handle_event_consumes_and_continues() {
        let mut tracker = StreamStatusTracker::new(ROOT).with_clock(|| 0.0);
        let result = tracker.handle_event("llm:stream_block_delta", &delta("delta", "hi"));
        assert_eq!(result.action, "continue");
        assert_eq!(tracker.preview(), Some(("text".to_string(), "hi".to_string())));
    }
}
