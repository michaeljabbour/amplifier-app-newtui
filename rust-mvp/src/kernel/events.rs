//! THE event contract: raw amplifier hook payloads → typed [`UIEvent`]s.
//!
//! All amplifier-core events are normalized at exactly this one boundary
//! (ADR-0007). Both channels are consumed and kept independent:
//!
//! - **Channel A** (live deltas, ad-hoc provider events):
//!   `llm:stream_block_start/delta/end`, `llm:stream_aborted`.
//! - **Channel B** (durable records, orchestrator events): `tool:pre/post/
//!   error`, `content_block:start/end`, `orchestrator:complete`.
//!
//! Never reconstruct one channel from the other. Tool correlation is by
//! `tool_call_id` only — never `tool_name` (parallel calls of the same
//! tool run concurrently).
//!
//! This module is intentionally **pure**: JSON map in, typed struct out.
//! [`normalize`] absorbs the payload variance documented in
//! RESEARCH-BRIEF §2:
//!
//! - delta text under `delta` | `text` | `content`;
//! - `task:agent_spawned`/`task:agent_completed` vs the legacy
//!   `task:spawned`/`task:completed` names;
//! - tool results under `result` vs `tool_response`;
//! - provider usage flat or nested under `usage`, with cache counters
//!   under `cache_read_input_tokens`/`cache_read` etc.
//!
//! Every event carries the envelope `{event_id, session_id, parent_id,
//! ts}`. `session_id`/`parent_id` come from the payload (stamped by
//! `hooks.set_default_fields`) and are the entire lane-routing key.
//!
//! Port of `src/amplifier_app_newtui/kernel/events.py`. This is the full
//! normalization layer; `protocol.rs` remains the thin wire enum until
//! later units migrate onto this vocabulary.

use std::str::FromStr;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

/// A raw hook payload — the JSON-object shape `normalize` consumes.
pub type Payload = Map<String, Value>;

static EVENT_COUNTER: AtomicU64 = AtomicU64::new(1);

fn mint_event_id() -> String {
    format!("ev{}", EVENT_COUNTER.fetch_add(1, Ordering::Relaxed))
}

fn now_ts() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// Serialize `Option<Decimal>` the way pydantic's JSON mode does (string
/// or null) and accept string/number/null back on the parse side.
mod decimal_opt {
    use super::*;
    use serde::{Deserializer, Serializer};

    pub fn serialize<S: Serializer>(
        value: &Option<Decimal>,
        serializer: S,
    ) -> Result<S::Ok, S::Error> {
        match value {
            Some(d) => serializer.serialize_str(&d.to_string()),
            None => serializer.serialize_none(),
        }
    }

    pub fn deserialize<'de, D: Deserializer<'de>>(
        deserializer: D,
    ) -> Result<Option<Decimal>, D::Error> {
        match Option::<Value>::deserialize(deserializer)? {
            None | Some(Value::Null) => Ok(None),
            Some(Value::String(s)) => Decimal::from_str(&s)
                .map(Some)
                .map_err(serde::de::Error::custom),
            Some(Value::Number(n)) => Decimal::from_str(&n.to_string())
                .map(Some)
                .map_err(serde::de::Error::custom),
            Some(other) => Err(serde::de::Error::custom(format!(
                "invalid decimal value: {other}"
            ))),
        }
    }
}

/// Declare one event struct carrying the common envelope
/// `{event_id, session_id, parent_id, ts}` plus its own fields.
///
/// Python's pydantic `_Envelope` base with `frozen=True, extra="forbid"`
/// maps to `deny_unknown_fields` + immutability by convention. The
/// container-level `#[serde(default)]` mirrors the pydantic
/// default/`default_factory` semantics (a missing `event_id` mints a
/// fresh one; a missing `ts` stamps now) via the manual [`Default`] impl.
macro_rules! ui_event_struct {
    (
        $(#[$meta:meta])*
        pub struct $name:ident {
            $(
                $(#[$fmeta:meta])*
                pub $field:ident : $fty:ty = $default:expr
            ),* $(,)?
        }
    ) => {
        $(#[$meta])*
        #[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
        #[serde(default, deny_unknown_fields)]
        pub struct $name {
            pub event_id: String,
            pub session_id: String,
            pub parent_id: Option<String>,
            pub ts: f64,
            $(
                $(#[$fmeta])*
                pub $field: $fty,
            )*
        }

        impl Default for $name {
            fn default() -> Self {
                Self {
                    event_id: mint_event_id(),
                    session_id: String::new(),
                    parent_id: None,
                    ts: now_ts(),
                    $($field: $default,)*
                }
            }
        }
    };
}

// --------------------------------------------------------------------------
// Channel A — live streaming deltas
// --------------------------------------------------------------------------

ui_event_struct! {
    /// A streaming content block opened (`llm:stream_block_start`).
    pub struct StreamBlockStart {
        pub request_id: String = String::new(),
        pub block_index: i64 = 0,
        pub block_type: String = "text".to_string(),
        pub name: String = String::new(),
    }
}

ui_event_struct! {
    /// One incremental text/thinking chunk (`llm:stream_block_delta`).
    ///
    /// `text` is canonical regardless of which raw key (`delta` /
    /// `text` / `content`) the provider used.
    pub struct StreamBlockDelta {
        pub request_id: String = String::new(),
        pub block_index: i64 = 0,
        pub block_type: String = "text".to_string(),
        pub sequence: i64 = 0,
        pub text: String = String::new(),
    }
}

ui_event_struct! {
    /// A streaming block closed — consolidate the live tail now.
    pub struct StreamBlockEnd {
        pub request_id: String = String::new(),
        pub block_index: i64 = 0,
        pub block_type: String = "text".to_string(),
    }
}

ui_event_struct! {
    /// The stream died mid-flight (`llm:stream_aborted`).
    pub struct StreamAborted {
        pub request_id: String = String::new(),
        pub error_type: String = String::new(),
        pub error_message: String = String::new(),
    }
}

// --------------------------------------------------------------------------
// Channel B — durable tool / content records
// --------------------------------------------------------------------------

ui_event_struct! {
    /// A tool call is about to run (`tool:pre`) — open the tool line.
    pub struct ToolPre {
        pub tool_name: String = String::new(),
        pub tool_call_id: String = String::new(),
        pub tool_input: Map<String, Value> = Map::new(),
        pub parallel_group_id: Option<String> = None,
    }
}

ui_event_struct! {
    /// A tool call finished (`tool:post`) — finalize + expandable body.
    ///
    /// `result` is the normalized payload whether the raw event used
    /// `result` or `tool_response`.
    pub struct ToolPost {
        pub tool_name: String = String::new(),
        pub tool_call_id: String = String::new(),
        pub tool_input: Map<String, Value> = Map::new(),
        pub result: Map<String, Value> = Map::new(),
    }
}

ui_event_struct! {
    /// A tool call failed (`tool:error`).
    pub struct ToolError {
        pub tool_name: String = String::new(),
        pub tool_call_id: String = String::new(),
        pub error_type: String = String::new(),
        pub error_message: String = String::new(),
    }
}

ui_event_struct! {
    /// Durable content block opened (`content_block:start`).
    pub struct ContentBlockStart {
        pub block_type: String = "text".to_string(),
        pub block_index: i64 = 0,
        pub total_blocks: i64 = 0,
    }
}

ui_event_struct! {
    /// Durable content block record (`content_block:end`) — the atomic,
    /// non-incremental source of truth for answer/thinking text.
    pub struct ContentBlockEnd {
        pub block_type: String = "text".to_string(),
        pub block_index: i64 = 0,
        pub total_blocks: i64 = 0,
        pub block: Map<String, Value> = Map::new(),
        pub usage: Map<String, Value> = Map::new(),
    }
}

/// Python `Literal["success", "cancelled", "incomplete"]`.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub enum OrchestratorStatus {
    #[default]
    #[serde(rename = "success")]
    Success,
    #[serde(rename = "cancelled")]
    Cancelled,
    #[serde(rename = "incomplete")]
    Incomplete,
}

impl OrchestratorStatus {
    /// The exact Python literal string values.
    pub fn as_str(self) -> &'static str {
        match self {
            OrchestratorStatus::Success => "success",
            OrchestratorStatus::Cancelled => "cancelled",
            OrchestratorStatus::Incomplete => "incomplete",
        }
    }
}

ui_event_struct! {
    /// The orchestrator loop ended (`orchestrator:complete`).
    pub struct OrchestratorComplete {
        pub orchestrator: String = String::new(),
        pub turn_count: i64 = 0,
        pub status: OrchestratorStatus = OrchestratorStatus::Success,
    }
}

// --------------------------------------------------------------------------
// Turn / execution lifecycle
// --------------------------------------------------------------------------

ui_event_struct! {
    /// A user prompt entered the engine (`prompt:submit`) — the turn
    /// boundary where the app stamps its monotonic turn_id.
    ///
    /// `mode` records the app posture (`chat`/`plan`/`brainstorm`/
    /// `build`/`auto`) active when the prompt was submitted, so the
    /// durable ui-events.jsonl log preserves which posture a historical
    /// turn ran under. Empty on legacy logs (pre-stamp) — the reducer
    /// then falls back to live mode.
    pub struct PromptSubmit {
        pub prompt: String = String::new(),
        pub mode: String = String::new(),
    }
}

ui_event_struct! {
    /// The prompt's turn finished (`prompt:complete`).
    ///
    /// The real runtime synthesizes this close-out event itself (after
    /// its end-of-turn git snapshot) and enriches it with the turn's
    /// concrete yield. Raw hook payloads normalized here carry only
    /// `response`; the yield fields default off.
    pub struct PromptComplete {
        pub response: String = String::new(),
        /// Files whose diffstat changed during the turn (git snapshot delta).
        pub files_changed: i64 = 0,
        /// `+142/−38` style line-delta label; empty when nothing changed.
        pub diffstat: String = String::new(),
        /// True/False when test commands ran this turn; None when they did not.
        pub tests_ok: Option<bool> = None,
    }
}

ui_event_struct! {
    /// Engine execution started (`execution:start`).
    pub struct ExecutionStart {}
}

ui_event_struct! {
    /// Engine execution ended (`execution:end`).
    pub struct ExecutionEnd {}
}

// --------------------------------------------------------------------------
// Provider telemetry / notices
// --------------------------------------------------------------------------

ui_event_struct! {
    /// Token usage from one provider response (`provider:response`).
    ///
    /// Drives live token counting, cache %, and per-turn cost (kernel
    /// SessionStatus counters are NOT populated — the app computes cost
    /// from these numbers itself).
    pub struct ProviderResponseUsage {
        pub input_tokens: i64 = 0,
        pub output_tokens: i64 = 0,
        pub cache_read: i64 = 0,
        pub cache_write: i64 = 0,
        pub model: String = String::new(),
        /// Provider-reported cost when available (e.g. loop-streaming's
        /// `content_block:end` usage payload) — authoritative over the
        /// local pricing-table estimate.
        #[serde(with = "decimal_opt")]
        pub cost_usd: Option<Decimal> = None,
    }
}

/// Python `Literal["error", "retry", "throttle"]`.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
pub enum NoticeKind {
    #[default]
    #[serde(rename = "error")]
    Error,
    #[serde(rename = "retry")]
    Retry,
    #[serde(rename = "throttle")]
    Throttle,
}

impl NoticeKind {
    /// The exact Python literal string values.
    pub fn as_str(self) -> &'static str {
        match self {
            NoticeKind::Error => "error",
            NoticeKind::Retry => "retry",
            NoticeKind::Throttle => "throttle",
        }
    }
}

ui_event_struct! {
    /// Provider error/retry/throttle notice (footer transient).
    pub struct ProviderNotice {
        pub notice: NoticeKind = NoticeKind::Error,
        pub message: String = String::new(),
    }
}

// --------------------------------------------------------------------------
// Session lifecycle
// --------------------------------------------------------------------------

ui_event_struct! {
    pub struct SessionStart {}
}

ui_event_struct! {
    pub struct SessionEnd {}
}

ui_event_struct! {
    /// A session forked (rewind); `source_session_id` is the parent.
    pub struct SessionFork {
        pub source_session_id: String = String::new(),
    }
}

ui_event_struct! {
    pub struct SessionResume {}
}

ui_event_struct! {
    /// A confirmed rewind boundary, persisted to the append-only log.
    ///
    /// The ui-events log never truncates, so a resume would otherwise
    /// replay the turns a rewind discarded (ghost turns). This marker is
    /// written to the log at fork time (never a raw hook — the app
    /// synthesizes it) and honored at read time by
    /// [`drop_rewound_events`]: everything after the `kept_turns`-th
    /// surviving turn, up to this marker, is dropped before the events
    /// reach the reducer.
    ///
    /// - `checkpoint_id`: the rewind target (`t2` …), for diagnostics.
    /// - `kept_turns`: how many `prompt_submit`-delimited turns survive
    ///   from the start of the reconstructed timeline. Python pins this
    ///   `ge=0`; `u64` enforces the same bound by construction.
    pub struct RewindMarker {
        pub checkpoint_id: String = String::new(),
        pub kept_turns: u64 = 0,
    }
}

// --------------------------------------------------------------------------
// Approvals / cancellation
// --------------------------------------------------------------------------

ui_event_struct! {
    /// An approval is being requested (`approval:required`).
    ///
    /// `options` always contains the verbatim strings `Allow once` /
    /// `Allow always` / `Deny` (fail-closed string matching).
    pub struct ApprovalRequired {
        pub prompt: String = String::new(),
        pub options: Vec<String> = Vec::new(),
    }
}

ui_event_struct! {
    pub struct ApprovalGranted {
        pub prompt: String = String::new(),
        pub choice: String = String::new(),
    }
}

ui_event_struct! {
    /// An approval was denied (`approval:denied`).
    ///
    /// `command` is the blocked thing for the ⊘ line (falls back to
    /// `prompt`); `continuation` is the deny-and-continue note
    /// (DESIGN-SPEC §7: `continuing without <thing>`).
    pub struct ApprovalDenied {
        pub prompt: String = String::new(),
        pub reason: String = String::new(),
        pub command: String = String::new(),
        pub continuation: String = String::new(),
    }
}

ui_event_struct! {
    /// Interrupt requested (`cancel:requested`) — esc while running.
    pub struct CancelRequested {}
}

ui_event_struct! {
    /// Interrupt landed at a step boundary (`cancel:completed`).
    pub struct CancelCompleted {}
}

// --------------------------------------------------------------------------
// Subagents / notifications
// --------------------------------------------------------------------------

ui_event_struct! {
    /// A subagent lane opened (`task:agent_spawned` / `task:spawned`).
    pub struct AgentSpawned {
        pub agent: String = String::new(),
        pub sub_session_id: String = String::new(),
        pub parent_session_id: String = String::new(),
    }
}

ui_event_struct! {
    /// A subagent finished (`task:agent_completed` / `task:completed`).
    pub struct AgentCompleted {
        pub agent: String = String::new(),
        pub sub_session_id: String = String::new(),
        pub parent_session_id: String = String::new(),
        pub success: bool = true,
        /// Short result summary for the lane line (e.g. `tests ✔`).
        pub result: String = String::new(),
    }
}

ui_event_struct! {
    /// A subagent lane reopened (`delegate:agent_resumed`).
    ///
    /// The resume payload carries only the child `session_id` (already
    /// the envelope's own field) and `parent_session_id` — no `agent`
    /// name. That's intentional: the lane already exists from the
    /// original spawn event, keyed by `sub_session_id`, so there's
    /// nothing new to key on here and `agent` is left empty rather than
    /// guessed.
    pub struct AgentResumed {
        pub agent: String = String::new(),
        pub parent_session_id: String = String::new(),
    }
}

ui_event_struct! {
    /// User-facing notice (`user:notification`) → transient notice slot.
    pub struct Notification {
        pub message: String = String::new(),
        pub level: String = "info".to_string(),
        pub source: String = String::new(),
        /// NeedsYouQueue id when `level == "decision"`: the deferral
        /// already parked its item kernel-side; the app resolves that
        /// item instead of re-deriving one from the message text. Empty
        /// for scripted/legacy notices — the adapter then supplies the
        /// decision data.
        pub decision_id: String = String::new(),
    }
}

ui_event_struct! {
    /// A persistent user-role context message was injected mid-turn.
    ///
    /// Emitted by the runtime when the StepBoundaryBridge applies a
    /// steer and/or answered deferred decisions (one combined injection
    /// message per step boundary). Foundation's fork slicing counts
    /// EVERY user-role message as a turn boundary, so checkpoint turn
    /// ids must advance past these injections (DESIGN-SPEC §9).
    pub struct ContextInjected {
        pub source: String = "steering".to_string(),
    }
}

ui_event_struct! {
    /// The mounted context compacted its request view.
    pub struct ContextCompacted {
        pub before_tokens: i64 = 0,
        pub after_tokens: i64 = 0,
        pub before_messages: i64 = 0,
        pub after_messages: i64 = 0,
        pub strategy_level: i64 = 0,
    }
}

/// Discriminated union of every normalized UI event (on `kind`).
///
/// Python's pydantic `Annotated[Union[...], Field(discriminator="kind")]`
/// maps to serde's internally-tagged enum: the `kind` string lives on the
/// wire exactly as pydantic dumps it, and each variant's payload is the
/// corresponding event struct.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind")]
pub enum UIEvent {
    #[serde(rename = "stream_block_start")]
    StreamBlockStart(StreamBlockStart),
    #[serde(rename = "stream_block_delta")]
    StreamBlockDelta(StreamBlockDelta),
    #[serde(rename = "stream_block_end")]
    StreamBlockEnd(StreamBlockEnd),
    #[serde(rename = "stream_aborted")]
    StreamAborted(StreamAborted),
    #[serde(rename = "tool_pre")]
    ToolPre(ToolPre),
    #[serde(rename = "tool_post")]
    ToolPost(ToolPost),
    #[serde(rename = "tool_error")]
    ToolError(ToolError),
    #[serde(rename = "content_block_start")]
    ContentBlockStart(ContentBlockStart),
    #[serde(rename = "content_block_end")]
    ContentBlockEnd(ContentBlockEnd),
    #[serde(rename = "orchestrator_complete")]
    OrchestratorComplete(OrchestratorComplete),
    #[serde(rename = "prompt_submit")]
    PromptSubmit(PromptSubmit),
    #[serde(rename = "prompt_complete")]
    PromptComplete(PromptComplete),
    #[serde(rename = "execution_start")]
    ExecutionStart(ExecutionStart),
    #[serde(rename = "execution_end")]
    ExecutionEnd(ExecutionEnd),
    #[serde(rename = "provider_response_usage")]
    ProviderResponseUsage(ProviderResponseUsage),
    #[serde(rename = "provider_notice")]
    ProviderNotice(ProviderNotice),
    #[serde(rename = "session_start")]
    SessionStart(SessionStart),
    #[serde(rename = "session_end")]
    SessionEnd(SessionEnd),
    #[serde(rename = "session_fork")]
    SessionFork(SessionFork),
    #[serde(rename = "session_resume")]
    SessionResume(SessionResume),
    #[serde(rename = "rewind_marker")]
    RewindMarker(RewindMarker),
    #[serde(rename = "approval_required")]
    ApprovalRequired(ApprovalRequired),
    #[serde(rename = "approval_granted")]
    ApprovalGranted(ApprovalGranted),
    #[serde(rename = "approval_denied")]
    ApprovalDenied(ApprovalDenied),
    #[serde(rename = "cancel_requested")]
    CancelRequested(CancelRequested),
    #[serde(rename = "cancel_completed")]
    CancelCompleted(CancelCompleted),
    #[serde(rename = "agent_spawned")]
    AgentSpawned(AgentSpawned),
    #[serde(rename = "agent_completed")]
    AgentCompleted(AgentCompleted),
    #[serde(rename = "agent_resumed")]
    AgentResumed(AgentResumed),
    #[serde(rename = "notification")]
    Notification(Notification),
    #[serde(rename = "context_injected")]
    ContextInjected(ContextInjected),
    #[serde(rename = "context_compacted")]
    ContextCompacted(ContextCompacted),
}

/// Apply one expression to the envelope fields of any [`UIEvent`] variant.
macro_rules! with_envelope {
    ($event:expr, $inner:ident, $body:expr) => {
        match $event {
            UIEvent::StreamBlockStart($inner) => $body,
            UIEvent::StreamBlockDelta($inner) => $body,
            UIEvent::StreamBlockEnd($inner) => $body,
            UIEvent::StreamAborted($inner) => $body,
            UIEvent::ToolPre($inner) => $body,
            UIEvent::ToolPost($inner) => $body,
            UIEvent::ToolError($inner) => $body,
            UIEvent::ContentBlockStart($inner) => $body,
            UIEvent::ContentBlockEnd($inner) => $body,
            UIEvent::OrchestratorComplete($inner) => $body,
            UIEvent::PromptSubmit($inner) => $body,
            UIEvent::PromptComplete($inner) => $body,
            UIEvent::ExecutionStart($inner) => $body,
            UIEvent::ExecutionEnd($inner) => $body,
            UIEvent::ProviderResponseUsage($inner) => $body,
            UIEvent::ProviderNotice($inner) => $body,
            UIEvent::SessionStart($inner) => $body,
            UIEvent::SessionEnd($inner) => $body,
            UIEvent::SessionFork($inner) => $body,
            UIEvent::SessionResume($inner) => $body,
            UIEvent::RewindMarker($inner) => $body,
            UIEvent::ApprovalRequired($inner) => $body,
            UIEvent::ApprovalGranted($inner) => $body,
            UIEvent::ApprovalDenied($inner) => $body,
            UIEvent::CancelRequested($inner) => $body,
            UIEvent::CancelCompleted($inner) => $body,
            UIEvent::AgentSpawned($inner) => $body,
            UIEvent::AgentCompleted($inner) => $body,
            UIEvent::AgentResumed($inner) => $body,
            UIEvent::Notification($inner) => $body,
            UIEvent::ContextInjected($inner) => $body,
            UIEvent::ContextCompacted($inner) => $body,
        }
    };
}

impl UIEvent {
    /// The `kind` discriminator string, exactly as Python's Literal values.
    pub fn kind(&self) -> &'static str {
        match self {
            UIEvent::StreamBlockStart(_) => "stream_block_start",
            UIEvent::StreamBlockDelta(_) => "stream_block_delta",
            UIEvent::StreamBlockEnd(_) => "stream_block_end",
            UIEvent::StreamAborted(_) => "stream_aborted",
            UIEvent::ToolPre(_) => "tool_pre",
            UIEvent::ToolPost(_) => "tool_post",
            UIEvent::ToolError(_) => "tool_error",
            UIEvent::ContentBlockStart(_) => "content_block_start",
            UIEvent::ContentBlockEnd(_) => "content_block_end",
            UIEvent::OrchestratorComplete(_) => "orchestrator_complete",
            UIEvent::PromptSubmit(_) => "prompt_submit",
            UIEvent::PromptComplete(_) => "prompt_complete",
            UIEvent::ExecutionStart(_) => "execution_start",
            UIEvent::ExecutionEnd(_) => "execution_end",
            UIEvent::ProviderResponseUsage(_) => "provider_response_usage",
            UIEvent::ProviderNotice(_) => "provider_notice",
            UIEvent::SessionStart(_) => "session_start",
            UIEvent::SessionEnd(_) => "session_end",
            UIEvent::SessionFork(_) => "session_fork",
            UIEvent::SessionResume(_) => "session_resume",
            UIEvent::RewindMarker(_) => "rewind_marker",
            UIEvent::ApprovalRequired(_) => "approval_required",
            UIEvent::ApprovalGranted(_) => "approval_granted",
            UIEvent::ApprovalDenied(_) => "approval_denied",
            UIEvent::CancelRequested(_) => "cancel_requested",
            UIEvent::CancelCompleted(_) => "cancel_completed",
            UIEvent::AgentSpawned(_) => "agent_spawned",
            UIEvent::AgentCompleted(_) => "agent_completed",
            UIEvent::AgentResumed(_) => "agent_resumed",
            UIEvent::Notification(_) => "notification",
            UIEvent::ContextInjected(_) => "context_injected",
            UIEvent::ContextCompacted(_) => "context_compacted",
        }
    }

    /// The envelope `event_id`, whichever variant this is.
    pub fn event_id(&self) -> &str {
        with_envelope!(self, e, &e.event_id)
    }

    /// The envelope `session_id`, whichever variant this is.
    pub fn session_id(&self) -> &str {
        with_envelope!(self, e, &e.session_id)
    }

    /// The envelope `parent_id`, whichever variant this is.
    pub fn parent_id(&self) -> Option<&str> {
        with_envelope!(self, e, e.parent_id.as_deref())
    }

    /// The envelope `ts`, whichever variant this is.
    pub fn ts(&self) -> f64 {
        with_envelope!(self, e, e.ts)
    }
}

/// Round-trip one stored event record back into a typed [`UIEvent`].
///
/// The inverse of `event.model_dump(mode="json")` as persisted by
/// `SessionStore.append_event` — powers resume transcript replay
/// (DESIGN-SPEC §3/§11). Returns `None` for foreign records: the event
/// log can carry other writers' lines today, and `deny_unknown_fields`
/// (Python's frozen `extra="forbid"` envelope) makes any raw hook
/// payload or unknown `kind` fail validation rather than half-parse.
pub fn parse_event(record: &Value) -> Option<UIEvent> {
    serde_json::from_value(record.clone()).ok()
}

/// Filter post-rewind ghost turns out of a persisted event stream.
///
/// The ui-events log is append-only, so a confirmed rewind leaves the
/// turns it discarded sitting in the log; a naive resume replays them as
/// ghost turns (issue #40). At fork time the app writes a
/// [`RewindMarker`] recording how many `prompt_submit`-delimited turns
/// survive from the start of the timeline. This honors those markers by
/// segmenting the stream into turns and truncating back to the marker's
/// `kept_turns` each time one is seen — the inverse, read-side half of
/// the append-only contract.
///
/// Turns are renumbered implicitly by position, so nested and repeated
/// rewinds compose. Events before the first prompt (session-start
/// preamble) are always kept; the markers themselves are dropped from
/// the result.
pub fn drop_rewound_events(events: &[UIEvent]) -> Vec<UIEvent> {
    let mut preamble: Vec<UIEvent> = Vec::new();
    let mut turns: Vec<Vec<UIEvent>> = Vec::new();
    // `None` = appending to the preamble; `Some(i)` = appending to turn i.
    let mut current: Option<usize> = None;
    for event in events {
        match event {
            UIEvent::RewindMarker(marker) => {
                let keep = (marker.kept_turns as usize).min(turns.len());
                turns.truncate(keep);
                current = if turns.is_empty() {
                    None
                } else {
                    Some(turns.len() - 1)
                };
            }
            UIEvent::PromptSubmit(_) => {
                turns.push(vec![event.clone()]);
                current = Some(turns.len() - 1);
            }
            _ => match current {
                Some(index) => turns[index].push(event.clone()),
                None => preamble.push(event.clone()),
            },
        }
    }
    let mut result = preamble;
    for turn in turns {
        result.extend(turn);
    }
    result
}

// --------------------------------------------------------------------------
// Normalization
// --------------------------------------------------------------------------

/// Python's `str(value)` for the payload scalars we encounter.
fn py_str(value: &Value) -> String {
    match value {
        Value::String(s) => s.clone(),
        Value::Bool(true) => "True".to_string(),
        Value::Bool(false) => "False".to_string(),
        Value::Number(n) => n.to_string(),
        // Containers: JSON text stands in for Python's repr (untested
        // shapes — normalize never str()s a container in practice).
        other => other.to_string(),
    }
}

/// Python truthiness for JSON values.
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

/// `_str`: first non-None key stringified, else the default.
fn get_str_or(data: &Payload, keys: &[&str], default: &str) -> String {
    for key in keys {
        if let Some(value) = data.get(*key) {
            if !value.is_null() {
                return py_str(value);
            }
        }
    }
    default.to_string()
}

fn get_str(data: &Payload, keys: &[&str]) -> String {
    get_str_or(data, keys, "")
}

/// `_int`: first key that survives Python `int(value)` (bools skipped,
/// floats truncated toward zero, integer-literal strings parsed), else 0.
fn get_int(data: &Payload, keys: &[&str]) -> i64 {
    for key in keys {
        let Some(value) = data.get(*key) else {
            continue;
        };
        match value {
            Value::Number(n) => {
                if let Some(i) = n.as_i64() {
                    return i;
                }
                if let Some(f) = n.as_f64() {
                    return f.trunc() as i64;
                }
            }
            Value::String(s) => {
                if let Ok(i) = s.trim().parse::<i64>() {
                    return i;
                }
            }
            // None skipped; bool explicitly skipped; containers TypeError.
            _ => {}
        }
    }
    0
}

/// `_dict`: first non-None key — mappings pass through; non-mapping
/// results (bare strings, model dumps as str) are preserved under
/// `{"value": ...}` rather than dropped.
fn get_dict(data: &Payload, keys: &[&str]) -> Map<String, Value> {
    for key in keys {
        match data.get(*key) {
            Some(Value::Object(map)) => return map.clone(),
            Some(value) if !value.is_null() => {
                let mut wrapped = Map::new();
                wrapped.insert("value".to_string(), value.clone());
                return wrapped;
            }
            _ => {}
        }
    }
    Map::new()
}

/// `_cost_usd`: `Decimal(str(value))` or None on absence/parse failure.
fn cost_usd(data: &Payload) -> Option<Decimal> {
    let value = data.get("cost_usd")?;
    if value.is_null() {
        return None;
    }
    Decimal::from_str(&py_str(value)).ok()
}

/// Extract `(type, message)` from `error` dicts or flat keys.
fn error_fields(data: &Payload) -> (String, String) {
    match data.get("error") {
        Some(Value::Object(error)) => (
            get_str(error, &["type", "error_type"]),
            get_str(error, &["msg", "message", "error_message"]),
        ),
        Some(Value::String(message)) => (String::new(), message.clone()),
        _ => (
            get_str(data, &["error_type"]),
            get_str(data, &["error_message", "msg", "message"]),
        ),
    }
}

/// The common envelope fields extracted from a raw payload.
struct Envelope {
    event_id: Option<String>,
    session_id: String,
    parent_id: Option<String>,
    ts: Option<f64>,
}

fn envelope(data: &Payload) -> Envelope {
    let event_id = get_str(data, &["event_id"]);
    // Python: `data.get("parent_id") or None` — falsy values become None.
    let parent_id = data
        .get("parent_id")
        .filter(|value| is_truthy(value))
        .map(py_str);
    // Python: `data.get("ts", data.get("timestamp"))` — a present-but-None
    // `ts` key shadows `timestamp`.
    let ts_value = if data.contains_key("ts") {
        data.get("ts")
    } else {
        data.get("timestamp")
    };
    let ts = ts_value.and_then(Value::as_f64);
    Envelope {
        event_id: if event_id.is_empty() {
            None
        } else {
            Some(event_id)
        },
        session_id: get_str(data, &["session_id"]),
        parent_id,
        ts,
    }
}

/// Build `$ty::default()` and stamp the extracted envelope onto it.
macro_rules! with_env {
    ($ty:ident, $env:expr) => {{
        let mut event = $ty::default();
        if let Some(event_id) = $env.event_id.clone() {
            event.event_id = event_id;
        }
        event.session_id = $env.session_id.clone();
        event.parent_id = $env.parent_id.clone();
        if let Some(ts) = $env.ts {
            event.ts = ts;
        }
        event
    }};
}

fn usage_source(data: &Payload) -> &Payload {
    match data.get("usage") {
        Some(Value::Object(usage)) => usage,
        _ => data,
    }
}

/// Synthesize provider telemetry from a `content_block:end` usage payload.
///
/// The streaming orchestrator does not fire `provider:response` hooks;
/// each response's usage (including a provider-computed `cost_usd`)
/// rides on every content block. Emit it only for the final block so one
/// provider response is counted once. A missing `total_blocks` remains
/// the legacy single-block shape. Without this, real-mode turn rules and
/// the footer read `0.0k tok · $0.00` forever.
pub fn usage_from_content_block_end(event: &ContentBlockEnd) -> Option<ProviderResponseUsage> {
    let usage = &event.usage;
    if usage.is_empty() || (event.total_blocks > 0 && event.block_index != event.total_blocks - 1) {
        return None;
    }
    Some(ProviderResponseUsage {
        session_id: event.session_id.clone(),
        parent_id: event.parent_id.clone(),
        input_tokens: get_int(usage, &["input_tokens", "prompt_tokens"]),
        output_tokens: get_int(usage, &["output_tokens", "completion_tokens"]),
        cache_read: get_int(
            usage,
            &["cache_read", "cache_read_input_tokens", "cache_read_tokens"],
        ),
        cache_write: get_int(
            usage,
            &[
                "cache_write",
                "cache_creation_input_tokens",
                "cache_write_tokens",
            ],
        ),
        cost_usd: cost_usd(usage),
        ..ProviderResponseUsage::default()
    })
}

/// One prompt string for a `recipe:approval` gate.
///
/// Used by [`normalize`] (durable ApprovalRequired record) AND the
/// kernel recipe bridge's broker ask, so the approval bar and the event
/// log show the same text. Names the recipe and stage explicitly — a
/// bare gate prompt like "Continue?" is meaningless without them.
pub fn recipe_approval_prompt(data: &Payload) -> String {
    let recipe = {
        let name = get_str(data, &["name"]);
        if name.is_empty() {
            "recipe".to_string()
        } else {
            name
        }
    };
    let stage = get_str(data, &["stage_name"]);
    let gate = {
        let prompt = get_str(data, &["prompt"]);
        if !prompt.is_empty() {
            prompt
        } else if !stage.is_empty() {
            format!("Approve completion of stage '{stage}'?")
        } else {
            "Approve to continue?".to_string()
        }
    };
    let mut subject = format!("Recipe '{recipe}'");
    if !stage.is_empty() {
        subject.push_str(&format!(" · stage '{stage}'"));
    }
    format!("{subject} — {gate}")
}

/// Normalize one raw hook payload into a typed [`UIEvent`].
///
/// Returns `None` for event names the UI does not consume — callers
/// drop those silently. Never fails on missing payload keys: unknown
/// shapes degrade to defaulted fields, because a rendering pipeline must
/// not crash on provider payload drift.
pub fn normalize(event_name: &str, data: Option<&Payload>) -> Option<UIEvent> {
    static EMPTY: std::sync::OnceLock<Payload> = std::sync::OnceLock::new();
    let payload: &Payload = data.unwrap_or_else(|| EMPTY.get_or_init(Map::new));
    let env = envelope(payload);

    match event_name {
        // -- Channel A -----------------------------------------------------
        "llm:stream_block_start" => {
            let mut event = with_env!(StreamBlockStart, env);
            event.request_id = get_str(payload, &["request_id"]);
            event.block_index = get_int(payload, &["block_index", "index"]);
            event.block_type = get_str_or(payload, &["block_type"], "text");
            event.name = get_str(payload, &["name"]);
            Some(UIEvent::StreamBlockStart(event))
        }
        "llm:stream_block_delta" => {
            let mut event = with_env!(StreamBlockDelta, env);
            event.request_id = get_str(payload, &["request_id"]);
            event.block_index = get_int(payload, &["block_index", "index"]);
            event.block_type = get_str_or(payload, &["block_type"], "text");
            event.sequence = get_int(payload, &["sequence", "seq"]);
            // Payload variance: delta | text | content (RESEARCH-BRIEF §2).
            event.text = get_str(payload, &["delta", "text", "content"]);
            Some(UIEvent::StreamBlockDelta(event))
        }
        "llm:stream_block_end" => {
            let mut event = with_env!(StreamBlockEnd, env);
            event.request_id = get_str(payload, &["request_id"]);
            event.block_index = get_int(payload, &["block_index", "index"]);
            event.block_type = get_str_or(payload, &["block_type"], "text");
            Some(UIEvent::StreamBlockEnd(event))
        }
        "llm:stream_aborted" => {
            let (error_type, error_message) = error_fields(payload);
            let mut event = with_env!(StreamAborted, env);
            event.request_id = get_str(payload, &["request_id"]);
            event.error_type = error_type;
            event.error_message = error_message;
            Some(UIEvent::StreamAborted(event))
        }
        // -- Channel B -----------------------------------------------------
        "tool:pre" => {
            let mut event = with_env!(ToolPre, env);
            event.tool_name = get_str(payload, &["tool_name", "name"]);
            event.tool_call_id = get_str(payload, &["tool_call_id", "tool_use_id", "id"]);
            event.tool_input = get_dict(payload, &["tool_input", "input"]);
            event.parallel_group_id = payload
                .get("parallel_group_id")
                .filter(|value| is_truthy(value))
                .map(py_str);
            Some(UIEvent::ToolPre(event))
        }
        "tool:post" => {
            let mut event = with_env!(ToolPost, env);
            event.tool_name = get_str(payload, &["tool_name", "name"]);
            event.tool_call_id = get_str(payload, &["tool_call_id", "tool_use_id", "id"]);
            event.tool_input = get_dict(payload, &["tool_input", "input"]);
            // Payload variance: result | tool_response (RESEARCH-BRIEF §2).
            event.result = get_dict(payload, &["result", "tool_response", "response"]);
            Some(UIEvent::ToolPost(event))
        }
        "tool:error" => {
            let (error_type, error_message) = error_fields(payload);
            let mut event = with_env!(ToolError, env);
            event.tool_name = get_str(payload, &["tool_name", "name"]);
            event.tool_call_id = get_str(payload, &["tool_call_id", "tool_use_id", "id"]);
            event.error_type = error_type;
            event.error_message = error_message;
            Some(UIEvent::ToolError(event))
        }
        "content_block:start" => {
            let mut event = with_env!(ContentBlockStart, env);
            event.block_type = get_str_or(payload, &["block_type"], "text");
            event.block_index = get_int(payload, &["block_index", "index"]);
            event.total_blocks = get_int(payload, &["total_blocks"]);
            Some(UIEvent::ContentBlockStart(event))
        }
        "content_block:end" => {
            let block = get_dict(payload, &["block"]);
            let mut event = with_env!(ContentBlockEnd, env);
            event.block_type = get_str_or(
                payload,
                &["block_type"],
                &get_str_or(&block, &["type"], "text"),
            );
            event.block_index = get_int(payload, &["block_index", "index"]);
            event.total_blocks = get_int(payload, &["total_blocks"]);
            event.usage = get_dict(payload, &["usage"]);
            event.block = block;
            Some(UIEvent::ContentBlockEnd(event))
        }
        "orchestrator:complete" => {
            let status = get_str_or(payload, &["status"], "success");
            let mut event = with_env!(OrchestratorComplete, env);
            event.orchestrator = get_str(payload, &["orchestrator"]);
            event.turn_count = get_int(payload, &["turn_count"]);
            event.status = match status.as_str() {
                "success" => OrchestratorStatus::Success,
                "cancelled" => OrchestratorStatus::Cancelled,
                // Unknown statuses degrade, never crash.
                _ => OrchestratorStatus::Incomplete,
            };
            Some(UIEvent::OrchestratorComplete(event))
        }
        // -- Turn lifecycle --------------------------------------------------
        "prompt:submit" => {
            let mut event = with_env!(PromptSubmit, env);
            event.prompt = get_str(payload, &["prompt", "text"]);
            event.mode = get_str(payload, &["mode"]);
            Some(UIEvent::PromptSubmit(event))
        }
        "prompt:complete" => {
            let mut event = with_env!(PromptComplete, env);
            event.response = get_str(payload, &["response"]);
            Some(UIEvent::PromptComplete(event))
        }
        "execution:start" => Some(UIEvent::ExecutionStart(with_env!(ExecutionStart, env))),
        "execution:end" => Some(UIEvent::ExecutionEnd(with_env!(ExecutionEnd, env))),
        // -- Provider ----------------------------------------------------------
        "provider:response" => {
            let usage = usage_source(payload);
            let mut event = with_env!(ProviderResponseUsage, env);
            event.input_tokens = get_int(usage, &["input_tokens", "prompt_tokens"]);
            event.output_tokens = get_int(usage, &["output_tokens", "completion_tokens"]);
            event.cache_read = get_int(
                usage,
                &["cache_read", "cache_read_input_tokens", "cache_read_tokens"],
            );
            event.cache_write = get_int(
                usage,
                &[
                    "cache_write",
                    "cache_creation_input_tokens",
                    "cache_write_tokens",
                ],
            );
            event.model = get_str(payload, &["model"]);
            Some(UIEvent::ProviderResponseUsage(event))
        }
        "provider:error" | "provider:retry" | "provider:throttle" => {
            let (_, message) = error_fields(payload);
            let mut event = with_env!(ProviderNotice, env);
            event.notice = match event_name {
                "provider:error" => NoticeKind::Error,
                "provider:retry" => NoticeKind::Retry,
                _ => NoticeKind::Throttle,
            };
            event.message = if message.is_empty() {
                get_str(payload, &["message", "reason"])
            } else {
                message
            };
            Some(UIEvent::ProviderNotice(event))
        }
        "context:compaction" => {
            let mut event = with_env!(ContextCompacted, env);
            event.before_tokens = get_int(payload, &["before_tokens"]);
            event.after_tokens = get_int(payload, &["after_tokens"]);
            event.before_messages = get_int(payload, &["before_messages"]);
            event.after_messages = get_int(payload, &["after_messages"]);
            event.strategy_level = get_int(payload, &["strategy_level"]);
            Some(UIEvent::ContextCompacted(event))
        }
        // -- Session lifecycle -------------------------------------------------
        "session:start" => Some(UIEvent::SessionStart(with_env!(SessionStart, env))),
        "session:end" => Some(UIEvent::SessionEnd(with_env!(SessionEnd, env))),
        "session:fork" => {
            let mut event = with_env!(SessionFork, env);
            event.source_session_id =
                get_str(payload, &["source_session_id", "parent_session_id"]);
            Some(UIEvent::SessionFork(event))
        }
        "session:resume" => Some(UIEvent::SessionResume(with_env!(SessionResume, env))),
        // -- Approvals / cancel --------------------------------------------------
        "approval:required" => {
            let options = match payload.get("options") {
                Some(Value::Array(raw_options)) => raw_options.iter().map(py_str).collect(),
                _ => Vec::new(),
            };
            let mut event = with_env!(ApprovalRequired, env);
            event.prompt = get_str(payload, &["prompt", "message"]);
            event.options = options;
            Some(UIEvent::ApprovalRequired(event))
        }
        "approval:granted" => {
            let mut event = with_env!(ApprovalGranted, env);
            event.prompt = get_str(payload, &["prompt", "message"]);
            event.choice = get_str(payload, &["choice", "option", "response"]);
            Some(UIEvent::ApprovalGranted(event))
        }
        "approval:denied" => {
            let mut event = with_env!(ApprovalDenied, env);
            event.prompt = get_str(payload, &["prompt", "message"]);
            event.reason = get_str(payload, &["reason"]);
            event.command = get_str(payload, &["command"]);
            event.continuation = get_str(payload, &["continuation"]);
            Some(UIEvent::ApprovalDenied(event))
        }
        "recipe:approval" => {
            // tool-recipes approval gate (amplifier-bundle-recipes
            // executor._show_progress → hooks.emit("recipe:approval")).
            // Payload: {name, description, current_step, total_steps,
            // steps, status: "waiting_approval", prompt, stage_name} — it
            // carries NO recipe session id; answer routing resolves that
            // through the tool's own `approvals` operation
            // (kernel/recipes.py). Options are not in the payload either:
            // the broker presents the fail-closed verbatim triple, so the
            // durable record states the same.
            let mut event = with_env!(ApprovalRequired, env);
            event.prompt = recipe_approval_prompt(payload);
            event.options = vec![
                "Allow once".to_string(),
                "Allow always".to_string(),
                "Deny".to_string(),
            ];
            Some(UIEvent::ApprovalRequired(event))
        }
        "cancel:requested" => Some(UIEvent::CancelRequested(with_env!(CancelRequested, env))),
        "cancel:completed" => Some(UIEvent::CancelCompleted(with_env!(CancelCompleted, env))),
        // -- Subagents (task:agent_* canonical; task:* + delegate:* aliases) ------
        "task:agent_spawned" | "task:spawned" | "delegate:agent_spawned" => {
            let mut event = with_env!(AgentSpawned, env);
            event.agent = get_str(payload, &["agent", "agent_name", "name"]);
            event.sub_session_id = get_str(payload, &["sub_session_id", "child_session_id"]);
            event.parent_session_id = get_str(payload, &["parent_session_id"]);
            Some(UIEvent::AgentSpawned(event))
        }
        "task:agent_completed" | "task:completed" | "delegate:agent_completed" => {
            let mut event = with_env!(AgentCompleted, env);
            event.agent = get_str(payload, &["agent", "agent_name", "name"]);
            event.sub_session_id = get_str(payload, &["sub_session_id", "child_session_id"]);
            event.parent_session_id = get_str(payload, &["parent_session_id"]);
            event.success = match payload.get("success") {
                None | Some(Value::Null) => true,
                Some(value) => is_truthy(value),
            };
            event.result = get_str(payload, &["result", "summary"]);
            Some(UIEvent::AgentCompleted(event))
        }
        "delegate:agent_resumed" => {
            let mut event = with_env!(AgentResumed, env);
            event.agent = get_str(payload, &["agent", "agent_name", "name"]);
            event.parent_session_id = get_str(payload, &["parent_session_id"]);
            Some(UIEvent::AgentResumed(event))
        }
        "delegate:agent_cancelled" => {
            let mut event = with_env!(AgentCompleted, env);
            event.agent = get_str(payload, &["agent", "agent_name", "name"]);
            event.sub_session_id = get_str(payload, &["sub_session_id", "child_session_id"]);
            event.parent_session_id = get_str(payload, &["parent_session_id"]);
            event.success = false;
            event.result = "cancelled".to_string();
            Some(UIEvent::AgentCompleted(event))
        }
        "delegate:error" => {
            let mut event = with_env!(AgentCompleted, env);
            event.agent = get_str(payload, &["agent", "agent_name", "name"]);
            event.sub_session_id = get_str(payload, &["sub_session_id", "child_session_id"]);
            event.parent_session_id = get_str(payload, &["parent_session_id"]);
            event.success = false;
            event.result = "error".to_string();
            Some(UIEvent::AgentCompleted(event))
        }
        "user:notification" => {
            let mut event = with_env!(Notification, env);
            event.message = get_str(payload, &["message", "text"]);
            event.level = get_str_or(payload, &["level"], "info");
            event.source = get_str(payload, &["source"]);
            event.decision_id = get_str(payload, &["decision_id"]);
            Some(UIEvent::Notification(event))
        }
        _ => None,
    }
}

// --------------------------------------------------------------------------
// Tests — ports of tests/test_kernel_events_normalize.py (the QueueBridge
// halves of that file and all of tests/test_kernel_event_canary.py pin the
// kernel/queue_bridge unit, which is not ported yet — see the notes on the
// individual tests below).
// --------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    /// Python's `SID = {"session_id": "sess-1", "parent_id": None}` merged
    /// into a payload literal.
    fn with_sid(value: Value) -> Payload {
        let mut map = match value {
            Value::Object(map) => map,
            _ => panic!("payload literal must be a JSON object"),
        };
        map.insert("session_id".to_string(), json!("sess-1"));
        map.insert("parent_id".to_string(), Value::Null);
        map
    }

    fn obj(value: Value) -> Payload {
        match value {
            Value::Object(map) => map,
            _ => panic!("payload literal must be a JSON object"),
        }
    }

    const ROOT: &str = "root-session";

    #[test]
    fn test_stream_block_start() {
        let payload = with_sid(json!({"request_id": "r1", "block_index": 0, "block_type": "text"}));
        let event = normalize("llm:stream_block_start", Some(&payload)).unwrap();
        let UIEvent::StreamBlockStart(event) = event else {
            panic!("expected StreamBlockStart, got {event:?}");
        };
        assert_eq!(event.request_id, "r1");
        assert_eq!(event.session_id, "sess-1");
        assert!(!event.event_id.is_empty()); // envelope minted
    }

    #[test]
    fn test_delta_text_key_variants() {
        // Delta text arrives under delta | text | content depending on provider.
        for key in ["delta", "text", "content"] {
            let payload = with_sid(json!({
                "request_id": "r1", "block_index": 0, "sequence": 3, key: "chunk",
            }));
            let event = normalize("llm:stream_block_delta", Some(&payload)).unwrap();
            let UIEvent::StreamBlockDelta(event) = event else {
                panic!("expected StreamBlockDelta for {key}");
            };
            assert_eq!(event.text, "chunk", "{key}");
            assert_eq!(event.sequence, 3);
        }
    }

    #[test]
    fn test_delta_prefers_delta_key_over_others() {
        let payload = with_sid(json!({"delta": "right", "text": "wrong"}));
        let event = normalize("llm:stream_block_delta", Some(&payload)).unwrap();
        let UIEvent::StreamBlockDelta(event) = event else {
            panic!("expected StreamBlockDelta");
        };
        assert_eq!(event.text, "right");
    }

    #[test]
    fn test_stream_end_and_abort() {
        let payload = with_sid(json!({"request_id": "r1", "block_index": 2}));
        let end = normalize("llm:stream_block_end", Some(&payload)).unwrap();
        let UIEvent::StreamBlockEnd(end) = end else {
            panic!("expected StreamBlockEnd");
        };
        assert_eq!(end.block_index, 2);

        let payload = with_sid(json!({
            "request_id": "r1", "error": {"type": "overloaded", "msg": "529"},
        }));
        let aborted = normalize("llm:stream_aborted", Some(&payload)).unwrap();
        let UIEvent::StreamAborted(aborted) = aborted else {
            panic!("expected StreamAborted");
        };
        assert_eq!(aborted.error_type, "overloaded");
        assert_eq!(aborted.error_message, "529");
    }

    #[test]
    fn test_tool_pre_keyed_by_tool_call_id() {
        let payload = with_sid(json!({
            "tool_name": "bash",
            "tool_call_id": "call-7",
            "tool_input": {"command": "pytest -q"},
            "parallel_group_id": "pg-1",
        }));
        let event = normalize("tool:pre", Some(&payload)).unwrap();
        let UIEvent::ToolPre(event) = event else {
            panic!("expected ToolPre");
        };
        assert_eq!(event.tool_call_id, "call-7");
        assert_eq!(event.tool_input, obj(json!({"command": "pytest -q"})));
        assert_eq!(event.parallel_group_id.as_deref(), Some("pg-1"));
    }

    #[test]
    fn test_tool_post_result_vs_tool_response_variants() {
        // Result payload arrives under result | tool_response.
        for key in ["result", "tool_response"] {
            let payload = with_sid(json!({
                "tool_name": "bash", "tool_call_id": "c1", key: {"output": "ok"},
            }));
            let event = normalize("tool:post", Some(&payload)).unwrap();
            let UIEvent::ToolPost(event) = event else {
                panic!("expected ToolPost for {key}");
            };
            assert_eq!(event.result, obj(json!({"output": "ok"})), "{key}");
        }
    }

    #[test]
    fn test_tool_post_non_mapping_result_preserved() {
        let payload = with_sid(json!({
            "tool_name": "bash", "tool_call_id": "c1", "result": "done",
        }));
        let event = normalize("tool:post", Some(&payload)).unwrap();
        let UIEvent::ToolPost(event) = event else {
            panic!("expected ToolPost");
        };
        assert_eq!(event.result, obj(json!({"value": "done"})));
    }

    #[test]
    fn test_tool_error() {
        let payload = with_sid(json!({
            "tool_name": "web_fetch",
            "tool_call_id": "c9",
            "error": {"type": "Timeout", "msg": "30s"},
        }));
        let event = normalize("tool:error", Some(&payload)).unwrap();
        let UIEvent::ToolError(event) = event else {
            panic!("expected ToolError");
        };
        assert_eq!(event.tool_call_id, "c9");
        assert_eq!(event.error_type, "Timeout");
    }

    #[test]
    fn test_content_block_end_carries_block_and_usage() {
        let payload = with_sid(json!({
            "block_type": "text",
            "block_index": 1,
            "total_blocks": 2,
            "block": {"text": "final answer"},
            "usage": {"output_tokens": 42},
        }));
        let event = normalize("content_block:end", Some(&payload)).unwrap();
        let UIEvent::ContentBlockEnd(event) = event else {
            panic!("expected ContentBlockEnd");
        };
        assert_eq!(event.block, obj(json!({"text": "final answer"})));
        assert_eq!(event.usage, obj(json!({"output_tokens": 42})));
    }

    #[test]
    fn test_content_block_end_derives_type_from_inner_block() {
        for block_type in ["thinking", "tool_call"] {
            let payload = with_sid(json!({
                "block": {"type": block_type}, "block_index": 0, "total_blocks": 1,
            }));
            let event = normalize("content_block:end", Some(&payload)).unwrap();
            let UIEvent::ContentBlockEnd(event) = event else {
                panic!("expected ContentBlockEnd for {block_type}");
            };
            assert_eq!(event.block_type, block_type);
        }
    }

    #[test]
    fn test_orchestrator_complete_status_validation() {
        let payload = with_sid(json!({
            "orchestrator": "loop-streaming", "turn_count": 4, "status": "cancelled",
        }));
        let event = normalize("orchestrator:complete", Some(&payload)).unwrap();
        let UIEvent::OrchestratorComplete(event) = event else {
            panic!("expected OrchestratorComplete");
        };
        assert_eq!(event.status, OrchestratorStatus::Cancelled);

        let payload = with_sid(json!({"status": "exploded"}));
        let weird = normalize("orchestrator:complete", Some(&payload)).unwrap();
        let UIEvent::OrchestratorComplete(weird) = weird else {
            panic!("expected OrchestratorComplete");
        };
        // Unknown statuses degrade, never crash.
        assert_eq!(weird.status, OrchestratorStatus::Incomplete);
    }

    #[test]
    fn test_turn_lifecycle_events() {
        let sid = with_sid(json!({}));
        let prompt = with_sid(json!({"prompt": "hi"}));
        assert!(matches!(
            normalize("prompt:submit", Some(&prompt)),
            Some(UIEvent::PromptSubmit(_))
        ));
        assert!(matches!(
            normalize("prompt:complete", Some(&sid)),
            Some(UIEvent::PromptComplete(_))
        ));
        assert!(matches!(
            normalize("execution:start", Some(&sid)),
            Some(UIEvent::ExecutionStart(_))
        ));
        assert!(matches!(
            normalize("execution:end", Some(&sid)),
            Some(UIEvent::ExecutionEnd(_))
        ));
    }

    #[test]
    fn test_prompt_submit_records_active_mode() {
        // The turn boundary carries the app posture so the durable log (and
        // resume replay) can show which mode a historical turn ran under.
        let payload = with_sid(json!({"prompt": "ship it", "mode": "build"}));
        let event = normalize("prompt:submit", Some(&payload)).unwrap();
        let UIEvent::PromptSubmit(event) = event else {
            panic!("expected PromptSubmit");
        };
        assert_eq!(event.mode, "build");
        // Legacy logs without a mode field stay valid (empty → live fallback).
        let payload = with_sid(json!({"prompt": "ship it"}));
        let legacy = normalize("prompt:submit", Some(&payload)).unwrap();
        let UIEvent::PromptSubmit(legacy) = legacy else {
            panic!("expected PromptSubmit");
        };
        assert_eq!(legacy.mode, "");
    }

    #[test]
    fn test_provider_usage_nested_and_flat() {
        let payload = with_sid(json!({
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 250,
                "cache_read_input_tokens": 800,
                "cache_creation_input_tokens": 100,
            },
        }));
        let nested = normalize("provider:response", Some(&payload)).unwrap();
        let UIEvent::ProviderResponseUsage(nested) = nested else {
            panic!("expected ProviderResponseUsage");
        };
        assert_eq!((nested.input_tokens, nested.output_tokens), (1000, 250));
        assert_eq!((nested.cache_read, nested.cache_write), (800, 100));

        let payload = with_sid(json!({
            "input_tokens": 10, "output_tokens": 5, "cache_read": 3, "cache_write": 1,
        }));
        let flat = normalize("provider:response", Some(&payload)).unwrap();
        let UIEvent::ProviderResponseUsage(flat) = flat else {
            panic!("expected ProviderResponseUsage");
        };
        assert_eq!((flat.cache_read, flat.cache_write), (3, 1));
    }

    #[test]
    fn test_provider_notices() {
        for (name, kind) in [
            ("provider:error", NoticeKind::Error),
            ("provider:retry", NoticeKind::Retry),
            ("provider:throttle", NoticeKind::Throttle),
        ] {
            let payload = with_sid(json!({"message": "boom"}));
            let event = normalize(name, Some(&payload)).unwrap();
            let UIEvent::ProviderNotice(event) = event else {
                panic!("expected ProviderNotice for {name}");
            };
            assert_eq!(event.notice, kind);
            assert_eq!(event.message, "boom");
        }
    }

    #[test]
    fn test_session_events_and_envelope_routing() {
        let payload = obj(json!({"session_id": "child-1", "parent_id": "sess-1"}));
        let start = normalize("session:start", Some(&payload)).unwrap();
        let UIEvent::SessionStart(start) = start else {
            panic!("expected SessionStart");
        };
        assert_eq!(start.parent_id.as_deref(), Some("sess-1"));

        let payload = with_sid(json!({"source_session_id": "sess-0"}));
        let fork = normalize("session:fork", Some(&payload)).unwrap();
        let UIEvent::SessionFork(fork) = fork else {
            panic!("expected SessionFork");
        };
        assert_eq!(fork.source_session_id, "sess-0");
    }

    #[test]
    fn test_approval_required_options_verbatim() {
        let payload = with_sid(json!({
            "prompt": "Run git push?",
            "options": ["Allow once", "Allow always", "Deny"],
        }));
        let event = normalize("approval:required", Some(&payload)).unwrap();
        let UIEvent::ApprovalRequired(event) = event else {
            panic!("expected ApprovalRequired");
        };
        assert_eq!(event.options, ["Allow once", "Allow always", "Deny"]);
    }

    #[test]
    fn test_cancel_events() {
        let sid = with_sid(json!({}));
        assert!(matches!(
            normalize("cancel:requested", Some(&sid)),
            Some(UIEvent::CancelRequested(_))
        ));
        assert!(matches!(
            normalize("cancel:completed", Some(&sid)),
            Some(UIEvent::CancelCompleted(_))
        ));
    }

    #[test]
    fn test_agent_spawned_canonical_and_legacy_names() {
        // task:agent_* is canonical; legacy task:* names normalize identically.
        let payload = with_sid(json!({
            "agent": "test-writer",
            "sub_session_id": "sess-1-abc_test-writer",
            "parent_session_id": "sess-1",
        }));
        for name in ["task:agent_spawned", "task:spawned"] {
            let event = normalize(name, Some(&payload)).unwrap();
            let UIEvent::AgentSpawned(event) = event else {
                panic!("expected AgentSpawned for {name}");
            };
            assert_eq!(event.agent, "test-writer");
            assert_eq!(event.sub_session_id, "sess-1-abc_test-writer");
        }
    }

    #[test]
    fn test_agent_completed_success_default_true() {
        for name in ["task:agent_completed", "task:completed"] {
            let payload = with_sid(json!({"agent": "a", "sub_session_id": "s"}));
            let event = normalize(name, Some(&payload)).unwrap();
            let UIEvent::AgentCompleted(event) = event else {
                panic!("expected AgentCompleted for {name}");
            };
            assert!(event.success);
        }
        let payload = with_sid(json!({"agent": "a", "success": false}));
        let failed = normalize("task:agent_completed", Some(&payload)).unwrap();
        let UIEvent::AgentCompleted(failed) = failed else {
            panic!("expected AgentCompleted");
        };
        assert!(!failed.success);
    }

    #[test]
    fn test_notification() {
        let payload = with_sid(json!({"message": "saved", "level": "info"}));
        let event = normalize("user:notification", Some(&payload)).unwrap();
        let UIEvent::Notification(event) = event else {
            panic!("expected Notification");
        };
        assert_eq!(event.message, "saved");
        assert_eq!(event.decision_id, "");
    }

    #[test]
    fn test_notification_carries_decision_id() {
        let payload = with_sid(json!({
            "message": "deferred", "level": "decision", "decision_id": "decision-3",
        }));
        let event = normalize("user:notification", Some(&payload)).unwrap();
        let UIEvent::Notification(event) = event else {
            panic!("expected Notification");
        };
        assert_eq!(event.decision_id, "decision-3");
    }

    #[test]
    fn test_context_compaction_stats_are_normalized() {
        let payload = with_sid(json!({
            "before_tokens": 120_000,
            "after_tokens": 60_000,
            "before_messages": 42,
            "after_messages": 23,
            "strategy_level": 3,
        }));
        let event = normalize("context:compaction", Some(&payload)).unwrap();
        let UIEvent::ContextCompacted(event) = event else {
            panic!("expected ContextCompacted");
        };
        assert_eq!(event.before_tokens, 120_000);
        assert_eq!(event.after_tokens, 60_000);
        assert_eq!(event.strategy_level, 3);
    }

    #[test]
    fn test_unknown_events_return_none() {
        let sid = with_sid(json!({}));
        assert!(normalize("context:pre_compact_unknown_thing", Some(&sid)).is_none());
        let empty = obj(json!({}));
        assert!(normalize("totally:made_up", Some(&empty)).is_none());
    }

    #[test]
    fn test_missing_payload_never_crashes() {
        // Payload drift degrades to defaults rather than raising.
        for name in [
            "llm:stream_block_delta",
            "tool:pre",
            "tool:post",
            "provider:response",
            "approval:required",
            "task:agent_spawned",
        ] {
            assert!(normalize(name, None).is_some(), "{name}");
        }
    }

    #[test]
    fn test_delegate_agent_lifecycle_aliases() {
        // NOTE: the Python test also asserts delegate:agent_spawned /
        // delegate:agent_completed sit in QueueBridge.EVENTS — that half
        // pins the kernel/queue_bridge unit (not ported yet).
        let payload = with_sid(json!({
            "agent": "reviewer",
            "sub_session_id": "sess-1-reviewer",
            "parent_session_id": "sess-1",
        }));
        let spawned = normalize("delegate:agent_spawned", Some(&payload)).unwrap();
        let UIEvent::AgentSpawned(spawned) = spawned else {
            panic!("expected AgentSpawned");
        };
        assert_eq!(spawned.agent, "reviewer");
        assert_eq!(spawned.sub_session_id, "sess-1-reviewer");

        let payload = with_sid(json!({
            "agent": "reviewer",
            "sub_session_id": "sess-1-reviewer",
            "parent_session_id": "sess-1",
            "success": true,
            "result": "review complete",
        }));
        let completed = normalize("delegate:agent_completed", Some(&payload)).unwrap();
        let UIEvent::AgentCompleted(completed) = completed else {
            panic!("expected AgentCompleted");
        };
        assert!(completed.success);
        assert_eq!(completed.result, "review complete");
    }

    #[test]
    fn test_normalize_delegate_agent_resumed() {
        // Resume reopens a lane without changing parent session.
        let raw = obj(json!({
            "session_id": "kid-1_worker",  // child session
            "parent_session_id": ROOT,
        }));
        let result = normalize("delegate:agent_resumed", Some(&raw)).unwrap();
        assert_eq!(result.kind(), "agent_resumed");
        let UIEvent::AgentResumed(result) = result else {
            panic!("expected AgentResumed");
        };
        assert_eq!(result.session_id, "kid-1_worker");
    }

    #[test]
    fn test_normalize_delegate_agent_cancelled() {
        // Cancellation is a terminal event with explicit state.
        let raw = obj(json!({
            "session_id": ROOT,
            "agent": "worker",
            "sub_session_id": "kid-1_worker",
            "parent_session_id": ROOT,
        }));
        let result = normalize("delegate:agent_cancelled", Some(&raw)).unwrap();
        assert_eq!(result.kind(), "agent_completed"); // normalized to agent_completed
        let UIEvent::AgentCompleted(result) = result else {
            panic!("expected AgentCompleted");
        };
        assert_eq!(result.session_id, ROOT);
        assert_eq!(result.result, "cancelled");
        assert!(!result.success);
    }

    #[test]
    fn test_normalize_delegate_error() {
        // Errors become agent_completed with error result.
        let raw = obj(json!({
            "session_id": ROOT,
            "agent": "worker",
            "sub_session_id": "kid-1_worker",
            "parent_session_id": ROOT,
            "error": "boom",
        }));
        let result = normalize("delegate:error", Some(&raw)).unwrap();
        assert_eq!(result.kind(), "agent_completed");
        let UIEvent::AgentCompleted(result) = result else {
            panic!("expected AgentCompleted");
        };
        assert_eq!(result.result, "error");
        assert!(!result.success);
    }

    #[test]
    fn test_event_ids_are_unique() {
        let sid = with_sid(json!({}));
        let a = normalize("execution:start", Some(&sid)).unwrap();
        let b = normalize("execution:start", Some(&sid)).unwrap();
        assert_ne!(a.event_id(), b.event_id());
    }

    #[test]
    fn test_events_json_roundtrip() {
        // Normalized events survive ui-events.jsonl round-trips.
        let payload = with_sid(json!({
            "tool_name": "bash", "tool_call_id": "c1", "result": {"output": "ok"},
        }));
        let event = normalize("tool:post", Some(&payload)).unwrap();
        assert!(matches!(event, UIEvent::ToolPost(_)));
        let restored: UIEvent =
            serde_json::from_str(&serde_json::to_string(&event).unwrap()).unwrap();
        assert_eq!(restored, event);
    }

    // Real runtime: usage rides on content_block:end (no provider:response).
    mod test_usage_from_content_block_end {
        use super::*;

        #[test]
        fn test_synthesizes_usage_with_provider_cost() {
            let payload = obj(json!({
                "block_type": "text",
                "block_index": 0,
                "total_blocks": 1,
                "block": {"text": "OK", "type": "text"},
                "usage": {
                    "input_tokens": 2,
                    "output_tokens": 4,
                    "cache_read_tokens": null,
                    "cache_creation_input_tokens": 88471,
                    "cost_usd": "1.1061075",
                },
            }));
            let block_end = normalize("content_block:end", Some(&payload)).unwrap();
            let UIEvent::ContentBlockEnd(block_end) = block_end else {
                panic!("expected ContentBlockEnd");
            };
            let usage = usage_from_content_block_end(&block_end).unwrap();
            assert_eq!(usage.input_tokens, 2);
            assert_eq!(usage.output_tokens, 4);
            assert_eq!(usage.cache_write, 88471);
            assert_eq!(usage.cost_usd, Some(Decimal::from_str("1.1061075").unwrap()));
            // NOTE: the Python test additionally asserts
            // `cost_of(usage) == Decimal("1.1061075")` — that half pins the
            // kernel/cost unit (not ported yet).
        }

        #[test]
        fn test_no_usage_payload_returns_none() {
            let payload = obj(json!({
                "block_type": "text", "block": {"text": "hi", "type": "text"},
            }));
            let block_end = normalize("content_block:end", Some(&payload)).unwrap();
            let UIEvent::ContentBlockEnd(block_end) = block_end else {
                panic!("expected ContentBlockEnd");
            };
            assert!(usage_from_content_block_end(&block_end).is_none());
        }

        /// Adapted from the Python QueueBridge test of the same name: the
        /// bridge plumbing lives in kernel/queue_bridge (not ported yet),
        /// but the once-per-response rule it exercises is THIS function —
        /// only the final block of a multi-block response yields usage.
        #[test]
        fn test_bridge_emits_usage_once_for_multi_block_response() {
            let blocks = [
                json!({"type": "thinking", "thinking": "considering"}),
                json!({"type": "text", "text": "Working on it."}),
                json!({"type": "tool_call", "name": "bash"}),
            ];
            let mut emitted: Vec<usize> = Vec::new();
            for (index, block) in blocks.iter().enumerate() {
                let payload = obj(json!({
                    "block_index": index,
                    "total_blocks": 3,
                    "block": block,
                    "usage": {"input_tokens": 2, "output_tokens": 4},
                }));
                let event = normalize("content_block:end", Some(&payload)).unwrap();
                let UIEvent::ContentBlockEnd(event) = event else {
                    panic!("expected ContentBlockEnd");
                };
                if usage_from_content_block_end(&event).is_some() {
                    emitted.push(index);
                }
            }
            assert_eq!(emitted, [2]); // exactly once, on the final block
        }
    }

    // Stored events.jsonl records round-trip back into typed UIEvents
    // (the resume transcript-replay loader, DESIGN-SPEC §3/§11).
    mod test_parse_event {
        use super::*;

        #[test]
        fn test_round_trips_a_persisted_record() {
            let event = UIEvent::ProviderResponseUsage(ProviderResponseUsage {
                session_id: "root01".to_string(),
                input_tokens: 10,
                output_tokens: 20,
                cost_usd: Some(Decimal::from_str("0.5").unwrap()),
                ..ProviderResponseUsage::default()
            });
            let record = serde_json::to_value(&event).unwrap();
            assert_eq!(parse_event(&record), Some(event));
        }

        #[test]
        fn test_rejects_foreign_and_malformed_records() {
            // Raw hook payloads from other writers sharing the file.
            assert_eq!(
                parse_event(&json!({"event": "tool:pre", "tool_name": "bash"})),
                None
            );
            // Unknown discriminator.
            assert_eq!(parse_event(&json!({"kind": "mystery_kind"})), None);
            // Extra keys fail the deny_unknown_fields envelope — a foreign
            // record can never half-parse into one of ours.
            let event = UIEvent::PromptSubmit(PromptSubmit {
                prompt: "hi".to_string(),
                ..PromptSubmit::default()
            });
            let mut record = serde_json::to_value(&event).unwrap();
            record["foreign_field"] = json!(true);
            assert_eq!(parse_event(&record), None);
        }
    }
}
