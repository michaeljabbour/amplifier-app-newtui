//! Runtime adapters: the seam between the app and a runtime.
//!
//! Port of `src/amplifier_app_newtui/ui/runtime_adapter.py` (assessment +
//! contract port).
//!
//! ADR-0007 §Runtimes: the app consumes one event stream and cannot tell a
//! demo session from a real one. The adapter owns the shared
//! interaction-state queues (steering / needs-you / denial log) so the
//! runtime wiring and the app act on the SAME objects.
//!
//! Ratatui adaptation (what ports and what does not):
//!
//! - The Python base class's optional-hook contract becomes the
//!   [`RuntimeAdapter`] trait; every neutral stub is a trait DEFAULT method
//!   carrying the exact Python value/string, so "all hooks optional" holds
//!   the same way (and the compiler replaces the Python
//!   introspection-guard test — a type cannot claim the contract without
//!   the full surface).
//! - The Python base class's owned state (shared queues, session identity,
//!   live `/config` state) becomes [`RuntimeAdapterBase`], which implements
//!   the trait with the base behaviors. Adapter implementations embed it
//!   (composition instead of inheritance).
//! - `RealRuntimeAdapter`'s thread machinery does NOT port: the runtime
//!   thread + event loop, `_AppLoopQueue`, `run_coroutine_threadsafe`
//!   marshalling (`_in_runtime`), `call_soon_threadsafe` hops for approvals
//!   /boot progress/broker presentation, and `shutdown()`'s loop-closed
//!   guards are Textual/asyncio mechanics. In this client the runtime is a
//!   spawned backend process ([`crate::core_client::CoreClientRuntime`])
//!   whose events arrive on the app's `mpsc` channel and whose teardown is
//!   `Drop`. What ports is the CONTRACT plus every pure rule:
//!   the pre-boot "session still starting" guard (Python `_run_op`'s
//!   `_runtime is None` branch), prompt-history recording, the
//!   deferred-decision resolution, and the decision narration.
//! - [`ClientRuntimeAdapter`] is the real-runtime counterpart: an adapter
//!   struct over `Box<dyn `[`Runtime`]`>` + the shared queues. The wire
//!   protocol carries only `submit` / `approve` / `interrupt` today, so
//!   those three forward; the passthrough session ops answer their Python
//!   *starting* values — the in-process runtime handle the Python guard
//!   tests for (`_runtime is None`) never exists here, because the live
//!   session lives behind `serve`. Extending the protocol moves an op from
//!   the guard branch to a real forward without touching the trait.
//! - Python's per-adapter `queue: asyncio.Queue[UIEvent]` does not port:
//!   event delivery in this client is the app-loop `mpsc::Sender<Msg>` the
//!   runtime is constructed with. `attach(app)` (the Textual app handle)
//!   does not port either — approval presentation is app-side state fed by
//!   decoded protocol events.
//! - `kernel.config_ops.save_config` (an unported kernel unit) is required
//!   by the base `/config save` contract, so its observable behavior is
//!   ported privately here ([`save_config`]): scope-file resolution
//!   (`AMPLIFIER_HOME` honored, injectable for tests like the Python
//!   `home=` parameter), deep-merge under the `configurator:` key, stale
//!   block removal on an empty change set, atomic tmp-file replace, and
//!   the exact user-facing messages.

use std::fmt;
use std::io;
use std::path::{Path, PathBuf};
use std::sync::Mutex;

use rust_decimal::Decimal;
use serde_json::{Map, Value};

use crate::kernel::events::UIEvent;
use crate::kernel::prompt_history::PromptHistoryStore;
use crate::model::blocks::{BlockIdAllocator, TranscriptBlock};
use crate::model::config::{
    default_config_state, ConfigChange, ConfigSnapshotView, SessionConfigState,
};
use crate::model::evidence::EvidenceLink;
use crate::model::queues::{DeferOptions, LaneSteeringQueue, NeedsYouQueue, SteeringQueue};
use crate::model::terminal::{TerminalSurface, DEFAULT_TERMINAL_COLS};
use crate::model::trust::DenialLog;
use crate::model::turn::OutcomeLedger;
use crate::runtime::Runtime;
use crate::ui::composer::ImageAttachment;
use crate::ui::directory_admin::{DirectoryEntry, DirectoryKind};
use crate::ui::reducer::{LaneSeed, TurnSpec};
use crate::ui::session_ops_view::{
    AccountingMode, CompactionConfig, ModelListing, SessionSummary, SkillInfo, StatusInfo,
    DEFAULT_CONTEXT_WINDOW,
};

/// Python `_STILL_STARTING` — the real adapter's shared "runtime thread
/// still booting" reply for the fallible `(ok, detail)` ops.
pub const STILL_STARTING: &str = "session still starting";

/// Mirror of `kernel.rewind.RewindError` (that kernel unit is not ported;
/// only the adapter-raised error surface crosses this boundary). The base
/// adapter also funnels the ledger's unknown-checkpoint `KeyError` through
/// it, keeping [`RuntimeAdapter::fork`]'s error type single.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct RewindError(pub String);

impl fmt::Display for RewindError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for RewindError {}

/// The base adapter contract (Python class `RuntimeAdapter`) — all hooks
/// optional; default bodies are the Python base class's neutral stubs with
/// their exact values and strings.
///
/// Where the surface overlaps
/// [`crate::ui::session_ops_controller::SessionOpsAdapter`] and
/// [`crate::ui::command_context::CommandHost`], the method names and
/// `(ok, detail)` tuple shapes match verbatim so the assembled app forwards
/// mechanically.
pub trait RuntimeAdapter {
    // -- shared interaction queues (Python `__init__` state) ----------------

    /// `adapter.steering` — pause-point steering FIFO shared with the kernel.
    fn steering(&self) -> &SteeringQueue;

    /// `adapter.lane_steering` — per-lane steer FIFOs (issue #39): a message
    /// aimed at a running delegate, delivered at that child's next step
    /// boundary. Shared with the kernel wiring so the app and runtime act
    /// on the SAME queues.
    fn lane_steering(&self) -> &LaneSteeringQueue;

    /// `adapter.needs_you` — deferred-decision queue.
    fn needs_you(&self) -> &NeedsYouQueue;

    /// `adapter.denial_log` (behind a lock, matching
    /// [`crate::ui::command_context::CommandHost::denial_log`]).
    fn denial_log(&self) -> &Mutex<DenialLog>;

    /// `adapter.terminal` — live terminal width shared with the kernel's
    /// width-aware surface-hint hook (#35).
    fn terminal(&self) -> &TerminalSurface;

    // -- session identity (defaults = Python `__init__` defaults) -----------

    /// `adapter.bundle_name`.
    fn bundle_name(&self) -> String {
        String::new()
    }

    /// `adapter.model_name` — primary model id, possibly provider-qualified
    /// (`anthropic/x`).
    fn model_name(&self) -> String {
        String::new()
    }

    /// `adapter.session_short`.
    fn session_short(&self) -> String {
        String::new()
    }

    /// `adapter.session_id` — full stored-session id, surfaced on exit so
    /// the CLI can print the exact `amplifier-newtui resume <id>` command
    /// (S4). Empty for demo sessions, which have no resumable store entry.
    fn session_id(&self) -> String {
        String::new()
    }

    /// `adapter.banner`.
    fn banner(&self) -> (String, String) {
        (String::new(), String::new())
    }

    /// `adapter.session_cost_start`.
    fn session_cost_start(&self) -> Decimal {
        Decimal::ZERO
    }

    /// `adapter.turn_base` — restored-history user-message count on resume
    /// (checkpoint turn ids offset past it — DESIGN-SPEC §9); 0 for
    /// fresh/demo sessions.
    fn turn_base(&self) -> u64 {
        0
    }

    /// `adapter.restored_history` — (role, text) pairs replayed into the
    /// transcript on resume.
    fn restored_history(&self) -> Vec<(String, String)> {
        Vec::new()
    }

    /// `adapter.restored_events` — the resumed session's stored UIEvents,
    /// replayed through the reducer to rebuild the full transcript
    /// (DESIGN-SPEC §3/§11); empty means the prose `restored_history`
    /// fallback renders instead.
    fn restored_events(&self) -> Vec<UIEvent> {
        Vec::new()
    }

    /// `adapter.startup_notices`.
    fn startup_notices(&self) -> Vec<String> {
        Vec::new()
    }

    /// `adapter.pending_directive` — a resumed fork child's primed starting
    /// directive; empty for fresh/demo sessions and ordinary resumes.
    fn pending_directive(&self) -> String {
        String::new()
    }

    /// `adapter.compaction` — Python default
    /// `CompactionConfig(auto_compact=True, compact_threshold=0.8)`.
    fn compaction(&self) -> CompactionConfig {
        base_compaction()
    }

    // -- lifecycle ----------------------------------------------------------

    /// Boot the runtime; call `ready()` once session identity
    /// (banner/bundle/session) is known and BEFORE producing turn events.
    fn start(&mut self, ready: &mut dyn FnMut()) {
        ready();
    }

    /// Run `text` as a new user turn (with optional image attachments).
    fn submit(&mut self, text: &str, attachments: &[ImageAttachment]) {
        let _ = (text, attachments);
    }

    /// Run a queue-drained message as the next turn (spec §5).
    ///
    /// Default: same as [`Self::submit`]. The demo adapter overrides it to
    /// skip its scripted mode notice — mockup `drainQueue` runs the drained
    /// turn without `setMode`, so nothing overwrites the
    /// `queued message picked up` notice.
    fn submit_queued(&mut self, text: &str) {
        self.submit(text, &[]);
    }

    // -- persistent prompt history (cross-session ↑ recall) ------------------
    // The store is keyed per working directory (ADR-0007: the adapter seam
    // owns filesystem/session access). The base and demo adapters have no
    // real project on disk, so both no-op — only the real adapter persists.

    /// Persist a submitted prompt for future sessions in this directory.
    fn record_prompt(&mut self, text: &str) {
        let _ = text;
    }

    /// Prompts submitted in this directory across sessions (oldest first).
    fn prompt_history(&mut self) -> Vec<String> {
        Vec::new()
    }

    // -- passthrough session ops (Python `SESSION_OPS` demo values) ----------
    // Python declares each op ONCE in the `SESSION_OPS` descriptor table
    // (name, demo value, starting value) and dispatches through the single
    // `_run_op` seam. The descriptor mechanism collapses asyncio
    // thread-marshalling twins, which do not exist here — the trait default
    // IS the demo value, and a real adapter's guard branch IS the starting
    // value ([`STILL_STARTING`]).

    /// Request an interrupt; `true` when the runtime accepted it.
    fn interrupt(&mut self) -> bool {
        false
    }

    /// Bundle-composed mode catalog (real sessions); `""` when absent.
    /// Typically a mapping with a `modes` list of {name, description,
    /// source} entries — whatever the mounted mode tool reports.
    fn list_native_modes(&mut self) -> Value {
        Value::String(String::new())
    }

    /// Activate/clear a bundle-provided mode via the native mode tool.
    fn set_native_mode(&mut self, name: Option<&str>) -> (bool, String) {
        let _ = name;
        (false, "native modes need a real session".to_string())
    }

    fn list_models(&mut self) -> ModelListing {
        ModelListing::default()
    }

    fn set_model(&mut self, model: &str) -> (bool, String) {
        let _ = model;
        (false, "switching models needs a real session".to_string())
    }

    fn get_effort(&mut self) -> Option<String> {
        None
    }

    fn set_effort(&mut self, level: &str) -> (bool, String) {
        let _ = level;
        (false, "reasoning effort needs a real session".to_string())
    }

    fn compact(&mut self, focus: &str) -> (bool, String) {
        let _ = focus;
        (false, "compaction needs a real session".to_string())
    }

    fn clear_context(&mut self) -> (bool, u64) {
        (false, 0)
    }

    fn status(&mut self) -> StatusInfo {
        StatusInfo::default()
    }

    fn list_tools(&mut self) -> Vec<String> {
        Vec::new()
    }

    fn list_agents(&mut self) -> Vec<String> {
        Vec::new()
    }

    fn diff(&mut self, staged: bool) -> Option<String> {
        let _ = staged;
        None
    }

    /// Relative paths available to composer `@file` autocomplete.
    fn workspace_files(&mut self) -> Vec<String> {
        Vec::new()
    }

    fn list_skills(&mut self) -> Vec<SkillInfo> {
        Vec::new()
    }

    fn load_skill(&mut self, name: &str) -> (bool, String) {
        let _ = name;
        (false, "skills need a real session".to_string())
    }

    fn mcp_tools(&mut self) -> Vec<String> {
        Vec::new()
    }

    /// Compose a deferred overlay into the live session (`/bundle load`).
    fn load_deferred_bundle(&mut self, name: &str) -> (bool, String) {
        let _ = name;
        (false, "loading a bundle needs a real session".to_string())
    }

    /// Overlay URIs held back from boot (`bundle.deferred`); empty for demo.
    fn deferred_bundles(&mut self) -> Vec<String> {
        Vec::new()
    }

    // -- stored-session lifecycle --------------------------------------------

    fn rename_session(&mut self, name: &str) -> (bool, String) {
        let _ = name;
        (false, "renaming needs a real session".to_string())
    }

    fn session_summaries(&mut self) -> Vec<SessionSummary> {
        Vec::new()
    }

    fn branch_session(&mut self, name: &str) -> (bool, String) {
        let _ = name;
        (false, "branching needs a real session".to_string())
    }

    fn fork_with_directive(&mut self, directive: &str) -> (bool, String) {
        let _ = directive;
        (false, "forking needs a real session".to_string())
    }

    fn directory_entries(&mut self, kind: DirectoryKind) -> Vec<DirectoryEntry> {
        let _ = kind;
        Vec::new()
    }

    fn update_directory(
        &mut self,
        kind: DirectoryKind,
        operation: &str,
        path: &str,
    ) -> (bool, String) {
        let _ = (kind, operation, path);
        (false, "directory management needs a real session".to_string())
    }

    /// Fork the session at `checkpoint_id`, then trim `ledger` (spec §9).
    ///
    /// Confirm-then-trim (ADR-0007 §Rewind): the ledger trims only after
    /// the backend confirms the fork; return a [`RewindError`] on failure
    /// and leave everything untouched. The base/demo runtime keeps its
    /// conversation in memory only, so confirmation is immediate.
    fn fork(&mut self, checkpoint_id: &str, ledger: &mut OutcomeLedger) -> Result<(), RewindError> {
        ledger
            .trim_to(checkpoint_id)
            .map_err(|error| RewindError(error.to_string()))
    }

    /// Route an approval-bar resolution back to the runtime.
    fn answer_approval(&mut self, ticket_id: &str, choice: &str) {
        let _ = (ticket_id, choice);
    }

    // -- /config live session config (base: in-memory state) -----------------
    // The state is shared verbatim by demo and real (ADR-0007 invariant 4);
    // real sessions reseed it from the mount plan at start(). State-backed,
    // so no trait default — [`RuntimeAdapterBase`] implements the shared
    // behavior every adapter embeds.

    /// Frozen, thread-hop-safe snapshot of the live config state.
    fn config_view(&mut self) -> ConfigSnapshotView;

    /// Enable/disable a config item in the session scope.
    fn config_toggle(&mut self, category: &str, name: &str, enable: bool) -> (bool, String);

    /// Set a config override (session scope) with type inference.
    fn config_set(&mut self, path: &str, value: &str) -> (bool, String);

    /// Changes to the config state since session start.
    fn config_diff(&mut self) -> Vec<ConfigChange>;

    /// Persist the session config changes to a settings scope file.
    fn config_save(&mut self, scope: &str) -> (bool, String);

    /// Park a live approval ticket into the needs-you queue WITHOUT
    /// answering it (ctrl-y on the approval bar).
    ///
    /// The base/demo runtime has no kernel broker, so the deferred decision
    /// is parked here directly: the pending approval is left untouched
    /// (deny-and-continue) and the item stays retro-answerable via ctrl-y
    /// (ADR-0007 resolution 5). `ticket_id` names the ticket for broker
    /// routing overrides; the base park keys off the visible prompt.
    fn defer_approval(&mut self, ticket_id: &str, prompt: &str, options: &[String]) {
        let _ = ticket_id;
        let question = prompt.trim();
        if question.is_empty() {
            return;
        }
        let _ = self.needs_you().defer(
            question,
            "deferred approval",
            DeferOptions {
                choices: options.to_vec(),
                action: question.to_string(),
                ..DeferOptions::default()
            },
        );
    }

    // -- optional data hooks (demo fidelity / real telemetry) -----------------

    /// Close-out spec for the turn started by `prompt` (demo parity).
    fn turn_spec(&mut self, prompt: &str) -> Option<TurnSpec> {
        let _ = prompt;
        None
    }

    /// Initial lane presentation data for a spawned agent.
    fn lane_seed(&mut self, agent_name: &str) -> Option<LaneSeed> {
        let _ = agent_name;
        None
    }

    /// The focused-lane transcript block list (spec §8), if known.
    fn lane_blocks(
        &mut self,
        name: &str,
        session_id: &str,
        allocator: &mut BlockIdAllocator,
    ) -> Option<Vec<TranscriptBlock>> {
        let _ = (name, session_id, allocator);
        None
    }

    /// Evidence links grounding the final answer `answer_text` (spec §10).
    fn evidence_links(&mut self, answer_text: &str) -> Vec<EvidenceLink> {
        let _ = answer_text;
        Vec::new()
    }

    /// `(question, reason, choices, highlight, action)` for a
    /// deferred-decision event — `highlight` is the question substring
    /// rendered teal; `action` is the denied action key (the /improve
    /// override-evidence join against the DenialLog). `decision_id` is the
    /// already-parked NeedsYouQueue item when the deferral happened
    /// kernel-side; empty for message-only (scripted) deferrals.
    fn deferred_decision(
        &mut self,
        message: &str,
        decision_id: &str,
    ) -> (String, String, Vec<String>, String, String) {
        let _ = decision_id;
        (
            message.to_string(),
            String::new(),
            Vec::new(),
            String::new(),
            String::new(),
        )
    }

    /// The `Applying decision: …` narration for an acted-on choice.
    /// `action` is the decision's denied-action key, when it has one
    /// (the base ignores it; the real adapter names it).
    fn decision_narration(&mut self, choice: &str, action: &str) -> String {
        let _ = action;
        format!("Applying decision: {choice}")
    }
}

/// Python base `RuntimeAdapter.compaction` default:
/// `CompactionConfig(auto_compact=True, compact_threshold=0.8)`.
fn base_compaction() -> CompactionConfig {
    CompactionConfig {
        max_tokens: DEFAULT_CONTEXT_WINDOW,
        auto_compact: Some(true),
        compact_threshold: Some(0.8),
        accounting: AccountingMode::Estimated,
    }
}

/// The Python base class's owned state: the shared interaction queues plus
/// session identity and the live `/config` state. Implements
/// [`RuntimeAdapter`] with the base behaviors; concrete adapters embed it
/// (composition replaces Python subclassing).
///
/// Fields are public the way the Python attributes are: `start()` on a real
/// adapter copies session identity into them, and app assembly reads them.
pub struct RuntimeAdapterBase {
    pub steering: SteeringQueue,
    pub lane_steering: LaneSteeringQueue,
    pub needs_you: NeedsYouQueue,
    pub denial_log: Mutex<DenialLog>,
    pub terminal: TerminalSurface,
    pub bundle_name: String,
    pub model_name: String,
    pub session_short: String,
    pub session_id: String,
    pub banner: (String, String),
    pub session_cost_start: Decimal,
    pub turn_base: u64,
    pub restored_history: Vec<(String, String)>,
    pub restored_events: Vec<UIEvent>,
    pub startup_notices: Vec<String>,
    pub pending_directive: String,
    pub compaction: CompactionConfig,
    /// Live `/config` state — shared by demo and real (invariant 4); real
    /// sessions reseed it from the mount plan at `start()`.
    pub config_state: SessionConfigState,
    /// Python `_config_project_dir = Path.cwd()`.
    pub config_project_dir: PathBuf,
    /// Test seam mirroring the Python `save_config(..., home=...)`
    /// parameter: an explicit amplifier home wins over `AMPLIFIER_HOME`.
    pub config_home: Option<PathBuf>,
}

impl RuntimeAdapterBase {
    pub fn new() -> Self {
        Self {
            steering: SteeringQueue::new(),
            lane_steering: LaneSteeringQueue::new(),
            needs_you: NeedsYouQueue::new(),
            denial_log: Mutex::new(DenialLog::new()),
            terminal: TerminalSurface::new(i64::from(DEFAULT_TERMINAL_COLS)),
            bundle_name: String::new(),
            model_name: String::new(),
            session_short: String::new(),
            session_id: String::new(),
            banner: (String::new(), String::new()),
            session_cost_start: Decimal::ZERO,
            turn_base: 0,
            restored_history: Vec::new(),
            restored_events: Vec::new(),
            startup_notices: Vec::new(),
            pending_directive: String::new(),
            compaction: base_compaction(),
            config_state: default_config_state(""),
            config_project_dir: std::env::current_dir().unwrap_or_else(|_| PathBuf::from(".")),
            config_home: None,
        }
    }
}

impl Default for RuntimeAdapterBase {
    fn default() -> Self {
        Self::new()
    }
}

impl RuntimeAdapter for RuntimeAdapterBase {
    fn steering(&self) -> &SteeringQueue {
        &self.steering
    }

    fn lane_steering(&self) -> &LaneSteeringQueue {
        &self.lane_steering
    }

    fn needs_you(&self) -> &NeedsYouQueue {
        &self.needs_you
    }

    fn denial_log(&self) -> &Mutex<DenialLog> {
        &self.denial_log
    }

    fn terminal(&self) -> &TerminalSurface {
        &self.terminal
    }

    fn bundle_name(&self) -> String {
        self.bundle_name.clone()
    }

    fn model_name(&self) -> String {
        self.model_name.clone()
    }

    fn session_short(&self) -> String {
        self.session_short.clone()
    }

    fn session_id(&self) -> String {
        self.session_id.clone()
    }

    fn banner(&self) -> (String, String) {
        self.banner.clone()
    }

    fn session_cost_start(&self) -> Decimal {
        self.session_cost_start
    }

    fn turn_base(&self) -> u64 {
        self.turn_base
    }

    fn restored_history(&self) -> Vec<(String, String)> {
        self.restored_history.clone()
    }

    fn restored_events(&self) -> Vec<UIEvent> {
        self.restored_events.clone()
    }

    fn startup_notices(&self) -> Vec<String> {
        self.startup_notices.clone()
    }

    fn pending_directive(&self) -> String {
        self.pending_directive.clone()
    }

    fn compaction(&self) -> CompactionConfig {
        self.compaction.clone()
    }

    fn config_view(&mut self) -> ConfigSnapshotView {
        ConfigSnapshotView::of(&self.config_state)
    }

    fn config_toggle(&mut self, category: &str, name: &str, enable: bool) -> (bool, String) {
        self.config_state.toggle(category, name, enable)
    }

    fn config_set(&mut self, path: &str, value: &str) -> (bool, String) {
        self.config_state.set_value(path, value)
    }

    fn config_diff(&mut self) -> Vec<ConfigChange> {
        self.config_state.diff()
    }

    fn config_save(&mut self, scope: &str) -> (bool, String) {
        save_config(
            &self.config_state,
            scope,
            &self.config_project_dir,
            self.config_home.as_deref(),
        )
    }
}

/// Adapter over a live backend runtime (the Rust counterpart of Python's
/// `RealRuntimeAdapter`, minus its thread machinery — see module docs).
///
/// Wraps any [`Runtime`] (in practice
/// [`crate::core_client::CoreClientRuntime`], which owns the spawned
/// `serve` backend). The three protocol ops forward over the wire; the
/// passthrough session ops answer their Python *starting* values because
/// the in-process runtime handle Python guards on never exists here (the
/// live session is behind `serve`). Prompts persist per project directory,
/// exactly like the Python real adapter.
pub struct ClientRuntimeAdapter {
    base: RuntimeAdapterBase,
    runtime: Box<dyn Runtime>,
    /// Python's boot future resolved: `start()` ran (the analog of
    /// awaiting `started` before wiring the session surface).
    started: bool,
    /// Lazily-built per-project prompt-history store
    /// (Python `_history_store`).
    prompt_store: Option<PromptHistoryStore>,
}

impl ClientRuntimeAdapter {
    pub fn new(runtime: Box<dyn Runtime>) -> Self {
        Self {
            base: RuntimeAdapterBase::new(),
            runtime,
            started: false,
            prompt_store: None,
        }
    }

    /// Inject an explicit prompt-history store (tests / custom homes);
    /// otherwise the store is derived from the project directory.
    pub fn with_prompt_store(mut self, store: PromptHistoryStore) -> Self {
        self.prompt_store = Some(store);
        self
    }

    /// The shared adapter state, for app assembly to read (identity fields)
    /// and fill as protocol lifecycle records arrive.
    pub fn base(&self) -> &RuntimeAdapterBase {
        &self.base
    }

    pub fn base_mut(&mut self) -> &mut RuntimeAdapterBase {
        &mut self.base
    }

    /// Python `_history_store()`: lazily build the per-project store keyed
    /// to the session's working directory.
    fn history_store(&mut self) -> &PromptHistoryStore {
        if self.prompt_store.is_none() {
            self.prompt_store = Some(PromptHistoryStore::for_project_dir(
                &self.base.config_project_dir,
            ));
        }
        self.prompt_store.as_ref().expect("just filled")
    }

    fn starting(&self) -> (bool, String) {
        (false, STILL_STARTING.to_string())
    }
}

impl RuntimeAdapter for ClientRuntimeAdapter {
    fn steering(&self) -> &SteeringQueue {
        &self.base.steering
    }

    fn lane_steering(&self) -> &LaneSteeringQueue {
        &self.base.lane_steering
    }

    fn needs_you(&self) -> &NeedsYouQueue {
        &self.base.needs_you
    }

    fn denial_log(&self) -> &Mutex<DenialLog> {
        &self.base.denial_log
    }

    fn terminal(&self) -> &TerminalSurface {
        &self.base.terminal
    }

    fn bundle_name(&self) -> String {
        self.base.bundle_name.clone()
    }

    fn model_name(&self) -> String {
        self.base.model_name.clone()
    }

    fn session_short(&self) -> String {
        self.base.session_short.clone()
    }

    fn session_id(&self) -> String {
        self.base.session_id.clone()
    }

    fn banner(&self) -> (String, String) {
        self.base.banner.clone()
    }

    fn session_cost_start(&self) -> Decimal {
        self.base.session_cost_start
    }

    fn turn_base(&self) -> u64 {
        self.base.turn_base
    }

    fn restored_history(&self) -> Vec<(String, String)> {
        self.base.restored_history.clone()
    }

    fn restored_events(&self) -> Vec<UIEvent> {
        self.base.restored_events.clone()
    }

    fn startup_notices(&self) -> Vec<String> {
        self.base.startup_notices.clone()
    }

    fn pending_directive(&self) -> String {
        self.base.pending_directive.clone()
    }

    fn compaction(&self) -> CompactionConfig {
        self.base.compaction.clone()
    }

    fn start(&mut self, ready: &mut dyn FnMut()) {
        // The backend process was spawned when the wrapped Runtime was
        // constructed; identity lands via protocol lifecycle records
        // (app assembly fills `base_mut()`). Marking started is the analog
        // of Python's `await started` boot gate.
        self.started = true;
        ready();
    }

    /// Python real `submit`: no-op before boot, forward once live. Image
    /// attachments are not carried by the wire protocol yet (client-side
    /// gap; the composer stages them, `serve` cannot receive them).
    fn submit(&mut self, text: &str, _attachments: &[ImageAttachment]) {
        if self.started {
            self.runtime.submit(text.to_string());
        }
    }

    fn record_prompt(&mut self, text: &str) {
        self.history_store().append(text);
    }

    fn prompt_history(&mut self) -> Vec<String> {
        self.history_store().load()
    }

    /// Pre-boot: Python's neutral `False`. Once live the request is
    /// dispatched over the wire and reported accepted — the backend owns
    /// whether anything was actually running to interrupt.
    fn interrupt(&mut self) -> bool {
        if !self.started {
            return false;
        }
        self.runtime.interrupt();
        true
    }

    // -- passthrough session ops: the Python `_run_op` guard branch ----------
    // `_runtime is None` is permanently true in this client (the live
    // session lives behind `serve`; no session-op wire surface yet), so
    // every fallible op answers its `starting` value and the rest keep
    // their neutral defaults. Extending the protocol turns one of these
    // into a real forward.

    fn set_native_mode(&mut self, _name: Option<&str>) -> (bool, String) {
        self.starting()
    }

    fn set_model(&mut self, _model: &str) -> (bool, String) {
        self.starting()
    }

    fn set_effort(&mut self, _level: &str) -> (bool, String) {
        self.starting()
    }

    fn compact(&mut self, _focus: &str) -> (bool, String) {
        self.starting()
    }

    fn load_skill(&mut self, _name: &str) -> (bool, String) {
        self.starting()
    }

    fn load_deferred_bundle(&mut self, _name: &str) -> (bool, String) {
        self.starting()
    }

    fn rename_session(&mut self, _name: &str) -> (bool, String) {
        self.starting()
    }

    fn branch_session(&mut self, _name: &str) -> (bool, String) {
        self.starting()
    }

    fn fork_with_directive(&mut self, _directive: &str) -> (bool, String) {
        self.starting()
    }

    fn update_directory(
        &mut self,
        _kind: DirectoryKind,
        _operation: &str,
        _path: &str,
    ) -> (bool, String) {
        self.starting()
    }

    /// Python real `fork` raises `RewindError("session not started")`
    /// without a live runtime handle; confirm-then-trim means the ledger is
    /// NEVER trimmed until the backend confirms, and the protocol has no
    /// fork op yet.
    fn fork(
        &mut self,
        _checkpoint_id: &str,
        _ledger: &mut OutcomeLedger,
    ) -> Result<(), RewindError> {
        Err(RewindError("session not started".to_string()))
    }

    /// Python real: silent return before boot; route the broker choice to
    /// the runtime once live (over the wire, the broker lives backend-side
    /// and unknown-ticket errors are swallowed there).
    fn answer_approval(&mut self, ticket_id: &str, choice: &str) {
        if self.started {
            self.runtime.answer_approval(ticket_id, choice);
        }
    }

    fn config_view(&mut self) -> ConfigSnapshotView {
        RuntimeAdapter::config_view(&mut self.base)
    }

    fn config_toggle(&mut self, category: &str, name: &str, enable: bool) -> (bool, String) {
        RuntimeAdapter::config_toggle(&mut self.base, category, name, enable)
    }

    fn config_set(&mut self, path: &str, value: &str) -> (bool, String) {
        RuntimeAdapter::config_set(&mut self.base, path, value)
    }

    fn config_diff(&mut self) -> Vec<ConfigChange> {
        RuntimeAdapter::config_diff(&mut self.base)
    }

    fn config_save(&mut self, scope: &str) -> (bool, String) {
        RuntimeAdapter::config_save(&mut self.base, scope)
    }

    /// Resolve the kernel-parked NeedsYouItem by id (Python real
    /// `deferred_decision`).
    ///
    /// Real deferrals park their item in the shared queue at the point of
    /// deferral; the decision notification carries only the id. Nothing is
    /// re-parsed from the message string. An unknown/empty id degrades to
    /// the base message-only stub.
    fn deferred_decision(
        &mut self,
        message: &str,
        decision_id: &str,
    ) -> (String, String, Vec<String>, String, String) {
        if !decision_id.is_empty() {
            for item in self.base.needs_you.items() {
                if item.decision_id == decision_id {
                    return (
                        item.question,
                        item.reason,
                        item.choices,
                        item.highlight,
                        item.action,
                    );
                }
            }
        }
        (
            message.to_string(),
            String::new(),
            Vec::new(),
            String::new(),
            String::new(),
        )
    }

    /// Name the denied action being applied, when the item carries one
    /// (Python real `decision_narration`).
    fn decision_narration(&mut self, choice: &str, action: &str) -> String {
        if !action.is_empty() {
            return format!("Applying decision: {choice} \u{b7} {action}");
        }
        format!("Applying decision: {choice}")
    }
}

// ---------------------------------------------------------------------------
// kernel.config_ops.save_config — ported observable behavior (see module
// docs). Private: the adapter is its only consumer, exactly like Python's
// lazy `from ..kernel.config_ops import save_config`.
// ---------------------------------------------------------------------------

/// Python `config_ops.CONFIGURATOR_KEY` — top-level settings key the
/// session config changes persist under (amplifier-app-cli parity).
const CONFIGURATOR_KEY: &str = "configurator";

/// Python `config_ops.amplifier_home()`: explicit argument wins (tests),
/// then `AMPLIFIER_HOME`, then `~/.amplifier`.
fn amplifier_home(explicit: Option<&Path>) -> PathBuf {
    if let Some(home) = explicit {
        return home.to_path_buf();
    }
    let env_home = std::env::var("AMPLIFIER_HOME").unwrap_or_default();
    let env_home = env_home.trim();
    if !env_home.is_empty() {
        return expand_user(env_home);
    }
    home_dir().join(".amplifier")
}

fn home_dir() -> PathBuf {
    std::env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("."))
}

/// Python `Path(...).expanduser()` for the leading-`~` case.
fn expand_user(path: &str) -> PathBuf {
    if path == "~" {
        return home_dir();
    }
    if let Some(rest) = path.strip_prefix("~/") {
        return home_dir().join(rest);
    }
    PathBuf::from(path)
}

/// Python `kernel.config.deep_merge`: recursively merge `overlay` onto
/// `base`; overlay wins on conflicts.
fn deep_merge(base: &Map<String, Value>, overlay: &Map<String, Value>) -> Map<String, Value> {
    let mut result = base.clone();
    for (key, value) in overlay {
        let merged = match (result.get(key), value) {
            (Some(Value::Object(existing)), Value::Object(incoming)) => {
                Value::Object(deep_merge(existing, incoming))
            }
            _ => value.clone(),
        };
        result.insert(key.clone(), merged);
    }
    result
}

/// Python `bundle_admin.read_scope`: one scope's raw settings dict
/// (`{}` when missing/malformed).
fn read_scope(path: &Path) -> Map<String, Value> {
    let Ok(text) = std::fs::read_to_string(path) else {
        return Map::new();
    };
    match serde_yaml::from_str::<Value>(&text) {
        Ok(Value::Object(map)) => map,
        _ => Map::new(),
    }
}

/// Python `bundle_admin.write_scope`: persist a scope dict atomically
/// (tmp-file → replace), mkdir parents. An empty dict removes the file
/// rather than leaving a stray `{}`.
fn write_scope(path: &Path, data: &Map<String, Value>) -> io::Result<()> {
    if data.is_empty() {
        match std::fs::remove_file(path) {
            Ok(()) => {}
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(error) => return Err(error),
        }
        return Ok(());
    }
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let text = serde_yaml::to_string(&Value::Object(data.clone()))
        .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
    let tmp = path.with_extension("yaml.tmp");
    std::fs::write(&tmp, text)?;
    std::fs::rename(&tmp, path)?;
    Ok(())
}

/// Python `kernel.config_ops.save_config`: persist the state's session
/// changes to the scope settings file; `(ok, message)`, never panics into
/// the UI.
fn save_config(
    state: &SessionConfigState,
    scope: &str,
    project_dir: &Path,
    home: Option<&Path>,
) -> (bool, String) {
    if !matches!(scope, "global" | "project" | "local") {
        return (
            false,
            format!("unknown scope '{scope}' \u{b7} use global | project | local"),
        );
    }
    let changes = state.to_settings();
    let path = match scope {
        "global" => amplifier_home(home).join("settings.yaml"),
        "project" => project_dir.join(".amplifier").join("settings.yaml"),
        _ => project_dir.join(".amplifier").join("settings.local.yaml"),
    };
    let existing = read_scope(&path);
    let has_changes = changes.as_object().is_some_and(|map| !map.is_empty());
    let merged = if has_changes {
        let mut overlay = Map::new();
        overlay.insert(CONFIGURATOR_KEY.to_string(), changes);
        deep_merge(&existing, &overlay)
    } else {
        // Nothing changed this session: drop any stale configurator block
        // rather than leave a misleading one behind.
        let mut merged = existing;
        merged.remove(CONFIGURATOR_KEY);
        merged
    };
    if let Err(error) = write_scope(&path, &merged) {
        return (
            false,
            format!("could not write {scope} settings \u{b7} {error}"),
        );
    }
    let count = state.change_count();
    let detail = if count > 0 {
        format!("{count} change(s)")
    } else {
        "no session changes".to_string()
    };
    (
        true,
        format!(
            "\u{2713} config saved \u{b7} {scope} scope \u{b7} {detail} \u{b7} {}",
            path.display()
        ),
    )
}

// ---------------------------------------------------------------------------
// Tests — port of tests/test_runtime_adapter_base.py in full plus the
// portable (non-thread) cases of tests/test_runtime_adapter_real.py.
// The asyncio/Textual thread-marshalling harness does not port; see the
// per-test notes and the unit report for the skipped cases.
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use std::cell::RefCell;
    use std::rc::Rc;

    use super::*;
    use crate::model::queues::NeedsYouStatus;
    use crate::model::turn::{OutcomeKind, TurnOutcome, TurnTelemetry};

    // -- base adapter (tests/test_runtime_adapter_base.py) -------------------

    // Python: test_neutral_table_covers_every_public_async_method — the
    // introspection guard is the type system here: coercing to
    // `&mut dyn RuntimeAdapter` compiles only when the full contract
    // surface (exact names and parameter lists) exists.
    #[test]
    fn test_neutral_table_covers_every_public_async_method() {
        let mut base = RuntimeAdapterBase::new();
        let adapter: &mut dyn RuntimeAdapter = &mut base;
        assert_eq!(adapter.session_short(), "");
    }

    // Python: test_base_stub_neutral_returns — the full NEUTRAL_CASES table.
    #[test]
    fn test_base_stub_neutral_returns() {
        let mut adapter = RuntimeAdapterBase::new();
        // ("submit", ("hello", ()), None)
        adapter.submit("hello", &[]);
        // ("interrupt", (), False)
        assert!(!adapter.interrupt());
        // ("list_native_modes", (), "")
        assert_eq!(adapter.list_native_modes(), Value::String(String::new()));
        // ("set_native_mode", ("plan",), (False, "native modes need a real session"))
        assert_eq!(
            adapter.set_native_mode(Some("plan")),
            (false, "native modes need a real session".to_string())
        );
        // ("list_models", (), ModelListing(provider="", current=""))
        assert_eq!(
            adapter.list_models(),
            ModelListing {
                provider: String::new(),
                current: String::new(),
                available: Vec::new()
            }
        );
        // ("set_model", ("gpt",), (False, "switching models needs a real session"))
        assert_eq!(
            adapter.set_model("gpt"),
            (false, "switching models needs a real session".to_string())
        );
        // ("get_effort", (), None)
        assert_eq!(adapter.get_effort(), None);
        // ("set_effort", ("high",), (False, "reasoning effort needs a real session"))
        assert_eq!(
            adapter.set_effort("high"),
            (false, "reasoning effort needs a real session".to_string())
        );
        // ("compact", ("focus",), (False, "compaction needs a real session"))
        assert_eq!(
            adapter.compact("focus"),
            (false, "compaction needs a real session".to_string())
        );
        // ("clear_context", (), (False, 0))
        assert_eq!(adapter.clear_context(), (false, 0));
        // ("status", (), StatusInfo())
        assert_eq!(adapter.status(), StatusInfo::default());
        // ("list_tools", (), ()) / ("list_agents", (), ())
        assert!(adapter.list_tools().is_empty());
        assert!(adapter.list_agents().is_empty());
        // ("diff", (True,), None)
        assert_eq!(adapter.diff(true), None);
        // ("workspace_files", (), ()) / ("list_skills", (), ())
        assert!(adapter.workspace_files().is_empty());
        assert!(adapter.list_skills().is_empty());
        // ("load_skill", ("brainstorming",), (False, "skills need a real session"))
        assert_eq!(
            adapter.load_skill("brainstorming"),
            (false, "skills need a real session".to_string())
        );
        // ("mcp_tools", (), ())
        assert!(adapter.mcp_tools().is_empty());
        // ("load_deferred_bundle", ("team",), (False, "loading a bundle needs a real session"))
        assert_eq!(
            adapter.load_deferred_bundle("team"),
            (false, "loading a bundle needs a real session".to_string())
        );
        // ("deferred_bundles", (), ())
        assert!(adapter.deferred_bundles().is_empty());
        // ("rename_session", ("auth work",), (False, "renaming needs a real session"))
        assert_eq!(
            adapter.rename_session("auth work"),
            (false, "renaming needs a real session".to_string())
        );
        // ("session_summaries", (), ())
        assert!(adapter.session_summaries().is_empty());
        // ("branch_session", ("spike",), (False, "branching needs a real session"))
        assert_eq!(
            adapter.branch_session("spike"),
            (false, "branching needs a real session".to_string())
        );
        // ("fork_with_directive", ("go",), (False, "forking needs a real session"))
        assert_eq!(
            adapter.fork_with_directive("go"),
            (false, "forking needs a real session".to_string())
        );
        // ("directory_entries", ("allowed",), ())
        assert!(adapter.directory_entries(DirectoryKind::Allowed).is_empty());
        // ("update_directory", ("allowed", "add", "/tmp/p"),
        //  (False, "directory management needs a real session"))
        assert_eq!(
            adapter.update_directory(DirectoryKind::Allowed, "add", "/tmp/p"),
            (
                false,
                "directory management needs a real session".to_string()
            )
        );
    }

    // Python: test_base_sync_hooks_neutral
    #[test]
    fn test_base_sync_hooks_neutral() {
        let mut adapter = RuntimeAdapterBase::new();
        assert_eq!(adapter.turn_spec("prompt"), None);
        assert_eq!(adapter.lane_seed("agent"), None);
        assert!(adapter
            .lane_blocks("lane", "s1", &mut BlockIdAllocator::new())
            .is_none());
        assert!(adapter.evidence_links("answer").is_empty());
        assert_eq!(
            adapter.deferred_decision("msg", ""),
            (
                "msg".to_string(),
                String::new(),
                Vec::new(),
                String::new(),
                String::new()
            )
        );
        assert_eq!(
            adapter.decision_narration("ship it", ""),
            "Applying decision: ship it"
        );
        adapter.answer_approval("t1", "allow"); // no-op
    }

    // Python: test_base_defer_approval_parks_into_needs_you_without_resolving
    #[test]
    fn test_base_defer_approval_parks_into_needs_you_without_resolving() {
        let mut adapter = RuntimeAdapterBase::new();
        assert_eq!(adapter.needs_you().pending_count(), 0);
        let options = vec![
            "Allow once".to_string(),
            "Allow always".to_string(),
            "Deny".to_string(),
        ];
        adapter.defer_approval("t1", "Run `pytest -q`?", &options);
        assert_eq!(adapter.needs_you().pending_count(), 1);
        let item = adapter.needs_you().pending()[0].clone();
        assert_eq!(item.question, "Run `pytest -q`?");
        assert_eq!(item.choices, options);
        assert_eq!(item.status, NeedsYouStatus::Pending); // parked, not answered
        // Empty/whitespace prompts never park a ghost decision.
        adapter.defer_approval("t2", "   ", &options);
        assert_eq!(adapter.needs_you().pending_count(), 1);
    }

    // Python: test_submit_queued_delegates_to_submit (class _RecordingSubmit)
    struct RecordingSubmit {
        base: RuntimeAdapterBase,
        submitted: Vec<(String, usize)>,
    }

    impl RecordingSubmit {
        fn new() -> Self {
            Self {
                base: RuntimeAdapterBase::new(),
                submitted: Vec::new(),
            }
        }
    }

    impl RuntimeAdapter for RecordingSubmit {
        fn steering(&self) -> &SteeringQueue {
            &self.base.steering
        }
        fn lane_steering(&self) -> &LaneSteeringQueue {
            &self.base.lane_steering
        }
        fn needs_you(&self) -> &NeedsYouQueue {
            &self.base.needs_you
        }
        fn denial_log(&self) -> &Mutex<DenialLog> {
            &self.base.denial_log
        }
        fn terminal(&self) -> &TerminalSurface {
            &self.base.terminal
        }
        fn submit(&mut self, text: &str, attachments: &[ImageAttachment]) {
            self.submitted.push((text.to_string(), attachments.len()));
        }
        fn config_view(&mut self) -> ConfigSnapshotView {
            RuntimeAdapter::config_view(&mut self.base)
        }
        fn config_toggle(&mut self, category: &str, name: &str, enable: bool) -> (bool, String) {
            RuntimeAdapter::config_toggle(&mut self.base, category, name, enable)
        }
        fn config_set(&mut self, path: &str, value: &str) -> (bool, String) {
            RuntimeAdapter::config_set(&mut self.base, path, value)
        }
        fn config_diff(&mut self) -> Vec<ConfigChange> {
            RuntimeAdapter::config_diff(&mut self.base)
        }
        fn config_save(&mut self, scope: &str) -> (bool, String) {
            RuntimeAdapter::config_save(&mut self.base, scope)
        }
    }

    #[test]
    fn test_submit_queued_delegates_to_submit() {
        let mut adapter = RecordingSubmit::new();
        adapter.submit_queued("x");
        assert_eq!(adapter.submitted, vec![("x".to_string(), 0)]);
    }

    // Python: test_base_config_surface_round_trips — the AMPLIFIER_HOME
    // monkeypatch becomes the injected `config_home` (the Python
    // `save_config(..., home=...)` seam), same observable behavior.
    #[test]
    fn test_base_config_surface_round_trips() {
        let dir = tempfile::tempdir().unwrap();
        let mut adapter = RuntimeAdapterBase::new();
        adapter.config_project_dir = dir.path().to_path_buf();
        adapter.config_home = Some(dir.path().to_path_buf());

        let view = adapter.config_view();
        assert!(view.items.iter().any(|item| item.category == "tools"));

        let (ok, message) = adapter.config_toggle("tools", "bash", false);
        assert!(ok && message.contains("Disabled bash"), "{message}");
        let (ok, message) = adapter.config_set("session.effort", "high");
        assert!(ok && message.contains("session.effort"), "{message}");

        let changes = adapter.config_diff();
        let seen: Vec<(String, String)> = changes
            .iter()
            .map(|change| (change.category.clone(), change.name.clone()))
            .collect();
        assert!(seen.contains(&("tools".to_string(), "bash".to_string())));
        assert!(seen.contains(&("set".to_string(), "session.effort".to_string())));

        let (ok, message) = adapter.config_save("global");
        assert!(ok && message.contains("global scope"), "{message}");
        let written = std::fs::read_to_string(dir.path().join("settings.yaml")).unwrap();
        assert!(written.contains("configurator") && written.contains("bash"));
    }

    // Python: test_base_config_toggle_hooks_read_only
    #[test]
    fn test_base_config_toggle_hooks_read_only() {
        let mut adapter = RuntimeAdapterBase::new();
        let (ok, message) = adapter.config_toggle("hooks", "hooks-mode", false);
        assert!(!ok && message.contains("read-only"), "{message}");
    }

    // Python: test_base_start_calls_ready_and_fork_trims — the observing
    // `_FakeLedger` becomes a real `OutcomeLedger` (the trait's fork takes
    // the concrete type); trimming is observed through `turn_count`.
    #[test]
    fn test_base_start_calls_ready_and_fork_trims() {
        let mut adapter = RuntimeAdapterBase::new();
        let mut ready_calls = 0;
        adapter.start(&mut || ready_calls += 1);
        assert_eq!(ready_calls, 1);

        let mut ledger = OutcomeLedger::new();
        ledger.record_turn(
            TurnTelemetry::new(1.0),
            TurnOutcome::new(OutcomeKind::Answer),
            1,
            0,
            "one",
            None,
        );
        ledger.record_turn(
            TurnTelemetry::new(1.0),
            TurnOutcome::new(OutcomeKind::Answer),
            2,
            1,
            "two",
            None,
        );
        // In-memory: confirmation is immediate.
        adapter.fork("t1", &mut ledger).expect("known checkpoint");
        assert_eq!(ledger.turn_count(), 1);
        // The ledger's unknown-checkpoint KeyError propagates (as RewindError).
        assert_eq!(
            adapter.fork("t9", &mut ledger),
            Err(RewindError("unknown checkpoint: t9".to_string()))
        );
    }

    // -- real adapter (portable cases of tests/test_runtime_adapter_real.py) --

    struct RecordingRuntime {
        calls: Rc<RefCell<Vec<String>>>,
    }

    impl Runtime for RecordingRuntime {
        fn submit(&mut self, prompt: String) {
            self.calls.borrow_mut().push(format!("submit:{prompt}"));
        }
        fn answer_approval(&mut self, ticket_id: &str, choice: &str) {
            self.calls
                .borrow_mut()
                .push(format!("approve:{ticket_id}:{choice}"));
        }
        fn interrupt(&mut self) {
            self.calls.borrow_mut().push("interrupt".to_string());
        }
    }

    fn client() -> (ClientRuntimeAdapter, Rc<RefCell<Vec<String>>>) {
        let calls = Rc::new(RefCell::new(Vec::new()));
        let adapter = ClientRuntimeAdapter::new(Box::new(RecordingRuntime {
            calls: Rc::clone(&calls),
        }));
        (adapter, calls)
    }

    // Python: test_proxies_neutral_before_boot — the PREBOOT_NEUTRALS table
    // (the `_runtime is None` guard values, exact strings).
    #[test]
    fn test_proxies_neutral_before_boot() {
        let (mut adapter, calls) = client();
        // ("submit", ("x", ()), None)
        adapter.submit("x", &[]);
        // ("interrupt", (), False)
        assert!(!adapter.interrupt());
        // ("list_native_modes", (), "")
        assert_eq!(adapter.list_native_modes(), Value::String(String::new()));
        // ("set_native_mode", ("m",), (False, "session still starting"))
        assert_eq!(
            adapter.set_native_mode(Some("m")),
            (false, "session still starting".to_string())
        );
        // ("list_models", (), ModelListing(provider="", current=""))
        assert_eq!(adapter.list_models(), ModelListing::default());
        // ("set_model", ("m",), (False, "session still starting"))
        assert_eq!(
            adapter.set_model("m"),
            (false, "session still starting".to_string())
        );
        // ("get_effort", (), None)
        assert_eq!(adapter.get_effort(), None);
        // ("set_effort", ("high",), (False, "session still starting"))
        assert_eq!(
            adapter.set_effort("high"),
            (false, "session still starting".to_string())
        );
        // ("compact", ("f",), (False, "session still starting"))
        assert_eq!(
            adapter.compact("f"),
            (false, "session still starting".to_string())
        );
        // ("clear_context", (), (False, 0))
        assert_eq!(adapter.clear_context(), (false, 0));
        // ("status", (), StatusInfo())
        assert_eq!(adapter.status(), StatusInfo::default());
        // ("list_tools", (), ()) / ("list_agents", (), ())
        assert!(adapter.list_tools().is_empty());
        assert!(adapter.list_agents().is_empty());
        // ("diff", (True,), None)
        assert_eq!(adapter.diff(true), None);
        // ("workspace_files", (), ()) / ("list_skills", (), ())
        assert!(adapter.workspace_files().is_empty());
        assert!(adapter.list_skills().is_empty());
        // ("load_skill", ("s",), (False, "session still starting"))
        assert_eq!(
            adapter.load_skill("s"),
            (false, "session still starting".to_string())
        );
        // ("mcp_tools", (), ())
        assert!(adapter.mcp_tools().is_empty());
        // ("directory_entries", ("allowed",), ())
        assert!(adapter.directory_entries(DirectoryKind::Allowed).is_empty());
        // ("update_directory", ("allowed", "add", "/p"),
        //  (False, "session still starting"))
        assert_eq!(
            adapter.update_directory(DirectoryKind::Allowed, "add", "/p"),
            (false, "session still starting".to_string())
        );
        // Nothing reached the wrapped runtime before boot.
        assert!(calls.borrow().is_empty());
    }

    // Python: test_neutral_guards_before_boot
    #[test]
    fn test_neutral_guards_before_boot() {
        let (mut adapter, calls) = client();
        let mut ledger = OutcomeLedger::new();
        assert_eq!(
            adapter.fork("cp-1", &mut ledger),
            Err(RewindError("session not started".to_string()))
        );
        assert!(adapter.evidence_links("answer").is_empty());
        adapter.answer_approval("t1", "allow"); // silent return
        assert_eq!(adapter.lane_seed("scout"), None);
        assert!(calls.borrow().is_empty());
    }

    // The portable slice of Python test_proxies_run_on_runtime_thread (T9):
    // the three wire ops forward to the wrapped runtime once started (the
    // seventeen remaining proxies are backend-side until the protocol
    // carries session ops).
    #[test]
    fn test_wire_ops_forward_to_the_wrapped_runtime() {
        let (mut adapter, calls) = client();
        let mut ready_calls = 0;
        adapter.start(&mut || ready_calls += 1);
        assert_eq!(ready_calls, 1);

        adapter.submit("hello", &[]);
        adapter.answer_approval("t1", "Allow once");
        assert!(adapter.interrupt()); // dispatched over the wire
        assert_eq!(
            *calls.borrow(),
            vec![
                "submit:hello".to_string(),
                "approve:t1:Allow once".to_string(),
                "interrupt".to_string(),
            ]
        );
    }

    // Pins RealRuntimeAdapter.deferred_decision (runtime_adapter.py):
    // resolve the kernel-parked NeedsYouItem by id; unknown/empty ids
    // degrade to the base message-only stub.
    #[test]
    fn test_deferred_decision_resolves_kernel_parked_item() {
        let (mut adapter, _calls) = client();
        let item = adapter
            .needs_you()
            .defer(
                "Push to main?",
                "governance",
                DeferOptions {
                    choices: vec!["yes".to_string(), "no".to_string()],
                    highlight: "main".to_string(),
                    action: "git push".to_string(),
                    ..DeferOptions::default()
                },
            )
            .expect("defer parks the item");
        assert_eq!(
            adapter.deferred_decision("msg", &item.decision_id),
            (
                "Push to main?".to_string(),
                "governance".to_string(),
                vec!["yes".to_string(), "no".to_string()],
                "main".to_string(),
                "git push".to_string()
            )
        );
        let stub = (
            "msg".to_string(),
            String::new(),
            Vec::new(),
            String::new(),
            String::new(),
        );
        assert_eq!(adapter.deferred_decision("msg", "decision-999"), stub);
        assert_eq!(adapter.deferred_decision("msg", ""), stub);
    }

    // Pins RealRuntimeAdapter.decision_narration (runtime_adapter.py):
    // name the denied action being applied, when the item carries one.
    #[test]
    fn test_decision_narration_names_the_denied_action() {
        let (mut adapter, _calls) = client();
        assert_eq!(
            adapter.decision_narration("ship it", "git push"),
            "Applying decision: ship it \u{b7} git push"
        );
        assert_eq!(
            adapter.decision_narration("ship it", ""),
            "Applying decision: ship it"
        );
    }

    // Pins RealRuntimeAdapter.record_prompt/prompt_history
    // (runtime_adapter.py): the real adapter persists per project; the
    // base stays a no-op.
    #[test]
    fn test_record_prompt_persists_for_the_project() {
        let dir = tempfile::tempdir().unwrap();
        let calls = Rc::new(RefCell::new(Vec::new()));
        let mut adapter = ClientRuntimeAdapter::new(Box::new(RecordingRuntime {
            calls: Rc::clone(&calls),
        }))
        .with_prompt_store(PromptHistoryStore::at_path(dir.path().join("history")));

        adapter.record_prompt("first prompt");
        adapter.record_prompt("second prompt");
        assert_eq!(
            adapter.prompt_history(),
            vec!["first prompt".to_string(), "second prompt".to_string()]
        );

        let mut base = RuntimeAdapterBase::new();
        base.record_prompt("never stored");
        assert!(base.prompt_history().is_empty());
    }

    // ADR-0007 invariant 4: the wrapper shares the SAME live config state
    // (real sessions reseed it from the mount plan at start()).
    #[test]
    fn test_client_adapter_config_state_is_shared_with_base() {
        let (mut adapter, _calls) = client();
        let (ok, _message) = adapter.config_toggle("tools", "bash", false);
        assert!(ok);
        let item = adapter
            .base()
            .config_state
            .find("tools", "bash")
            .expect("demo item present")
            .clone();
        assert!(!item.enabled);
    }
}
