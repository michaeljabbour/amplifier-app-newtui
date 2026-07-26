//! ApprovalBroker: the app's ApprovalSystem implementation (ADR-0007 §Approvals).
//!
//! Port of `src/amplifier_app_newtui/kernel/approval.py` — a request broker
//! with a FIFO of [`ApprovalTicket`]s. The Python kernel-facing contract is
//! the 4-point-boundary signature
//! `async request_approval(prompt, options, timeout, default) -> str`; here
//! the asyncio future/timeout plumbing stays in the Python backend, and the
//! Rust client drives the same ticket/decision state machine synchronously:
//!
//! - [`ApprovalBroker::request_approval`] opens a ticket and returns its id
//!   (the Python coroutine parked on a future at this point).
//! - [`ApprovalBroker::answer`] resolves a ticket with the human's choice
//!   (the future's result) and retires it from the FIFO.
//! - [`ApprovalBroker::resolve_timeout`] applies the timeout-to-default rule
//!   (the Python `asyncio.timeout` branch): `Allow once` for an allow
//!   default, `Deny` otherwise, recording the denial on the [`DenialLog`].
//!
//! We own both ends of the approval path (the governance hook stages a
//! structured [`ApprovalDetail`] on this broker; the kernel then calls
//! `request_approval` with the same prompt), so rich detail travels through
//! the broker itself — no module-global keyed-by-prompt smuggling. Staging is
//! instance-scoped and consumed FIFO per prompt, so concurrent identical
//! prompts pair with their details in request order.
//!
//! Fail-closed invariants (Rust string-matches "Allow"-family options):
//!
//! - Presented options ALWAYS contain the verbatim strings `Allow once` /
//!   `Allow always` / `Deny`.
//! - Timeouts resolve to the ticket's default (deny unless stated otherwise).
//! - Deferrals are parked directly into the `NeedsYouQueue` by governance
//!   (classifier denials → `NeedsYouQueue::defer`), NOT by the broker: a
//!   deny-and-continue ticket times out to its default here while the
//!   needs-you item stays retro-answerable (ADR-0007 resolution 5).
//!
//! "Allow always" persistence is NOT handled here (user directive:
//! permissions are managed natively) — the asker (hooks-approval) receives
//! the choice string back and owns remember/allow-always bookkeeping.

use std::collections::{HashMap, VecDeque};
use std::fmt;
use std::sync::{Arc, Mutex, OnceLock};
use std::time::Instant;

use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

use crate::model::queues::{ListenerId, NeedsYouQueue};
use crate::model::trust::{CapabilityClass, DenialLog};

pub const ALLOW_ONCE: &str = "Allow once";
pub const ALLOW_ALWAYS: &str = "Allow always";
pub const DENY: &str = "Deny";
pub const STANDARD_OPTIONS: [&str; 3] = [ALLOW_ONCE, ALLOW_ALWAYS, DENY];

/// Python `request_approval` default timeout (seconds).
pub const DEFAULT_TIMEOUT: f64 = 300.0;

const ALLOW_FAMILY: [&str; 3] = ["allow", "allow once", "allow always"];

type Clock = Box<dyn Fn() -> f64 + Send + Sync>;

/// Monotonic clock in fractional seconds (Python's `time.monotonic`),
/// anchored at first use within this process.
fn monotonic() -> f64 {
    static START: OnceLock<Instant> = OnceLock::new();
    START.get_or_init(Instant::now).elapsed().as_secs_f64()
}

/// `ApprovalDefault = Literal["allow", "deny"]`.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum ApprovalDefault {
    #[serde(rename = "allow")]
    Allow,
    #[default]
    #[serde(rename = "deny")]
    Deny,
}

/// Errors mirroring the Python `ValueError` / `KeyError` split.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ApprovalError {
    /// Python `ValueError` — the message text matches the original exactly.
    Value(String),
    /// Python `KeyError(f"unknown approval ticket: {ticket_id}")`.
    UnknownTicket(String),
}

impl fmt::Display for ApprovalError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ApprovalError::Value(message) => write!(f, "{message}"),
            ApprovalError::UnknownTicket(id) => write!(f, "unknown approval ticket: {id}"),
        }
    }
}

impl std::error::Error for ApprovalError {}

/// Structured payload behind one approval prompt (ctrl-a detail view).
///
/// Fields mirror the mockup's detail rows: command, cwd, the trust rule
/// that fired, and the capability class. Frozen in Python
/// (`frozen=True, extra="forbid"`): treated as immutable by convention
/// here; unknown fields are rejected on deserialization.
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ApprovalDetail {
    #[serde(default)]
    pub command: String,
    #[serde(default)]
    pub cwd: String,
    #[serde(default)]
    pub rule: String,
    #[serde(default)]
    pub capability: String,
    #[serde(default)]
    pub tool_name: String,
    #[serde(default)]
    pub tool_input: Map<String, Value>,
}

/// True when *choice* is in the fail-closed Allow family.
pub fn is_allow(choice: &str) -> bool {
    let folded = choice.trim().to_lowercase();
    ALLOW_FAMILY.contains(&folded.as_str())
}

/// One in-flight approval request (FIFO position = arrival order).
///
/// The Python dataclass carries the `asyncio.Future` the coroutine parks on;
/// here resolution happens through the broker by `ticket_id`
/// ([`ApprovalBroker::answer`] / [`ApprovalBroker::resolve_timeout`]).
#[derive(Clone, Debug, PartialEq)]
pub struct ApprovalTicket {
    pub ticket_id: String,
    pub prompt: String,
    pub options: Vec<String>,
    pub detail: ApprovalDetail,
    pub timeout: f64,
    pub default: ApprovalDefault,
    pub created_at: f64,
}

type Listener = Arc<dyn Fn() + Send + Sync>;

struct BrokerState {
    next_id: u64,
    tickets: Vec<ApprovalTicket>,
    staged: HashMap<String, VecDeque<ApprovalDetail>>,
}

struct ListenerState {
    next_id: ListenerId,
    entries: Vec<(ListenerId, Listener)>,
}

/// FIFO approval request broker (kernel ApprovalSystem implementation).
///
/// The inline approval bar answers [`ApprovalBroker::head`]. UI listeners
/// fire on every queue change.
pub struct ApprovalBroker {
    needs_you: Option<Arc<NeedsYouQueue>>,
    denial_log: Option<Arc<Mutex<DenialLog>>>,
    clock: Clock,
    /// Floor for ticket timeouts. An interactive app sets this HIGH:
    /// the kernel's default (300s) silently timed approvals out to deny
    /// while the supervisor was still reading the plan (found live —
    /// every file write of a run "came back denied" untouched).
    min_timeout: f64,
    state: Mutex<BrokerState>,
    listeners: Mutex<ListenerState>,
}

impl ApprovalBroker {
    /// Python constructor defaults: no queues, `clock=monotonic`,
    /// `min_timeout=0.0`.
    pub fn new() -> Self {
        Self::with_config(None, None, Box::new(monotonic), 0.0)
    }

    /// Full constructor (Python keyword arguments).
    pub fn with_config(
        needs_you: Option<Arc<NeedsYouQueue>>,
        denial_log: Option<Arc<Mutex<DenialLog>>>,
        clock: Clock,
        min_timeout: f64,
    ) -> Self {
        Self {
            needs_you,
            denial_log,
            clock,
            min_timeout,
            state: Mutex::new(BrokerState {
                next_id: 1,
                tickets: Vec::new(),
                staged: HashMap::new(),
            }),
            listeners: Mutex::new(ListenerState {
                next_id: 1,
                entries: Vec::new(),
            }),
        }
    }

    // -- introspection ------------------------------------------------------

    /// All unresolved tickets in FIFO order.
    pub fn pending(&self) -> Vec<ApprovalTicket> {
        self.state.lock().unwrap().tickets.clone()
    }

    /// The ticket the inline approval bar is answering (the oldest
    /// pending ticket).
    pub fn head(&self) -> Option<ApprovalTicket> {
        self.state.lock().unwrap().tickets.first().cloned()
    }

    /// The needs-you queue governance parks deferrals into (held for
    /// wiring parity with the Python constructor; the broker itself never
    /// defers — ADR-0007 resolution 5).
    pub fn needs_you(&self) -> Option<&Arc<NeedsYouQueue>> {
        self.needs_you.as_ref()
    }

    /// Register a change listener; returns a handle for
    /// [`ApprovalBroker::remove_listener`] (the Python original returns a
    /// removal closure instead — same semantics, removal is idempotent).
    pub fn add_listener(&self, listener: impl Fn() + Send + Sync + 'static) -> ListenerId {
        let mut state = self.listeners.lock().unwrap();
        let id = state.next_id;
        state.next_id += 1;
        state.entries.push((id, Arc::new(listener)));
        id
    }

    pub fn remove_listener(&self, id: ListenerId) {
        let mut state = self.listeners.lock().unwrap();
        state.entries.retain(|(entry_id, _)| *entry_id != id);
    }

    // -- detail staging (governance hook side) -------------------------------

    /// Attach structured detail to the next [`ApprovalBroker::request_approval`]
    /// call bearing *prompt*. Instance-scoped, FIFO per prompt.
    pub fn stage_detail(&self, prompt: &str, detail: ApprovalDetail) {
        self.state
            .lock()
            .unwrap()
            .staged
            .entry(prompt.to_string())
            .or_default()
            .push_back(detail);
    }

    /// Consume the oldest staged detail for *prompt* (empty detail when none
    /// is staged). Python-private `_pop_staged`; public here because the
    /// governance seam and its tests inspect staged pairing through it.
    pub fn pop_staged(&self, prompt: &str) -> ApprovalDetail {
        let mut state = self.state.lock().unwrap();
        Self::pop_staged_locked(&mut state, prompt)
    }

    fn pop_staged_locked(state: &mut BrokerState, prompt: &str) -> ApprovalDetail {
        let Some(queue) = state.staged.get_mut(prompt) else {
            return ApprovalDetail::default();
        };
        let detail = queue.pop_front().unwrap_or_default();
        if queue.is_empty() {
            state.staged.remove(prompt);
        }
        detail
    }

    // -- kernel-facing contract ----------------------------------------------

    /// Open one approval ticket and return its id.
    ///
    /// Python: `async request_approval(...)` parks on a future here and
    /// resolves via [`ApprovalBroker::answer`] or timeout-to-default
    /// ([`ApprovalBroker::resolve_timeout`]); it never raises to the kernel.
    /// Python defaults: `options=None`, `timeout=300.0`
    /// ([`DEFAULT_TIMEOUT`]), `default="deny"`.
    ///
    /// NO local "Allow always" bookkeeping (user directive): the asker
    /// (natively, hooks-approval) owns allow-always persistence — it
    /// receives the choice string back and stops asking. A second
    /// remember table here would shadow the native one.
    pub fn request_approval<I, S>(
        &self,
        prompt: &str,
        options: I,
        timeout: f64,
        default: ApprovalDefault,
    ) -> String
    where
        I: IntoIterator<Item = S>,
        S: AsRef<str>,
    {
        let timeout = timeout.max(self.min_timeout);
        let ticket_id;
        {
            let mut state = self.state.lock().unwrap();
            let detail = Self::pop_staged_locked(&mut state, prompt);
            ticket_id = format!("approval-{}", state.next_id);
            state.next_id += 1;
            state.tickets.push(ApprovalTicket {
                ticket_id: ticket_id.clone(),
                prompt: prompt.to_string(),
                options: presented_options(options),
                detail,
                timeout,
                default,
                created_at: (self.clock)(),
            });
        }
        self.notify();
        ticket_id
    }

    // -- UI-facing actions ---------------------------------------------------

    /// Resolve one pending ticket with the human's *choice*; returns the
    /// choice the Python future would deliver back to the kernel.
    ///
    /// Errors with [`ApprovalError::UnknownTicket`] (Python `KeyError`) for
    /// unknown/already-resolved tickets and [`ApprovalError::Value`] (Python
    /// `ValueError`) for a choice not among the presented options.
    pub fn answer(&self, ticket_id: &str, choice: &str) -> Result<String, ApprovalError> {
        {
            let mut state = self.state.lock().unwrap();
            let index = Self::find_locked(&state, ticket_id)?;
            let ticket = &state.tickets[index];
            if !ticket.options.iter().any(|option| option == choice) {
                return Err(ApprovalError::Value(format!(
                    "choice {} is not one of {}",
                    py_str_repr(choice),
                    py_tuple_repr(&ticket.options),
                )));
            }
            state.tickets.remove(index);
        }
        self.notify();
        Ok(choice.to_string())
    }

    /// Apply the timeout-to-default rule to one pending ticket (the Python
    /// `asyncio.timeout` branch): `Allow once` for an allow default, `Deny`
    /// otherwise — a deny records on the [`DenialLog`]. The caller (the
    /// protocol client's timer) decides WHEN a ticket has timed out;
    /// [`ApprovalTicket::timeout`] / [`ApprovalTicket::created_at`] carry
    /// the deadline inputs.
    pub fn resolve_timeout(&self, ticket_id: &str) -> Result<String, ApprovalError> {
        let ticket = {
            let mut state = self.state.lock().unwrap();
            let index = Self::find_locked(&state, ticket_id)?;
            state.tickets.remove(index)
        };
        let choice = match ticket.default {
            ApprovalDefault::Allow => ALLOW_ONCE,
            ApprovalDefault::Deny => DENY,
        };
        self.record_timeout(&ticket, choice);
        self.notify();
        Ok(choice.to_string())
    }

    // -- internals -----------------------------------------------------------

    fn record_timeout(&self, ticket: &ApprovalTicket, choice: &str) {
        if choice != DENY {
            return;
        }
        let Some(denial_log) = &self.denial_log else {
            return;
        };
        let capability = capability_or_exec(&ticket.detail.capability);
        let action = if ticket.detail.command.is_empty() {
            &ticket.prompt
        } else {
            &ticket.detail.command
        };
        // The reason is a non-empty constant, so recording cannot fail.
        let _ = denial_log.lock().unwrap().record_denial(
            capability,
            action,
            "approval timed out · denied by default",
        );
    }

    fn find_locked(state: &BrokerState, ticket_id: &str) -> Result<usize, ApprovalError> {
        state
            .tickets
            .iter()
            .position(|ticket| ticket.ticket_id == ticket_id)
            .ok_or_else(|| ApprovalError::UnknownTicket(ticket_id.to_string()))
    }

    /// Snapshot then call OUTSIDE the lock (Python `tuple(self._listeners)`).
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

impl Default for ApprovalBroker {
    fn default() -> Self {
        Self::new()
    }
}

/// First candidate appearing verbatim in *question* — the teal accent
/// substring of a needs-you row (DESIGN-SPEC §7). Candidates come from
/// the native approval payload (target/cwd before command); anything
/// absent from the question, empty, or beyond the queue's 200-char
/// highlight bound yields no accent rather than a broken one.
pub fn deferral_highlight<S: AsRef<str>>(question: &str, candidates: &[S]) -> String {
    for candidate in candidates {
        let clean = candidate
            .as_ref()
            .split_whitespace()
            .collect::<Vec<_>>()
            .join(" ");
        if !clean.is_empty() && clean.chars().count() <= 200 && question.contains(&clean) {
            return clean;
        }
    }
    String::new()
}

/// The options the approval bar shows: the verbatim standard triple,
/// plus any caller-provided options outside the standard/allow/deny set.
pub fn presented_options<I, S>(options: I) -> Vec<String>
where
    I: IntoIterator<Item = S>,
    S: AsRef<str>,
{
    let mut presented: Vec<String> = STANDARD_OPTIONS.iter().map(|s| s.to_string()).collect();
    presented.extend(
        options
            .into_iter()
            .map(|option| option.as_ref().to_string())
            .filter(|option| {
                !STANDARD_OPTIONS.contains(&option.as_str())
                    && !matches!(option.trim().to_lowercase().as_str(), "allow" | "deny")
            }),
    );
    presented
}

/// `CapabilityClass(value)` with EXEC as the `ValueError` fallback
/// (Python `_capability_or_exec`).
fn capability_or_exec(value: &str) -> CapabilityClass {
    match value {
        "read" => CapabilityClass::Read,
        "write" => CapabilityClass::Write,
        "net" => CapabilityClass::Net,
        "test" => CapabilityClass::Test,
        "spend" => CapabilityClass::Spend,
        "exec" => CapabilityClass::Exec,
        "outside-project" => CapabilityClass::OutsideProject,
        _ => CapabilityClass::Exec,
    }
}

/// Python `repr()` of a short string (quote preference only — enough for
/// option/choice labels, which carry no control characters).
fn py_str_repr(value: &str) -> String {
    if value.contains('\'') && !value.contains('"') {
        format!("\"{}\"", value.replace('\\', "\\\\"))
    } else {
        format!("'{}'", value.replace('\\', "\\\\").replace('\'', "\\'"))
    }
}

/// Python `repr()` of a tuple of strings (the ValueError embeds
/// `ticket.options`, a tuple).
fn py_tuple_repr(values: &[String]) -> String {
    let items: Vec<String> = values.iter().map(|value| py_str_repr(value)).collect();
    if items.len() == 1 {
        format!("({},)", items[0])
    } else {
        format!("({})", items.join(", "))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};

    fn make_broker() -> (ApprovalBroker, Arc<NeedsYouQueue>, Arc<Mutex<DenialLog>>) {
        let needs_you = Arc::new(NeedsYouQueue::new());
        let denial_log = Arc::new(Mutex::new(DenialLog::new()));
        let broker = ApprovalBroker::with_config(
            Some(Arc::clone(&needs_you)),
            Some(Arc::clone(&denial_log)),
            Box::new(monotonic),
            0.0,
        );
        (broker, needs_you, denial_log)
    }

    fn standard() -> Vec<String> {
        STANDARD_OPTIONS.iter().map(|s| s.to_string()).collect()
    }

    const NO_OPTIONS: [&str; 0] = [];

    // -- options ------------------------------------------------------------

    #[test]
    fn test_presented_options_always_contain_standard_triple() {
        assert_eq!(presented_options(NO_OPTIONS), standard());
        assert_eq!(presented_options(["Allow", "Deny"]), standard());
        assert_eq!(
            presented_options(["Allow once", "Deny", "Skip"]),
            vec!["Allow once", "Allow always", "Deny", "Skip"],
        );
    }

    #[test]
    fn test_is_allow_family_matching() {
        assert!(is_allow("Allow once"));
        assert!(is_allow("Allow always"));
        assert!(is_allow("Allow"));
        assert!(!is_allow("Deny"));
        assert!(!is_allow("Skip"));
    }

    // -- FIFO / answer --------------------------------------------------------

    #[test]
    fn test_request_approval_fifo_and_answer() {
        let (broker, _, _) = make_broker();
        broker.request_approval("Allow first?", ["Deny"], DEFAULT_TIMEOUT, ApprovalDefault::Deny);
        broker.request_approval("Allow second?", ["Deny"], DEFAULT_TIMEOUT, ApprovalDefault::Deny);

        let prompts: Vec<String> = broker.pending().iter().map(|t| t.prompt.clone()).collect();
        assert_eq!(prompts, vec!["Allow first?", "Allow second?"]);
        let head = broker.head().expect("head pending");
        assert_eq!(head.prompt, "Allow first?");
        assert_eq!(head.options, standard());

        assert_eq!(
            broker.answer(&head.ticket_id, ALLOW_ONCE),
            Ok(ALLOW_ONCE.to_string())
        );

        let head = broker.head().expect("second head pending");
        assert_eq!(head.prompt, "Allow second?");
        assert_eq!(broker.answer(&head.ticket_id, DENY), Ok(DENY.to_string()));
        assert_eq!(broker.pending(), Vec::new());
    }

    #[test]
    fn test_answer_rejects_unknown_ticket_and_invalid_choice() {
        let (broker, _, _) = make_broker();
        broker.request_approval("Allow x?", NO_OPTIONS, DEFAULT_TIMEOUT, ApprovalDefault::Deny);
        let head = broker.head().expect("head pending");
        assert_eq!(
            broker.answer(&head.ticket_id, "Maybe"),
            Err(ApprovalError::Value(
                "choice 'Maybe' is not one of ('Allow once', 'Allow always', 'Deny')".to_string()
            ))
        );
        assert_eq!(
            broker.answer("approval-999", ALLOW_ONCE),
            Err(ApprovalError::UnknownTicket("approval-999".to_string()))
        );
        assert_eq!(broker.answer(&head.ticket_id, DENY), Ok(DENY.to_string()));
    }

    #[test]
    fn test_listeners_fire_on_queue_changes() {
        let (broker, _, _) = make_broker();
        let calls = Arc::new(AtomicUsize::new(0));
        let seen = Arc::clone(&calls);
        let listener = broker.add_listener(move || {
            seen.fetch_add(1, Ordering::SeqCst);
        });
        broker.request_approval("Allow x?", NO_OPTIONS, DEFAULT_TIMEOUT, ApprovalDefault::Deny);
        assert!(calls.load(Ordering::SeqCst) > 0); // new ticket notified
        let head = broker.head().expect("head pending");
        broker.answer(&head.ticket_id, DENY).expect("valid answer");
        assert!(calls.load(Ordering::SeqCst) >= 2);
        broker.remove_listener(listener);
    }

    // -- allow-always pass-through --------------------------------------------

    /// User directive: the asker (natively hooks-approval) owns allow-always
    /// persistence — the broker must NOT keep a shadow remember table, so an
    /// identical follow-up ask presents a fresh ticket.
    #[test]
    fn test_allow_always_passes_through_without_local_bookkeeping() {
        let (broker, _, _) = make_broker();
        broker.request_approval(
            "Allow git push?",
            NO_OPTIONS,
            DEFAULT_TIMEOUT,
            ApprovalDefault::Deny,
        );
        let head = broker.head().expect("head pending");
        assert_eq!(
            broker.answer(&head.ticket_id, ALLOW_ALWAYS),
            Ok(ALLOW_ALWAYS.to_string())
        );

        broker.request_approval(
            "Allow git push?",
            NO_OPTIONS,
            DEFAULT_TIMEOUT,
            ApprovalDefault::Deny,
        );
        let head = broker.head().expect("asked again — no local short-circuit");
        assert_eq!(
            broker.answer(&head.ticket_id, ALLOW_ONCE),
            Ok(ALLOW_ONCE.to_string())
        );
    }

    // -- timeout ---------------------------------------------------------------

    /// Python awaits a 0.01s real timeout; the wall-clock wait is asyncio
    /// mechanics — the pinned decision logic (allow default → `Allow once`,
    /// nothing recorded on the denial log) applies via `resolve_timeout`.
    #[test]
    fn test_timeout_with_allow_default_returns_allow_once() {
        let (broker, _, denial_log) = make_broker();
        let ticket_id =
            broker.request_approval("Allow read?", NO_OPTIONS, 0.01, ApprovalDefault::Allow);
        assert_eq!(
            broker.resolve_timeout(&ticket_id),
            Ok(ALLOW_ONCE.to_string())
        );
        assert_eq!(denial_log.lock().unwrap().total_count(), 0);
        assert_eq!(broker.pending(), Vec::new());
    }

    /// Extra (not a Python pin): the deny-default branch of `_record_timeout`
    /// — the timeout denies and records on the denial log with the exact
    /// Python reason string, preferring the staged command over the prompt.
    #[test]
    fn extra_timeout_with_deny_default_records_denial() {
        let (broker, _, denial_log) = make_broker();
        broker.stage_detail(
            "Allow rm?",
            ApprovalDetail {
                command: "rm -rf build".to_string(),
                capability: "exec".to_string(),
                ..ApprovalDetail::default()
            },
        );
        let ticket_id =
            broker.request_approval("Allow rm?", NO_OPTIONS, 0.01, ApprovalDefault::Deny);
        assert_eq!(broker.resolve_timeout(&ticket_id), Ok(DENY.to_string()));
        let log = denial_log.lock().unwrap();
        assert_eq!(log.total_count(), 1);
        let record = log.records().last().expect("one denial recorded");
        assert_eq!(record.capability, CapabilityClass::Exec);
        assert_eq!(record.action, "rm -rf build");
        assert_eq!(record.reason, "approval timed out · denied by default");
    }

    // -- staged detail -----------------------------------------------------------

    #[test]
    fn test_staged_details_pair_fifo_per_prompt() {
        let (broker, _, _) = make_broker();
        broker.stage_detail(
            "Allow x?",
            ApprovalDetail {
                command: "first".to_string(),
                ..ApprovalDetail::default()
            },
        );
        broker.stage_detail(
            "Allow x?",
            ApprovalDetail {
                command: "second".to_string(),
                ..ApprovalDetail::default()
            },
        );
        broker.request_approval("Allow x?", NO_OPTIONS, DEFAULT_TIMEOUT, ApprovalDefault::Deny);
        broker.request_approval("Allow x?", NO_OPTIONS, DEFAULT_TIMEOUT, ApprovalDefault::Deny);
        let commands: Vec<String> = broker
            .pending()
            .iter()
            .map(|t| t.detail.command.clone())
            .collect();
        assert_eq!(commands, vec!["first", "second"]);
        for ticket in broker.pending() {
            assert_eq!(broker.answer(&ticket.ticket_id, DENY), Ok(DENY.to_string()));
        }
        assert_eq!(broker.pending(), Vec::new());
    }

    // -- extras (module surface not covered by the pinned files) -----------------

    /// Extra (not a Python pin): candidates are whitespace-normalized before
    /// the verbatim substring match, empty/oversized candidates yield no
    /// accent (oracle-checked against the Python module).
    #[test]
    fn extra_deferral_highlight_first_verbatim_candidate() {
        assert_eq!(
            deferral_highlight("run  git push now", &["git  push", "", "run"]),
            "git push"
        );
        assert_eq!(deferral_highlight("question", &["absent"]), "");
        let oversized = "x".repeat(201);
        assert_eq!(
            deferral_highlight(&format!("q {oversized}"), &[oversized.as_str()]),
            ""
        );
    }
}
