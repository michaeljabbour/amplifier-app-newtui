//! The transcript block grammar: every visible transcript element as data.
//!
//! This is the single vocabulary the transcript renderer understands
//! (DESIGN-SPEC §3). Blocks are frozen models — rendering is a pure
//! function of `(blocks, width, theme)`. Colors are referenced ONLY by
//! theme-token *name* ([`StyleToken`] fields naming DESIGN-SPEC §1 tokens);
//! hex values never appear in block state, so a runtime theme switch is a
//! repaint, not a rebuild (ADR-0007 resolution 11).
//!
//! # Stable IDs
//!
//! Every block carries a monotonic string `id` minted by [`BlockIdAllocator`]
//! (`"b1"`, `"b2"`, …). IDs are the contract for in-place mutation
//! (tool-line expand/collapse, live plan updates), click routing (turn rules →
//! rewind, answers → evidence) and rewind trimming — never reverse
//! string-matching on rendered text.
//!
//! # Discriminated union
//!
//! Each block declares a `kind` literal; [`TranscriptBlock`] is the
//! discriminated union over `kind` (serde internally tagged), so blocks
//! round-trip through JSON (ui-events.jsonl replay) losslessly and stay
//! wire-compatible with the pydantic dumps of the Python app.
//!
//! Port of `src/amplifier_app_newtui/model/blocks.py`.

use std::fmt;

use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

use crate::model::evidence::EvidenceLink;
use crate::model::turn::TurnTelemetry;

// Spec glyphs (DESIGN-SPEC §1) — renderers must use these exact characters.
pub const GLYPH_PROMPT: &str = "❯";
pub const GLYPH_BULLET: &str = "●";
pub const GLYPH_SPINNER_FRAMES: [&str; 4] = ["✳", "✦", "✧", "✦"];
pub const GLYPH_PLAN_DONE: &str = "✔";
pub const GLYPH_PLAN_ACTIVE: &str = "■";
pub const GLYPH_PLAN_PENDING: &str = "□";
pub const GLYPH_BLOCKED: &str = "⊘";
pub const GLYPH_LANE_RUNNING: &str = "◐";
pub const GLYPH_TREE_BRANCH: &str = "├─";
pub const GLYPH_TREE_END: &str = "└";
pub const GLYPH_STEER: &str = "↳";
pub const GLYPH_YIELD: &str = "▲";
pub const GLYPH_QUEUED: &str = "▹";
pub const GLYPH_REWIND_LEFT: &str = "‹";
pub const GLYPH_REWIND_RIGHT: &str = "›";
pub const GLYPH_ERROR: &str = "✖";
pub const GLYPH_CHEVRON_COLLAPSED: &str = "▸";
pub const GLYPH_CHEVRON_EXPANDED: &str = "▾";
/// Markdown task-list glyphs for `- [x]` / `- [ ]` items in answers.
/// Lighter cousins of PlanBlock's `✔`/`□` (they *rhyme*, not collide):
/// checked reads green, empty reads dim — the same done/pending grammar.
pub const GLYPH_CHECKBOX_CHECKED: &str = "✓";
pub const GLYPH_CHECKBOX_EMPTY: &str = "☐";
/// Blockquote left gutter in answers — the TUI-native frame for the
/// insight/machete callouts hooks-inline-blocks teaches the model to emit
/// (Rich draws the same `▌` edge for blockquotes in the line-mode CLI).
pub const GLYPH_QUOTE_GUTTER: &str = "▌ ";

/// Theme-token names a [`Segment`] may reference (DESIGN-SPEC §1 table rows).
///
/// Python `StyleToken = Literal["bg-page", …, "rule"]`; the serde names and
/// [`StyleToken::as_str`] values are those exact literal strings. Other units
/// (modes, lanes) import this as `crate::model::blocks::StyleToken`.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum StyleToken {
    #[serde(rename = "bg-page")]
    BgPage,
    #[serde(rename = "bg-term")]
    BgTerm,
    #[serde(rename = "bg-chrome")]
    BgChrome,
    #[serde(rename = "bg-tab")]
    BgTab,
    #[default]
    #[serde(rename = "fg")]
    Fg,
    #[serde(rename = "bright")]
    Bright,
    #[serde(rename = "dim")]
    Dim,
    #[serde(rename = "dimmer")]
    Dimmer,
    #[serde(rename = "green")]
    Green,
    #[serde(rename = "orange")]
    Orange,
    #[serde(rename = "red")]
    Red,
    #[serde(rename = "blue")]
    Blue,
    #[serde(rename = "teal")]
    Teal,
    #[serde(rename = "rule")]
    Rule,
}

impl StyleToken {
    /// The exact Python literal string values.
    pub fn as_str(self) -> &'static str {
        match self {
            StyleToken::BgPage => "bg-page",
            StyleToken::BgTerm => "bg-term",
            StyleToken::BgChrome => "bg-chrome",
            StyleToken::BgTab => "bg-tab",
            StyleToken::Fg => "fg",
            StyleToken::Bright => "bright",
            StyleToken::Dim => "dim",
            StyleToken::Dimmer => "dimmer",
            StyleToken::Green => "green",
            StyleToken::Orange => "orange",
            StyleToken::Red => "red",
            StyleToken::Blue => "blue",
            StyleToken::Teal => "teal",
            StyleToken::Rule => "rule",
        }
    }
}

impl fmt::Display for StyleToken {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// One styled run of text inside a rich block (e.g. an [`Answer`]).
///
/// `style_token`/`bg_token` name DESIGN-SPEC §1 tokens; the renderer maps
/// token name → theme variable at paint time. Inline code in answers is a
/// Segment with `style_token: StyleToken::Teal`.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Segment {
    pub text: String,
    #[serde(default)]
    pub style_token: StyleToken,
    #[serde(default)]
    pub bold: bool,
    #[serde(default)]
    pub italic: bool,
    #[serde(default)]
    pub bg_token: Option<StyleToken>,
    /// Target URL for an OSC 8 terminal hyperlink. When set, the segment
    /// paints as a real clickable link (Markdown `[text](url)` and bare
    /// `https://` URLs in answers); `None` for ordinary text.
    #[serde(default)]
    pub link: Option<String>,
}

impl Segment {
    /// Constructor defaults matching Python (`text` is the only required field).
    pub fn new(text: impl Into<String>) -> Self {
        Self {
            text: text.into(),
            style_token: StyleToken::Fg,
            bold: false,
            italic: false,
            bg_token: None,
            link: None,
        }
    }
}

/// Mints monotonic string block IDs (`b1`, `b2`, …).
///
/// One allocator per session transcript. Monotonicity gives stable
/// ordering keys for rewind trimming; string form keeps them JSON-safe.
#[derive(Debug)]
pub struct BlockIdAllocator {
    counter: u64,
}

impl BlockIdAllocator {
    /// Python default `start=1`.
    pub fn new() -> Self {
        Self::starting_at(1)
    }

    pub fn starting_at(start: u64) -> Self {
        Self { counter: start }
    }

    pub fn next_id(&mut self) -> String {
        let id = format!("b{}", self.counter);
        self.counter += 1;
        id
    }
}

impl Default for BlockIdAllocator {
    fn default() -> Self {
        Self::new()
    }
}

fn default_mode() -> String {
    "chat".to_string()
}

fn default_true() -> bool {
    true
}

fn default_interrupt_hint() -> String {
    "esc to interrupt".to_string()
}

fn default_steer_hint() -> String {
    "type to steer".to_string()
}

fn default_steer_note() -> String {
    "applies at next step boundary".to_string()
}

fn default_window_label() -> String {
    "200k".to_string()
}

fn default_bar_width() -> u32 {
    10
}

/// Session start banner (DESIGN-SPEC §11).
///
/// Line 1 (bright bold): `Amplifier <version> · core <core-version>`;
/// line 2 (dim): `Bundle: <bundle> | Provider: <provider> | <model> ·
/// session <id6>`. For a focused subagent, `focus_note` carries the
/// `focused: <name> · subagent of …` banner text instead.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SessionBanner {
    pub id: String,
    pub headline: String,
    #[serde(default)]
    pub detail: String,
    #[serde(default)]
    pub focus_note: String,
}

impl SessionBanner {
    pub fn new(id: impl Into<String>, headline: impl Into<String>) -> Self {
        Self {
            id: id.into(),
            headline: headline.into(),
            detail: String::new(),
            focus_note: String::new(),
        }
    }
}

/// User prompt echo: `❯ [mode] text` (DESIGN-SPEC §3).
///
/// The mode badge stamps scrollback permanently — `mode` is the mode id
/// at submit time (`chat`/`plan`/`brainstorm`/`build`/`auto`, or
/// `delegated` inside a focused subagent transcript).
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct UserLine {
    pub id: String,
    pub text: String,
    #[serde(default = "default_mode")]
    pub mode: String,
}

impl UserLine {
    pub fn new(id: impl Into<String>, text: impl Into<String>) -> Self {
        Self {
            id: id.into(),
            text: text.into(),
            mode: default_mode(),
        }
    }
}

/// Agent narration line: bright `● ` bullet + fg text.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Narration {
    pub id: String,
    pub text: String,
}

impl Narration {
    pub fn new(id: impl Into<String>, text: impl Into<String>) -> Self {
        Self {
            id: id.into(),
            text: text.into(),
        }
    }
}

/// Python `ToolLineStatus = Literal["running", "completed", "failed", "blocked"]`.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ToolLineStatus {
    #[default]
    Running,
    Completed,
    Failed,
    Blocked,
}

impl ToolLineStatus {
    /// The exact Python literal string values.
    pub fn as_str(self) -> &'static str {
        match self {
            ToolLineStatus::Running => "running",
            ToolLineStatus::Completed => "completed",
            ToolLineStatus::Failed => "failed",
            ToolLineStatus::Blocked => "blocked",
        }
    }
}

/// Python `ToolLineBodyStyle = Literal["plain", "diff"]`.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ToolLineBodyStyle {
    #[default]
    Plain,
    /// `diff` gives expanded +/-/@@ lines theme-aware patch styling.
    Diff,
}

impl ToolLineBodyStyle {
    /// The exact Python literal string values.
    pub fn as_str(self) -> &'static str {
        match self {
            ToolLineBodyStyle::Plain => "plain",
            ToolLineBodyStyle::Diff => "diff",
        }
    }
}

/// Collapsed/expandable tool activity line (DESIGN-SPEC §3).
///
/// Collapsed: `  ● <summary>` dim + `· click to expand` dimmer.
/// `expanded: true` shows the indented dimmer `body` lines below.
/// One ToolLine may summarize a whole batch (`Ran 2 shell commands`);
/// `tool_call_ids` keeps the correlation keys for evidence links.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ToolLine {
    pub id: String,
    pub summary: String,
    #[serde(default)]
    pub body: Vec<String>,
    #[serde(default)]
    pub expanded: bool,
    #[serde(default)]
    pub status: ToolLineStatus,
    #[serde(default)]
    pub tool_call_ids: Vec<String>,
    #[serde(default)]
    pub body_style: ToolLineBodyStyle,
}

impl ToolLine {
    pub fn new(id: impl Into<String>, summary: impl Into<String>) -> Self {
        Self {
            id: id.into(),
            summary: summary.into(),
            body: Vec::new(),
            expanded: false,
            status: ToolLineStatus::Running,
            tool_call_ids: Vec::new(),
            body_style: ToolLineBodyStyle::Plain,
        }
    }
}

/// Live executing command: `  └ ` dimmer + `$ <cmd>` dim.
///
/// Rendered only while executing; replaced by the collapsed [`ToolLine`]
/// when the command completes (same transcript slot, new block id not
/// needed — the ToolLine's id takes over).
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LiveCommand {
    pub id: String,
    pub command: String,
}

impl LiveCommand {
    pub fn new(id: impl Into<String>, command: impl Into<String>) -> Self {
        Self {
            id: id.into(),
            command: command.into(),
        }
    }
}

/// Python `PlanItemState = Literal["pending", "active", "done"]`.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum PlanItemState {
    #[default]
    Pending,
    Active,
    Done,
}

impl PlanItemState {
    /// The exact Python literal string values.
    pub fn as_str(self) -> &'static str {
        match self {
            PlanItemState::Pending => "pending",
            PlanItemState::Active => "active",
            PlanItemState::Done => "done",
        }
    }
}

/// One plan checklist row: `□` pending / `■` active / `✔` done.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PlanItem {
    pub text: String,
    #[serde(default)]
    pub state: PlanItemState,
}

impl PlanItem {
    pub fn new(text: impl Into<String>) -> Self {
        Self {
            text: text.into(),
            state: PlanItemState::Pending,
        }
    }
}

/// Plan checklist: `· ` orange header + trailing live dim telemetry.
///
/// `read_only: true` marks a plan produced in plan mode — the header is
/// suffixed `(read-only)` and the recap offers the build handoff
/// (DESIGN-SPEC §4).
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PlanBlock {
    pub id: String,
    pub title: String,
    #[serde(default, with = "wire::telemetry_option")]
    pub telemetry: Option<TurnTelemetry>,
    #[serde(default)]
    pub items: Vec<PlanItem>,
    #[serde(default)]
    pub read_only: bool,
}

impl PlanBlock {
    pub fn new(id: impl Into<String>, title: impl Into<String>) -> Self {
        Self {
            id: id.into(),
            title: title.into(),
            telemetry: None,
            items: Vec::new(),
            read_only: false,
        }
    }
}

/// Python `TodoStatus = Literal["pending", "in_progress", "completed"]`.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TodoStatus {
    #[default]
    Pending,
    InProgress,
    Completed,
}

impl TodoStatus {
    /// The exact Python literal string values.
    pub fn as_str(self) -> &'static str {
        match self {
            TodoStatus::Pending => "pending",
            TodoStatus::InProgress => "in_progress",
            TodoStatus::Completed => "completed",
        }
    }
}

/// One row of the `todo` tool's list, rendered by the ambient plan panel
/// (`ui/plan_panel.py`): `○` pending / `▶` in-progress / `✔` completed.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TodoItem {
    pub content: String,
    #[serde(default)]
    pub status: TodoStatus,
}

impl TodoItem {
    pub fn new(content: impl Into<String>) -> Self {
        Self {
            content: content.into(),
            status: TodoStatus::Pending,
        }
    }
}

/// Python `DelegateState = Literal["running", "done", "error", "cancelled"]`.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DelegateState {
    #[default]
    Running,
    Done,
    Error,
    Cancelled,
}

impl DelegateState {
    /// The exact Python literal string values.
    pub fn as_str(self) -> &'static str {
        match self {
            DelegateState::Running => "running",
            DelegateState::Done => "done",
            DelegateState::Error => "error",
            DelegateState::Cancelled => "cancelled",
        }
    }
}

/// One agent row inside a [`DelegateSummaryBlock`].
///
/// `state` maps to a glyph: `✔` done / `✖` error / `⊘` cancelled /
/// `◐` running. `snippet` is the agent's short result summary
/// (`AgentCompleted.result`), truncated by the renderer to fit the width.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DelegateEntry {
    pub agent: String,
    #[serde(default)]
    pub state: DelegateState,
    #[serde(default)]
    pub elapsed_s: f64,
    #[serde(default)]
    pub snippet: String,
}

impl DelegateEntry {
    pub fn new(agent: impl Into<String>) -> Self {
        Self {
            agent: agent.into(),
            state: DelegateState::Running,
            elapsed_s: 0.0,
            snippet: String::new(),
        }
    }
}

/// One durable, expandable summary per delegate fan-out (ambient-progress D5).
///
/// Replaces the per-agent tree-line Answer rows. Lives in the transcript as
/// a single line while running (`● N delegates running…`) and collapses at
/// fan-out end to `● Used N delegates · Plan X/Y · MmSSs ▸`. `expanded`
/// is UI-toggled (click/Enter) — the reducer always writes it false; see the
/// ToolLine-digest precedent for why a mid-flight replace may collapse it.
/// `plan_final` folds the turn's final todo state into the durable block
/// (design D3); `None` means "no plan this turn" and the header omits the
/// `Plan X/Y` segment.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DelegateSummaryBlock {
    pub id: String,
    #[serde(default)]
    pub entries: Vec<DelegateEntry>,
    #[serde(default)]
    pub plan_final: Option<Vec<TodoItem>>,
    #[serde(default)]
    pub duration_s: f64,
    #[serde(default)]
    pub expanded: bool,
}

impl DelegateSummaryBlock {
    pub fn new(id: impl Into<String>) -> Self {
        Self {
            id: id.into(),
            entries: Vec::new(),
            plan_final: None,
            duration_s: 0.0,
            expanded: false,
        }
    }
}

/// Deny-and-continue marker: `  ⊘ blocked · <cmd>` red + dim tail.
///
/// Never halts the turn by itself (DESIGN-SPEC §3/§7): `continuation`
/// says what the agent does instead (`continuing without <thing>`).
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Blocked {
    pub id: String,
    pub cmd: String,
    pub reason: String,
    #[serde(default)]
    pub continuation: String,
}

impl Blocked {
    pub fn new(id: impl Into<String>, cmd: impl Into<String>, reason: impl Into<String>) -> Self {
        Self {
            id: id.into(),
            cmd: cmd.into(),
            reason: reason.into(),
            continuation: String::new(),
        }
    }
}

/// One row of the live activity tree beneath the working pulse.
///
/// `running: true` is the in-flight op (brighter, `●`); completed ops
/// are dim. The reducer keeps a small bounded ring of the most recent
/// branches so the supervisor feels the action without the transcript
/// accumulating a durable line per tool (DESIGN-SPEC §3).
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ActivityBranch {
    pub text: String,
    #[serde(default)]
    pub running: bool,
}

impl ActivityBranch {
    pub fn new(text: impl Into<String>) -> Self {
        Self {
            text: text.into(),
            running: false,
        }
    }
}

/// Pulsing working line shown while a turn runs (DESIGN-SPEC §3).
///
/// `✳/✦/✧` orange spinner + `working · Ns · ↓ X.Xk tok · ` dim +
/// `esc to interrupt · type to steer` dimmer, with a bounded live
/// activity tree of recent ops rendered as `└`/`├` branches beneath.
/// A fan-out turn (`agent_count > 1`) renders `Coordinating N agents ·
/// Ns · ↓ X.Xk tok · ` dim + `esc to interrupt` dimmer instead (mockup
/// runAgentsTurn). Updated every second via the live tail; removed at
/// turn end (never persisted to history).
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct WorkingStatus {
    pub id: String,
    #[serde(with = "wire::telemetry")]
    pub telemetry: TurnTelemetry,
    #[serde(default)]
    pub agent_count: u32,
    /// Legacy single-op note (kept for compatibility); the live tree in
    /// `activity_lines` is the primary activity surface now.
    #[serde(default)]
    pub activity: String,
    /// Bounded live tree of recent ops (newest last) — single-agent turns.
    #[serde(default)]
    pub activity_lines: Vec<ActivityBranch>,
    #[serde(default = "default_interrupt_hint")]
    pub interrupt_hint: String,
    #[serde(default = "default_steer_hint")]
    pub steer_hint: String,
    #[serde(default)]
    pub spinner_frame: u32,
    /// Fast, presentation-only phase for the subtle label shimmer.
    #[serde(default)]
    pub motion_frame: u32,
}

impl WorkingStatus {
    pub fn new(id: impl Into<String>, telemetry: TurnTelemetry) -> Self {
        Self {
            id: id.into(),
            telemetry,
            agent_count: 0,
            activity: String::new(),
            activity_lines: Vec::new(),
            interrupt_hint: default_interrupt_hint(),
            steer_hint: default_steer_hint(),
            spinner_frame: 0,
            motion_frame: 0,
        }
    }
}

/// Turn-end recap: `✳ ` dimmer + italic dim `Goal: …. Next: ….`
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Recap {
    pub id: String,
    pub goal: String,
    pub next: String,
}

impl Recap {
    pub fn new(id: impl Into<String>, goal: impl Into<String>, next: impl Into<String>) -> Self {
        Self {
            id: id.into(),
            goal: goal.into(),
            next: next.into(),
        }
    }
}

/// Collapsible model-thinking block, rendered inline in the transcript
/// where the model reasoned — before the answer (issue #129).
///
/// Thinking is durable scrollback, not the ephemeral live-tail strip: it
/// lands where the model thought so a supervisor can reopen the reasoning
/// long after the turn ends. Default `expanded: false` (Claude-Code
/// style): collapsed shows one dim summary line, click / `ctrl-g`
/// expands it to the reasoning prose.
///
/// `text` holds the reasoning. Core may withhold it — its `ThinkingBlock`
/// carries a `visibility` enum (`ALL`/`LLM_ONLY`/`USER_ONLY`) and only
/// surfaces the prose to the UI when policy allows — in which case the
/// `content_block:end` payload arrives with empty text. The block then
/// degrades honestly to a single "content withheld by provider" line that
/// never expands, rather than rendering nothing.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Thinking {
    pub id: String,
    #[serde(default)]
    pub text: String,
    #[serde(default)]
    pub expanded: bool,
}

impl Thinking {
    pub fn new(id: impl Into<String>) -> Self {
        Self {
            id: id.into(),
            text: String::new(),
            expanded: false,
        }
    }
}

/// Final answer text: styled spans with teal inline code.
///
/// `spans` carry selective bright/bold and teal code runs; a click on
/// the answer opens the evidence block for `evidence_refs`
/// (DESIGN-SPEC §10).
///
/// `clickable` is false for answer-shaped lines the mockup creates
/// with `click: null` (agent tree lines, non-Goal/Next ✳ recap
/// lines) — only true final answers are evidence click targets.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Answer {
    pub id: String,
    pub spans: Vec<Segment>,
    #[serde(default)]
    pub evidence_refs: Vec<EvidenceLink>,
    #[serde(default = "default_true")]
    pub clickable: bool,
    /// Suppress paragraph spacing for structural rows such as agent trees.
    #[serde(default)]
    pub compact: bool,
}

impl Answer {
    pub fn new(id: impl Into<String>, spans: Vec<Segment>) -> Self {
        Self {
            id: id.into(),
            spans,
            evidence_refs: Vec::new(),
            clickable: true,
            compact: false,
        }
    }
}

/// Steer acknowledgement: `  ↳ steer queued: "<text>"` teal +
/// `· applies at next step boundary` dimmer.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SteerEcho {
    pub id: String,
    pub text: String,
    #[serde(default = "default_steer_note")]
    pub note: String,
}

impl SteerEcho {
    pub fn new(id: impl Into<String>, text: impl Into<String>) -> Self {
        Self {
            id: id.into(),
            text: text.into(),
            note: default_steer_note(),
        }
    }
}

/// Turn separator rule + right-aligned telemetry label (DESIGN-SPEC §3).
///
/// Label: `<Ns> · <X.Xk> tok, <N>% cached · $<cost> · <outcome>` — dim
/// when `shipped`, dimmer otherwise. Carries the checkpoint id stamped
/// at emit time so a click opens the rewind picker at this exact
/// checkpoint (never reverse string matching).
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TurnRule {
    pub id: String,
    pub checkpoint_id: String,
    pub label: String,
    #[serde(default)]
    pub shipped: bool,
}

impl TurnRule {
    pub fn new(
        id: impl Into<String>,
        checkpoint_id: impl Into<String>,
        label: impl Into<String>,
    ) -> Self {
        Self {
            id: id.into(),
            checkpoint_id: checkpoint_id.into(),
            label: label.into(),
            shipped: false,
        }
    }
}

/// Evidence panel printed on answer click (DESIGN-SPEC §10).
///
/// Header `· Evidence  1/N · ←/→ select · enter expand · esc close` +
/// numbered teal claims `¹ "quote" → <tool call>`. `selected` is the
/// 0-based highlighted claim index.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EvidenceBlock {
    pub id: String,
    pub links: Vec<EvidenceLink>,
    #[serde(default)]
    pub selected: usize,
}

impl EvidenceBlock {
    pub fn new(id: impl Into<String>, links: Vec<EvidenceLink>) -> Self {
        Self {
            id: id.into(),
            links,
            selected: 0,
        }
    }
}

/// Session ledger scrollback print (DESIGN-SPEC §10).
///
/// `· Session ledger  <session> · <bundle>` +
/// `  N turns · $X.XX · N shipped · N answer-only · cache hit NN%`.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LedgerBlock {
    pub id: String,
    pub session: String,
    pub bundle: String,
    pub turns: u64,
    #[serde(with = "wire::decimal")]
    pub spend: Decimal,
    pub shipped: u64,
    pub answer_only: u64,
    pub cache_hit_pct: u8,
}

/// `/context` usage print: `· Context  NN% of 200k` + usage bar.
///
/// `segments` are (label, cells) pairs for the `████████░░` bar in
/// order conversation/tools/memory/free; cells sum to `bar_width`.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ContextBlock {
    pub id: String,
    pub used_pct: u8,
    #[serde(default = "default_window_label")]
    pub window_label: String,
    #[serde(default)]
    pub segments: Vec<(String, u32)>,
    #[serde(default = "default_bar_width")]
    pub bar_width: u32,
}

impl ContextBlock {
    pub fn new(id: impl Into<String>, used_pct: u8) -> Self {
        Self {
            id: id.into(),
            used_pct,
            window_label: default_window_label(),
            segments: Vec::new(),
            bar_width: default_bar_width(),
        }
    }
}

/// One actionable chip on a needs-you decision, e.g. `yes · push to fork`.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NeedsYouChoice {
    pub label: String,
    pub answer: String,
}

impl NeedsYouChoice {
    pub fn new(label: impl Into<String>, answer: impl Into<String>) -> Self {
        Self {
            label: label.into(),
            answer: answer.into(),
        }
    }
}

/// One numbered deferred decision rendered inside a [`NeedsYouBlock`].
///
/// (Named `Entry` to avoid colliding with the queue-side
/// `crate::model::queues` NeedsYouItem.)
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NeedsYouEntry {
    pub decision_id: String,
    pub question: String,
    #[serde(default)]
    pub reason: String,
    #[serde(default)]
    pub choices: Vec<NeedsYouChoice>,
    /// Substring of `question` rendered teal (mockup: `mj/waypoint`).
    #[serde(default)]
    pub highlight: String,
}

impl NeedsYouEntry {
    pub fn new(decision_id: impl Into<String>, question: impl Into<String>) -> Self {
        Self {
            decision_id: decision_id.into(),
            question: question.into(),
            reason: String::new(),
            choices: Vec::new(),
            highlight: String::new(),
        }
    }
}

/// `Needs you  N deferred decision` orange block (DESIGN-SPEC §7).
///
/// Lists numbered decisions with inline actionable choice chips; acting
/// on one logs `Applying decision: …` narration and clears the footer
/// badge.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct NeedsYouBlock {
    pub id: String,
    pub items: Vec<NeedsYouEntry>,
}

impl NeedsYouBlock {
    pub fn new(id: impl Into<String>, items: Vec<NeedsYouEntry>) -> Self {
        Self {
            id: id.into(),
            items,
        }
    }
}

/// One numbered orange finding from `/doctor`.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DoctorFinding {
    pub number: u32,
    pub text: String,
}

impl DoctorFinding {
    pub fn new(number: u32, text: impl Into<String>) -> Self {
        Self {
            number,
            text: text.into(),
        }
    }
}

/// `/doctor` checkup: `· Doctor  <headline>` header + `✔` green
/// healthy lines + numbered findings (orange number, dim text).
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DoctorBlock {
    pub id: String,
    #[serde(default)]
    pub headline: String,
    #[serde(default)]
    pub healthy: Vec<String>,
    #[serde(default)]
    pub findings: Vec<DoctorFinding>,
}

impl DoctorBlock {
    pub fn new(id: impl Into<String>) -> Self {
        Self {
            id: id.into(),
            headline: String::new(),
            healthy: Vec::new(),
            findings: Vec::new(),
        }
    }
}

/// One `/improve` proposal derived from the ledger + denial log.
///
/// `action` (when set) is the concrete command named once in green
/// after the dim `title` prefix (mockup: `allowlist: ` +
/// `uv run pytest` green + rationale); rows without an action render
/// as one dim run `<title> <rationale>`.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ImproveProposal {
    pub title: String,
    pub rationale: String,
    #[serde(default)]
    pub action: String,
}

impl ImproveProposal {
    pub fn new(title: impl Into<String>, rationale: impl Into<String>) -> Self {
        Self {
            title: title.into(),
            rationale: rationale.into(),
            action: String::new(),
        }
    }
}

/// `/improve` proposals block — proposals only, never applied silently.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ImproveBlock {
    pub id: String,
    #[serde(default)]
    pub proposals: Vec<ImproveProposal>,
}

impl ImproveBlock {
    pub fn new(id: impl Into<String>) -> Self {
        Self {
            id: id.into(),
            proposals: Vec::new(),
        }
    }
}

/// One divergent idea line emitted in brainstorm mode.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BrainstormIdea {
    pub id: String,
    pub text: String,
    #[serde(default)]
    pub number: u32,
}

impl BrainstormIdea {
    pub fn new(id: impl Into<String>, text: impl Into<String>) -> Self {
        Self {
            id: id.into(),
            text: text.into(),
            number: 0,
        }
    }
}

macro_rules! transcript_block_union {
    ($( $kind:literal => $variant:ident($ty:ty), )*) => {
        /// Discriminated union of every transcript block (discriminates on `kind`).
        ///
        /// Python: pydantic `Annotated[… | …, Field(discriminator="kind")]`.
        /// The serde tag names are the exact Python `kind` literals, so JSON
        /// produced by either side round-trips through the other.
        #[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
        #[serde(tag = "kind")]
        pub enum TranscriptBlock {
            $( #[serde(rename = $kind)] $variant($ty), )*
        }

        impl TranscriptBlock {
            /// The block's `kind` literal (the union discriminator).
            pub fn kind(&self) -> &'static str {
                match self {
                    $( TranscriptBlock::$variant(_) => $kind, )*
                }
            }

            /// The stable block id every union member carries.
            pub fn id(&self) -> &str {
                match self {
                    $( TranscriptBlock::$variant(block) => &block.id, )*
                }
            }
        }

        $(
            impl From<$ty> for TranscriptBlock {
                fn from(block: $ty) -> Self {
                    TranscriptBlock::$variant(block)
                }
            }
        )*
    };
}

transcript_block_union! {
    "session_banner" => SessionBanner(SessionBanner),
    "user_line" => UserLine(UserLine),
    "narration" => Narration(Narration),
    "tool_line" => ToolLine(ToolLine),
    "live_command" => LiveCommand(LiveCommand),
    "plan" => Plan(PlanBlock),
    "blocked" => Blocked(Blocked),
    "working_status" => WorkingStatus(WorkingStatus),
    "recap" => Recap(Recap),
    "thinking" => Thinking(Thinking),
    "answer" => Answer(Answer),
    "steer_echo" => SteerEcho(SteerEcho),
    "turn_rule" => TurnRule(TurnRule),
    "evidence" => Evidence(EvidenceBlock),
    "ledger" => Ledger(LedgerBlock),
    "context" => Context(ContextBlock),
    "needs_you" => NeedsYou(NeedsYouBlock),
    "doctor" => Doctor(DoctorBlock),
    "improve" => Improve(ImproveBlock),
    "brainstorm_idea" => BrainstormIdea(BrainstormIdea),
    "delegate_summary" => DelegateSummary(DelegateSummaryBlock),
}

/// serde adapters for field types that don't derive serde themselves
/// (`TurnTelemetry`, `Decimal`), matching pydantic's JSON shapes exactly:
/// `Decimal` serializes as a string (`"0.12"`), telemetry as an object with
/// the Python field names and defaults.
mod wire {
    use rust_decimal::Decimal;
    use serde::{Deserialize, Serialize};

    use crate::model::turn::TurnTelemetry;

    pub mod decimal {
        use rust_decimal::Decimal;
        use serde::de::Error as _;
        use serde::{Deserialize, Deserializer, Serializer};

        /// Accepts pydantic's string form and bare JSON numbers alike
        /// (pydantic validates both on the way in).
        #[derive(Deserialize)]
        #[serde(untagged)]
        enum DecimalWire {
            Text(String),
            Number(serde_json::Number),
        }

        pub fn serialize<S: Serializer>(value: &Decimal, serializer: S) -> Result<S::Ok, S::Error> {
            serializer.serialize_str(&value.to_string())
        }

        pub fn deserialize<'de, D: Deserializer<'de>>(deserializer: D) -> Result<Decimal, D::Error> {
            let text = match DecimalWire::deserialize(deserializer)? {
                DecimalWire::Text(text) => text,
                DecimalWire::Number(number) => number.to_string(),
            };
            text.parse().map_err(D::Error::custom)
        }
    }

    /// Wire twin of [`TurnTelemetry`] with the Python field names, defaults
    /// and `extra="forbid"` semantics.
    #[derive(Serialize, Deserialize)]
    #[serde(deny_unknown_fields)]
    struct TelemetryWire {
        secs: f64,
        #[serde(default)]
        tokens_down: u64,
        #[serde(default)]
        cached_pct: Option<u8>,
        #[serde(default, with = "decimal")]
        cost: Decimal,
        #[serde(default)]
        estimated: bool,
    }

    impl From<TelemetryWire> for TurnTelemetry {
        fn from(wire: TelemetryWire) -> Self {
            TurnTelemetry {
                secs: wire.secs,
                tokens_down: wire.tokens_down,
                cached_pct: wire.cached_pct,
                cost: wire.cost,
                estimated: wire.estimated,
            }
        }
    }

    impl From<&TurnTelemetry> for TelemetryWire {
        fn from(telemetry: &TurnTelemetry) -> Self {
            TelemetryWire {
                secs: telemetry.secs,
                tokens_down: telemetry.tokens_down,
                cached_pct: telemetry.cached_pct,
                cost: telemetry.cost,
                estimated: telemetry.estimated,
            }
        }
    }

    pub mod telemetry {
        use serde::{Deserialize, Deserializer, Serialize, Serializer};

        use super::TelemetryWire;
        use crate::model::turn::TurnTelemetry;

        pub fn serialize<S: Serializer>(
            value: &TurnTelemetry,
            serializer: S,
        ) -> Result<S::Ok, S::Error> {
            TelemetryWire::from(value).serialize(serializer)
        }

        pub fn deserialize<'de, D: Deserializer<'de>>(
            deserializer: D,
        ) -> Result<TurnTelemetry, D::Error> {
            TelemetryWire::deserialize(deserializer).map(Into::into)
        }
    }

    pub mod telemetry_option {
        use serde::{Deserialize, Deserializer, Serializer};

        use super::TelemetryWire;
        use crate::model::turn::TurnTelemetry;

        pub fn serialize<S: Serializer>(
            value: &Option<TurnTelemetry>,
            serializer: S,
        ) -> Result<S::Ok, S::Error> {
            match value {
                Some(telemetry) => serializer.serialize_some(&TelemetryWire::from(telemetry)),
                None => serializer.serialize_none(),
            }
        }

        pub fn deserialize<'de, D: Deserializer<'de>>(
            deserializer: D,
        ) -> Result<Option<TurnTelemetry>, D::Error> {
            Ok(Option::<TelemetryWire>::deserialize(deserializer)?.map(Into::into))
        }
    }
}

#[cfg(test)]
mod tests {
    //! Pins `tests/test_model_blocks.py`. Each test is named after the
    //! Python case it ports.
    //!
    //! Not ported (with reasons):
    //! - `test_blocks_are_frozen` — pydantic's runtime `frozen=True` guard is
    //!   Rust's compile-time immutability (no `&mut` → no mutation); there is
    //!   no runtime mutation attempt to assert on. The immutable-copy update
    //!   idiom it protects is pinned by
    //!   `test_tool_line_expand_toggle_via_copy`.

    use std::collections::HashSet;

    use super::*;

    fn dec(value: &str) -> Decimal {
        value.parse().expect("valid decimal literal")
    }

    fn roundtrip(block: &TranscriptBlock) -> TranscriptBlock {
        let json = serde_json::to_string(block).expect("block serializes");
        serde_json::from_str(&json).expect("block deserializes")
    }

    #[test]
    fn test_block_id_allocator_is_monotonic() {
        let mut ids = BlockIdAllocator::new();
        let minted: Vec<String> = (0..3).map(|_| ids.next_id()).collect();
        assert_eq!(minted, ["b1", "b2", "b3"]);
    }

    /// Every block in the union carries id + kind and JSON round-trips.
    #[test]
    fn test_every_block_kind_has_stable_id_and_roundtrips() {
        let telemetry = TurnTelemetry {
            tokens_down: 3200,
            cached_pct: Some(80),
            cost: dec("0.12"),
            ..TurnTelemetry::new(4.0)
        };
        let blocks: Vec<TranscriptBlock> = vec![
            SessionBanner {
                detail: "Bundle: dev".to_string(),
                ..SessionBanner::new("b1", "Amplifier 0.1.0 · core 1.6.0")
            }
            .into(),
            UserLine {
                mode: "build".to_string(),
                ..UserLine::new("b2", "fix the bug")
            }
            .into(),
            Narration::new("b3", "Reading the failing test").into(),
            ToolLine {
                body: vec!["$ pytest".to_string(), "34 passed".to_string()],
                ..ToolLine::new("b4", "Ran 2 shell commands")
            }
            .into(),
            LiveCommand::new("b5", "pytest -q").into(),
            PlanBlock {
                telemetry: Some(telemetry.clone()),
                items: vec![
                    PlanItem {
                        state: PlanItemState::Done,
                        ..PlanItem::new("reproduce")
                    },
                    PlanItem {
                        state: PlanItemState::Active,
                        ..PlanItem::new("patch")
                    },
                    PlanItem {
                        state: PlanItemState::Pending,
                        ..PlanItem::new("verify")
                    },
                ],
                ..PlanBlock::new("b6", "Fix flaky retry test")
            }
            .into(),
            Blocked {
                continuation: "continuing without push".to_string(),
                ..Blocked::new("b7", "git push", "denied by user")
            }
            .into(),
            WorkingStatus {
                agent_count: 2,
                ..WorkingStatus::new("b8", telemetry.clone())
            }
            .into(),
            Recap::new("b9", "ship the fix", "run full suite").into(),
            Thinking {
                text: "weigh the retry approaches\npick the deadline poll".to_string(),
                expanded: true,
                ..Thinking::new("b9t")
            }
            .into(),
            Answer {
                evidence_refs: vec![EvidenceLink::new("34 passed", "pytest run")],
                ..Answer::new(
                    "b10",
                    vec![
                        Segment::new("Fixed in "),
                        Segment {
                            style_token: StyleToken::Teal,
                            ..Segment::new("retry.py")
                        },
                    ],
                )
            }
            .into(),
            SteerEcho::new("b11", "also update the docs").into(),
            TurnRule::new("b12", "t1", "24s · 3.2k tok, 80% cached · $0.12 · answer").into(),
            EvidenceBlock::new("b13", vec![EvidenceLink::new("34 passed", "pytest run")]).into(),
            LedgerBlock {
                id: "b14".to_string(),
                session: "a1b2c3".to_string(),
                bundle: "dev".to_string(),
                turns: 4,
                spend: dec("1.02"),
                shipped: 2,
                answer_only: 2,
                cache_hit_pct: 74,
            }
            .into(),
            ContextBlock {
                segments: vec![("conversation".to_string(), 4), ("free".to_string(), 6)],
                ..ContextBlock::new("b15", 31)
            }
            .into(),
            NeedsYouBlock::new(
                "b16",
                vec![NeedsYouEntry {
                    choices: vec![NeedsYouChoice::new("yes · push to fork", "yes")],
                    ..NeedsYouEntry::new("decision-1", "push to fork?")
                }],
            )
            .into(),
            DoctorBlock {
                healthy: vec!["provider ok".to_string()],
                findings: vec![DoctorFinding::new(1, "no git remote")],
                ..DoctorBlock::new("b17")
            }
            .into(),
            ImproveBlock::new("b18").into(),
            BrainstormIdea {
                number: 1,
                ..BrainstormIdea::new("b19", "event-sourced transcript")
            }
            .into(),
            DelegateSummaryBlock {
                entries: vec![
                    DelegateEntry {
                        state: DelegateState::Done,
                        elapsed_s: 4.4,
                        snippet: "3 findings".to_string(),
                        ..DelegateEntry::new("researcher")
                    },
                    DelegateEntry::new("coder"),
                ],
                plan_final: Some(vec![TodoItem {
                    status: TodoStatus::Completed,
                    ..TodoItem::new("scan provider docs")
                }]),
                duration_s: 102.0,
                expanded: true,
                ..DelegateSummaryBlock::new("b20")
            }
            .into(),
        ];
        let mut seen_kinds: HashSet<&'static str> = HashSet::new();
        for block in &blocks {
            assert!(!block.id().is_empty());
            assert!(!block.kind().is_empty());
            seen_kinds.insert(block.kind());
            let restored = roundtrip(block);
            assert_eq!(&restored, block, "{} did not round-trip", block.kind());
        }
        assert_eq!(seen_kinds.len(), 21);
    }

    #[test]
    fn test_kind_discriminates_union() {
        let restored: TranscriptBlock =
            serde_json::from_str(r#"{"id": "b9", "kind": "recap", "goal": "g", "next": "n"}"#)
                .expect("kind discriminates");
        assert!(matches!(restored, TranscriptBlock::Recap(_)));
    }

    #[test]
    fn test_segment_uses_token_names_not_hex() {
        let segment = Segment {
            style_token: StyleToken::Teal,
            bold: true,
            ..Segment::new("code")
        };
        assert_eq!(segment.style_token, StyleToken::Teal);
        assert_eq!(segment.style_token.as_str(), "teal");
        // Python: constructing with a hex value raises ValidationError; in
        // Rust the type system forbids it statically — the runtime surface
        // is deserialization, which must reject a hex value the same way.
        let bad: Result<Segment, _> =
            serde_json::from_str(r##"{"text": "bad", "style_token": "#6fc3c3"}"##);
        assert!(bad.is_err());
    }

    #[test]
    fn test_plan_item_states_match_spec() {
        for (name, state) in [
            ("pending", PlanItemState::Pending),
            ("active", PlanItemState::Active),
            ("done", PlanItemState::Done),
        ] {
            let item: PlanItem =
                serde_json::from_str(&format!(r#"{{"text": "x", "state": "{name}"}}"#))
                    .expect("valid state parses");
            assert_eq!(item.state, state);
            assert_eq!(item.state.as_str(), name);
        }
        let bad: Result<PlanItem, _> =
            serde_json::from_str(r#"{"text": "x", "state": "completed"}"#);
        assert!(bad.is_err());
    }

    #[test]
    fn test_spec_glyphs_exact() {
        assert_eq!(GLYPH_PROMPT, "❯");
        assert_eq!(GLYPH_SPINNER_FRAMES, ["✳", "✦", "✧", "✦"]);
        assert_eq!(
            (GLYPH_PLAN_DONE, GLYPH_PLAN_ACTIVE, GLYPH_PLAN_PENDING),
            ("✔", "■", "□")
        );
        assert_eq!(GLYPH_BLOCKED, "⊘");
    }

    /// Expansion is modeled as an immutable copy keyed by the stable id.
    #[test]
    fn test_tool_line_expand_toggle_via_copy() {
        let tool = ToolLine {
            body: vec!["out".to_string()],
            status: ToolLineStatus::Completed,
            ..ToolLine::new("b4", "Ran 1 shell command")
        };
        let expanded = ToolLine {
            expanded: true,
            ..tool.clone()
        };
        assert_eq!(expanded.id, tool.id);
        assert!(expanded.expanded && !tool.expanded);
    }

    #[test]
    fn test_turn_rule_carries_checkpoint_id() {
        let rule = TurnRule::new("b12", "t3", "12s · 1.1k tok · $0.05 · answer");
        assert_eq!(rule.checkpoint_id, "t3");
        assert!(!rule.shipped);
    }

    /// Not a pinned Python test: wire-parity oracle. Each line is the exact
    /// `model_dump_json()` output of the real pydantic models (captured from
    /// `uv run python`), pinning that Rust deserializes Python's JSON and
    /// re-serializes to the identical value tree (ui-events.jsonl replay
    /// compatibility) — including Decimal-as-string and telemetry shape.
    #[test]
    fn oracle_python_pydantic_dumps_are_wire_compatible() {
        let dumps = [
            r#"{"id":"b6","kind":"plan","title":"Fix flaky retry test","telemetry":{"secs":4.0,"tokens_down":3200,"cached_pct":80,"cost":"0.12","estimated":false},"items":[{"text":"reproduce","state":"done"}],"read_only":false}"#,
            r#"{"id":"b8","kind":"working_status","telemetry":{"secs":4.0,"tokens_down":0,"cached_pct":null,"cost":"0","estimated":false},"agent_count":2,"activity":"","activity_lines":[],"interrupt_hint":"esc to interrupt","steer_hint":"type to steer","spinner_frame":0,"motion_frame":0}"#,
            r#"{"id":"b14","kind":"ledger","session":"a1b2c3","bundle":"dev","turns":4,"spend":"1.02","shipped":2,"answer_only":2,"cache_hit_pct":74}"#,
            r#"{"id":"b10","kind":"answer","spans":[{"text":"Fixed in ","style_token":"fg","bold":false,"italic":false,"bg_token":null,"link":null},{"text":"retry.py","style_token":"teal","bold":false,"italic":false,"bg_token":null,"link":null}],"evidence_refs":[{"claim_quote":"34 passed","tool_ref":"pytest run","tool_call_id":""}],"clickable":true,"compact":false}"#,
            r#"{"id":"b20","kind":"delegate_summary","entries":[{"agent":"coder","state":"running","elapsed_s":0.0,"snippet":""}],"plan_final":null,"duration_s":0.0,"expanded":false}"#,
            r#"{"id":"b15","kind":"context","used_pct":31,"window_label":"200k","segments":[["conversation",4],["free",6]],"bar_width":10}"#,
        ];
        for dump in dumps {
            let block: TranscriptBlock =
                serde_json::from_str(dump).expect("Python dump deserializes");
            let reserialized: serde_json::Value =
                serde_json::to_value(&block).expect("block reserializes");
            let python: serde_json::Value = serde_json::from_str(dump).expect("dump is JSON");
            assert_eq!(reserialized, python, "wire mismatch for {}", block.kind());
        }
    }

    /// Not a pinned Python test: pins `extra="forbid"` (`_FrozenModel`) on
    /// the wire — unknown fields are rejected exactly like pydantic's
    /// ValidationError.
    #[test]
    fn oracle_extra_fields_are_forbidden() {
        let bad: Result<TranscriptBlock, _> = serde_json::from_str(
            r#"{"id": "b3", "kind": "narration", "text": "hi", "surprise": 1}"#,
        );
        assert!(bad.is_err());
    }

    /// Not a pinned Python test: pins the constructor defaults the Python
    /// field declarations carry (mode="chat", clickable=True, hints, note,
    /// window label, bar width) — deserialization applies the same defaults
    /// when fields are omitted, matching pydantic.
    #[test]
    fn oracle_defaults_match_python_field_declarations() {
        let user = UserLine::new("b1", "hi");
        assert_eq!(user.mode, "chat");

        let answer = Answer::new("b2", vec![Segment::new("ok")]);
        assert!(answer.clickable);
        assert!(!answer.compact);

        let steer = SteerEcho::new("b3", "s");
        assert_eq!(steer.note, "applies at next step boundary");

        let working = WorkingStatus::new("b4", TurnTelemetry::new(1.0));
        assert_eq!(working.interrupt_hint, "esc to interrupt");
        assert_eq!(working.steer_hint, "type to steer");

        let context = ContextBlock::new("b5", 10);
        assert_eq!(context.window_label, "200k");
        assert_eq!(context.bar_width, 10);

        let parsed: TranscriptBlock =
            serde_json::from_str(r#"{"id": "b6", "kind": "user_line", "text": "t"}"#)
                .expect("defaults apply on deserialize");
        let TranscriptBlock::UserLine(line) = parsed else {
            panic!("expected user_line");
        };
        assert_eq!(line.mode, "chat");
    }
}
