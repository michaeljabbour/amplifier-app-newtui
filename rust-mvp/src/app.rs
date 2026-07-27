//! The composition root: the assembled ratatui App (ADR-0007's `NewTuiApp`).
//!
//! Rust rendering of `src/amplifier_app_newtui/ui/app.py`: the app consumes
//! the runtime's wire events through [`TranscriptReducer`], owns only
//! interaction state (running, mode, palette filter, open strips, queued
//! message, approval head), and the ported units own their own state.
//!
//! Layout (DESIGN-SPEC §2, top → bottom): TitleBar / TranscriptView /
//! LiveTail / NoticeSlot / overlay strips (palette · lanes · plan · rewind ·
//! queued · file-mentions) / composer-or-approval-bar / FooterBar — painted
//! by `ui::draw` as a pure function of this state.
//!
//! # Assembly notes (deviations from the Python shape, all deliberate)
//!
//! - The reducer OWNS its host ([`Shell`]) by value, so the mutable UI state
//!   lives in one `Rc<RefCell<UiState>>` shared between the reducer host and
//!   the [`App`] (single-threaded event loop; never crosses threads).
//! - The reducer owns the authoritative `OutcomeLedger` and block-id
//!   allocator. The app keeps a `Mutex<OutcomeLedger>` MIRROR for the
//!   `CommandHost` surface (synced by clone around command dispatch) and its
//!   own allocator in a disjoint id range (`b1000000+`) — Python shared one
//!   allocator object; the ported reducer does not expose its own.
//! - The shared interaction queues (steering / needs-you / lane-steering /
//!   denial log) are owned by the App, NOT read out of the adapter (the
//!   adapter sits behind a `RefCell` for its `&mut` ops, and `CommandHost`
//!   returns plain references). The adapter's internal queue copies are
//!   deliberately unused.

use std::cell::RefCell;
use std::collections::HashMap;
use std::rc::Rc;
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Instant, SystemTime, UNIX_EPOCH};

use ratatui::style::Color;
use rust_decimal::Decimal;

use crate::commands::builtin::build_registry;
use crate::commands::context::ContextUsage;
use crate::commands::improve::ApprovalJournal;
use crate::commands::permissions::PermissionSurface;
use crate::commands::registry::{CommandRegistry, CommandSpec};
use crate::kernel::events as ev;
use crate::model::blocks::{
    Answer, BlockIdAllocator, EvidenceBlock, Segment, SessionBanner, SteerEcho, StyleToken,
    TodoItem, ToolLine, TranscriptBlock, UserLine,
};
use crate::model::lanes::LaneRegistry;
use crate::model::modes::{cycle_mode, get_mode, ModeProfile};
use crate::model::native_modes::posture_conflict_notice;
use crate::model::queues::{
    DeferOptions, LaneSteeringQueue, MessageKind, NeedsYouQueue, SteeringQueue,
};
use crate::model::trust::DenialLog;
use crate::model::turn::{Checkpoint, OutcomeLedger};
use crate::protocol::WireEvent;
use crate::runtime::{Runtime, ScriptedDemoRuntime};
use crate::ui::app_support::{
    self, resolve_esc, EscAction, EscFlags, EscSequence, PlanSurface, APPROVAL_NOTICE,
    APPROVAL_NOTICE_DURATION, QUEUED_NOTICE, STEER_DISCARDED_NOTICE, STEER_NOTICE,
    STEER_NOTICE_LEGACY,
};
use crate::ui::approval_bar::{ApprovalBar, ApprovalMsg, KeyOutcome, DEFAULT_OPTIONS};
use crate::ui::chrome::{TitleBar, TitleChanged};
use crate::ui::command_context::{AppCommandContext, CommandHost};
use crate::ui::composer::{Composer, ComposerMessage};
use crate::ui::config_admin::{self, ConfigAdminHost};
use crate::ui::demo_wiring::{
    DemoWiring, DEMO_BANNER, DEMO_BUNDLE, DEMO_MODEL, DEMO_SESSION_SHORT,
};
use crate::ui::directory_admin::{self, DirectoryAdminHost, DirectoryEntry, DirectoryKind};
use crate::ui::file_mentions::{
    close_file_mentions, handle_file_mention_intent, FileMentionStrip, MentionHost,
};
use crate::ui::footer::FooterState;
use crate::ui::keymap::Context;
use crate::ui::lanes_panel::{LanesMsg, LanesPanel};
use crate::ui::live_tail::LiveTail;
use crate::ui::notices::NoticeSlot;
use crate::ui::notifications::Reason;
use crate::ui::palette::{PaletteMessage, PaletteStrip};
use crate::ui::plan_panel::PlanPanel;
use crate::ui::queued_strip::QueuedStrip;
use crate::ui::reducer::{ReducerHost, ReducerOptions, TranscriptReducer};
use crate::ui::rewind_strip::{RewindMsg, RewindStrip};
use crate::ui::runtime_adapter::{RuntimeAdapter, RuntimeAdapterBase};
use crate::ui::session_ops_controller::{SessionOpsAdapter, SessionOpsController, SessionOpsHost};
use crate::ui::session_ops_view::{
    sessions_spans, CompactionConfig, ModelListing, SkillInfo, StatusInfo,
};
use crate::ui::splash::BootSplash;
use crate::ui::themes::{theme, Theme, DEFAULT_THEME, THEME_TOKENS};
use crate::ui::transcript::{TranscriptMsg, TranscriptView};
use crate::ui::FrameLayout;

/// App-side block ids mint from a disjoint range so they can never collide
/// with the reducer-owned allocator (`b1`, `b2`, …). See module docs.
const APP_ID_RANGE_START: u64 = 1_000_000;

/// Copy-on-select settle delay in seconds (Python `set_timer(0.4,
/// self._copy_settled_selection)` in `ui/app.py`).
const SELECTION_SETTLE_SECONDS: f64 = 0.4;

/// Monotonic clock in fractional seconds, anchored at first use.
pub fn monotonic() -> f64 {
    static START: OnceLock<Instant> = OnceLock::new();
    START.get_or_init(Instant::now).elapsed().as_secs_f64()
}

/// Wall clock (the event `ts` domain).
pub fn wall_now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// All fourteen §1 tokens of `name`'s theme resolved to ratatui colors —
/// the ONE token → color table `segments::to_ratatui_line` consumes.
pub fn token_colors(theme_name: &str) -> HashMap<StyleToken, Color> {
    let Some(theme): Option<Theme> = theme(theme_name) else {
        return HashMap::new();
    };
    build_color_table(&theme)
}

fn build_color_table(theme: &Theme) -> HashMap<StyleToken, Color> {
    let tokens = [
        StyleToken::BgPage,
        StyleToken::BgTerm,
        StyleToken::BgChrome,
        StyleToken::BgTab,
        StyleToken::Fg,
        StyleToken::Bright,
        StyleToken::Dim,
        StyleToken::Dimmer,
        StyleToken::Green,
        StyleToken::Orange,
        StyleToken::Red,
        StyleToken::Blue,
        StyleToken::Teal,
        StyleToken::Rule,
    ];
    tokens
        .into_iter()
        .filter_map(|token| theme.color(token.as_str()).map(|color| (token, color)))
        .collect()
}

// ---------------------------------------------------------------------------
// UiState — the mutable UI surface, shared by the reducer host and the App
// ---------------------------------------------------------------------------

pub struct UiState {
    pub transcript: TranscriptView,
    pub live_tail: LiveTail,
    pub notices: NoticeSlot,
    pub lanes_panel: LanesPanel,
    pub plan_panel: PlanPanel,
    pub plan_items: Vec<TodoItem>,
    pub queued_strip: QueuedStrip,
    pub file_mentions: FileMentionStrip,
    pub rewind: RewindStrip,
    pub palette: PaletteStrip<CommandSpec>,
    pub composer: Composer,
    pub title: TitleBar,
    pub approval: Option<ApprovalBar>,
    pub mode: &'static ModeProfile,
    pub permissions: PermissionSurface,
    pub native_modes: Vec<String>,
    pub theme_name: String,
    pub colors: HashMap<StyleToken, Color>,
    pub splash: Option<BootSplash>,
    pub turn_active: bool,
    pub should_quit: bool,
    pub esc: EscSequence,
    pub term_width: u16,
    pub term_height: u16,
    /// Session identity (protocol `session.started` / adapter banner).
    pub bundle: String,
    pub model_name: String,
    pub session_short: String,
    // -- cross-callback flags the App settles after each event ------------
    /// `lanes_changed` fired — the App re-feeds the lanes panel.
    pub lanes_dirty: bool,
    /// Deferred decisions from `decision_deferred` (message, decision_id).
    pub pending_deferrals: Vec<(String, String)>,
    /// End-of-turn queue duties pending (drained once events settle).
    pub turn_queues_pending: bool,
    /// Live-tail trailing repaint deadline (monotonic; `LiveTail::feed`).
    pub live_tail_deadline: Option<f64>,
    /// Transcript resize-reflow debounce deadline (monotonic).
    pub reflow_deadline: Option<f64>,
    /// Wheel-scroll offset honored while the tail anchor is released
    /// (`transcript.follow()` false); clamped to content at draw time.
    pub transcript_scroll: usize,
    /// Turn-start timestamp (monotonic) — the attention bell's elapsed basis.
    pub turn_started_at: Option<f64>,
    /// The attention bell should ring (`\x07`); the main loop drains it.
    /// Python rings only via the driver-safe `App.bell`; neither client
    /// models terminal focus, so this follows the same observable rule
    /// (turn ≥ ATTENTION_MIN_TURN_SECONDS, deferrals always).
    pub bell_pending: bool,
    /// The native terminal title to emit (OSC), already deduped by
    /// `TitleBar::repaint`; the main loop drains it.
    pub pending_title: Option<String>,
    /// A `boot.progress` record landed: the protocol phases own the splash
    /// status from here on (raw stderr chatter no longer overwrites them).
    pub boot_progress_seen: bool,
    /// Transcript drag-selection as (anchor, head) content positions —
    /// each a `(line, column)` pair in content-line index / terminal-cell
    /// column space, inclusive both ends (mouse capture swallows the
    /// terminal's native selection, so the app models its own, character-
    /// ranged like Python's Textual screen selections: partial first line
    /// from the anchor column, full middle lines, partial last line to the
    /// head column).
    pub selection: Option<((usize, usize), (usize, usize))>,
    /// Mouse-down content position while the left button is held over the
    /// transcript (the drag anchor; cleared on mouse-up).
    pub selection_drag_anchor: Option<(usize, usize)>,
    /// Copy-on-select settle deadline (monotonic) — Python's 0.4s
    /// `_selection_timer`, restarted on every selection change.
    pub selection_settle_deadline: Option<f64>,
    /// Suppress duplicate auto-copies of the same settled selection
    /// (Python `_last_selection_copied`).
    pub last_selection_copied: String,
    /// The evidence block currently owning the keyboard (spec §10 — the
    /// Rust rendering of Python `widget.focus()` on the mounted evidence
    /// block: while set, ←/→/enter/esc route to that widget).
    pub focused_evidence: Option<String>,
    /// Cross-thread mirror of the live mode id — the demo script's
    /// step-boundary trust gate reads it from its worker thread (Python
    /// `mode_source=self._current_mode` reads `app.mode_id` same-loop).
    pub mode_shared: Arc<Mutex<String>>,
}

impl UiState {
    fn new(kitty_protocol: bool, initial_mode: Option<&str>, specs: Vec<CommandSpec>) -> Self {
        let mode = get_mode(initial_mode);
        let mut composer = Composer::new(kitty_protocol);
        composer.set_mode(mode);
        let theme_name = DEFAULT_THEME.to_string();
        let colors = token_colors(&theme_name);
        UiState {
            transcript: TranscriptView::new(),
            live_tail: LiveTail::new(),
            notices: NoticeSlot::new(),
            lanes_panel: LanesPanel::new(),
            plan_panel: PlanPanel::new(),
            plan_items: Vec::new(),
            queued_strip: QueuedStrip::new(),
            file_mentions: FileMentionStrip::new(),
            rewind: RewindStrip::new(),
            palette: PaletteStrip::new(specs),
            composer,
            title: TitleBar::new(),
            approval: None,
            mode,
            permissions: PermissionSurface::default(),
            native_modes: Vec::new(),
            theme_name,
            colors,
            splash: Some(BootSplash::new()),
            turn_active: false,
            should_quit: false,
            esc: EscSequence::new(),
            term_width: 80,
            term_height: 24,
            bundle: String::new(),
            model_name: String::new(),
            session_short: String::new(),
            lanes_dirty: false,
            pending_deferrals: Vec::new(),
            turn_queues_pending: false,
            live_tail_deadline: None,
            reflow_deadline: None,
            transcript_scroll: 0,
            turn_started_at: None,
            bell_pending: false,
            pending_title: None,
            boot_progress_seen: false,
            selection: None,
            selection_drag_anchor: None,
            selection_settle_deadline: None,
            last_selection_copied: String::new(),
            focused_evidence: None,
            mode_shared: Arc::new(Mutex::new(mode.id.as_str().to_string())),
        }
    }

    /// Park a [`TitleChanged`] for the main loop's OSC-title writer (the
    /// Rust rendering of Python forwarding `TitleChanged` to
    /// `write_terminal_title`; dedupe already happened in `TitleBar`).
    pub fn note_title(&mut self, changed: Option<TitleChanged>) {
        if let Some(changed) = changed {
            self.pending_title = Some(changed.terminal_title);
        }
    }

    /// Python `NewTuiApp.show_notice`: the approval bar owns its
    /// explanatory notice while open.
    pub fn show_notice(&mut self, text: &str, duration: Option<f64>) {
        if self.approval.is_some() && !text.contains("approval required") {
            return;
        }
        self.notices.show_notice(text, duration);
    }

    /// Python `set_mode_by_id` (minus the async native-mode bridge, which
    /// has no wire op — see caveats).
    pub fn set_mode_by_id(&mut self, mode_id: &str, notify: bool) {
        self.mode = get_mode(Some(mode_id));
        self.permissions.set_mode(self.mode.id.as_str());
        self.composer.set_mode(self.mode);
        *self.mode_shared.lock().unwrap() = self.mode.id.as_str().to_string();
        if notify {
            let notice = self.mode.notice();
            self.show_notice(&notice, None);
        }
        let conflict = posture_conflict_notice(self.mode.id.as_str(), &self.native_modes);
        if !conflict.is_empty() {
            self.show_notice(&conflict, None);
        }
    }

    /// Python `set_theme_by_name` (`/theme`, DESIGN-SPEC §1).
    pub fn set_theme_by_name(&mut self, name: &str) {
        let names: Vec<&str> = THEME_TOKENS.iter().map(|(n, _)| *n).collect();
        let mut name = name.to_string();
        if name.is_empty() {
            let index = names
                .iter()
                .position(|n| *n == self.theme_name)
                .map(|i| i as i64)
                .unwrap_or(-1);
            name = names[((index + 1) as usize) % names.len()].to_string();
        }
        if !names.contains(&name.as_str()) {
            let list = names.join(", ");
            self.show_notice(&format!("unknown theme · {name} · themes: {list}"), None);
            return;
        }
        self.theme_name = name.clone();
        self.colors = token_colors(&name);
        self.show_notice(&format!("theme {name}"), None);
    }

    /// Python `footer_context`.
    pub fn footer_context(&self) -> Context {
        if self.approval.is_some() {
            return Context::Approval;
        }
        if self.transcript.focused_lane().is_some() {
            return Context::LaneFocus;
        }
        if self.palette.is_open() {
            return Context::Palette;
        }
        if self.turn_active {
            return Context::Running;
        }
        Context::Idle
    }

    /// Responsive plan ladder (design D2): panel wide, footer count narrow.
    fn sync_plan_surfaces(&mut self) {
        match app_support::plan_surface(&self.plan_items, self.term_width as usize) {
            PlanSurface::Panel { .. } => {
                self.plan_panel.update_plan(self.plan_items.clone());
                self.plan_panel.show_panel();
            }
            PlanSurface::Hidden => {
                self.plan_panel.update_plan(self.plan_items.clone());
                self.plan_panel.hide_panel();
            }
        }
    }
}

impl MentionHost for UiState {
    fn file_mentions(&mut self) -> &mut FileMentionStrip {
        &mut self.file_mentions
    }

    fn clear_palette_filter(&mut self) {
        self.palette.apply_filter(None);
    }

    fn set_composer_mention_open(&mut self, open: bool) {
        self.composer.mention_open = open;
    }

    fn apply_file_mention(&mut self, path: &str) {
        self.composer.apply_file_mention(path);
    }

    fn focus_composer_input(&mut self) {
        // The composer always holds keyboard focus in this client.
    }
}

/// The [`ReducerHost`] the reducer owns — a shared handle onto [`UiState`].
pub struct Shell(pub Rc<RefCell<UiState>>);

impl ReducerHost for Shell {
    fn mode_id(&self) -> String {
        self.0.borrow().mode.id.as_str().to_string()
    }

    fn append_block(&mut self, block: TranscriptBlock) {
        let mut ui = self.0.borrow_mut();
        let _ = ui.transcript.append(block, monotonic());
    }

    fn replace_block(&mut self, block: TranscriptBlock) {
        let mut ui = self.0.borrow_mut();
        // Python: `except KeyError: append` — an unknown id appends.
        if ui.transcript.replace(block.clone(), monotonic()).is_err() {
            let _ = ui.transcript.append(block, monotonic());
        }
    }

    fn remove_block(&mut self, block_id: &str) {
        let _ = self.0.borrow_mut().transcript.remove_block(block_id);
    }

    fn show_notice(&mut self, text: &str) {
        self.0.borrow_mut().show_notice(text, None);
    }

    fn set_mode_by_id(&mut self, mode_id: &str, notify: bool) {
        self.0.borrow_mut().set_mode_by_id(mode_id, notify);
    }

    fn turn_started(&mut self) {
        let mut ui = self.0.borrow_mut();
        ui.turn_active = true;
        ui.composer.running = true;
        ui.turn_started_at = Some(monotonic()); // attention-bell elapsed basis
        let changed = ui.title.set_running(true);
        ui.note_title(changed);
    }

    fn turn_finished(&mut self) {
        let mut ui = self.0.borrow_mut();
        ui.turn_active = false;
        ui.composer.running = false;
        let changed = ui.title.set_running(false);
        ui.note_title(changed);
        ui.turn_queues_pending = true; // drained once end-of-turn events settle (§5)
        // Attention signal (Python `_notify_attention("turn_finished")`):
        // ring after long turns only — policy + rationale in
        // app_support::attention_bell_needed. Caveat: Python's rung 1 is the
        // driver-safe `App.bell`; neither client knows terminal focus, so
        // the rule is purely elapsed-time-based here too.
        let elapsed = ui.turn_started_at.take().map_or(0.0, |at| monotonic() - at);
        if app_support::attention_bell_needed(Reason::TurnFinished, elapsed, None) {
            ui.bell_pending = true;
        }
    }

    fn lanes_changed(&mut self) {
        self.0.borrow_mut().lanes_dirty = true;
    }

    fn plan_changed(&mut self, items: &[TodoItem]) {
        let mut ui = self.0.borrow_mut();
        ui.plan_items = items.to_vec();
        ui.sync_plan_surfaces();
    }

    fn approval_opened(&mut self, _prompt: &str, _options: &[String]) {
        // Presentation runs off the ticket-bearing wire record
        // (`App::handle_wire`), mirroring Python's `present_approval`.
    }

    fn decision_deferred(&mut self, message: &str, decision_id: &str) {
        self.0
            .borrow_mut()
            .pending_deferrals
            .push((message.to_string(), decision_id.to_string()));
    }

    fn stream_opened(&mut self, block_type: &str) {
        let mut ui = self.0.borrow_mut();
        let now = monotonic();
        ui.transcript.set_streaming(true, now);
        ui.live_tail.open_stream(block_type, now);
    }

    fn stream_delta(&mut self, text: &str) {
        let mut ui = self.0.borrow_mut();
        let deadline = ui.live_tail.feed(text, monotonic());
        if let Some(delay) = deadline {
            ui.live_tail_deadline = Some(monotonic() + delay);
        }
    }

    fn stream_closed(&mut self) {
        let mut ui = self.0.borrow_mut();
        // Durable text arrives on Channel B (`prompt_complete.response` /
        // content_block_end); the tail's consolidation artifact is
        // discarded (never reconstruct one channel from the other).
        let _ = ui.live_tail.consolidate("live-tail-discard");
        ui.live_tail_deadline = None;
        ui.transcript.set_streaming(false, monotonic());
    }

    fn lane_tail_updated(&mut self, text: &str) {
        self.0.borrow_mut().lanes_panel.show_lane_tail(text);
    }

    fn lane_tail_cleared(&mut self) {
        self.0.borrow_mut().lanes_panel.clear_lane_tail();
    }
}

// ---------------------------------------------------------------------------
// Demo adapter — RuntimeAdapterBase + DemoRuntime + DemoWiring data hooks
// ---------------------------------------------------------------------------

/// `--demo` adapter: the base contract over the scripted
/// [`ScriptedDemoRuntime`] (the `kernel/demo.py` engine port), with
/// [`DemoWiring`]'s pure data hooks (turn specs, deferred decisions,
/// decision narrations, scripted lane transcripts, evidence, the $0.40
/// session-cost baseline — $0.57 once the seed rule is cut).
pub struct DemoAdapter {
    pub base: RuntimeAdapterBase,
    runtime: Rc<RefCell<ScriptedDemoRuntime>>,
    wiring: Rc<RefCell<DemoWiring>>,
}

impl DemoAdapter {
    pub fn new(
        runtime: Rc<RefCell<ScriptedDemoRuntime>>,
        wiring: Rc<RefCell<DemoWiring>>,
    ) -> Self {
        let mut base = RuntimeAdapterBase::new();
        base.bundle_name = DEMO_BUNDLE.to_string();
        base.model_name = DEMO_MODEL.to_string();
        base.session_short = DEMO_SESSION_SHORT.to_string();
        base.banner = (DEMO_BANNER.0.to_string(), DEMO_BANNER.1.to_string());
        base.session_cost_start = wiring.borrow().session_cost_start();
        Self { base, runtime, wiring }
    }
}

impl RuntimeAdapter for DemoAdapter {
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
    fn terminal(&self) -> &crate::model::terminal::TerminalSurface {
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
    fn session_cost_start(&self) -> Decimal {
        self.base.session_cost_start
    }
    fn banner(&self) -> (String, String) {
        self.base.banner.clone()
    }
    /// Python `DemoRuntimeAdapter.start`: identity is known immediately
    /// (`ready()` first), then the seed transcript replays as a live turn.
    fn start(&mut self, ready: &mut dyn FnMut()) {
        self.wiring.borrow_mut().mark_seed_played();
        ready();
        self.runtime.borrow_mut().play_seed();
    }
    fn submit(&mut self, text: &str, _attachments: &[crate::ui::composer::ImageAttachment]) {
        self.wiring.borrow_mut().record_submit(text);
        self.runtime.borrow_mut().submit(text.to_string());
    }
    /// Queue-drained turn (spec §5): the scripted mode notice is skipped
    /// so `queued message picked up` stays visible (Python `submit_queued`).
    fn submit_queued(&mut self, text: &str) {
        self.wiring.borrow_mut().record_submit(text);
        self.runtime.borrow_mut().submit_queued(text.to_string());
    }
    fn interrupt(&mut self) -> bool {
        self.runtime.borrow_mut().interrupt();
        true
    }
    fn answer_approval(&mut self, ticket_id: &str, choice: &str) {
        self.wiring.borrow_mut().record_approval_choice(choice);
        self.runtime.borrow_mut().answer_approval(ticket_id, choice);
    }
    fn lane_blocks(
        &mut self,
        name: &str,
        session_id: &str,
        allocator: &mut BlockIdAllocator,
    ) -> Option<Vec<TranscriptBlock>> {
        self.wiring.borrow().lane_blocks(name, session_id, allocator)
    }
    fn evidence_links(
        &mut self,
        answer_text: &str,
    ) -> Vec<crate::model::evidence::EvidenceLink> {
        self.wiring.borrow().evidence_links(answer_text)
    }
    fn deferred_decision(
        &mut self,
        message: &str,
        decision_id: &str,
    ) -> (String, String, Vec<String>, String, String) {
        self.wiring.borrow().deferred_decision(message, decision_id)
    }
    fn decision_narration(&mut self, choice: &str, action: &str) -> String {
        self.wiring.borrow().decision_narration(choice, action)
    }
    fn config_view(&mut self) -> crate::model::config::ConfigSnapshotView {
        RuntimeAdapter::config_view(&mut self.base)
    }
    fn config_toggle(&mut self, category: &str, name: &str, enable: bool) -> (bool, String) {
        RuntimeAdapter::config_toggle(&mut self.base, category, name, enable)
    }
    fn config_set(&mut self, path: &str, value: &str) -> (bool, String) {
        RuntimeAdapter::config_set(&mut self.base, path, value)
    }
    fn config_diff(&mut self) -> Vec<crate::model::config::ConfigChange> {
        RuntimeAdapter::config_diff(&mut self.base)
    }
    fn config_save(&mut self, scope: &str) -> (bool, String) {
        RuntimeAdapter::config_save(&mut self.base, scope)
    }
}

// ---------------------------------------------------------------------------
// App
// ---------------------------------------------------------------------------

pub struct App {
    pub ui: Rc<RefCell<UiState>>,
    pub reducer: TranscriptReducer<Shell>,
    pub adapter: RefCell<Box<dyn RuntimeAdapter>>,
    pub commands: CommandRegistry,
    pub journal: Mutex<ApprovalJournal>,
    /// Command-facing mirror of the reducer-owned ledger (module docs).
    pub ledger: Mutex<OutcomeLedger>,
    /// Shared so the demo script's worker thread can consume steers at
    /// step boundaries (Python `steer_source=self._consume_steer`).
    pub steering: Arc<SteeringQueue>,
    pub lane_steering: LaneSteeringQueue,
    pub needs_you: NeedsYouQueue,
    pub denial_log: Mutex<DenialLog>,
    pub allocator: RefCell<BlockIdAllocator>,
    /// Core version learned over the protocol (`""` until it lands).
    pub core_version: RefCell<String>,
    /// FULL session id from `session.started` (`""` until it lands; demo
    /// sessions never set it) — drives the exit resume hint, the analogue
    /// of Python's `adapter.session_id` in `_print_resume_hint`.
    session_id: RefCell<String>,
    /// The boot-failure diagnosis already rendered — a trailing
    /// `BackendExited` (the failed backend's EOF) must not overwrite it.
    boot_failure_announced: bool,
    /// Last frame's hit-testing geometry, written by [`crate::ui::draw`].
    pub layout: RefCell<FrameLayout>,
    kitty_protocol: bool,
    /// Per-animation cadence clocks: the loop tick fires faster than any single
    /// animation, so each advances only when its own interval elapsed
    /// (splash 0.05s, lane shimmer 0.08s, title spinner 0.26s — Python parity).
    last_splash_frame: std::cell::Cell<f64>,
    last_motion_frame: std::cell::Cell<f64>,
    last_spinner_frame: std::cell::Cell<f64>,
    /// Working-line heartbeat (Python ui/app.py `set_interval(1.0,
    /// lambda: self.reducer.tick(time.time()))`): 1s block replaces pulse
    /// the ✳/✦/✧ glyph and the seconds counter.
    last_reducer_tick: std::cell::Cell<f64>,
    /// Working-label shimmer (Python per-widget `_motion_timer` at
    /// `transcript::MOTION_INTERVAL_SECONDS`).
    last_working_motion: std::cell::Cell<f64>,
    /// Clipboard writer for transcript-selection copies — injectable so
    /// headless tests record what would land on the OS clipboard. Returns
    /// true when a tool accepted the text (Python `_os_clipboard_copied`).
    clipboard_copier: Box<dyn Fn(&str) -> bool>,
    /// ↳ steer-echo bookkeeping: queued `message_id` → echo block id
    /// (Python `app.steer_echoes`; `sync_steer_echoes` drops stale ones).
    steer_echoes: RefCell<HashMap<String, String>>,
    /// Demo-only: copies the runtime's esc-interrupt close-out into the
    /// [`DemoWiring`] when a cancelled close-out lands, so the reducer's
    /// close-time `spec_lookup` re-resolve sees it (Python `turn_spec`
    /// reads `self._runtime.interrupted_close` live, same event loop).
    demo_interrupt_bridge: Option<Box<dyn Fn()>>,
}

impl App {
    pub fn new(
        adapter: Box<dyn RuntimeAdapter>,
        kitty_protocol: bool,
        initial_mode: Option<&str>,
        demo_wiring: Option<Rc<RefCell<DemoWiring>>>,
    ) -> Self {
        // Python `keymap.validate()` at construction: a malformed table is
        // a programming error, caught at boot rather than at dispatch.
        crate::ui::keymap::validate(&crate::ui::keymap::KEYMAP).expect("keymap table valid");
        let commands = build_registry();
        let ui = Rc::new(RefCell::new(UiState::new(
            kitty_protocol,
            initial_mode,
            commands.specs(),
        )));
        // Python wires the demo adapter's three data hooks straight into
        // the reducer (`spec_lookup=adapter.turn_spec,
        // lane_seed_lookup=adapter.lane_seed,
        // evidence_lookup=adapter.evidence_links`).
        let options = match demo_wiring {
            Some(wiring) => ReducerOptions {
                session_cost_start: adapter.session_cost_start(),
                spec_lookup: Some(Box::new({
                    let wiring = Rc::clone(&wiring);
                    move |prompt: &str| {
                        wiring.borrow().turn_spec(prompt).map(|spec| spec.reducer_spec())
                    }
                })),
                lane_seed_lookup: Some(Box::new({
                    let wiring = Rc::clone(&wiring);
                    move |agent: &str| wiring.borrow().lane_seed(agent)
                })),
                evidence_lookup: Some(Box::new(move |answer: &str| {
                    wiring.borrow().evidence_links(answer)
                })),
                ..ReducerOptions::default()
            },
            None => ReducerOptions {
                session_cost_start: adapter.session_cost_start(),
                ..ReducerOptions::default()
            },
        };
        let reducer = TranscriptReducer::with_options(
            Shell(Rc::clone(&ui)),
            BlockIdAllocator::new(),
            OutcomeLedger::new(),
            LaneRegistry::new(),
            options,
        );
        App {
            ui,
            reducer,
            adapter: RefCell::new(adapter),
            commands,
            journal: Mutex::new(ApprovalJournal::new()),
            ledger: Mutex::new(OutcomeLedger::new()),
            steering: Arc::new(SteeringQueue::new()),
            lane_steering: LaneSteeringQueue::new(),
            needs_you: NeedsYouQueue::new(),
            denial_log: Mutex::new(DenialLog::new()),
            allocator: RefCell::new(BlockIdAllocator::starting_at(APP_ID_RANGE_START)),
            core_version: RefCell::new(String::new()),
            session_id: RefCell::new(String::new()),
            boot_failure_announced: false,
            layout: RefCell::new(FrameLayout::default()),
            kitty_protocol,
            last_splash_frame: std::cell::Cell::new(0.0),
            last_motion_frame: std::cell::Cell::new(0.0),
            last_spinner_frame: std::cell::Cell::new(0.0),
            last_reducer_tick: std::cell::Cell::new(0.0),
            last_working_motion: std::cell::Cell::new(0.0),
            clipboard_copier: Box::new(app_support::os_clipboard_copy),
            steer_echoes: RefCell::new(HashMap::new()),
            demo_interrupt_bridge: None,
        }
    }

    /// Swap the clipboard writer (headless tests inject a recorder).
    pub fn set_clipboard_copier(&mut self, copier: Box<dyn Fn(&str) -> bool>) {
        self.clipboard_copier = copier;
    }

    /// Install the demo esc-interrupt close-out bridge (see the field docs
    /// on [`App::demo_interrupt_bridge`]); `--demo` composition only.
    pub fn set_demo_interrupt_bridge(&mut self, bridge: Box<dyn Fn()>) {
        self.demo_interrupt_bridge = Some(bridge);
    }

    /// Boot the runtime (Python `_boot_runtime`): start the adapter; when it
    /// already knows its session identity (demo), announce ready now —
    /// protocol sessions announce on the `session.started` record instead.
    pub fn boot(&mut self) {
        {
            let mut adapter = self.adapter.borrow_mut();
            adapter.start(&mut || {});
        }
        let identity = {
            let adapter = self.adapter.borrow_mut();
            (
                adapter.bundle_name(),
                adapter.model_name(),
                adapter.session_short(),
            )
        };
        if !identity.2.is_empty() {
            self.announce_ready(&identity.0, &identity.1, &identity.2);
        }
    }

    /// Session identity landed: dissolve the splash, append the session
    /// banner, fill the chrome, seed prompt history and `@file` completion
    /// (Python `app_support.announce_ready`).
    fn announce_ready(&mut self, bundle: &str, model: &str, session_short: &str) {
        let (history, files, notices, banner) = {
            let mut adapter = self.adapter.borrow_mut();
            (
                adapter.prompt_history(),
                adapter.workspace_files(),
                adapter.startup_notices(),
                adapter.banner(),
            )
        };
        // Protocol sessions learn identity from `session.started`, which
        // carries no version headline (the Python boot banner's actual
        // payload). Synthesizing a headline-less identity line here just
        // duplicated the footer verbatim (user report: "Bundle: newtui |
        // anthropic/claude-fable-5 · session 680b51d" as boot noise), so an
        // empty banner appends NOTHING — deliberate divergence from the
        // Python boot banner until the wire carries version info. The demo
        // banner (real headline) and `/about` still render it.
        let (headline, detail) = banner;
        let mut ui = self.ui.borrow_mut();
        ui.splash = None;
        if !headline.is_empty() || !detail.is_empty() {
            let id = self.allocator.borrow_mut().next_id();
            let _ = ui.transcript.append(
                SessionBanner {
                    detail,
                    ..SessionBanner::new(id, headline)
                }
                .into(),
                monotonic(),
            );
        }
        ui.bundle = bundle.to_string();
        ui.model_name = model.to_string();
        ui.session_short = session_short.to_string();
        let changed = ui.title.set_bundle(bundle.to_string());
        ui.note_title(changed);
        let changed = ui.title.set_session_short(session_short.to_string());
        ui.note_title(changed);
        ui.composer.seed_history(history);
        ui.file_mentions.set_files(files);
        for notice in notices {
            ui.show_notice(&notice, None);
        }
    }

    // -- wire events ---------------------------------------------------------

    pub fn handle_wire(&mut self, event: WireEvent) {
        match event {
            WireEvent::SessionStarted {
                session_id,
                bundle,
                model,
            } => {
                let short: String = session_id.chars().take(7).collect();
                *self.session_id.borrow_mut() = session_id;
                self.announce_ready(&bundle, &model, &short);
            }
            WireEvent::Approval {
                ticket_id,
                prompt,
                options,
            } => {
                // Reducer state first (approval_opened), then presentation
                // (Python: broker present_approval + the runtime event).
                self.reducer
                    .handle(&ev::UIEvent::ApprovalRequired(ev::ApprovalRequired {
                        prompt: prompt.clone(),
                        options: options.clone(),
                        ..ev::ApprovalRequired::default()
                    }));
                let options = if options.is_empty() {
                    DEFAULT_OPTIONS.iter().map(|s| s.to_string()).collect()
                } else {
                    options
                };
                let mut ui = self.ui.borrow_mut();
                // Spec §7.3 (Python `app_support.mount_approval`): an approval
                // arriving while a lane is focused auto-returns to the parent
                // transcript; the approval notice lands first and the
                // auto-return notice overwrites it and stays.
                let lane_was_focused = ui.transcript.focused_lane().is_some();
                if lane_was_focused {
                    let _ = ui.transcript.restore_main(monotonic());
                    ui.lanes_panel.set_focused(None);
                }
                // The approval bar owns the keyboard (spec §7): an open
                // palette strip would otherwise steal the arrow keys, and
                // a focused evidence block loses focus to `bar.focus()`.
                ui.palette.apply_filter(None);
                ui.focused_evidence = None;
                if let Ok(mut bar) = ApprovalBar::new(ticket_id, prompt, options) {
                    bar.update_wrap(ui.term_width as usize);
                    ui.approval = Some(bar);
                }
                ui.show_notice(APPROVAL_NOTICE, Some(APPROVAL_NOTICE_DURATION));
                if lane_was_focused {
                    ui.show_notice(
                        "back to parent · approval required",
                        Some(APPROVAL_NOTICE_DURATION),
                    );
                }
                drop(ui);
                self.settle_after_event();
            }
            WireEvent::BootProgress { action, detail } => {
                // Python `NewTuiApp.boot_progress`: snake_case phases read as
                // words; the splash status is `"{action} · {detail}"` (or the
                // bare action). Protocol phases win over stderr chatter.
                let action = action.replace('_', " ");
                let status = if detail.is_empty() {
                    action
                } else {
                    format!("{action} · {detail}")
                };
                let mut ui = self.ui.borrow_mut();
                ui.boot_progress_seen = true;
                if let Some(splash) = ui.splash.as_mut() {
                    let _ = splash.set_status(&status);
                }
            }
            WireEvent::Error { error, error_type } => {
                let booting = self.ui.borrow().splash.is_some();
                if booting {
                    // Boot failure: serve emitted the error instead of
                    // `session.started` (exit 1 follows).
                    let detail = if error.trim().is_empty() {
                        error_type
                    } else {
                        error.trim().to_string()
                    };
                    self.announce_boot_failure(&detail);
                } else {
                    // A failed turn (Python `_submit_prompt`'s except-arm:
                    // notice only, the session stays live).
                    self.ui
                        .borrow_mut()
                        .show_notice(&format!("turn failed · {error}"), None);
                }
            }
            WireEvent::Event(event) => {
                // Demo esc-interrupt: the cancelled close-out precedes its
                // `prompt_complete` on the wire — copy the runtime's
                // interrupted close-out spec into the wiring NOW so the
                // reducer's close-time spec re-resolve sees it.
                if let ev::UIEvent::OrchestratorComplete(complete) = &event {
                    if complete.status == ev::OrchestratorStatus::Cancelled {
                        if let Some(bridge) = self.demo_interrupt_bridge.as_ref() {
                            bridge();
                        }
                    }
                }
                self.consume_steer_on_wire_narration(&event);
                self.reducer.handle(&event);
                self.settle_after_event();
            }
        }
    }

    /// Wire-driven steer-echo sync: the backend consumed a steer at a step
    /// boundary and narrated it (RealRuntime `_steer_applied`'s durable
    /// root-session `Applying steer: …` block). The local queue copy exists
    /// only for the ↳ echo/badge UX — consume it now so `sync_steer_echoes`
    /// drops the echo and the turn-end drain doesn't false-report a discard.
    /// Both queues are FIFO fed by the same submits, so the narration always
    /// names the oldest pending steer. Protocol sessions only (`session_id`
    /// set by `session.started`): the demo runtime consumes the shared local
    /// queue directly through its `steer_source`, before its narration event
    /// ever arrives here.
    fn consume_steer_on_wire_narration(&mut self, event: &ev::UIEvent) {
        let session_id = self.session_id.borrow().clone();
        if session_id.is_empty() {
            return;
        }
        let ev::UIEvent::ContentBlockEnd(block) = event else {
            return;
        };
        if block.session_id != session_id
            || block.block.get("demo_role").and_then(serde_json::Value::as_str)
                != Some("narration")
        {
            return;
        }
        let applied = block
            .block
            .get("text")
            .and_then(serde_json::Value::as_str)
            .and_then(|text| text.strip_prefix("Applying steer: "));
        let Some(applied) = applied else { return };
        let head_matches = self
            .steering
            .pending_steers()
            .first()
            .is_some_and(|steer| steer.text == applied);
        if head_matches {
            let _ = self.steering.consume_next_steer();
        }
    }

    /// The backend process's stdout closed. Before identity this is a boot
    /// failure — run the same diagnosis as a structured `error` record
    /// (previously the splash hung forever); mid-session the session is
    /// gone, so say so honestly.
    pub fn on_backend_exited(&mut self) {
        if self.boot_failure_announced {
            return; // the error record's diagnosis already rendered
        }
        let booting = self.ui.borrow().splash.is_some();
        if booting {
            self.announce_boot_failure("backend exited before session.started");
        } else {
            // A dead backend can never deliver the in-flight turn's
            // `prompt_complete`: without this the working pulse stays
            // mounted (and "working…") forever. Settle it as interrupted —
            // the same durable shape replay gives a log that ended mid-turn
            // (`test_replay_closes_a_dangling_turn_as_interrupted`).
            if self.reducer.turn_running() {
                self.reducer.close_dangling_turn();
                self.settle_after_event();
            }
            self.ui
                .borrow_mut()
                .show_notice("backend exited · session lost — ctrl+d to quit", None);
        }
    }

    /// Port of `ui/app_support.py::announce_boot_failure`: dismiss the
    /// splash immediately (error text, not a melting wordmark) and render
    /// the readable diagnosis + doctor hint with Python's exact strings.
    fn announce_boot_failure(&mut self, detail: &str) {
        self.boot_failure_announced = true;
        let mut ui = self.ui.borrow_mut();
        ui.splash = None; // Python `clear_boot_progress(immediate=True)`
        let id = self.allocator.borrow_mut().next_id();
        let _ = ui.transcript.append(
            Answer {
                clickable: false,
                ..Answer::new(
                    id,
                    vec![
                        Segment {
                            style_token: StyleToken::Red,
                            ..Segment::new("⊘ session failed to start · ")
                        },
                        Segment::new(detail),
                    ],
                )
            }
            .into(),
            monotonic(),
        );
        let hint = "Check provider setup with `amplifier-newtui doctor`, or run \
`--demo` for a credential-free UI. Press ctrl+d to quit.";
        let id = self.allocator.borrow_mut().next_id();
        let _ = ui.transcript.append(
            Answer {
                clickable: false,
                ..Answer::new(
                    id,
                    vec![Segment {
                        style_token: StyleToken::Dim,
                        ..Segment::new(hint)
                    }],
                )
            }
            .into(),
            monotonic(),
        );
        ui.show_notice("session failed to start", None);
    }

    /// The full stored session id (empty for demo/unstarted sessions) —
    /// the exit resume hint's input.
    pub fn resume_session_id(&self) -> String {
        self.session_id.borrow().clone()
    }

    /// Post-event duties (Python `_consume_events` after `reducer.handle`).
    fn settle_after_event(&mut self) {
        self.sync_lanes_panel();
        self.sync_title();
        self.sync_rewind_checkpoints();
        self.sync_steer_echoes();
        self.drain_deferrals();
        self.drain_turn_queues();
    }

    /// Drop the ↳ echo of any steer no longer pending (spec §5) — Python
    /// `sync_steer_echoes`, a steering-queue listener; here it runs with
    /// the other post-event duties (a steer leaves the queue either when
    /// the runtime consumes it at a step boundary or at turn-end discard).
    fn sync_steer_echoes(&mut self) {
        let stale: Vec<(String, String)> = {
            let pending: std::collections::HashSet<String> = self
                .steering
                .pending_steers()
                .into_iter()
                .map(|message| message.message_id)
                .collect();
            self.steer_echoes
                .borrow()
                .iter()
                .filter(|(message_id, _)| !pending.contains(*message_id))
                .map(|(message_id, block_id)| (message_id.clone(), block_id.clone()))
                .collect()
        };
        for (message_id, block_id) in stale {
            self.steer_echoes.borrow_mut().remove(&message_id);
            let _ = self.ui.borrow_mut().transcript.remove_block(&block_id);
        }
    }

    fn sync_lanes_panel(&mut self) {
        let dirty = { std::mem::take(&mut self.ui.borrow_mut().lanes_dirty) };
        if !dirty {
            return;
        }
        // A finished delegate never reaches another step boundary: drop its
        // undelivered steers so no stale ▸ badge pins to a done lane (#39).
        for record in self.reducer.lanes().lanes() {
            if record.lane.state == crate::model::lanes::LaneStateName::Done
                && self.lane_steering.queued_count(&record.session_id) > 0
            {
                let _ = self.lane_steering.drain(&record.session_id);
            }
        }
        let records = self.reducer.lanes().lanes();
        let tailed = self.reducer.lanes().tail_lane();
        let counts = self.lane_steering.counts();
        let mut ui = self.ui.borrow_mut();
        let was_open = ui.lanes_panel.display();
        ui.lanes_panel.update_lanes(
            &records,
            tailed.as_ref().map(|record| record.session_id.as_str()),
            Some(&counts),
        );
        // Mockup runAgentsTurn: the panel opens automatically at fan-out
        // and STAYS visible on ✔ done (retracts on ctrl-t / esc).
        if self.reducer.lanes().active_count() > 0 && !was_open {
            ui.lanes_panel.show_panel();
        }
    }

    fn sync_title(&mut self) {
        let state = self.reducer.title_state();
        let mut ui = self.ui.borrow_mut();
        let changed = ui.title.set_state_text(state);
        ui.note_title(changed);
    }

    fn sync_rewind_checkpoints(&mut self) {
        let checkpoints: Vec<Checkpoint> = self
            .reducer
            .ledger
            .checkpoints()
            .into_iter()
            .cloned()
            .collect();
        self.ui.borrow_mut().rewind.sync_checkpoints(&checkpoints);
    }

    /// Park deferred decisions the reducer flagged (Python
    /// `decision_deferred`): kernel-parked ids are already in the queue;
    /// message-only deferrals derive their item through the adapter.
    fn drain_deferrals(&mut self) {
        let pending = { std::mem::take(&mut self.ui.borrow_mut().pending_deferrals) };
        for (message, decision_id) in pending {
            let parked = !decision_id.is_empty()
                && self
                    .needs_you
                    .items()
                    .iter()
                    .any(|item| item.decision_id == decision_id);
            if !parked {
                let (question, reason, choices, highlight, action) = self
                    .adapter
                    .borrow_mut()
                    .deferred_decision(&message, &decision_id);
                let _ = self.needs_you.defer(
                    &question,
                    &reason,
                    DeferOptions {
                        choices,
                        highlight,
                        action,
                        ..DeferOptions::default()
                    },
                );
            }
            // A deferred decision blocks on the human: always worth
            // notifying (Python `_notify_attention("decision_deferred")`).
            if app_support::attention_bell_needed(Reason::DecisionDeferred, 0.0, None) {
                self.ui.borrow_mut().bell_pending = true;
            }
        }
    }

    /// Run the deferred turn-end queue duties once (Python
    /// `drain_turn_queues` + `finish_turn_queues`): the queued next-turn
    /// message becomes the next submitted turn.
    fn drain_turn_queues(&mut self) {
        let pending = {
            let mut ui = self.ui.borrow_mut();
            if !ui.turn_queues_pending || ui.turn_active {
                return;
            }
            ui.turn_queues_pending = false;
            true
        };
        if !pending {
            return;
        }
        // Leftover steers are discarded at turn end (mockup §5) — but say
        // so (Python `finish_turn_queues`): silent loss of typed input
        // reads as a bug. `sync_steer_echoes` drops the ↳ echoes.
        if !self.steering.drain_steers().is_empty() {
            self.ui.borrow_mut().show_notice(STEER_DISCARDED_NOTICE, None);
        }
        self.sync_steer_echoes();
        if let Some(message) = self.steering.consume_next_turn_message() {
            {
                let mut ui = self.ui.borrow_mut();
                ui.queued_strip.clear_queued();
                ui.show_notice("queued message picked up", None);
            }
            self.adapter.borrow_mut().submit_queued(&message.text);
        }
    }

    // -- keys ------------------------------------------------------------------

    /// One key press, in Textual chord names (`"enter"`, `"shift+tab"`,
    /// `"ctrl+t"`, single chars insert themselves).
    pub fn on_key(&mut self, key: &str) {
        // Global quit chords (Textual stock ctrl+q + app-cli parity ctrl+d).
        if key == "ctrl+q" || key == "ctrl+d" {
            self.ui.borrow_mut().should_quit = true;
            return;
        }
        // ctrl+c (Python `action_copy_selection`): copy wins whenever text is
        // actually selected — the transcript drag-selection copies (and
        // clears) instead of quitting. With nothing selected, keep the
        // terminal/Mac convention: a running turn interrupts, an idle app
        // quits (like ctrl+d).
        if key == "ctrl+c" {
            let text = self.selected_text();
            if !text.is_empty() {
                let copied = (self.clipboard_copier)(&text);
                let chars = text.chars().count();
                let mut ui = self.ui.borrow_mut();
                ui.selection = None;
                ui.selection_settle_deadline = None;
                let notice = if copied {
                    format!("copied · {chars} chars")
                } else {
                    format!(
                        "copied · {chars} chars · empty clipboard? allow terminal clipboard access"
                    )
                };
                ui.show_notice(&notice, None);
                return;
            }
            let running = self.ui.borrow().turn_active;
            if running {
                self.interrupt_turn();
                self.ui.borrow_mut().show_notice("interrupting… (ctrl+c)", None);
            } else {
                self.ui.borrow_mut().should_quit = true;
            }
            return;
        }

        // The approval bar owns the keyboard while open (spec §7).
        let approval_outcome = {
            let mut ui = self.ui.borrow_mut();
            ui.approval.as_mut().map(|bar| bar.handle_key(key))
        };
        if let Some(outcome) = approval_outcome {
            match outcome {
                KeyOutcome::Emit(ApprovalMsg::Resolved { ticket_id, choice }) => {
                    self.resolve_approval(&ticket_id, &choice);
                }
                KeyOutcome::Emit(ApprovalMsg::Deferred { ticket_id }) => {
                    self.defer_approval(&ticket_id);
                }
                KeyOutcome::Handled | KeyOutcome::Ignored => {}
            }
            return;
        }

        // Global chords (all suppressed under the approval bar above).
        match key {
            "shift+tab" => {
                self.action_cycle_mode_impl();
                return;
            }
            "ctrl+p" => {
                let mut ui = self.ui.borrow_mut();
                let trust = ui.mode.trust_str;
                ui.show_notice(&format!("trust · {trust} · edit via /permissions"), None);
                return;
            }
            "ctrl+t" => {
                self.toggle_lanes();
                return;
            }
            "ctrl+o" => {
                self.cycle_tail();
                return;
            }
            "ctrl+g" => {
                self.toggle_thinking();
                return;
            }
            "ctrl+l" => {
                self.run_slash_command("/ledger");
                return;
            }
            "ctrl+y" => {
                self.show_needs_you();
                return;
            }
            "ctrl+r" => {
                self.open_rewind_strip(None);
                return;
            }
            _ => {}
        }

        // A focused evidence block owns its advertised keys (←/→ select ·
        // enter expand · esc close — keymap `evidence` context, spec §10):
        // the Rust rendering of Python giving the mounted widget keyboard
        // focus, so its bindings run before the app's.
        if matches!(key, "left" | "right" | "enter" | "escape") {
            let focused = self.ui.borrow().focused_evidence.clone();
            if let Some(block_id) = focused {
                let routed = {
                    let mut ui = self.ui.borrow_mut();
                    match ui
                        .transcript
                        .get_widget_mut(&block_id)
                        .and_then(|widget| widget.as_block_mut())
                    {
                        Some(widget) => Some(widget.handle_key(key, monotonic())),
                        None => {
                            // The block vanished (trim/compaction): drop
                            // the stale focus and fall through.
                            ui.focused_evidence = None;
                            None
                        }
                    }
                };
                if let Some(msg) = routed {
                    if let Some(msg) = msg {
                        self.handle_transcript_msg(msg);
                    }
                    return;
                }
            }
        }

        // Rewind strip navigation while open (keymap `rewind` context).
        let rewind_open = self.ui.borrow().rewind.display();
        if rewind_open {
            match key {
                "left" => {
                    self.ui.borrow_mut().rewind.nav(-1);
                    return;
                }
                "right" => {
                    self.ui.borrow_mut().rewind.nav(1);
                    return;
                }
                "enter" => {
                    let msg = self.ui.borrow_mut().rewind.fork();
                    if let Some(RewindMsg::ForkRequested { checkpoint_id }) = msg {
                        self.handle_fork(&checkpoint_id);
                    }
                    return;
                }
                _ => {} // printable keys type through to the composer below
            }
        }

        // Palette arrows while open (mockup: arrows cycle the palette).
        let palette_arrows = {
            let ui = self.ui.borrow();
            ui.palette.is_open() && (key == "up" || key == "down")
        };
        if palette_arrows {
            let delta = if key == "up" { -1 } else { 1 };
            self.ui.borrow_mut().palette.move_selection(delta);
            return;
        }

        // Everything else is composer input.
        self.ui.borrow_mut().composer.handle_key(key);
        self.drain_composer_messages();
    }

    /// A bracketed paste arrived.
    pub fn on_paste(&mut self, payload: &str) {
        self.ui.borrow_mut().composer.handle_paste(payload);
        self.drain_composer_messages();
    }

    pub fn on_resize(&mut self, width: u16, height: u16) {
        let mut ui = self.ui.borrow_mut();
        ui.term_width = width;
        ui.term_height = height;
        // Feed the live width to the kernel's width-aware surface hint (#35).
        self.adapter.borrow().terminal().set_cols(i64::from(width));
        let now = monotonic();
        if let Some(delay) = ui.transcript.on_resize(width.saturating_sub(2) as usize, now) {
            ui.reflow_deadline = Some(now + delay);
        }
        if let Some(bar) = ui.approval.as_mut() {
            bar.update_wrap(width as usize);
        }
        ui.sync_plan_surfaces();
    }

    // -- mouse -------------------------------------------------------------------
    //
    // Hit-testing runs against the [`FrameLayout`] the last `ui::draw`
    // recorded, so clicks map to exactly what is on screen. The ported
    // units own the semantics (`BlockWidget::click`, `ApprovalBar::click`,
    // `LanesPanel::on_click`, `TranscriptView::on_mouse_scroll_*`).

    fn rect_contains(rect: ratatui::layout::Rect, x: u16, y: u16) -> bool {
        x >= rect.x && x < rect.x + rect.width && y >= rect.y && y < rect.y + rect.height
    }

    /// Mouse wheel over the transcript (Python `on_mouse_scroll_up/_down`):
    /// wheel-up releases the tail-follow anchor; wheel-down re-arms it once
    /// the view is back at the bottom.
    pub fn on_mouse_scroll(&mut self, up: bool, x: u16, y: u16) {
        let (rect, total) = {
            let layout = self.layout.borrow();
            (layout.transcript, layout.transcript_total_lines)
        };
        if !Self::rect_contains(rect, x, y) {
            return;
        }
        /// Textual's default vertical wheel sensitivity (lines per notch).
        const WHEEL_STEP: usize = 2;
        let bottom = total.saturating_sub(rect.height as usize);
        let mut ui = self.ui.borrow_mut();
        let current = if ui.transcript.follow() {
            bottom
        } else {
            ui.transcript_scroll.min(bottom)
        };
        if up {
            ui.transcript_scroll = current.saturating_sub(WHEEL_STEP);
            ui.transcript.on_mouse_scroll_up();
        } else {
            let next = (current + WHEEL_STEP).min(bottom);
            ui.transcript_scroll = next;
            ui.transcript.on_mouse_scroll_down(next >= bottom);
        }
    }

    /// Map a screen (x, y) over the transcript to a content position —
    /// `(line, column)` in content-line index / terminal-cell column space
    /// (clamped into the rect and to the painted content).
    fn transcript_content_pos(&self, x: u16, y: u16) -> Option<(usize, usize)> {
        let layout = self.layout.borrow();
        let rect = layout.transcript;
        if rect.width == 0 || rect.height == 0 || layout.transcript_total_lines == 0 {
            return None;
        }
        let y = y.clamp(rect.y, rect.y + rect.height - 1);
        let line = layout.transcript_scroll + (y - rect.y) as usize;
        let x = x.clamp(rect.x, rect.x + rect.width - 1);
        Some((line.min(layout.transcript_total_lines - 1), (x - rect.x) as usize))
    }

    /// Reveal a block (Python `transcript.scroll_block_visible`): release
    /// the tail anchor and point the scroll offset at the block's first
    /// painted line from the last frame (clamped to content at draw time).
    fn scroll_block_into_view(&mut self, block_id: &str) {
        let start = {
            let layout = self.layout.borrow();
            layout
                .block_lines
                .iter()
                .find(|(id, _, _)| id == block_id)
                .map(|(_, start, _)| *start)
        };
        let Some(start) = start else { return };
        let mut ui = self.ui.borrow_mut();
        ui.transcript.release_anchor();
        ui.transcript_scroll = start;
    }

    /// A left-button press at screen (x, y).
    pub fn on_mouse_down(&mut self, x: u16, y: u16) {
        let layout = self.layout.borrow().clone();

        // Python screen-selection semantics: a fresh press clears any
        // transcript selection (a drag re-creates one) BEFORE the normal
        // click dispatch below.
        {
            let mut ui = self.ui.borrow_mut();
            ui.selection = None;
            ui.selection_settle_deadline = None;
            ui.selection_drag_anchor = None;
        }
        if Self::rect_contains(layout.transcript, x, y) {
            // Anchor a possible drag-selection at the pressed cell.
            let anchor = self.transcript_content_pos(x, y);
            self.ui.borrow_mut().selection_drag_anchor = anchor;
        }

        // Approval option chips / the composer's [mode] badge (chunk 7).
        if Self::rect_contains(layout.input, x, y) {
            let row = (y - layout.input.y) as usize;
            let col = (x - layout.input.x) as usize;
            let approval_open = self.ui.borrow().approval.is_some();
            if approval_open {
                // Python `on_approval_bar_option_clicked`: select + confirm.
                let hit = {
                    let ui = self.ui.borrow();
                    ui.approval
                        .as_ref()
                        .and_then(|bar| crate::ui::approval_hit(bar, col, row))
                };
                if let Some(index) = hit {
                    let msg = {
                        let mut ui = self.ui.borrow_mut();
                        ui.approval.as_mut().map(|bar| bar.click(index))
                    };
                    if let Some(ApprovalMsg::Resolved { ticket_id, choice }) = msg {
                        self.resolve_approval(&ticket_id, &choice);
                    }
                }
            } else if row == 0 && col < layout.mode_badge_width as usize {
                // Python `ModeBadge.on_click` → the app cycles the mode.
                self.ui.borrow_mut().composer.badge_clicked();
                self.drain_composer_messages();
            }
            return;
        }

        // Footer waiting badge (Python `FooterBar.WaitingBadgeClicked` →
        // `action_show_needs_you`, the ctrl+y action).
        if Self::rect_contains(layout.footer, x, y) {
            if y == layout.footer.y {
                if let Some((start, end)) = layout.badge_span {
                    if x >= start && x < end {
                        self.show_needs_you();
                    }
                }
            }
            return;
        }

        // Lanes panel rows (Python `_LaneRow.on_click`): focus that lane.
        if Self::rect_contains(layout.lanes, x, y) {
            let row = (y - layout.lanes.y) as usize;
            let msg = layout
                .lane_rows
                .get(row)
                .copied()
                .flatten()
                .and_then(|index| self.ui.borrow().lanes_panel.on_click(index));
            if let Some(LanesMsg::FocusLane { name, session_id }) = msg {
                self.focus_lane(&name, &session_id);
            }
            return;
        }

        // Transcript blocks: map screen y → content line → (block, row) and
        // route through `BlockWidget::click` (Python `on_click`).
        if Self::rect_contains(layout.transcript, x, y) {
            let line = layout.transcript_scroll + (y - layout.transcript.y) as usize;
            let Some((block_id, row)) = layout.block_at_line(line) else {
                return;
            };
            // Transcript clicks never strand the keyboard (DESIGN-SPEC
            // §12); the one exception is the evidence block, which keeps
            // the focus it took on click (Python `on_click`).
            {
                let mut ui = self.ui.borrow_mut();
                if ui.focused_evidence.as_deref() != Some(block_id.as_str()) {
                    ui.focused_evidence = None;
                }
            }
            let msg = {
                let mut ui = self.ui.borrow_mut();
                ui.transcript
                    .get_widget_mut(&block_id)
                    .and_then(|widget| widget.as_block_mut())
                    .and_then(|widget| widget.click(row as isize, monotonic()))
            };
            if let Some(msg) = msg {
                self.handle_transcript_msg(msg);
            }
        }
    }

    /// Left-button drag: extend the transcript selection from the mouse-down
    /// anchor (Python's screen drag-selection). Every extension restarts the
    /// copy-on-settle timer, mirroring `_selection_changed`'s 0.4s debounce.
    pub fn on_mouse_drag(&mut self, x: u16, y: u16) {
        let anchor = self.ui.borrow().selection_drag_anchor;
        let Some(anchor) = anchor else { return };
        let Some(head) = self.transcript_content_pos(x, y) else { return };
        let mut ui = self.ui.borrow_mut();
        ui.selection = Some((anchor, head));
        ui.selection_settle_deadline = Some(monotonic() + SELECTION_SETTLE_SECONDS);
    }

    /// Left-button release: the drag ends. The settle timer armed by the
    /// last extension keeps running and fires copy-on-select via `tick`.
    pub fn on_mouse_up(&mut self, _x: u16, _y: u16) {
        self.ui.borrow_mut().selection_drag_anchor = None;
    }

    /// The current transcript selection as plain text: the exact character
    /// range of the rendered lines (last frame's plain-text projection) —
    /// partial first line from the anchor column, full middle lines,
    /// partial last line to the head column (terminal-style, normalized
    /// for drag direction; a cell in the middle of a wide glyph rounds to
    /// include it). Empty when nothing (or only whitespace) is selected.
    pub fn selected_text(&self) -> String {
        let Some((anchor, head)) = self.ui.borrow().selection else {
            return String::new();
        };
        let (start, end) = crate::ui::normalize_selection(anchor, head);
        let layout = self.layout.borrow();
        let text = layout
            .transcript_plain_lines
            .iter()
            .enumerate()
            .filter_map(|(index, line)| {
                crate::ui::selection_line_cells(start, end, index)
                    .map(|(lo, hi)| crate::ui::slice_line_cells(line, lo, hi))
            })
            .collect::<Vec<_>>()
            .join("\n");
        if text.trim().is_empty() {
            String::new() // a drag over blank cells selects no text
        } else {
            text
        }
    }

    /// The settle timer fired (Python `_copy_settled_selection`): auto-copy
    /// the settled drag-selection unless it is empty or a duplicate.
    fn copy_settled_selection(&mut self) {
        let text = self.selected_text();
        if text.is_empty() || text == self.ui.borrow().last_selection_copied {
            return;
        }
        self.ui.borrow_mut().last_selection_copied = text.clone();
        let _ = (self.clipboard_copier)(&text);
        self.ui.borrow_mut().show_notice(
            &format!("copied on select · {} chars", text.chars().count()),
            None,
        );
    }

    /// Dispatch a transcript widget message (the Python `on_<message>`
    /// handlers of `ui/app.py`, minus keyboard-focus juggling — the
    /// composer always holds the keyboard in this client).
    fn handle_transcript_msg(&mut self, msg: TranscriptMsg) {
        match msg {
            TranscriptMsg::ToolLineToggled { block_id, .. } => {
                self.ui.borrow_mut().transcript.on_tool_line_toggled(&block_id);
            }
            TranscriptMsg::DelegateSummaryToggled { block_id, expanded } => {
                let mut ui = self.ui.borrow_mut();
                ui.transcript.on_delegate_summary_toggled(&block_id);
                // Drill-down v1 (ambient-progress D5): an expanded summary
                // opens the lanes panel.
                if expanded {
                    ui.lanes_panel.show_panel();
                }
            }
            TranscriptMsg::ThinkingToggled { block_id, .. } => {
                self.ui.borrow_mut().transcript.on_thinking_toggled(&block_id);
            }
            TranscriptMsg::CopyCodeFence { text, .. } => {
                let _ = app_support::os_clipboard_copy(&text);
                self.ui.borrow_mut().show_notice(
                    &format!("copied code · {} chars", text.chars().count()),
                    None,
                );
            }
            TranscriptMsg::ShowEvidence { links, .. } => {
                if links.is_empty() {
                    self.ui
                        .borrow_mut()
                        .show_notice("no evidence recorded for this answer", None);
                    return;
                }
                let mut ui = self.ui.borrow_mut();
                // Repeat clicks must not stack duplicate evidence blocks —
                // refocus the already-open block instead (Python
                // `on_show_evidence`).
                let last = ui
                    .transcript
                    .block_ids()
                    .last()
                    .and_then(|id| ui.transcript.get_block(id));
                match &last {
                    Some(TranscriptBlock::Evidence(existing)) if existing.links == links => {
                        ui.focused_evidence = Some(existing.id.clone());
                    }
                    _ => {
                        let id = self.allocator.borrow_mut().next_id();
                        let _ = ui
                            .transcript
                            .append(EvidenceBlock::new(id.clone(), links).into(), monotonic());
                        // The block owns the keyboard while open so its
                        // advertised keys (←/→ select · enter expand · esc
                        // close, spec §10) work; esc hands it back.
                        ui.focused_evidence = Some(id);
                    }
                }
                // Mockup revealEvidence ends with this exact notice.
                ui.show_notice("evidence revealed · every claim traces to a tool call", None);
            }
            TranscriptMsg::OpenRewind { checkpoint_id } => {
                let index = self
                    .reducer
                    .ledger
                    .checkpoints()
                    .iter()
                    .position(|checkpoint| checkpoint.id == checkpoint_id);
                self.open_rewind_strip(index);
            }
            TranscriptMsg::ExpandEvidenceClaim { link, .. } => {
                // Enter on the evidence block (Python
                // `on_expand_evidence_claim`): deep-link the selected claim
                // to the tool line that grounds it (correlation key, spec
                // §10) — expand it and scroll it into view.
                if !link.tool_call_id.is_empty() {
                    let target = {
                        let ui = self.ui.borrow();
                        ui.transcript.blocks().into_iter().find_map(|block| {
                            match block {
                                TranscriptBlock::ToolLine(tool)
                                    if tool.tool_call_ids.contains(&link.tool_call_id) =>
                                {
                                    Some(tool)
                                }
                                _ => None,
                            }
                        })
                    };
                    if let Some(tool) = target {
                        let tool_id = tool.id.clone();
                        if !tool.body.is_empty() && !tool.expanded {
                            let expanded = ToolLine {
                                expanded: true,
                                ..tool
                            };
                            let _ = self
                                .ui
                                .borrow_mut()
                                .transcript
                                .replace(expanded.into(), monotonic());
                        }
                        self.scroll_block_into_view(&tool_id);
                        return;
                    }
                }
                // No correlated tool line in the transcript: surface the
                // grounding reference itself instead of silently doing
                // nothing (Python's exact notice).
                self.ui
                    .borrow_mut()
                    .show_notice(&format!("grounded by {}", link.tool_ref), None);
            }
            TranscriptMsg::CloseEvidence { block_id } => {
                // Esc on the evidence block (Python `on_close_evidence`):
                // close it and hand the keyboard back to the composer.
                let mut ui = self.ui.borrow_mut();
                if ui.transcript.get_block(&block_id).is_some() {
                    let _ = ui.transcript.remove_block(&block_id);
                }
                if ui.focused_evidence.as_deref() == Some(block_id.as_str()) {
                    ui.focused_evidence = None;
                }
            }
            TranscriptMsg::LaneFocusChanged { .. } => {
                // Focus swap follow-ups run at the call sites (focus_lane /
                // handle_esc) — the message is informational here.
            }
        }
    }

    /// Drain the pending native-terminal title (main loop OSC writer).
    pub fn take_pending_title(&self) -> Option<String> {
        self.ui.borrow_mut().pending_title.take()
    }

    /// Drain the pending attention bell (main loop `\x07` writer).
    pub fn take_bell(&self) -> bool {
        std::mem::take(&mut self.ui.borrow_mut().bell_pending)
    }

    fn drain_composer_messages(&mut self) {
        let messages = { self.ui.borrow_mut().composer.drain_messages() };
        for message in messages {
            match message {
                ComposerMessage::Submit { text, .. } => self.on_submit(&text),
                ComposerMessage::Steer { text } => self.on_steer(&text),
                ComposerMessage::QueueMessage { text } => self.on_queue_message(&text),
                ComposerMessage::OpenPalette { filter } => {
                    let mut ui = self.ui.borrow_mut();
                    close_file_mentions(&mut *ui);
                    ui.palette.apply_filter(Some(&filter));
                }
                ComposerMessage::PaletteFilterCleared => {
                    self.ui.borrow_mut().palette.apply_filter(None);
                }
                ComposerMessage::EscPressed => self.handle_esc(),
                ComposerMessage::Mention(intent) => {
                    let mut ui = self.ui.borrow_mut();
                    handle_file_mention_intent(&mut *ui, &intent);
                }
                ComposerMessage::NavKey { delta } => {
                    let mut ui = self.ui.borrow_mut();
                    if ui.lanes_panel.display() {
                        ui.lanes_panel.move_selection(delta);
                    }
                }
                ComposerMessage::EnterEmpty => {
                    let msg = {
                        let ui = self.ui.borrow();
                        if ui.lanes_panel.display() {
                            ui.lanes_panel.focus_selected()
                        } else {
                            None
                        }
                    };
                    if let Some(LanesMsg::FocusLane { name, session_id }) = msg {
                        self.focus_lane(&name, &session_id);
                    }
                }
                ComposerMessage::CycleModeRequested => self.action_cycle_mode_impl(),
                ComposerMessage::PasteImage => {
                    // kernel/clipboard is an unported unit — honest no-image.
                    self.ui.borrow_mut().show_notice("no image in clipboard", None);
                }
            }
        }
        // Palette rows can also emit run/close messages (click paths);
        // drain them so state never wedges.
        let palette_msgs = { self.ui.borrow_mut().palette.take_messages() };
        for msg in palette_msgs {
            if let PaletteMessage::CommandRun(spec) = msg {
                {
                    let mut ui = self.ui.borrow_mut();
                    ui.composer.clear();
                    ui.palette.apply_filter(None);
                }
                self.run_registry_command(&spec.name);
            }
        }
    }

    /// Idle Enter (Python `on_composer_submit`).
    fn on_submit(&mut self, text: &str) {
        self.adapter.borrow_mut().record_prompt(text);
        let selected = {
            let mut ui = self.ui.borrow_mut();
            close_file_mentions(&mut *ui);
            let selected = if ui.palette.is_open() {
                ui.palette.selected_command().cloned()
            } else {
                None
            };
            ui.palette.apply_filter(None);
            selected
        };
        if text.starts_with('/') {
            if self.run_slash_command(text) {
                return;
            }
            if let Some(spec) = selected {
                self.run_registry_command(&spec.name);
                return;
            }
            let name = text.split_whitespace().next().unwrap_or(text).to_string();
            self.ui
                .borrow_mut()
                .show_notice(&format!("unknown command: {name} · / lists commands"), None);
            return;
        }
        self.submit_prompt(text);
    }

    /// Running Enter (Python `on_composer_steer`).
    ///
    /// The local SteeringQueue owns the ↳ echo/badge UX; for protocol
    /// sessions the steer ALSO goes over the wire (`steer` op) into
    /// RealRuntime's queue, where the StepBoundaryBridge consumes it at the
    /// next step boundary. The backend's `Applying steer: …` narration then
    /// drops the local echo (see `handle_wire`'s wire-driven consume).
    pub(crate) fn on_steer(&mut self, text: &str) {
        self.adapter.borrow_mut().record_prompt(text);
        let selected = {
            let mut ui = self.ui.borrow_mut();
            close_file_mentions(&mut *ui);
            if ui.palette.is_open() {
                ui.palette.selected_command().cloned()
            } else {
                None
            }
        };
        if let Some(spec) = selected {
            self.ui.borrow_mut().palette.apply_filter(None);
            if !self.run_slash_command(text) {
                self.run_registry_command(&spec.name);
            }
            return;
        }
        // A focused lane targets THAT delegate (issue #39).
        let focused = { self.ui.borrow().transcript.focused_lane().map(str::to_string) };
        if let Some(focused) = focused {
            if let Some(record) = self.reducer.lanes().get(&focused) {
                if record.lane.state != crate::model::lanes::LaneStateName::Done {
                    match self.lane_steering.enqueue(&record.session_id, text) {
                        Ok(_) => {
                            let name = record.lane.name.clone();
                            let mut ui = self.ui.borrow_mut();
                            ui.show_notice(&format!("steer queued for {name}"), None);
                            ui.lanes_dirty = true;
                            drop(ui);
                            self.sync_lanes_panel();
                        }
                        Err(error) => {
                            self.ui.borrow_mut().show_notice(&error.to_string(), None);
                        }
                    }
                    return;
                }
            }
        }
        if !self.steering.pending_steers().is_empty() {
            self.queue_message(text); // second steer queues (spec §5)
            return;
        }
        // Python `echo_steer`: queue the mid-turn steer and stamp its
        // ↳ echo block + the queue-chord notice (spec §5).
        match self.steering.enqueue(text, MessageKind::Steer) {
            Ok(queued) => {
                let id = self.allocator.borrow_mut().next_id();
                self.steer_echoes
                    .borrow_mut()
                    .insert(queued.message_id.clone(), id.clone());
                {
                    let mut ui = self.ui.borrow_mut();
                    let _ = ui
                        .transcript
                        .append(SteerEcho::new(id, text).into(), monotonic());
                    // Advertise the queue chord the terminal can actually
                    // deliver (README/§12: alt+enter is the legacy fallback).
                    let notice = if self.kitty_protocol {
                        STEER_NOTICE
                    } else {
                        STEER_NOTICE_LEGACY
                    };
                    ui.show_notice(notice, None);
                }
                // Wire delivery: the backend owns the actual injection
                // (protocol `steer` op; no-op for demo/base adapters, whose
                // runtime consumes the shared local queue in-process).
                self.adapter.borrow_mut().steer(text);
            }
            Err(error) => self.ui.borrow_mut().show_notice(&error.to_string(), None),
        }
    }

    /// Shift+Enter (Python `on_composer_queue_message`).
    fn on_queue_message(&mut self, text: &str) {
        self.adapter.borrow_mut().record_prompt(text);
        let selected = {
            let mut ui = self.ui.borrow_mut();
            close_file_mentions(&mut *ui);
            if ui.palette.is_open() {
                ui.palette.selected_command().cloned()
            } else {
                None
            }
        };
        if let Some(spec) = selected {
            self.ui.borrow_mut().palette.apply_filter(None);
            if !self.run_slash_command(text) {
                self.run_registry_command(&spec.name);
            }
            return;
        }
        let running = self.ui.borrow().turn_active;
        if !running {
            self.submit_prompt(text);
            return;
        }
        self.queue_message(text);
    }

    fn queue_message(&mut self, text: &str) {
        match self.steering.enqueue(text, MessageKind::NextTurn) {
            Ok(_) => {
                let mut ui = self.ui.borrow_mut();
                ui.queued_strip.show_queued(text);
                ui.show_notice(QUEUED_NOTICE, None);
            }
            Err(error) => {
                self.ui.borrow_mut().show_notice(&error.to_string(), None);
            }
        }
    }

    pub fn submit_prompt(&mut self, text: &str) {
        let splash = self.ui.borrow().splash.is_some();
        if splash {
            // Mid-boot submits keep the supervisor's words (Python parity).
            let mut ui = self.ui.borrow_mut();
            ui.composer.insert_text(text);
            ui.show_notice("session still starting · message kept in the composer", None);
            return;
        }
        self.adapter.borrow_mut().submit(text, &[]);
    }

    pub fn interrupt_turn(&mut self) {
        self.adapter.borrow_mut().interrupt();
    }

    // -- approvals ---------------------------------------------------------------

    fn resolve_approval(&mut self, ticket_id: &str, choice: &str) {
        let prompt = {
            let mut ui = self.ui.borrow_mut();
            let prompt = ui.approval.as_ref().map(|bar| bar.prompt.clone());
            ui.approval = None;
            prompt
        };
        if let Some(prompt) = prompt {
            let _ = self
                .journal
                .lock()
                .unwrap()
                .record_ask(&prompt, choice != "Deny", "");
        }
        self.adapter.borrow_mut().answer_approval(ticket_id, choice);
    }

    /// ctrl-y on the approval bar: park the live ticket WITHOUT answering
    /// it (deny-and-continue, ADR-0007 resolution 5).
    fn defer_approval(&mut self, ticket_id: &str) {
        let parked = {
            let mut ui = self.ui.borrow_mut();
            let bar = ui.approval.take();
            bar.map(|bar| (bar.prompt, bar.options))
        };
        let Some((prompt, options)) = parked else { return };
        let _ = ticket_id; // broker routing is backend-side over the wire
        let question = prompt.trim().to_string();
        if !question.is_empty() {
            let _ = self.needs_you.defer(
                &question,
                "deferred approval",
                DeferOptions {
                    choices: options,
                    action: question.clone(),
                    ..DeferOptions::default()
                },
            );
        }
        self.ui.borrow_mut().show_notice(
            "decision deferred to queue · answer later with ctrl-y",
            None,
        );
    }

    // -- actions -------------------------------------------------------------------

    fn action_cycle_mode_impl(&mut self) {
        let next = {
            let ui = self.ui.borrow();
            cycle_mode(Some(ui.mode.id.as_str()), 1)
        };
        self.ui.borrow_mut().set_mode_by_id(next.id.as_str(), true);
    }

    fn toggle_lanes(&mut self) {
        let open = self.ui.borrow().lanes_panel.display();
        if open {
            self.ui.borrow_mut().lanes_panel.hide_panel();
            return;
        }
        let records = self.reducer.lanes().lanes();
        let counts = self.lane_steering.counts();
        let mut ui = self.ui.borrow_mut();
        ui.lanes_panel.update_lanes(&records, None, Some(&counts));
        ui.lanes_panel.show_panel();
    }

    fn cycle_tail(&mut self) {
        let record = self.reducer.lanes_mut().cycle_tail_focus();
        match record {
            None => self
                .ui
                .borrow_mut()
                .show_notice("no running lanes to tail", None),
            Some(record) => {
                self.ui.borrow_mut().lanes_dirty = true;
                self.sync_lanes_panel();
                self.reducer.repaint_lane_tail();
                let name = record.lane.name;
                self.ui.borrow_mut().show_notice(&format!("tail · {name}"), None);
            }
        }
    }

    /// ctrl+g: expand/collapse thinking (issue #129).
    fn toggle_thinking(&mut self) {
        let toggled = {
            let mut ui = self.ui.borrow_mut();
            let target = ui
                .transcript
                .blocks()
                .into_iter()
                .rev()
                .find_map(|block| match block {
                    TranscriptBlock::Thinking(thinking) if !thinking.text.is_empty() => {
                        Some(thinking)
                    }
                    _ => None,
                });
            match target {
                Some(mut thinking) => {
                    thinking.expanded = !thinking.expanded;
                    let expanded = thinking.expanded;
                    let _ = ui.transcript.replace(thinking.into(), monotonic());
                    Some(expanded)
                }
                None => None,
            }
        };
        match toggled {
            Some(true) => self.ui.borrow_mut().show_notice("thinking · expanded", None),
            Some(false) => self.ui.borrow_mut().show_notice("thinking · collapsed", None),
            None => {
                let mut ui = self.ui.borrow_mut();
                let revealed = ui.live_tail.toggle_reveal(monotonic());
                let notice = if revealed {
                    "thinking · shown"
                } else {
                    "thinking · hidden"
                };
                ui.show_notice(notice, None);
            }
        }
    }

    fn show_needs_you(&mut self) {
        let pending = self.needs_you.pending();
        let block = app_support::needs_you_block(&pending, &mut self.allocator.borrow_mut());
        match block {
            None => self
                .ui
                .borrow_mut()
                .show_notice("no decisions waiting", None),
            Some(block) => {
                let mut ui = self.ui.borrow_mut();
                let _ = ui.transcript.append(block.into(), monotonic());
            }
        }
    }

    pub fn open_rewind_strip(&mut self, index: Option<usize>) {
        let checkpoints: Vec<Checkpoint> = self
            .reducer
            .ledger
            .checkpoints()
            .into_iter()
            .cloned()
            .collect();
        if checkpoints.is_empty() {
            self.ui
                .borrow_mut()
                .show_notice("no rewind checkpoints yet", None);
            return;
        }
        let mut ui = self.ui.borrow_mut();
        // The strip takes the keyboard (Python `strip.focus()`): a focused
        // evidence block hands it over.
        ui.focused_evidence = None;
        ui.rewind.show_checkpoints(&checkpoints, index);
    }

    /// Confirm-then-trim rewind (ADR-0007 §Rewind): the adapter confirms
    /// the fork, then the mirror + reducer ledger + transcript trim.
    fn handle_fork(&mut self, checkpoint_id: &str) {
        {
            *self.ledger.lock().unwrap() = self.reducer.ledger.clone();
        }
        let result = {
            let mut ledger = self.ledger.lock().unwrap();
            self.adapter.borrow_mut().fork(checkpoint_id, &mut ledger)
        };
        match result {
            Ok(()) => {
                self.reducer.ledger = self.ledger.lock().unwrap().clone();
                {
                    let mut ui = self.ui.borrow_mut();
                    app_support::trim_after_checkpoint(&mut ui.transcript, checkpoint_id);
                    ui.show_notice(&format!("forked at {checkpoint_id}"), None);
                }
                self.sync_rewind_checkpoints();
            }
            Err(error) => {
                self.ui.borrow_mut().show_notice(&error.to_string(), None);
            }
        }
    }

    fn focus_lane(&mut self, name: &str, session_id: &str) {
        let blocks = {
            let mut adapter = self.adapter.borrow_mut();
            adapter.lane_blocks(name, session_id, &mut self.allocator.borrow_mut())
        };
        let key = if session_id.is_empty() { name } else { session_id };
        let blocks = blocks.or_else(|| self.reducer.lane_transcript(key));
        let Some(blocks) = blocks else {
            self.ui
                .borrow_mut()
                .show_notice(&format!("no transcript for lane · {name}"), None);
            return;
        };
        let mut ui = self.ui.borrow_mut();
        ui.lanes_panel.set_focused(Some(name));
        let _ = ui.transcript.focus_lane(key, blocks, monotonic());
    }

    fn handle_esc(&mut self) {
        let action = {
            let mut ui = self.ui.borrow_mut();
            let flags = EscFlags {
                lane_focus: ui.transcript.focused_lane().is_some(),
                palette: ui.palette.filter_text().is_some(),
                rewind: ui.rewind.display(),
                lanes: ui.lanes_panel.display(),
                running: ui.turn_active,
            };
            resolve_esc(flags, &mut ui.esc, monotonic())
        };
        match action {
            Some(EscAction::LaneUnfocus) => {
                let mut ui = self.ui.borrow_mut();
                let _ = ui.transcript.restore_main(monotonic());
                ui.lanes_panel.set_focused(None);
                // Python `handle_lane_focus_change(lane_id=None)`: the
                // composer path shows the exact return notice (the
                // approval auto-return path shows its own instead).
                ui.show_notice("back to parent session", None);
            }
            Some(EscAction::ClosePalette) => {
                self.ui.borrow_mut().palette.apply_filter(None);
            }
            Some(EscAction::CloseRewind) => {
                let _ = self.ui.borrow_mut().rewind.close_strip();
            }
            Some(EscAction::CloseLanes) => {
                self.ui.borrow_mut().lanes_panel.hide_panel();
            }
            Some(EscAction::InterruptRunning) => self.interrupt_turn(),
            Some(EscAction::OpenRewind) => self.open_rewind_strip(None),
            None => {}
        }
    }

    // -- commands --------------------------------------------------------------

    /// Parse-and-run a slash command through the ONE registry; the ledger
    /// mirror syncs around dispatch (module docs).
    pub fn run_slash_command(&mut self, text: &str) -> bool {
        {
            *self.ledger.lock().unwrap() = self.reducer.ledger.clone();
        }
        let ran = {
            let ctx = AppCommandContext::new(self);
            self.commands.parse_and_run(&ctx, text)
        };
        self.reducer.ledger = self.ledger.lock().unwrap().clone();
        ran
    }

    fn run_registry_command(&mut self, name: &str) {
        {
            *self.ledger.lock().unwrap() = self.reducer.ledger.clone();
        }
        {
            let ctx = AppCommandContext::new(self);
            let _ = self.commands.run(name, &ctx, "");
        }
        self.reducer.ledger = self.ledger.lock().unwrap().clone();
    }

    // -- timers ------------------------------------------------------------------

    /// The app-loop heartbeat: reducer tick (working line / lane clocks),
    /// notice expiry, live-tail trailing paint, resize-reflow debounce,
    /// pending history compaction, splash/spinner/motion frames.
    pub fn tick(&mut self) {
        self.tick_at(monotonic());
    }

    /// [`App::tick`] against an explicit monotonic-domain clock — the whole
    /// heartbeat is deterministic under test. The loop tick fires faster
    /// than any single animation; each cadence gates itself here.
    pub fn tick_at(&mut self, now: f64) {
        // Working-line 1s heartbeat (Python ui/app.py:
        // `set_interval(1.0, lambda: self.reducer.tick(time.time()))`) —
        // ungated this ran at the 25ms loop tick, replacing the working
        // block (and resetting its shimmer to `motion_frame: 0`) 40× a
        // second while flickering the pulse glyph far off Python's cadence.
        if now - self.last_reducer_tick.get() >= crate::ui::transcript::SPINNER_INTERVAL_SECONDS {
            self.last_reducer_tick.set(now);
            self.reducer.tick(wall_now());
        }
        let mut ui = self.ui.borrow_mut();
        ui.notices.tick();
        if ui.live_tail_deadline.is_some_and(|deadline| now >= deadline) {
            ui.live_tail_deadline = None;
            ui.live_tail.fire_timer(now);
        }
        if ui.reflow_deadline.is_some_and(|deadline| now >= deadline) {
            ui.reflow_deadline = None;
            ui.transcript.debounce_fired(now);
        }
        // Copy-on-select settle (Python's 0.4s `_selection_timer`).
        let selection_settled = ui
            .selection_settle_deadline
            .is_some_and(|deadline| now >= deadline);
        if selection_settled {
            ui.selection_settle_deadline = None;
        }
        if ui.transcript.compaction_pending() {
            ui.transcript.compact_history();
        }
        if ui.title.running() && now - self.last_spinner_frame.get() >= crate::ui::chrome::SPINNER_INTERVAL {
            self.last_spinner_frame.set(now);
            let changed = ui.title.advance_spinner();
            ui.note_title(changed);
        }
        if ui.lanes_panel.motion_running()
            && now - self.last_motion_frame.get() >= crate::ui::lanes_panel::LANE_MOTION_INTERVAL_SECONDS
        {
            self.last_motion_frame.set(now);
            ui.lanes_panel.advance_motion();
        }
        // Working-label shimmer (Python: each working-status widget's own
        // `_motion_timer` at MOTION_INTERVAL_SECONDS) — without this the
        // band froze at whatever frame the last block replace carried.
        if now - self.last_working_motion.get()
            >= crate::ui::transcript::MOTION_INTERVAL_SECONDS
        {
            self.last_working_motion.set(now);
            ui.transcript.advance_working_motion(now);
        }
        let (width, height) = (ui.term_width as usize, ui.term_height as usize);
        if ui.splash.is_some() && now - self.last_splash_frame.get() >= crate::ui::splash::FRAME_SECONDS {
            self.last_splash_frame.set(now);
            if let Some(splash) = ui.splash.as_mut() {
                let _ = splash.advance(width, height.saturating_sub(4));
            }
        }
        drop(ui);
        if selection_settled {
            self.copy_settled_selection();
        }
        self.settle_after_event();
    }

    /// Backend boot chatter (the serve process's stderr): while the splash is
    /// up, show the latest line as the boot status so slow module loads are
    /// visible. FALLBACK only — once a structured `boot.progress` record has
    /// landed, the protocol phases own the status (chatter never overwrites).
    pub fn on_boot_chatter(&mut self, line: &str) {
        let mut ui = self.ui.borrow_mut();
        if ui.boot_progress_seen {
            return;
        }
        if let Some(splash) = ui.splash.as_mut() {
            let text: String = line.chars().take(72).collect();
            let _ = splash.set_status(&text);
        }
    }

    /// The footer's frozen paint state, derived per frame.
    pub fn footer_state(&self) -> FooterState {
        let ui = self.ui.borrow();
        let (plan_done, plan_total) =
            app_support::plan_footer_counts(&ui.plan_items, ui.plan_panel.display());
        let model = ui
            .model_name
            .rsplit('/')
            .next()
            .unwrap_or(&ui.model_name)
            .to_string();
        FooterState {
            mode_id: ui.mode.id,
            native_modes: ui.native_modes.clone(),
            bundle: ui.bundle.clone(),
            model,
            session_short: ui.session_short.clone(),
            cost: self.reducer.live_session_cost(),
            cost_estimated: self.reducer.live_cost_estimated(),
            shipped: self.reducer.ledger.last_shipped(),
            queued: self.steering.pending_next_turn().len() as u64,
            waiting: self.needs_you.pending_count() as u64,
            plan_done: plan_done as u64,
            plan_total: plan_total as u64,
            context: ui.footer_context(),
            kitty_protocol: self.kitty_protocol,
        }
    }

    pub fn should_quit(&self) -> bool {
        self.ui.borrow().should_quit
    }
}

// ---------------------------------------------------------------------------
// CommandHost — the commands ↔ app boundary (all members)
// ---------------------------------------------------------------------------

impl App {
    fn ops_starting(&self) -> bool {
        if self.ui.borrow().splash.is_some() {
            self.ui.borrow_mut().show_notice(
                "session still starting · try again once the banner lands",
                None,
            );
            return true;
        }
        false
    }
}

impl CommandHost for App {
    fn ledger(&self) -> &Mutex<OutcomeLedger> {
        &self.ledger
    }

    fn denial_log(&self) -> &Mutex<DenialLog> {
        &self.denial_log
    }

    fn steering(&self) -> &SteeringQueue {
        &self.steering
    }

    fn needs_you(&self) -> &NeedsYouQueue {
        &self.needs_you
    }

    fn session_cost(&self) -> Decimal {
        self.reducer.session_cost
    }

    fn session_short(&self) -> String {
        self.ui.borrow().session_short.clone()
    }

    fn bundle_name(&self) -> String {
        self.ui.borrow().bundle.clone()
    }

    fn next_block_id(&self) -> String {
        self.allocator.borrow_mut().next_id()
    }

    fn context_usage(&self) -> ContextUsage {
        let window = self.adapter.borrow_mut().compaction().max_tokens;
        let memory = (self.reducer.memory_tokens.max(0) as u64).min(window);
        let tools = (self.reducer.tool_tokens.max(0) as u64).min(window - memory);
        ContextUsage {
            conversation: (self.reducer.total_tokens.max(0) as u64)
                .min(window - memory - tools),
            tools,
            memory,
            window,
        }
    }

    fn journal(&self) -> &Mutex<ApprovalJournal> {
        &self.journal
    }

    fn transcript_blocks(&self) -> Vec<TranscriptBlock> {
        self.ui.borrow().transcript.blocks()
    }

    fn core_version(&self) -> String {
        self.core_version.borrow().clone()
    }

    fn echo_user_line(&self, text: &str) {
        let mut ui = self.ui.borrow_mut();
        let mode = ui.mode.id.as_str().to_string();
        let id = self.allocator.borrow_mut().next_id();
        let _ = ui.transcript.append(
            UserLine {
                mode,
                ..UserLine::new(id, text.to_string())
            }
            .into(),
            monotonic(),
        );
    }

    fn append_block(&self, block: TranscriptBlock) {
        let _ = self.ui.borrow_mut().transcript.append(block, monotonic());
    }

    fn show_notice(&self, text: &str) {
        self.ui.borrow_mut().show_notice(text, None);
    }

    fn action_cycle_mode(&self) {
        let next = {
            let ui = self.ui.borrow();
            cycle_mode(Some(ui.mode.id.as_str()), 1)
        };
        self.ui.borrow_mut().set_mode_by_id(next.id.as_str(), true);
    }

    fn set_mode_by_id(&self, mode_id: &str) {
        self.ui.borrow_mut().set_mode_by_id(mode_id, true);
    }

    fn set_theme_by_name(&self, name: &str) {
        self.ui.borrow_mut().set_theme_by_name(name);
    }

    fn action_toggle_lanes(&self) {
        // `/tasks` from a command handler (immutable receiver): panel data
        // was already synced by the last lanes_changed; toggle display.
        let mut ui = self.ui.borrow_mut();
        if ui.lanes_panel.display() {
            ui.lanes_panel.hide_panel();
        } else {
            ui.lanes_panel.show_panel();
        }
    }

    fn action_open_rewind(&self) {
        let checkpoints: Vec<Checkpoint> = self
            .reducer
            .ledger
            .checkpoints()
            .into_iter()
            .cloned()
            .collect();
        let mut ui = self.ui.borrow_mut();
        if checkpoints.is_empty() {
            ui.show_notice("no rewind checkpoints yet", None);
            return;
        }
        ui.rewind.show_checkpoints(&checkpoints, None);
    }

    fn open_permissions(&self) {
        let mut ui = self.ui.borrow_mut();
        let block = app_support::permissions_block(
            &ui.permissions,
            ui.mode.trust_str,
            &mut self.allocator.borrow_mut(),
        );
        let _ = ui.transcript.append(block.into(), monotonic());
    }

    fn manage_directories(&self, kind: &str, args: &str) {
        let mut host = AdminHost { app: self };
        directory_admin::manage(&mut host, kind, args);
    }

    fn exit(&self) {
        self.ui.borrow_mut().should_quit = true;
    }

    fn copy_to_clipboard(&self, text: &str) {
        // OS clipboard tool (pbcopy/wl-copy/xclip) — OSC 52 emission is a
        // terminal-writer concern the draw loop does not implement yet.
        let _ = app_support::os_clipboard_copy(text);
    }

    fn show_native_modes(&self) {
        if self.ops_starting() {
            return;
        }
        let catalog = self.adapter.borrow_mut().list_native_modes();
        let ui_width = self.ui.borrow().term_width as usize;
        let active = self.ui.borrow().native_modes.clone();
        let active_refs: Vec<&str> = active.iter().map(String::as_str).collect();
        let active_line = if active.is_empty() {
            String::new()
        } else {
            format!(" · active: {}", active.join(", "))
        };
        let mut spans = vec![
            Segment {
                style_token: StyleToken::Blue,
                ..Segment::new("· ")
            },
            Segment {
                style_token: StyleToken::Bright,
                bold: true,
                ..Segment::new("Modes")
            },
            Segment {
                style_token: StyleToken::Dim,
                ..Segment::new(format!(
                    "  postures: chat plan brainstorm build auto · shift+tab cycles · trust layer{active_line}\n"
                ))
            },
        ];
        let native = app_support::native_modes_segments(&catalog, ui_width, &active_refs);
        if native.is_empty() {
            spans.push(Segment {
                style_token: StyleToken::Dimmer,
                ..Segment::new("  no bundle-composed modes (demo or minimal session)")
            });
        } else {
            spans.extend(native);
        }
        let id = self.allocator.borrow_mut().next_id();
        let _ = self
            .ui
            .borrow_mut()
            .transcript
            .append(Answer::new(id, spans).into(), monotonic());
    }

    fn activate_native_mode(&self, name: Option<&str>) {
        match name {
            None => {
                let _ = self.adapter.borrow_mut().set_native_mode(None);
                let mut ui = self.ui.borrow_mut();
                ui.native_modes.clear();
                ui.show_notice("mode off · native (bundle)", None);
            }
            Some(name) => {
                let (ok, detail) = self.adapter.borrow_mut().set_native_mode(Some(name));
                let mut ui = self.ui.borrow_mut();
                if ok {
                    ui.native_modes.retain(|active| active != name);
                    ui.native_modes.push(name.to_string());
                    ui.show_notice(&format!("mode {name} · native (bundle)"), None);
                    let conflict =
                        posture_conflict_notice(ui.mode.id.as_str(), &ui.native_modes);
                    if !conflict.is_empty() {
                        ui.show_notice(&conflict, None);
                    }
                } else if detail.is_empty() {
                    ui.show_notice(&format!("no such mode · {name}"), None);
                } else {
                    ui.show_notice(&detail, None);
                }
            }
        }
    }

    fn deactivate_native_mode(&self, name: &str) {
        let active = self.ui.borrow().native_modes.clone();
        if !active.iter().any(|n| n == name) {
            self.ui
                .borrow_mut()
                .show_notice(&format!("mode not active · {name}"), None);
            return;
        }
        let remaining: Vec<String> = active.into_iter().filter(|n| n != name).collect();
        let primary = remaining.last().map(String::as_str);
        let (ok, detail) = self.adapter.borrow_mut().set_native_mode(primary);
        let mut ui = self.ui.borrow_mut();
        if ok {
            ui.native_modes = remaining;
            let tail = match ui.native_modes.last() {
                Some(promoted) => format!(" · now {promoted}"),
                None => String::new(),
            };
            ui.show_notice(&format!("mode -{name} · native (bundle){tail}"), None);
        } else if detail.is_empty() {
            ui.show_notice(&format!("could not deactivate · {name}"), None);
        } else {
            ui.show_notice(&detail, None);
        }
    }

    // -- in-session ops (SessionOpsController, issue #31) --------------------

    fn show_status(&self) {
        let host = self.ops_host();
        SessionOpsController::new(&host).show_status();
    }

    fn show_model(&self, arg: &str) {
        let host = self.ops_host();
        SessionOpsController::new(&host).show_model(arg);
    }

    fn apply_effort(&self, arg: &str) {
        let host = self.ops_host();
        SessionOpsController::new(&host).apply_effort(arg);
    }

    fn compact_context(&self, focus: &str) {
        let host = self.ops_host();
        SessionOpsController::new(&host).compact_context(focus);
    }

    fn clear_context(&self) {
        let host = self.ops_host();
        SessionOpsController::new(&host).clear_context();
    }

    fn show_tools(&self) {
        let host = self.ops_host();
        SessionOpsController::new(&host).show_tools();
    }

    fn show_agents(&self) {
        let host = self.ops_host();
        SessionOpsController::new(&host).show_agents();
    }

    fn show_diff(&self, arg: &str) {
        let host = self.ops_host();
        SessionOpsController::new(&host).show_diff(arg);
    }

    fn show_skills(&self) {
        let host = self.ops_host();
        SessionOpsController::new(&host).show_skills();
    }

    fn load_skill(&self, name: &str) {
        let host = self.ops_host();
        SessionOpsController::new(&host).load_skill(name);
    }

    fn manage_mcp(&self, args: &str) {
        let host = self.ops_host();
        SessionOpsController::new(&host).manage_mcp(args);
    }

    fn load_bundle(&self, args: &str) {
        let host = self.ops_host();
        SessionOpsController::new(&host).load_bundle(args);
    }

    fn manage_config(&self, args: &str) {
        let mut host = AdminHost { app: self };
        config_admin::manage(&mut host, args);
    }

    // -- stored-session lifecycle --------------------------------------------

    fn rename_session(&self, name: &str) {
        if name.trim().is_empty() {
            self.ui.borrow_mut().show_notice("usage: /rename <new name>", None);
            return;
        }
        if self.ops_starting() {
            return;
        }
        let (ok, detail) = self.adapter.borrow_mut().rename_session(name.trim());
        let notice = if ok {
            format!("session renamed · {detail}")
        } else {
            detail
        };
        self.ui.borrow_mut().show_notice(&notice, None);
    }

    fn show_sessions(&self) {
        let summaries = self.adapter.borrow_mut().session_summaries();
        let current = self.ui.borrow().session_short.clone();
        let id = self.allocator.borrow_mut().next_id();
        let spans = sessions_spans(&summaries, &current);
        let _ = self
            .ui
            .borrow_mut()
            .transcript
            .append(Answer::new(id, spans).into(), monotonic());
    }

    fn branch_session(&self, name: &str) {
        if self.ops_starting() {
            return;
        }
        let (ok, detail) = self.adapter.borrow_mut().branch_session(name.trim());
        let notice = if ok {
            let id: String = detail.chars().take(12).collect();
            let short: String = detail.chars().take(8).collect();
            format!("branch created · {id} · resume: amplifier-newtui resume {short}")
        } else {
            detail
        };
        self.ui.borrow_mut().show_notice(&notice, None);
    }

    fn fork_session(&self, directive: &str) {
        if directive.trim().is_empty() {
            self.ui.borrow_mut().show_notice("usage: /fork <directive>", None);
            return;
        }
        if self.ops_starting() {
            return;
        }
        let (ok, detail) = self
            .adapter
            .borrow_mut()
            .fork_with_directive(directive.trim());
        let notice = if ok {
            let id: String = detail.chars().take(12).collect();
            let short: String = detail.chars().take(8).collect();
            format!(
                "fork primed · {id} · resume runs the directive: amplifier-newtui resume {short}"
            )
        } else {
            detail
        };
        self.ui.borrow_mut().show_notice(&notice, None);
    }
}

// ---------------------------------------------------------------------------
// SessionOps / admin host views (per-call adapters over the RefCells)
// ---------------------------------------------------------------------------

struct OpsAdapterView<'a>(&'a RefCell<Box<dyn RuntimeAdapter>>);

impl SessionOpsAdapter for OpsAdapterView<'_> {
    fn bundle_name(&self) -> String {
        self.0.borrow_mut().bundle_name()
    }
    fn session_short(&self) -> String {
        self.0.borrow_mut().session_short()
    }
    fn compaction(&self) -> CompactionConfig {
        self.0.borrow_mut().compaction()
    }
    fn status(&self) -> StatusInfo {
        self.0.borrow_mut().status()
    }
    fn set_model(&self, model: &str) -> (bool, String) {
        self.0.borrow_mut().set_model(model)
    }
    fn list_models(&self) -> ModelListing {
        self.0.borrow_mut().list_models()
    }
    fn set_effort(&self, level: &str) -> (bool, String) {
        self.0.borrow_mut().set_effort(level)
    }
    fn get_effort(&self) -> Option<String> {
        self.0.borrow_mut().get_effort()
    }
    fn compact(&self, focus: &str) -> (bool, String) {
        self.0.borrow_mut().compact(focus)
    }
    fn clear_context(&self) -> (bool, u64) {
        self.0.borrow_mut().clear_context()
    }
    fn list_tools(&self) -> Vec<String> {
        self.0.borrow_mut().list_tools()
    }
    fn list_agents(&self) -> Vec<String> {
        self.0.borrow_mut().list_agents()
    }
    fn diff(&self, staged: bool) -> Option<String> {
        self.0.borrow_mut().diff(staged)
    }
    fn list_skills(&self) -> Vec<SkillInfo> {
        self.0.borrow_mut().list_skills()
    }
    fn load_skill(&self, name: &str) -> (bool, String) {
        self.0.borrow_mut().load_skill(name)
    }
    fn mcp_tools(&self) -> Vec<String> {
        self.0.borrow_mut().mcp_tools()
    }
    fn deferred_bundles(&self) -> Vec<String> {
        self.0.borrow_mut().deferred_bundles()
    }
    fn load_deferred_bundle(&self, name: &str) -> (bool, String) {
        self.0.borrow_mut().load_deferred_bundle(name)
    }
}

struct OpsHost<'a> {
    app: &'a App,
    adapter: OpsAdapterView<'a>,
}

impl App {
    fn ops_host(&self) -> OpsHost<'_> {
        OpsHost {
            app: self,
            adapter: OpsAdapterView(&self.adapter),
        }
    }
}

impl SessionOpsHost for OpsHost<'_> {
    fn adapter(&self) -> &dyn SessionOpsAdapter {
        &self.adapter
    }
    fn next_block_id(&self) -> String {
        self.app.allocator.borrow_mut().next_id()
    }
    fn mode_id(&self) -> String {
        self.app.ui.borrow().mode.id.as_str().to_string()
    }
    fn session_cost(&self) -> Decimal {
        self.app.reducer.session_cost
    }
    fn splash_active(&self) -> bool {
        self.app.ui.borrow().splash.is_some()
    }
    fn append_block(&self, block: TranscriptBlock) {
        let _ = self
            .app
            .ui
            .borrow_mut()
            .transcript
            .append(block, monotonic());
    }
    fn show_notice(&self, text: &str) {
        self.app.ui.borrow_mut().show_notice(text, None);
    }
    fn refresh_status(&self) {
        // The draw loop derives title/footer from state each frame.
    }
    // kernel.mcp_config is an unported server-side unit; the store is not
    // wired — listing is empty, mutations are honest no-ops.
    fn mcp_servers(&self) -> Vec<(String, String)> {
        Vec::new()
    }
    fn add_mcp_stdio_server(&self, _name: &str, _command: &str, _args: &[String]) {}
    fn remove_mcp_server(&self, _name: &str) -> bool {
        false
    }
}

/// `/config` + `/dirs` admin host (both traits share the same four app
/// touchpoints, so one view serves both).
struct AdminHost<'a> {
    app: &'a App,
}

impl ConfigAdminHost for AdminHost<'_> {
    fn config_view(&mut self) -> crate::model::config::ConfigSnapshotView {
        self.app.adapter.borrow_mut().config_view()
    }
    fn config_toggle(&mut self, category: &str, name: &str, enable: bool) -> (bool, String) {
        self.app.adapter.borrow_mut().config_toggle(category, name, enable)
    }
    fn config_set(&mut self, path: &str, value: &str) -> (bool, String) {
        self.app.adapter.borrow_mut().config_set(path, value)
    }
    fn config_diff(&mut self) -> Vec<crate::model::config::ConfigChange> {
        self.app.adapter.borrow_mut().config_diff()
    }
    fn config_save(&mut self, scope: &str) -> (bool, String) {
        self.app.adapter.borrow_mut().config_save(scope)
    }
    fn next_id(&mut self) -> String {
        self.app.allocator.borrow_mut().next_id()
    }
    fn append_block(&mut self, block: Answer) {
        let _ = self
            .app
            .ui
            .borrow_mut()
            .transcript
            .append(block.into(), monotonic());
    }
    fn show_notice(&mut self, text: &str, duration: Option<f64>) {
        self.app.ui.borrow_mut().show_notice(text, duration);
    }
}

impl DirectoryAdminHost for AdminHost<'_> {
    fn directory_entries(&mut self, kind: DirectoryKind) -> Vec<DirectoryEntry> {
        self.app.adapter.borrow_mut().directory_entries(kind)
    }
    fn update_directory(
        &mut self,
        kind: DirectoryKind,
        operation: &str,
        path: &str,
    ) -> (bool, String) {
        self.app
            .adapter
            .borrow_mut()
            .update_directory(kind, operation, path)
    }
    fn next_id(&mut self) -> String {
        self.app.allocator.borrow_mut().next_id()
    }
    fn append_block(&mut self, block: Answer) {
        let _ = self
            .app
            .ui
            .borrow_mut()
            .transcript
            .append(block.into(), monotonic());
    }
    fn show_notice(&mut self, text: &str, duration: Option<f64>) {
        self.app.ui.borrow_mut().show_notice(text, duration);
    }
}
