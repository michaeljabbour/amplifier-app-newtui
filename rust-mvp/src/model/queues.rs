//! Bounded steering, next-turn message, and needs-you queues.
//!
//! Port of `src/amplifier_app_newtui/model/queues.py`.
//!
//! Steering contract (ADR-0007): exactly ONE steering path — a bounded
//! [`SteeringQueue`] (32 items / 32KB per item) consumed one-per-
//! `provider:request` step boundary on the root session. Leftover steers
//! are discarded at turn end (mockup: a steer the runtime never consumed
//! must not become a turn the user never sent).
//!
//! Needs-you contract (DESIGN-SPEC §7, ADR-0007 resolution 5): deferred
//! decisions never halt the turn. A deferred approval resolves to its
//! default (deny) at timeout, lands in the DenialLog AND stays retro-
//! answerable here — answering later injects a next-turn user instruction
//! (the mockup's `Applying decision: …` flow).
//!
//! Thread-safety: both queues are mutated from TWO event loops — the UI
//! loop (composer enqueue / answer) and the runtime thread's loop (steer
//! consume at step boundary, kernel-side defer). Each queue guards its
//! pending/items state and id counter with a [`std::sync::Mutex`]. Change
//! notification runs OUTSIDE the lock so a listener that re-reads the
//! queue (a common UI pattern) can never deadlock against the mutation
//! that woke it.

use std::collections::{HashMap, HashSet};
use std::fmt;
use std::sync::{Arc, Mutex, OnceLock};
use std::time::Instant;

use serde::{Deserialize, Serialize};

pub const MAX_QUEUE_ITEMS: usize = 32;
pub const MAX_ITEM_CHARS: usize = 32_768;

/// Handle returned by the `add_*listener` registrations; pass it to the
/// matching `remove_*listener` (the Python original returns a removal
/// closure instead — same semantics, removal is idempotent).
pub type ListenerId = u64;

type Clock = Box<dyn Fn() -> f64 + Send + Sync>;

/// Monotonic clock in fractional seconds (Python's `time.monotonic`),
/// anchored at first use within this process.
fn monotonic() -> f64 {
    static START: OnceLock<Instant> = OnceLock::new();
    START.get_or_init(Instant::now).elapsed().as_secs_f64()
}

/// Strip control characters (keeping newline/tab) and cap length (chars).
fn clean_multiline(value: &str, limit: usize) -> String {
    value
        .chars()
        .filter(|&ch| ch == '\n' || ch == '\t' || (ch as u32) >= 32)
        .take(limit)
        .collect()
}

fn clean_line(value: &str, limit: usize) -> String {
    clean_multiline(value, limit)
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}

/// Sanitize + de-dupe dependency keys (order-preserving), bounded `cap`.
fn clean_keys<S: AsRef<str>>(keys: &[S], limit: usize, cap: usize) -> Vec<String> {
    let mut cleaned: Vec<String> = Vec::new();
    for raw in keys.iter().take(cap) {
        let clean = clean_line(raw.as_ref(), limit);
        if !clean.is_empty() && !cleaned.contains(&clean) {
            cleaned.push(clean);
        }
    }
    cleaned
}

/// Errors mirroring the Python `ValueError` / `KeyError` split.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum QueueError {
    /// Python `ValueError` — the message text matches the original exactly.
    Value(String),
    /// Python `KeyError(f"unknown decision: {decision_id}")`.
    UnknownDecision(String),
}

impl fmt::Display for QueueError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            QueueError::Value(message) => write!(f, "{message}"),
            QueueError::UnknownDecision(id) => write!(f, "unknown decision: {id}"),
        }
    }
}

impl std::error::Error for QueueError {}

/// `kind` of a [`QueuedMessage`] (Python `Literal["steer", "next_turn"]`).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub enum MessageKind {
    #[default]
    #[serde(rename = "steer")]
    Steer,
    #[serde(rename = "next_turn")]
    NextTurn,
}

impl fmt::Display for MessageKind {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            MessageKind::Steer => "steer",
            MessageKind::NextTurn => "next_turn",
        })
    }
}

/// One queued item: a mid-turn steer or a full next-turn message.
///
/// `kind = Steer` applies at the next step boundary of the running turn;
/// `kind = NextTurn` runs as its own turn when the current one ends
/// (footer `q1` badge, `▹ queued next:` strip). Frozen in Python —
/// treated as immutable here (no mutation after construction).
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct QueuedMessage {
    pub message_id: String,
    pub text: String,
    #[serde(default)]
    pub kind: MessageKind,
    #[serde(default)]
    pub created_at: f64,
}

type Callback<A> = Arc<dyn Fn(&A) + Send + Sync>;

/// Shared change-notification plumbing (Python `_ListenerMixin`).
struct Callbacks<A: ?Sized> {
    inner: Mutex<CallbackState<A>>,
}

struct CallbackState<A: ?Sized> {
    next_id: ListenerId,
    entries: Vec<(ListenerId, Callback<A>)>,
}

impl<A: ?Sized> Callbacks<A> {
    fn new() -> Self {
        Self {
            inner: Mutex::new(CallbackState {
                next_id: 1,
                entries: Vec::new(),
            }),
        }
    }

    fn add(&self, listener: impl Fn(&A) + Send + Sync + 'static) -> ListenerId {
        let mut state = self.inner.lock().unwrap();
        let id = state.next_id;
        state.next_id += 1;
        state.entries.push((id, Arc::new(listener)));
        id
    }

    fn remove(&self, id: ListenerId) {
        let mut state = self.inner.lock().unwrap();
        state.entries.retain(|(entry_id, _)| *entry_id != id);
    }

    /// Snapshot then call OUTSIDE the lock (Python `tuple(self._listeners)`).
    fn notify(&self, arg: &A) {
        let snapshot: Vec<_> = self
            .inner
            .lock()
            .unwrap()
            .entries
            .iter()
            .map(|(_, listener)| Arc::clone(listener))
            .collect();
        for listener in snapshot {
            listener(arg);
        }
    }
}

// --- steering queue (bounded 32/32KB) ----------------------------------------

struct SteeringState {
    next_id: u64,
    pending: Vec<QueuedMessage>,
}

/// Bounded FIFO of mid-turn steers and queued next-turn messages.
///
/// Bounds: [`MAX_QUEUE_ITEMS`] items, [`MAX_ITEM_CHARS`] chars per item.
/// `enqueue` errors at the limit — the UI surfaces that as a notice; it
/// must never drop text silently.
pub struct SteeringQueue {
    clock: Clock,
    state: Mutex<SteeringState>,
    listeners: Callbacks<()>,
}

impl Default for SteeringQueue {
    fn default() -> Self {
        Self::new()
    }
}

impl SteeringQueue {
    pub fn new() -> Self {
        Self::with_clock(Box::new(monotonic))
    }

    pub fn with_clock(clock: Clock) -> Self {
        Self {
            clock,
            state: Mutex::new(SteeringState {
                next_id: 1,
                pending: Vec::new(),
            }),
            listeners: Callbacks::new(),
        }
    }

    /// Register a change callback; remove it via [`Self::remove_listener`].
    pub fn add_listener(&self, listener: impl Fn() + Send + Sync + 'static) -> ListenerId {
        self.listeners.add(move |_: &()| listener())
    }

    pub fn remove_listener(&self, id: ListenerId) {
        self.listeners.remove(id);
    }

    fn notify(&self) {
        self.listeners.notify(&());
    }

    pub fn pending(&self) -> Vec<QueuedMessage> {
        self.state.lock().unwrap().pending.clone()
    }

    pub fn pending_steers(&self) -> Vec<QueuedMessage> {
        self.state
            .lock()
            .unwrap()
            .pending
            .iter()
            .filter(|m| m.kind == MessageKind::Steer)
            .cloned()
            .collect()
    }

    /// Queued full next-turn messages (the footer `qN` count).
    pub fn pending_next_turn(&self) -> Vec<QueuedMessage> {
        self.state
            .lock()
            .unwrap()
            .pending
            .iter()
            .filter(|m| m.kind == MessageKind::NextTurn)
            .cloned()
            .collect()
    }

    /// Queue a steer or next-turn message; errors when full or empty after
    /// sanitizing.
    ///
    /// The next-turn slot holds exactly ONE message (mockup single slot,
    /// `this.queued = text`): a second `NextTurn` enqueue REPLACES the
    /// queued one, so the footer badge is only ever `· q1`.
    pub fn enqueue(&self, text: &str, kind: MessageKind) -> Result<QueuedMessage, QueueError> {
        let clean = clean_multiline(text, MAX_ITEM_CHARS);
        if clean.trim().is_empty() {
            return Err(QueueError::Value("queued text cannot be empty".into()));
        }
        let message = {
            let mut state = self.state.lock().unwrap();
            if kind == MessageKind::NextTurn {
                state.pending.retain(|m| m.kind != MessageKind::NextTurn);
            }
            if state.pending.len() >= MAX_QUEUE_ITEMS {
                return Err(QueueError::Value("steering queue limit reached".into()));
            }
            let message = QueuedMessage {
                message_id: format!("q-{}", state.next_id),
                text: clean,
                kind,
                created_at: (self.clock)(),
            };
            state.next_id += 1;
            state.pending.push(message.clone());
            message
        };
        self.notify();
        Ok(message)
    }

    /// Pop the oldest steer (called once per `provider:request`).
    pub fn consume_next_steer(&self) -> Option<QueuedMessage> {
        self.consume_next_of_kind(MessageKind::Steer)
    }

    /// Pop the oldest queued next-turn message (called at turn end).
    pub fn consume_next_turn_message(&self) -> Option<QueuedMessage> {
        self.consume_next_of_kind(MessageKind::NextTurn)
    }

    fn consume_next_of_kind(&self, kind: MessageKind) -> Option<QueuedMessage> {
        let popped = {
            let mut state = self.state.lock().unwrap();
            let index = state.pending.iter().position(|m| m.kind == kind);
            index.map(|i| state.pending.remove(i))
        };
        if popped.is_some() {
            self.notify();
        }
        popped
    }

    /// Remove and return all leftover steers (turn ended before they
    /// applied) — the app discards them at turn end (mockup §5).
    pub fn drain_steers(&self) -> Vec<QueuedMessage> {
        let leftover: Vec<QueuedMessage> = {
            let mut state = self.state.lock().unwrap();
            let leftover: Vec<QueuedMessage> = state
                .pending
                .iter()
                .filter(|m| m.kind == MessageKind::Steer)
                .cloned()
                .collect();
            if !leftover.is_empty() {
                state.pending.retain(|m| m.kind != MessageKind::Steer);
            }
            leftover
        };
        if !leftover.is_empty() {
            self.notify();
        }
        leftover
    }
}

// --- per-lane steering (issue #39) --------------------------------------------

struct LaneSteeringState {
    next_id: u64,
    pending: HashMap<String, Vec<QueuedMessage>>,
}

/// Per-lane steering: a bounded steer FIFO per running delegate.
///
/// The root [`SteeringQueue`] steers the coordinator; this steers a
/// *child* session (issue #39). It mirrors the same next-boundary
/// semantics — each queued message is delivered at that delegate's next
/// `provider:request` step boundary — but keys the FIFOs by child
/// `session_id` so every live lane gets its own queue.
///
/// Bounds match [`SteeringQueue`]: [`MAX_QUEUE_ITEMS`] items /
/// [`MAX_ITEM_CHARS`] chars per item, per lane. `enqueue` errors when
/// full or empty — the UI surfaces that as a notice; typed text is never
/// dropped silently.
pub struct LaneSteeringQueue {
    clock: Clock,
    state: Mutex<LaneSteeringState>,
    listeners: Callbacks<()>,
}

impl Default for LaneSteeringQueue {
    fn default() -> Self {
        Self::new()
    }
}

impl LaneSteeringQueue {
    pub fn new() -> Self {
        Self::with_clock(Box::new(monotonic))
    }

    pub fn with_clock(clock: Clock) -> Self {
        Self {
            clock,
            state: Mutex::new(LaneSteeringState {
                next_id: 1,
                pending: HashMap::new(),
            }),
            listeners: Callbacks::new(),
        }
    }

    pub fn add_listener(&self, listener: impl Fn() + Send + Sync + 'static) -> ListenerId {
        self.listeners.add(move |_: &()| listener())
    }

    pub fn remove_listener(&self, id: ListenerId) {
        self.listeners.remove(id);
    }

    fn notify(&self) {
        self.listeners.notify(&());
    }

    /// Queue a steer for the delegate `session_id`; errors when that
    /// lane's queue is full or the text is empty.
    pub fn enqueue(&self, session_id: &str, text: &str) -> Result<QueuedMessage, QueueError> {
        if session_id.is_empty() {
            return Err(QueueError::Value("lane steering needs a session id".into()));
        }
        let clean = clean_multiline(text, MAX_ITEM_CHARS);
        if clean.trim().is_empty() {
            return Err(QueueError::Value("queued text cannot be empty".into()));
        }
        let message = {
            let mut state = self.state.lock().unwrap();
            let next_id = state.next_id;
            let queue = state.pending.entry(session_id.to_string()).or_default();
            if queue.len() >= MAX_QUEUE_ITEMS {
                return Err(QueueError::Value("lane steering queue limit reached".into()));
            }
            let message = QueuedMessage {
                message_id: format!("lane-{next_id}"),
                text: clean,
                kind: MessageKind::Steer,
                created_at: (self.clock)(),
            };
            queue.push(message.clone());
            state.next_id += 1;
            message
        };
        self.notify();
        Ok(message)
    }

    /// The lane's queued steers, oldest first.
    pub fn pending_for(&self, session_id: &str) -> Vec<QueuedMessage> {
        self.state
            .lock()
            .unwrap()
            .pending
            .get(session_id)
            .cloned()
            .unwrap_or_default()
    }

    /// Depth of one lane's queue — the `N queued` lane-row badge.
    pub fn queued_count(&self, session_id: &str) -> usize {
        self.state
            .lock()
            .unwrap()
            .pending
            .get(session_id)
            .map_or(0, Vec::len)
    }

    /// `{session_id: depth}` for every lane with queued steers.
    pub fn counts(&self) -> HashMap<String, usize> {
        self.state
            .lock()
            .unwrap()
            .pending
            .iter()
            .filter(|(_, queue)| !queue.is_empty())
            .map(|(sid, queue)| (sid.clone(), queue.len()))
            .collect()
    }

    pub fn total_pending(&self) -> usize {
        self.state
            .lock()
            .unwrap()
            .pending
            .values()
            .map(Vec::len)
            .sum()
    }

    /// Pop the lane's oldest steer (once per child `provider:request`).
    pub fn consume_next(&self, session_id: &str) -> Option<QueuedMessage> {
        let message = {
            let mut state = self.state.lock().unwrap();
            let queue = state.pending.get_mut(session_id)?;
            if queue.is_empty() {
                return None;
            }
            let message = queue.remove(0);
            if queue.is_empty() {
                state.pending.remove(session_id);
            }
            message
        };
        self.notify();
        Some(message)
    }

    /// Drop a finished lane's undelivered steers (it will never reach
    /// another step boundary) — the lane analogue of
    /// [`SteeringQueue::drain_steers`].
    pub fn drain(&self, session_id: &str) -> Vec<QueuedMessage> {
        let leftover = self
            .state
            .lock()
            .unwrap()
            .pending
            .remove(session_id)
            .unwrap_or_default();
        if !leftover.is_empty() {
            self.notify();
        }
        leftover
    }
}

// --- needs-you queue (DESIGN-SPEC §7) ----------------------------------------

/// Python `NeedsYouStatus = Literal["pending", "answered", "consumed",
/// "dismissed"]`.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub enum NeedsYouStatus {
    #[default]
    #[serde(rename = "pending")]
    Pending,
    #[serde(rename = "answered")]
    Answered,
    #[serde(rename = "consumed")]
    Consumed,
    #[serde(rename = "dismissed")]
    Dismissed,
}

impl fmt::Display for NeedsYouStatus {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            NeedsYouStatus::Pending => "pending",
            NeedsYouStatus::Answered => "answered",
            NeedsYouStatus::Consumed => "consumed",
            NeedsYouStatus::Dismissed => "dismissed",
        })
    }
}

/// One deferred decision awaiting the human (DESIGN-SPEC §7).
///
/// `choices` are the inline actionable chip labels (e.g.
/// `yes · push to fork`); `answer` is filled when the human acts.
/// Frozen in Python — treated as immutable here.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NeedsYouItem {
    pub decision_id: String,
    pub question: String,
    #[serde(default)]
    pub reason: String,
    #[serde(default)]
    pub choices: Vec<String>,
    /// Substring of `question` the UI accents teal (mockup `mj/waypoint`).
    #[serde(default)]
    pub highlight: String,
    /// The denied action this decision defers (joins override records to
    /// the DenialLog for /improve trust-slot evidence).
    #[serde(default)]
    pub action: String,
    /// Keys a later tool call may DEPEND ON while this decision is parked:
    /// the denied action itself and any declared orchestration ids. A
    /// dependent step matching one of these is denied-and-continued until
    /// the decision is answered.
    #[serde(default)]
    pub dependencies: Vec<String>,
    #[serde(default)]
    pub status: NeedsYouStatus,
    #[serde(default)]
    pub answer: String,
    #[serde(default)]
    pub created_at: f64,
}

/// Optional fields of [`NeedsYouQueue::defer`] (Python keyword arguments).
#[derive(Clone, Debug, Default)]
pub struct DeferOptions {
    pub choices: Vec<String>,
    pub highlight: String,
    pub action: String,
    pub dependencies: Vec<String>,
}

struct NeedsYouState {
    next_id: u64,
    items: Vec<NeedsYouItem>,
}

/// Deferred-decision queue behind the footer `N decisions waiting ·
/// ctrl-y` badge and the Needs-you block.
///
/// Lifecycle: `defer` → `answer` (human acts; logs `Applying
/// decision: …`) → `consume_answered` (the answer became a next-turn
/// instruction). `dismiss` drops a decision without acting.
pub struct NeedsYouQueue {
    clock: Clock,
    state: Mutex<NeedsYouState>,
    listeners: Callbacks<()>,
    defer_listeners: Callbacks<NeedsYouItem>,
}

impl Default for NeedsYouQueue {
    fn default() -> Self {
        Self::new()
    }
}

impl NeedsYouQueue {
    const MAX_DECISIONS: usize = 100;

    pub fn new() -> Self {
        Self::with_clock(Box::new(monotonic))
    }

    pub fn with_clock(clock: Clock) -> Self {
        Self {
            clock,
            state: Mutex::new(NeedsYouState {
                next_id: 1,
                items: Vec::new(),
            }),
            listeners: Callbacks::new(),
            defer_listeners: Callbacks::new(),
        }
    }

    pub fn add_listener(&self, listener: impl Fn() + Send + Sync + 'static) -> ListenerId {
        self.listeners.add(move |_: &()| listener())
    }

    pub fn remove_listener(&self, id: ListenerId) {
        self.listeners.remove(id);
    }

    /// Register a per-item deferral callback; remove it via
    /// [`Self::remove_defer_listener`].
    ///
    /// Plain change listeners can't tell WHAT changed; the real runtime
    /// needs the created item (decision_id) to surface each kernel-side
    /// deferral as one UI event without re-deriving it from text.
    pub fn add_defer_listener(
        &self,
        listener: impl Fn(&NeedsYouItem) + Send + Sync + 'static,
    ) -> ListenerId {
        self.defer_listeners.add(listener)
    }

    pub fn remove_defer_listener(&self, id: ListenerId) {
        self.defer_listeners.remove(id);
    }

    fn notify(&self) {
        self.listeners.notify(&());
    }

    pub fn items(&self) -> Vec<NeedsYouItem> {
        self.state.lock().unwrap().items.clone()
    }

    pub fn pending(&self) -> Vec<NeedsYouItem> {
        self.state
            .lock()
            .unwrap()
            .items
            .iter()
            .filter(|item| item.status == NeedsYouStatus::Pending)
            .cloned()
            .collect()
    }

    /// The footer badge count (`N decisions waiting · ctrl-y`).
    pub fn pending_count(&self) -> usize {
        self.pending().len()
    }

    pub fn answered(&self) -> Vec<NeedsYouItem> {
        self.state
            .lock()
            .unwrap()
            .items
            .iter()
            .filter(|item| item.status == NeedsYouStatus::Answered)
            .cloned()
            .collect()
    }

    /// Parked (`Pending`) decisions that block any of `dependencies`.
    ///
    /// A tool call `depends on` a decision when they share a dependency
    /// key. Only `Pending` decisions block: answering one lets its
    /// dependents proceed (DESIGN-SPEC §7 — a deferred decision never
    /// halts *unrelated* work, but a step that literally needs the parked
    /// answer waits for it). Empty / unmatched keys never block.
    pub fn blocking_decisions<I, S>(&self, dependencies: I) -> Vec<NeedsYouItem>
    where
        I: IntoIterator<Item = S>,
        S: AsRef<str>,
    {
        let keys: HashSet<String> = dependencies
            .into_iter()
            .map(|raw| clean_line(raw.as_ref(), 200))
            .filter(|key| !key.is_empty())
            .collect();
        if keys.is_empty() {
            return Vec::new();
        }
        self.state
            .lock()
            .unwrap()
            .items
            .iter()
            .filter(|item| {
                item.status == NeedsYouStatus::Pending
                    && item.dependencies.iter().any(|dep| keys.contains(dep))
            })
            .cloned()
            .collect()
    }

    /// Whether an unanswered parked decision blocks `dependency`.
    pub fn dependency_blocked(&self, dependency: &str) -> bool {
        !self.blocking_decisions([dependency]).is_empty()
    }

    /// Park a decision for later; errors when full/empty.
    ///
    /// `dependencies` are the keys a later tool call may DEPEND ON while
    /// this decision is parked (the denied action, declared orchestration
    /// ids). [`Self::blocking_decisions`] matches them so a dependent step
    /// is denied-and-continued until the human answers.
    pub fn defer(
        &self,
        question: &str,
        reason: &str,
        options: DeferOptions,
    ) -> Result<NeedsYouItem, QueueError> {
        let item = {
            let mut state = self.state.lock().unwrap();
            let active = state
                .items
                .iter()
                .filter(|item| {
                    matches!(
                        item.status,
                        NeedsYouStatus::Pending | NeedsYouStatus::Answered
                    )
                })
                .count();
            if active >= Self::MAX_DECISIONS {
                return Err(QueueError::Value("deferred decision limit reached".into()));
            }
            let clean_question = clean_line(question, 4_096);
            if clean_question.is_empty() {
                return Err(QueueError::Value("decision question cannot be empty".into()));
            }
            let item = NeedsYouItem {
                decision_id: format!("decision-{}", state.next_id),
                question: clean_question,
                reason: clean_line(reason, 4_096),
                choices: options
                    .choices
                    .iter()
                    .map(|choice| clean_line(choice, 200))
                    .filter(|choice| !choice.is_empty())
                    .collect(),
                highlight: clean_line(&options.highlight, 200),
                action: clean_line(&options.action, 4_096),
                dependencies: clean_keys(&options.dependencies, 200, 100),
                status: NeedsYouStatus::Pending,
                answer: String::new(),
                created_at: (self.clock)(),
            };
            state.next_id += 1;
            state.items.push(item.clone());
            item
        };
        self.notify();
        self.defer_listeners.notify(&item);
        Ok(item)
    }

    /// Record the human's answer (drives `Applying decision: …`).
    pub fn answer(&self, decision_id: &str, answer: &str) -> Result<NeedsYouItem, QueueError> {
        let clean_answer = clean_line(answer, 4_096);
        if clean_answer.is_empty() {
            return Err(QueueError::Value("decision answer cannot be empty".into()));
        }
        self.transition(decision_id, NeedsYouStatus::Answered, &clean_answer)
    }

    pub fn dismiss(&self, decision_id: &str) -> Result<NeedsYouItem, QueueError> {
        self.transition(decision_id, NeedsYouStatus::Dismissed, "")
    }

    /// Mark all answered decisions consumed (their answers were injected
    /// as next-turn instructions); returns what was consumed.
    pub fn consume_answered(&self) -> Vec<NeedsYouItem> {
        let consumed: Vec<NeedsYouItem> = {
            let mut state = self.state.lock().unwrap();
            let mut consumed = Vec::new();
            for item in state.items.iter_mut() {
                if item.status == NeedsYouStatus::Answered {
                    item.status = NeedsYouStatus::Consumed;
                    consumed.push(item.clone());
                }
            }
            consumed
        };
        if !consumed.is_empty() {
            self.notify();
        }
        consumed
    }

    fn transition(
        &self,
        decision_id: &str,
        status: NeedsYouStatus,
        answer: &str,
    ) -> Result<NeedsYouItem, QueueError> {
        let updated = {
            let mut state = self.state.lock().unwrap();
            let Some(item) = state
                .items
                .iter_mut()
                .find(|item| item.decision_id == decision_id)
            else {
                return Err(QueueError::UnknownDecision(decision_id.to_string()));
            };
            if item.status != NeedsYouStatus::Pending {
                return Err(QueueError::Value(format!(
                    "decision is already {}",
                    item.status
                )));
            }
            item.status = status;
            item.answer = answer.to_string();
            item.clone()
        };
        self.notify();
        Ok(updated)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // --- steering queue (bounded 32/32KB) ------------------------------------

    #[test]
    fn test_steering_queue_steer_vs_next_turn() {
        let queue = SteeringQueue::new();
        queue.enqueue("focus on tests", MessageKind::Steer).unwrap();
        queue
            .enqueue("then update docs", MessageKind::NextTurn)
            .unwrap();
        assert_eq!(queue.pending_steers().len(), 1);
        assert_eq!(queue.pending_next_turn().len(), 1);
        let steer = queue.consume_next_steer().expect("steer queued");
        assert_eq!(steer.text, "focus on tests");
        assert!(queue.consume_next_steer().is_none());
        let follow_up = queue.consume_next_turn_message().expect("next-turn queued");
        assert_eq!(follow_up.text, "then update docs");
    }

    #[test]
    fn test_next_turn_slot_replaces_on_second_enqueue() {
        // Mockup single slot (`this.queued = text`): a second next-turn
        // message replaces the first — the footer badge is only ever q1.
        let queue = SteeringQueue::new();
        queue
            .enqueue("first follow-up", MessageKind::NextTurn)
            .unwrap();
        queue
            .enqueue("second follow-up", MessageKind::NextTurn)
            .unwrap();
        assert_eq!(queue.pending_next_turn().len(), 1);
        assert_eq!(queue.pending_next_turn()[0].text, "second follow-up");
        let picked = queue.consume_next_turn_message().expect("one queued");
        assert_eq!(picked.text, "second follow-up");
        assert!(queue.consume_next_turn_message().is_none());
    }

    #[test]
    fn test_steering_queue_bounds() {
        let queue = SteeringQueue::new();
        for i in 0..MAX_QUEUE_ITEMS {
            queue
                .enqueue(&format!("steer {i}"), MessageKind::Steer)
                .unwrap();
        }
        let err = queue
            .enqueue("one too many", MessageKind::Steer)
            .expect_err("queue is full");
        assert!(err.to_string().contains("limit"));
        assert_eq!(queue.pending().len(), MAX_QUEUE_ITEMS); // queue left intact
    }

    #[test]
    fn test_steering_queue_truncates_oversized_text() {
        let queue = SteeringQueue::new();
        let message = queue
            .enqueue(&"x".repeat(40_000), MessageKind::Steer)
            .unwrap();
        assert_eq!(message.text.chars().count(), 32_768);
    }

    #[test]
    fn test_drain_steers_removes_leftovers_for_discard() {
        let queue = SteeringQueue::new();
        queue.enqueue("a", MessageKind::Steer).unwrap();
        queue.enqueue("b", MessageKind::NextTurn).unwrap();
        let leftover = queue.drain_steers();
        assert_eq!(
            leftover.iter().map(|m| m.text.as_str()).collect::<Vec<_>>(),
            vec!["a"]
        );
        assert_eq!(
            queue
                .pending()
                .iter()
                .map(|m| m.text.as_str())
                .collect::<Vec<_>>(),
            vec!["b"]
        );
    }

    #[test]
    fn test_steering_queue_rejects_empty() {
        let err = SteeringQueue::new()
            .enqueue("   ", MessageKind::Steer)
            .expect_err("empty text rejected");
        assert_eq!(
            err,
            QueueError::Value("queued text cannot be empty".into())
        );
    }

    // --- needs-you queue (DESIGN-SPEC §7) -------------------------------------

    #[test]
    fn test_needs_you_lifecycle() {
        let queue = NeedsYouQueue::new();
        let item = queue
            .defer(
                "push to fork?",
                "no push permission",
                DeferOptions {
                    choices: vec!["yes · push to fork".to_string()],
                    ..DeferOptions::default()
                },
            )
            .unwrap();
        assert_eq!(queue.pending_count(), 1);
        let answered = queue.answer(&item.decision_id, "yes").unwrap();
        assert_eq!(answered.status, NeedsYouStatus::Answered);
        assert_eq!(queue.pending_count(), 0);
        let consumed = queue.consume_answered();
        assert_eq!(
            consumed
                .iter()
                .map(|c| c.decision_id.as_str())
                .collect::<Vec<_>>(),
            vec![item.decision_id.as_str()]
        );
        assert_eq!(consumed[0].status, NeedsYouStatus::Consumed);
    }

    #[test]
    fn test_needs_you_cannot_answer_twice() {
        let queue = NeedsYouQueue::new();
        let item = queue.defer("q?", "r", DeferOptions::default()).unwrap();
        queue.answer(&item.decision_id, "yes").unwrap();
        let err = queue
            .answer(&item.decision_id, "no")
            .expect_err("already answered");
        assert_eq!(
            err,
            QueueError::Value("decision is already answered".into())
        );
    }

    #[test]
    fn test_needs_you_listener_fires() {
        let queue = NeedsYouQueue::new();
        let calls: Arc<Mutex<Vec<i32>>> = Arc::new(Mutex::new(Vec::new()));
        let sink = Arc::clone(&calls);
        let listener_id = queue.add_listener(move || sink.lock().unwrap().push(1));
        queue.defer("q?", "r", DeferOptions::default()).unwrap();
        assert!(!calls.lock().unwrap().is_empty());
        queue.remove_listener(listener_id);
        queue.defer("q2?", "r", DeferOptions::default()).unwrap();
        assert_eq!(calls.lock().unwrap().len(), 1);
    }

    #[test]
    fn test_needs_you_dependency_blocks_only_matching_keys() {
        let queue = NeedsYouQueue::new();
        let item = queue
            .defer(
                "Allow git push origin main?",
                "unrequested push",
                DeferOptions {
                    action: "git push origin main".to_string(),
                    dependencies: vec![
                        "git push origin main".to_string(),
                        "push-step".to_string(),
                    ],
                    ..DeferOptions::default()
                },
            )
            .unwrap();
        assert_eq!(
            item.dependencies,
            vec!["git push origin main".to_string(), "push-step".to_string()]
        );
        // Matches by action key and by declared orchestration id...
        assert!(queue.dependency_blocked("git push origin main"));
        assert!(queue.dependency_blocked("push-step"));
        assert_eq!(queue.blocking_decisions(["push-step"]), vec![item]);
        // ...but an unrelated call sharing no key is never blocked.
        assert!(!queue.dependency_blocked("read_file · a.py"));
        assert!(queue.blocking_decisions(["deploy-step"]).is_empty());
        assert!(queue.blocking_decisions(Vec::<&str>::new()).is_empty());
    }

    #[test]
    fn test_needs_you_answer_unblocks_dependents() {
        let queue = NeedsYouQueue::new();
        let item = queue
            .defer(
                "Allow deploy?",
                "waits on push",
                DeferOptions {
                    dependencies: vec!["push-step".to_string()],
                    ..DeferOptions::default()
                },
            )
            .unwrap();
        assert!(queue.dependency_blocked("push-step"));
        queue.answer(&item.decision_id, "yes").unwrap();
        // Only PENDING decisions block: answering lets its dependents proceed.
        assert!(!queue.dependency_blocked("push-step"));
        assert!(queue.blocking_decisions(["push-step"]).is_empty());
    }

    #[test]
    fn test_needs_you_dependencies_are_cleaned_and_deduped() {
        let queue = NeedsYouQueue::new();
        let item = queue
            .defer(
                "q?",
                "r",
                DeferOptions {
                    dependencies: vec![
                        "push-step".to_string(),
                        "push-step".to_string(),
                        "  ".to_string(),
                        "x".repeat(500),
                    ],
                    ..DeferOptions::default()
                },
            )
            .unwrap();
        assert_eq!(
            item.dependencies,
            vec!["push-step".to_string(), "x".repeat(200)]
        );
    }
}
