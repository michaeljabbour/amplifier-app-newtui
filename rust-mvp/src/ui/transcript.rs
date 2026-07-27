//! The transcript: durable history state per DESIGN-SPEC §3 + §11.
//!
//! Port of `src/amplifier_app_newtui/ui/transcript.py`.
//!
//! Two-region model (ADR-0007): this module is the *durable history* region.
//! Recent blocks use one interactive [`BlockWidget`] each; finalized older
//! blocks consolidate into one selectable, action-aware [`HistoryArchive`]
//! so arbitrarily long chats stay cheap to lay out. The mutable streaming
//! region lives in `ui/live_tail.rs` and consolidates into an `Answer`
//! block that gets appended here.
//!
//! Ratatui adaptation — the pure state/selection/anchoring logic ports;
//! Textual widget mechanics do not:
//!
//! - Textual messages become the returned [`TranscriptMsg`] values (and
//!   [`DecisionTaken`] for archive decision chips); the app-assembly layer
//!   dispatches them instead of a message pump.
//! - `mount`/`unmount`/CSS never port. The per-kind CSS `margin-top`
//!   rhythm is exported as [`block_margin_top`] (the archive uses it
//!   internally; app assembly must apply the same gaps between mounted
//!   widgets when painting).
//! - Timers become injected clocks + explicit fire methods (the
//!   `ui/live_tail.rs` precedent): [`TranscriptView::on_resize`] returns
//!   the 75ms trailing-debounce delay to schedule and the app calls
//!   [`TranscriptView::debounce_fired`] when it elapses (restarting the
//!   timer on every resize); the working line's 1s spinner/telemetry tick
//!   and the shimmer cadence are [`BlockWidget::advance_spinner`] /
//!   [`BlockWidget::advance_motion`], driven by app timers at
//!   [`SPINNER_INTERVAL_SECONDS`] / [`MOTION_INTERVAL_SECONDS`]. Timer
//!   teardown on unmount (`_spin_timer.stop()`) is the host's job.
//! - `call_later(_compact_history)` becomes the
//!   [`TranscriptView::compaction_pending`] flag + explicit
//!   [`TranscriptView::compact_history`]; app assembly must run the
//!   pending compaction on its next idle tick.
//! - Textual's standing tail `anchor()` becomes the
//!   [`TranscriptView::follow`] flag: the render host must keep the view
//!   scrolled to the bottom while `follow()` is true (through appends AND
//!   late height growth), route mouse wheel up to
//!   [`TranscriptView::on_mouse_scroll_up`] and wheel down to
//!   [`TranscriptView::on_mouse_scroll_down`] with its own
//!   at-bottom check, and floor scroll writes at 0 (the Python
//!   `set_reactive` clamp) so short content stays top-aligned.
//! - `scroll_block_visible` returns a [`ScrollRequest`]; for an archived
//!   block the host scrolls to `max(0, archive_region_y + offset - 2)`.
//! - Click hit-testing is the host's: it maps a click on a mounted block
//!   to [`BlockWidget::click`] with the content row, a click on an archive
//!   `@click` markup action to [`TranscriptView::archive_activate`] /
//!   [`TranscriptView::archive_decision`], and keyboard focus + key events
//!   to [`BlockWidget::handle_key`] / the archive evidence actions.

use std::collections::HashMap;

use ratatui::text::Span;

use crate::model::blocks::{
    DelegateSummaryBlock, NeedsYouBlock, Segment, StyleToken, ToolLine, TranscriptBlock,
};
use crate::model::evidence::EvidenceLink;
use crate::ui::keymap::{Binding, Context, ContextSet, KEYMAP};
use crate::ui::motion::SHIMMER_INTERVAL_SECONDS;
use crate::ui::needs_you::{DecisionTaken, NeedsYouList};
use crate::ui::segments::{line_plain, segment_markup, Line};
use crate::ui::transcript_render::{fence_text_at_row, render_block, render_block_markup};

/// Trailing debounce for resize reflow (per ADR-0007 / codex precedent).
pub const REFLOW_DEBOUNCE_SECONDS: f64 = 0.075;

/// Working-line glyph cadence: the mockup advances ✳/✦/✧/✦ inside the
/// 1000ms telemetry tick (design-v3-cohesive.html runTurn, `secs % 4`) —
/// the faster 260ms spinTimer is the §2 TITLE-bar spinner only.
pub const SPINNER_INTERVAL_SECONDS: f64 = 1.0;

/// Active-only soft-band cadence for working/coordinating labels.
pub const MOTION_INTERVAL_SECONDS: f64 = SHIMMER_INTERVAL_SECONDS;

/// Width used before first layout (corrected by the first real resize).
pub const FALLBACK_WIDTH: usize = 80;

/// Recent blocks kept as fully independent widgets.
pub const HISTORY_WIDGET_LIMIT: usize = 1_000;

/// Hysteresis avoids rebuilding the archive for every new durable block.
pub const HISTORY_COMPACT_TRIGGER: usize = 1_200;

/// Mockup line 46: the rule row advertises its rewind anchor via a hover
/// title (verbatim) — Python sets it as the widget tooltip.
pub const TURN_RULE_TOOLTIP: &str = "turn rule · click to open rewind picker";

/// Terminal cell width of `s` (Python: `rich.cells.cell_len`).
fn cell_len(s: &str) -> usize {
    Span::raw(s).width()
}

/// Python `repr()` of a plain string — the exact spelling Python's f-string
/// `{value!r}` bakes into error messages and archive `@click` actions.
fn py_repr(s: &str) -> String {
    if s.contains('\'') && !s.contains('"') {
        format!("\"{s}\"")
    } else {
        format!("'{}'", s.replace('\\', "\\\\").replace('\'', "\\'"))
    }
}

// --------------------------------------------------------------------------
// Messages — the ONLY way transcript widgets talk to the app
// --------------------------------------------------------------------------

/// The Textual `Message` classes of `ui/transcript.py` as one returned enum
/// (archive decision chips speak [`DecisionTaken`] directly, mirroring
/// Python posting `NeedsYouList.DecisionTaken`).
#[derive(Clone, Debug, PartialEq)]
pub enum TranscriptMsg {
    /// A final answer was clicked → open its evidence block (spec §10).
    ShowEvidence {
        block_id: String,
        links: Vec<EvidenceLink>,
    },
    /// A turn rule was clicked → open the rewind picker at this checkpoint.
    OpenRewind { checkpoint_id: String },
    /// A fenced code block inside an answer was clicked → copy just that
    /// fence to the clipboard (`/copy` still grabs the whole answer).
    CopyCodeFence { block_id: String, text: String },
    /// A tool line's body was expanded/collapsed in place.
    ToolLineToggled { block_id: String, expanded: bool },
    /// A delegate summary was expanded/collapsed in place (click or enter).
    DelegateSummaryToggled { block_id: String, expanded: bool },
    /// A thinking block was expanded/collapsed in place (click or enter).
    ThinkingToggled { block_id: String, expanded: bool },
    /// Enter on a focused evidence block (spec §10 `enter expand`) —
    /// deep-link the selected claim to the tool call that grounds it.
    ExpandEvidenceClaim { block_id: String, link: EvidenceLink },
    /// Esc on a focused evidence block (spec §10 `esc close`).
    CloseEvidence { block_id: String },
    /// The transcript swapped to a subagent lane (or back: `lane_id=None`).
    LaneFocusChanged { lane_id: Option<String> },
}

// --------------------------------------------------------------------------
// Widgets
// --------------------------------------------------------------------------

/// Evidence-context chords sourced from the keymap table (single source:
/// the keys that work and the keys the header advertises can never drift).
///
/// Python `_EVIDENCE_BINDINGS`: bindings whose contexts are exactly
/// `{"evidence"}`.
pub fn evidence_bindings() -> Vec<&'static Binding> {
    const EVIDENCE_ONLY: ContextSet = ContextSet::of(&[Context::Evidence]);
    KEYMAP
        .iter()
        .filter(|binding| binding.contexts == EVIDENCE_ONLY)
        .collect()
}

/// Python `_EVIDENCE_ACTIONS` membership.
pub fn is_evidence_action(action: &str) -> bool {
    evidence_bindings()
        .iter()
        .any(|binding| binding.action == action)
}

/// One transcript block as a widget (ADR-0007 open-q 6: widget-per-block).
///
/// State is the block itself; the widget re-derives its painted markup from
/// `render_block_markup(block, width)` on every repaint. In-place mutation
/// (tool expand/collapse, live plan updates, working-line telemetry)
/// happens via [`BlockWidget::update_block`] keyed by the block's stable
/// id; the app's 1s timer drives [`BlockWidget::advance_spinner`] to pulse
/// the spinner AND keep the wall-clock seconds counting between
/// event-driven updates (mockup 1000ms tick — spec §3 "Updates every
/// second", §11 live counting).
#[derive(Clone, Debug)]
pub struct BlockWidget {
    block: TranscriptBlock,
    painted_width: Option<usize>,
    painted: String,
    spinner_offset: u32,
    motion_offset: u32,
    /// Wall-clock anchor for the working line's seconds (spec §3
    /// "Updates every second" / §11 live counting — mockup 1000ms tick):
    /// event-driven replaces reset it; between events (silent tool
    /// calls, open approval bars) the displayed secs keep advancing.
    telemetry_anchor: Option<f64>,
}

impl BlockWidget {
    /// Construct (Python `__init__` + `on_mount` repaint); `now` is
    /// monotonic seconds from the injected clock.
    pub fn new(block: TranscriptBlock, now: f64) -> Self {
        let telemetry_anchor = (block.kind() == "working_status").then_some(now);
        Self {
            block,
            painted_width: None,
            painted: String::new(),
            spinner_offset: 0,
            motion_offset: 0,
            telemetry_anchor,
        }
    }

    pub fn block(&self) -> &TranscriptBlock {
        &self.block
    }

    /// Whether this block takes keyboard focus. Evidence blocks focus so
    /// the header's advertised keys work (keymap "evidence" context, spec
    /// §10); thinking/tool/delegate blocks focus so `enter` toggles them.
    pub fn can_focus(&self) -> bool {
        matches!(
            self.block.kind(),
            "tool_line" | "evidence" | "delegate_summary" | "thinking"
        )
    }

    /// The hover title (turn rules only — mockup line 46, verbatim).
    pub fn tooltip(&self) -> Option<&'static str> {
        (self.block.kind() == "turn_rule").then_some(TURN_RULE_TOOLTIP)
    }

    /// Width of the last repaint (`None` before first layout).
    pub fn painted_width(&self) -> Option<usize> {
        self.painted_width
    }

    /// The markup last painted (the bytes Python passed to `update()`).
    pub fn painted(&self) -> &str {
        &self.painted
    }

    /// Spinner ticks accumulated since the last event-driven replace.
    pub fn spinner_offset(&self) -> u32 {
        self.spinner_offset
    }

    /// Pulse ✳/✦/✧ (and tick wall-clock secs) between event replaces.
    pub fn advance_spinner(&mut self, now: f64) {
        self.spinner_offset += 1;
        self.repaint_current(now);
    }

    /// Move the active label highlight without mutating transcript text.
    pub fn advance_motion(&mut self, now: f64) {
        self.motion_offset += 1;
        self.repaint_current(now);
    }

    /// Replace this widget's block in place (same stable id).
    pub fn update_block(&mut self, block: TranscriptBlock, now: f64) -> Result<(), String> {
        if block.id() != self.block.id() {
            return Err(format!(
                "block id mismatch: widget has {}, got {}",
                py_repr(self.block.id()),
                py_repr(block.id())
            ));
        }
        let re_anchor = block.kind() == "working_status";
        self.block = block;
        if re_anchor {
            // Fresh event telemetry: re-anchor the wall-clock secs tick.
            self.telemetry_anchor = Some(now);
        }
        self.repaint_current(now);
        Ok(())
    }

    /// The block as displayed: the working line's spinner/motion offsets
    /// and elapsed wall-clock seconds folded in (paint-time only — the
    /// stored block never mutates).
    pub fn display_block(&self, now: f64) -> TranscriptBlock {
        let TranscriptBlock::WorkingStatus(status) = &self.block else {
            return self.block.clone();
        };
        let mut status = status.clone();
        if self.spinner_offset > 0 {
            status.spinner_frame += self.spinner_offset;
        }
        if self.motion_offset > 0 {
            status.motion_frame += self.motion_offset;
        }
        if let Some(anchor) = self.telemetry_anchor {
            // Whole wall-clock seconds since the last event-driven
            // replace — the working line keeps counting while the
            // runtime is silent (mockup setInterval secs++, spec §11).
            let elapsed = (now - anchor) as i64;
            if elapsed > 0 {
                status.telemetry.secs += elapsed as f64;
            }
        }
        TranscriptBlock::WorkingStatus(status)
    }

    /// Re-derive content from (block, width). Width 0 falls back to
    /// [`FALLBACK_WIDTH`] (Python `self.size.width or FALLBACK_WIDTH`).
    pub fn repaint_block(&mut self, width: usize, now: f64) {
        let width = if width == 0 { FALLBACK_WIDTH } else { width };
        self.painted_width = Some(width);
        let block = self.display_block(now);
        self.painted = render_block_markup(&block, width);
    }

    fn repaint_current(&mut self, now: f64) {
        self.repaint_block(self.painted_width.unwrap_or(FALLBACK_WIDTH), now);
    }

    /// A click at content row `row` (Python `on_click`): clicking directly
    /// on a fenced code block copies just that fence; any other spot on an
    /// answer falls through to the normal activate (evidence, spec §10).
    pub fn click(&mut self, row: isize, now: f64) -> Option<TranscriptMsg> {
        if self.block.kind() == "answer" {
            if let Some(text) = self.fence_click_text(row) {
                return Some(TranscriptMsg::CopyCodeFence {
                    block_id: self.block.id().to_string(),
                    text,
                });
            }
        }
        self.activate(now)
    }

    /// Dedented source of the code fence at content row `row`, or `None`.
    pub fn fence_click_text(&self, row: isize) -> Option<String> {
        let width = self.painted_width.unwrap_or(FALLBACK_WIDTH);
        fence_text_at_row(&render_block(&self.block, width), row)
    }

    /// Gate kind-specific bindings: evidence chords fire only on a
    /// focused evidence block; enter there means expand, not activate.
    pub fn check_action(&self, action: &str) -> bool {
        if is_evidence_action(action) {
            return self.block.kind() == "evidence";
        }
        if action == "activate" {
            return self.block.kind() != "evidence";
        }
        true
    }

    /// Dispatch a key press through the widget's bindings (enter →
    /// activate, plus the evidence-context chords), gated by
    /// [`BlockWidget::check_action`] exactly like Textual does.
    pub fn handle_key(&mut self, key: &str, now: f64) -> Option<TranscriptMsg> {
        if key == "enter" && self.check_action("activate") {
            return self.action_activate(now);
        }
        for binding in evidence_bindings() {
            if binding.keys.contains(&key) && self.check_action(binding.action)
            {
                return match binding.action {
                    "evidence_prev" => {
                        self.action_evidence_prev(now);
                        None
                    }
                    "evidence_next" => {
                        self.action_evidence_next(now);
                        None
                    }
                    "evidence_expand" => self.action_evidence_expand(),
                    "close_evidence" => self.action_close_evidence(),
                    _ => None,
                };
            }
        }
        None
    }

    pub fn action_activate(&mut self, now: f64) -> Option<TranscriptMsg> {
        self.activate(now)
    }

    pub fn action_evidence_prev(&mut self, now: f64) {
        self.move_evidence_selection(-1, now);
    }

    pub fn action_evidence_next(&mut self, now: f64) {
        self.move_evidence_selection(1, now);
    }

    pub fn action_evidence_expand(&self) -> Option<TranscriptMsg> {
        let TranscriptBlock::Evidence(block) = &self.block else {
            return None;
        };
        if block.links.is_empty() {
            return None;
        }
        Some(TranscriptMsg::ExpandEvidenceClaim {
            block_id: block.id.clone(),
            link: block.links[block.selected].clone(),
        })
    }

    pub fn action_close_evidence(&self) -> Option<TranscriptMsg> {
        let TranscriptBlock::Evidence(block) = &self.block else {
            return None;
        };
        Some(TranscriptMsg::CloseEvidence {
            block_id: block.id.clone(),
        })
    }

    /// ←/→ move the highlighted claim; the header 1/N tracks it.
    fn move_evidence_selection(&mut self, delta: isize, now: f64) {
        let TranscriptBlock::Evidence(block) = &self.block else {
            return;
        };
        if block.links.is_empty() {
            return;
        }
        let selected = (block.selected as isize + delta)
            .clamp(0, block.links.len() as isize - 1) as usize;
        if selected != block.selected {
            let mut updated = block.clone();
            updated.selected = selected;
            self.block = TranscriptBlock::Evidence(updated);
            self.repaint_current(now);
        }
    }

    fn activate(&mut self, now: f64) -> Option<TranscriptMsg> {
        match self.block.clone() {
            TranscriptBlock::ToolLine(tool) if !tool.body.is_empty() => {
                let toggled = ToolLine {
                    expanded: !tool.expanded,
                    ..tool
                };
                let msg = TranscriptMsg::ToolLineToggled {
                    block_id: toggled.id.clone(),
                    expanded: toggled.expanded,
                };
                self.block = TranscriptBlock::ToolLine(toggled);
                self.repaint_current(now);
                Some(msg)
            }
            TranscriptBlock::DelegateSummary(summary) if !summary.entries.is_empty() => {
                let toggled = DelegateSummaryBlock {
                    expanded: !summary.expanded,
                    ..summary
                };
                let msg = TranscriptMsg::DelegateSummaryToggled {
                    block_id: toggled.id.clone(),
                    expanded: toggled.expanded,
                };
                self.block = TranscriptBlock::DelegateSummary(toggled);
                self.repaint_current(now);
                Some(msg)
            }
            // Withheld thinking (empty text) is not expandable — nothing to show.
            TranscriptBlock::Thinking(thinking) if !thinking.text.is_empty() => {
                let mut toggled = thinking;
                toggled.expanded = !toggled.expanded;
                let msg = TranscriptMsg::ThinkingToggled {
                    block_id: toggled.id.clone(),
                    expanded: toggled.expanded,
                };
                self.block = TranscriptBlock::Thinking(toggled);
                self.repaint_current(now);
                Some(msg)
            }
            TranscriptBlock::Answer(answer) if answer.clickable => {
                Some(TranscriptMsg::ShowEvidence {
                    block_id: answer.id,
                    links: answer.evidence_refs,
                })
            }
            TranscriptBlock::TurnRule(rule) => Some(TranscriptMsg::OpenRewind {
                checkpoint_id: rule.checkpoint_id,
            }),
            _ => None,
        }
    }
}

/// A needs-you block mounted in the transcript flow (DESIGN-SPEC §7).
///
/// The mockup attaches the click handler *per decision row*
/// (design-v3-cohesive.html:286-292) — acting on one decision applies
/// THAT decision, so the transcript holds the per-row hit-testing
/// [`NeedsYouList`] instead of a single flat [`BlockWidget`]. Chip/row
/// clicks yield [`DecisionTaken`] via the list; the header is not a click
/// target.
#[derive(Clone, Debug)]
pub struct NeedsYouBlockWidget {
    block: NeedsYouBlock,
    list: NeedsYouList,
}

impl NeedsYouBlockWidget {
    pub fn new(block: NeedsYouBlock) -> Self {
        Self {
            list: NeedsYouList::new(Some(block.clone())),
            block,
        }
    }

    pub fn block(&self) -> &NeedsYouBlock {
        &self.block
    }

    /// The per-row hit-testing list (map clicks to `activate_chip`/`activate_row`).
    pub fn list(&self) -> &NeedsYouList {
        &self.list
    }

    /// Replace this widget's block in place (same stable id).
    pub fn update_block(&mut self, block: TranscriptBlock) -> Result<(), String> {
        if block.id() != self.block.id {
            return Err(format!(
                "block id mismatch: widget has {}, got {}",
                py_repr(&self.block.id),
                py_repr(block.id())
            ));
        }
        let TranscriptBlock::NeedsYou(needs_you) = block else {
            return Err(format!(
                "needs_you widget got block kind {}",
                py_repr(block.kind())
            ));
        };
        self.block = needs_you.clone();
        self.list.update_block(needs_you);
        Ok(())
    }

    /// Width-pure rows re-layout themselves; nothing to re-derive.
    pub fn repaint_block(&mut self) {}
}

/// One mounted transcript block (needs-you blocks get per-row widgets).
#[derive(Clone, Debug)]
pub enum TranscriptWidget {
    Block(BlockWidget),
    NeedsYou(NeedsYouBlockWidget),
}

impl TranscriptWidget {
    /// The widget's current block (live UI-local state included).
    pub fn block(&self) -> TranscriptBlock {
        match self {
            TranscriptWidget::Block(widget) => widget.block().clone(),
            TranscriptWidget::NeedsYou(widget) => {
                TranscriptBlock::NeedsYou(widget.block().clone())
            }
        }
    }

    pub fn update_block(&mut self, block: TranscriptBlock, now: f64) -> Result<(), String> {
        match self {
            TranscriptWidget::Block(widget) => widget.update_block(block, now),
            TranscriptWidget::NeedsYou(widget) => widget.update_block(block),
        }
    }

    pub fn repaint_block(&mut self, width: usize, now: f64) {
        match self {
            TranscriptWidget::Block(widget) => widget.repaint_block(width, now),
            TranscriptWidget::NeedsYou(widget) => widget.repaint_block(),
        }
    }

    pub fn as_block(&self) -> Option<&BlockWidget> {
        match self {
            TranscriptWidget::Block(widget) => Some(widget),
            TranscriptWidget::NeedsYou(_) => None,
        }
    }

    pub fn as_block_mut(&mut self) -> Option<&mut BlockWidget> {
        match self {
            TranscriptWidget::Block(widget) => Some(widget),
            TranscriptWidget::NeedsYou(_) => None,
        }
    }
}

/// The widget for one block: per-row needs-you list, else BlockWidget.
pub fn build_block_widget(block: TranscriptBlock, now: f64) -> TranscriptWidget {
    match block {
        TranscriptBlock::NeedsYou(needs_you) => {
            TranscriptWidget::NeedsYou(NeedsYouBlockWidget::new(needs_you))
        }
        other => TranscriptWidget::Block(BlockWidget::new(other, now)),
    }
}

/// Mirror the per-kind CSS rhythm inside the consolidated archive.
pub fn block_margin_top(block: &TranscriptBlock) -> usize {
    match block {
        TranscriptBlock::UserLine(_)
        | TranscriptBlock::TurnRule(_)
        | TranscriptBlock::Ledger(_)
        | TranscriptBlock::Context(_)
        | TranscriptBlock::Doctor(_)
        | TranscriptBlock::Improve(_)
        | TranscriptBlock::NeedsYou(_) => 1,
        TranscriptBlock::Plan(plan) => {
            if plan.read_only {
                0
            } else {
                1
            }
        }
        TranscriptBlock::Answer(answer) => {
            if answer.compact {
                0
            } else {
                1
            }
        }
        _ => 0,
    }
}

/// One selectable, interactive visual for finalized older history.
///
/// It removes thousands of children from the compositor without removing a
/// single line from the conversation. Theme-token markup keeps archived
/// text visually identical, and `@click` metadata in the markup preserves
/// tool, evidence, rewind, and deferred-decision actions even after
/// consolidation (the host maps a clicked action string to
/// [`TranscriptView::archive_activate`] / [`TranscriptView::archive_decision`]).
#[derive(Clone, Debug, Default)]
pub struct HistoryArchive {
    blocks: Vec<TranscriptBlock>,
    painted_width: Option<usize>,
    painted: String,
    plain: String,
    block_offsets: HashMap<String, usize>,
    active_evidence_id: Option<String>,
}

impl HistoryArchive {
    fn new() -> Self {
        Self::default()
    }

    pub fn blocks(&self) -> &[TranscriptBlock] {
        &self.blocks
    }

    /// The markup last painted (Python `self.update(Content.from_markup(…))`).
    pub fn markup(&self) -> &str {
        &self.painted
    }

    /// Style-free text of the archive (Python `str(archive.content)` /
    /// `get_selection(SELECT_ALL)` — copy source stays intact).
    pub fn plain_text(&self) -> &str {
        &self.plain
    }

    /// The evidence block the archive's evidence chords act on.
    pub fn active_evidence_id(&self) -> Option<&str> {
        self.active_evidence_id.as_deref()
    }

    fn update_blocks(&mut self, blocks: Vec<TranscriptBlock>, width: usize) {
        self.blocks = blocks;
        if let Some(active) = &self.active_evidence_id {
            if !self.blocks.iter().any(|block| block.id() == active) {
                self.active_evidence_id = None;
            }
        }
        self.repaint_archive(width);
    }

    /// Gate evidence chords: they fire only while an archived evidence
    /// block was activated (Python `check_action`).
    pub fn check_action(&self, action: &str) -> bool {
        if is_evidence_action(action) {
            return self.active_evidence_id.is_some();
        }
        true
    }

    fn block_action(block: &TranscriptBlock) -> Option<String> {
        let activate = |id: &str| Some(format!("archive_activate({})", py_repr(id)));
        match block {
            TranscriptBlock::ToolLine(tool) if !tool.body.is_empty() => activate(&tool.id),
            TranscriptBlock::DelegateSummary(summary) if !summary.entries.is_empty() => {
                activate(&summary.id)
            }
            TranscriptBlock::Thinking(thinking) if !thinking.text.is_empty() => {
                activate(&thinking.id)
            }
            TranscriptBlock::Answer(answer) if answer.clickable => activate(&answer.id),
            TranscriptBlock::TurnRule(rule) => activate(&rule.id),
            TranscriptBlock::Evidence(evidence) => activate(&evidence.id),
            _ => None,
        }
    }

    fn styled_segment(segment: &Segment, action: Option<&str>) -> String {
        let markup = segment_markup(segment);
        match action {
            Some(action) if !markup.is_empty() => format!("[@click={action}]{markup}[/]"),
            _ => markup,
        }
    }

    fn block_markup(block: &TranscriptBlock, width: usize) -> (String, Vec<Line>) {
        let lines = render_block(block, width);
        let default_action = Self::block_action(block);
        let mut rendered_lines: Vec<String> = Vec::new();
        for (line_index, line) in lines.iter().enumerate() {
            let mut default_line_action = default_action.clone();
            let mut choice_index = 0usize;
            let item_index = line_index as isize - 1;
            let needs_you_entry = match block {
                TranscriptBlock::NeedsYou(needs_you)
                    if item_index >= 0 && (item_index as usize) < needs_you.items.len() =>
                {
                    let entry = &needs_you.items[item_index as usize];
                    default_line_action = (!entry.choices.is_empty()).then(|| {
                        format!(
                            "archive_decision({}, {}, 0)",
                            py_repr(&needs_you.id),
                            item_index
                        )
                    });
                    Some(&needs_you.id)
                }
                _ => None,
            };
            let mut parts: Vec<String> = Vec::new();
            for segment in line {
                let mut action = default_line_action.clone();
                if let Some(block_id) = needs_you_entry {
                    if segment.bg_token == Some(StyleToken::BgTab) {
                        action = Some(format!(
                            "archive_decision({}, {}, {})",
                            py_repr(block_id),
                            item_index,
                            choice_index
                        ));
                        choice_index += 1;
                    }
                }
                parts.push(Self::styled_segment(segment, action.as_deref()));
            }
            rendered_lines.push(parts.concat());
        }
        (rendered_lines.join("\n"), lines)
    }

    /// Height the block's rendered lines occupy at `width` (the Python
    /// `Content.get_height(styles, width)` stand-in: per-line cell width,
    /// ceil-divided by the wrap width).
    fn block_height(lines: &[Line], width: usize) -> usize {
        lines
            .iter()
            .map(|line| {
                let cells: usize = line.iter().map(|segment| cell_len(&segment.text)).sum();
                if cells == 0 {
                    1
                } else {
                    cells.div_ceil(width.max(1))
                }
            })
            .sum()
    }

    fn repaint_archive(&mut self, width: usize) {
        let width = if width == 0 { FALLBACK_WIDTH } else { width };
        self.painted_width = Some(width);
        let mut parts: Vec<String> = Vec::new();
        let mut plain_parts: Vec<String> = Vec::new();
        let mut offsets: HashMap<String, usize> = HashMap::new();
        let mut row = 0usize;
        for (index, block) in self.blocks.iter().enumerate() {
            if index > 0 {
                parts.push("\n".to_string());
                plain_parts.push("\n".to_string());
            }
            let margin = block_margin_top(block);
            if margin > 0 {
                parts.push("\n".repeat(margin));
                plain_parts.push("\n".repeat(margin));
                row += margin;
            }
            offsets.insert(block.id().to_string(), row);
            let (markup, lines) = Self::block_markup(block, width);
            plain_parts.push(
                lines
                    .iter()
                    .map(|line| line_plain(line))
                    .collect::<Vec<_>>()
                    .join("\n"),
            );
            parts.push(markup);
            row += Self::block_height(&lines, width).max(1);
        }
        self.block_offsets = offsets;
        self.painted = parts.concat();
        self.plain = plain_parts.concat();
    }

    /// The archive-local row a block starts at (for scroll targeting).
    pub fn block_offset(&self, block_id: &str) -> Option<usize> {
        self.block_offsets.get(block_id).copied()
    }
}

/// Where the host must scroll to reveal a block
/// (see [`TranscriptView::scroll_block_visible`]).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ScrollRequest {
    /// Scroll the mounted widget for this block id into view.
    Widget(String),
    /// The block lives in the archive at this archive-local row; scroll to
    /// `max(0, archive_region_y + row - 2)` (Python `scroll_block_visible`).
    ArchiveRow(usize),
}

/// Scrollable durable history with a bounded interactive widget tail.
///
/// The newest ~1k blocks retain their independent widgets. Older blocks are
/// painted by one [`HistoryArchive`], which remains selectable and keeps
/// the same click/keyboard actions through `@click` action metadata. This
/// preserves the infinite-chat feel while bounding layout work.
///
/// - **Tail-follow anchor**: [`TranscriptView::follow`] is true while the
///   host must stick to the bottom whenever content height grows (append,
///   late height growth, wrap reflow); the user scrolling up releases it
///   and scrolling back to the bottom re-arms it.
/// - **Keyed mutation**: [`TranscriptView::append`] /
///   [`TranscriptView::replace`] / [`TranscriptView::remove_block`]
///   address blocks by stable id.
/// - **Lane focus** (spec §8): [`TranscriptView::focus_lane`] swaps the
///   visible block list to a subagent's transcript;
///   [`TranscriptView::restore_main`] (the app's esc handler) swaps back.
///   While focused, append/replace/remove address the *stashed parent*
///   list (mockup: `this.lines` keeps accumulating separately from
///   `focusLines`), so a turn that keeps running during focus is fully up
///   to date when esc restores the parent transcript.
/// - **Resize reflow**: 75ms trailing debounce; deferred during streaming
///   with one forced reflow at [`TranscriptView::set_streaming`] (false).
#[derive(Debug, Default)]
pub struct TranscriptView {
    blocks: HashMap<String, TranscriptBlock>,
    widgets: HashMap<String, TranscriptWidget>,
    order: Vec<String>,
    archive: Option<HistoryArchive>,
    archive_ids: Vec<String>,
    compaction_pending: bool,
    focused_lane: Option<String>,
    main_stash: Option<Vec<TranscriptBlock>>,
    streaming: bool,
    reflow_hold: bool,
    reflow_deferred: bool,
    last_width: Option<usize>,
    anchored: bool,
    anchor_released: bool,
}

impl TranscriptView {
    /// Construct mounted: the standing tail anchor engages immediately
    /// (Python `on_mount` → `self.anchor()`).
    pub fn new() -> Self {
        Self {
            anchored: true,
            ..Self::default()
        }
    }

    // -- block CRUD --------------------------------------------------------

    pub fn block_ids(&self) -> Vec<String> {
        self.order.clone()
    }

    /// Every visible block, in order. A mounted widget may hold transient
    /// UI-local state (for example the selected evidence claim) before it
    /// ever becomes archival state; read that live state first — archived
    /// blocks come directly from the canonical store.
    pub fn blocks(&self) -> Vec<TranscriptBlock> {
        self.order
            .iter()
            .map(|block_id| match self.widgets.get(block_id) {
                Some(widget) => widget.block(),
                None => self.blocks[block_id].clone(),
            })
            .collect()
    }

    pub fn get_block(&self, block_id: &str) -> Option<TranscriptBlock> {
        match self.widgets.get(block_id) {
            Some(widget) => Some(widget.block()),
            None => self.blocks.get(block_id).cloned(),
        }
    }

    /// The mounted widget for `block_id` (None while stashed/archived/unknown).
    pub fn get_widget(&self, block_id: &str) -> Option<&TranscriptWidget> {
        self.widgets.get(block_id)
    }

    pub fn get_widget_mut(&mut self, block_id: &str) -> Option<&mut TranscriptWidget> {
        self.widgets.get_mut(block_id)
    }

    /// Every visible block as *displayed*: the working line's spinner/motion
    /// offsets and elapsed wall-clock seconds folded in (paint-time only —
    /// the stored blocks never mutate). Python folds these inside
    /// `BlockWidget.repaint_block`; the ratatui host re-renders from blocks
    /// each frame, so its draw path must read the same fold or the shimmer
    /// band freezes at the reducer's `motion_frame: 0`.
    pub fn display_blocks(&self, now: f64) -> Vec<TranscriptBlock> {
        self.order
            .iter()
            .map(|block_id| match self.widgets.get(block_id) {
                Some(TranscriptWidget::Block(widget)) => widget.display_block(now),
                Some(widget) => widget.block(),
                None => self.blocks[block_id].clone(),
            })
            .collect()
    }

    /// Advance the shimmer frame of every mounted working-status widget.
    ///
    /// Python: each working-status `BlockWidget` runs its own Textual
    /// `_motion_timer` at [`MOTION_INTERVAL_SECONDS`]; this host has one
    /// tick clock instead, which fans the advance out here (the app gates
    /// the cadence). Stashed parent lists and archived history hold plain
    /// blocks, so a focused lane naturally pauses the parent's shimmer —
    /// exactly like an unmounted Textual widget's stopped timer. Returns
    /// true when any working line advanced (a repaint is due).
    pub fn advance_working_motion(&mut self, now: f64) -> bool {
        let mut advanced = false;
        for widget in self.widgets.values_mut() {
            if let TranscriptWidget::Block(block_widget) = widget {
                if block_widget.block().kind() == "working_status" {
                    block_widget.advance_motion(now);
                    advanced = true;
                }
            }
        }
        advanced
    }

    /// Mounted widget count (bounded by [`HISTORY_WIDGET_LIMIT`] after
    /// compaction).
    pub fn widget_count(&self) -> usize {
        self.widgets.len()
    }

    /// The consolidated older-history visual, once compaction has run.
    pub fn archive(&self) -> Option<&HistoryArchive> {
        self.archive.as_ref()
    }

    fn paint_width(&self) -> usize {
        self.last_width.unwrap_or(FALLBACK_WIDTH)
    }

    /// Mount a new block at the end (follows the tail when anchored).
    ///
    /// While a lane is focused the append lands in the stashed *parent*
    /// list (spec §8: the parent turn keeps accumulating during focus)
    /// and returns `Ok(None)` — nothing is mounted until esc restores.
    pub fn append(
        &mut self,
        block: TranscriptBlock,
        now: f64,
    ) -> Result<Option<&mut TranscriptWidget>, String> {
        if self.focused_lane.is_some() {
            if let Some(stash) = self.main_stash.as_mut() {
                if stash.iter().any(|stashed| stashed.id() == block.id()) {
                    return Err(format!("duplicate block id: {}", py_repr(block.id())));
                }
                stash.push(block);
                return Ok(None);
            }
        }
        let block_id = block.id().to_string();
        if self.blocks.contains_key(&block_id) {
            return Err(format!("duplicate block id: {}", py_repr(&block_id)));
        }
        let mut widget = build_block_widget(block.clone(), now);
        widget.repaint_block(self.paint_width(), now); // "mount" repaint
        self.blocks.insert(block_id.clone(), block);
        self.order.push(block_id.clone());
        self.widgets.insert(block_id.clone(), widget);
        // No one-shot scroll here: while the tail anchor is engaged the
        // host keeps the view at the bottom through this mount AND any
        // later height growth; while released (user scrolled up) it must
        // not move.
        self.schedule_compaction();
        Ok(self.widgets.get_mut(&block_id))
    }

    /// Carry a user's UI-local expansion across a reducer replace.
    ///
    /// The reducer always re-renders a delegate summary collapsed
    /// (expansion is UI-local by design); without this merge, a
    /// mid-flight update — or a post-turn straggler `AgentCompleted`,
    /// which replaces the block after the turn ended — collapses a
    /// summary the user has opened (review finding H1).
    fn preserve_expansion(&self, block: TranscriptBlock) -> TranscriptBlock {
        let TranscriptBlock::DelegateSummary(summary) = &block else {
            return block;
        };
        if summary.expanded {
            return block;
        }
        let current: Option<TranscriptBlock> = match self.widgets.get(block.id()) {
            Some(TranscriptWidget::Block(widget)) => Some(widget.block().clone()),
            _ => {
                if self.focused_lane.is_some() && self.main_stash.is_some() {
                    self.main_stash
                        .as_ref()
                        .and_then(|stash| {
                            stash.iter().find(|stashed| stashed.id() == block.id())
                        })
                        .cloned()
                } else {
                    self.blocks.get(block.id()).cloned()
                }
            }
        };
        if let Some(TranscriptBlock::DelegateSummary(current)) = current {
            if current.expanded {
                let mut merged = summary.clone();
                merged.expanded = true;
                return TranscriptBlock::DelegateSummary(merged);
            }
        }
        block
    }

    /// Swap a block's content in place, keyed by its stable id
    /// (`preserve_expansion` defaults on — see [`TranscriptView::replace_with`]).
    pub fn replace(&mut self, block: TranscriptBlock, now: f64) -> Result<(), String> {
        self.replace_with(block, true, now)
    }

    /// Swap a block's content in place, keyed by its stable id.
    ///
    /// `preserve_expansion=false` is for explicit user toggles (the
    /// archive activate path) — data replaces from the reducer keep the
    /// default and never collapse an opened summary.
    ///
    /// While a lane is focused the replace addresses the stashed parent
    /// list — the focused child transcript is a read-only snapshot.
    pub fn replace_with(
        &mut self,
        block: TranscriptBlock,
        preserve_expansion: bool,
        now: f64,
    ) -> Result<(), String> {
        let block = if preserve_expansion {
            self.preserve_expansion(block)
        } else {
            block
        };
        if self.focused_lane.is_some() {
            if let Some(stash) = self.main_stash.as_mut() {
                for stashed in stash.iter_mut() {
                    if stashed.id() == block.id() {
                        *stashed = block;
                        return Ok(());
                    }
                }
                return Err(format!("unknown block id: {}", py_repr(block.id())));
            }
        }
        let block_id = block.id().to_string();
        if !self.blocks.contains_key(&block_id) {
            return Err(format!("unknown block id: {}", py_repr(&block_id)));
        }
        self.blocks.insert(block_id.clone(), block.clone());
        if let Some(widget) = self.widgets.get_mut(&block_id) {
            widget.update_block(block, now)?;
        } else if self.archive.is_some() && self.archive_ids.contains(&block_id) {
            let archived: Vec<TranscriptBlock> = self
                .archive_ids
                .iter()
                .map(|archive_id| self.blocks[archive_id].clone())
                .collect();
            let width = self.paint_width();
            if let Some(archive) = self.archive.as_mut() {
                archive.update_blocks(archived, width);
            }
        } else {
            // internal representation invariant
            return Err(format!(
                "block {} is neither mounted nor archived",
                py_repr(&block_id)
            ));
        }
        Ok(())
    }

    /// Unmount a block (e.g. the working status line at turn end).
    ///
    /// While a lane is focused the removal addresses the stashed parent
    /// list, so e.g. the working line dropped at turn end never survives
    /// into the restored parent transcript.
    pub fn remove_block(&mut self, block_id: &str) -> Result<(), String> {
        if self.focused_lane.is_some() {
            if let Some(stash) = self.main_stash.as_mut() {
                if let Some(index) = stash.iter().position(|stashed| stashed.id() == block_id) {
                    stash.remove(index);
                    return Ok(());
                }
                return Err(format!("unknown block id: {}", py_repr(block_id)));
            }
        }
        if self.blocks.remove(block_id).is_none() {
            return Err(format!("unknown block id: {}", py_repr(block_id)));
        }
        self.order.retain(|order_id| order_id != block_id);
        if self.widgets.remove(block_id).is_some() {
            return Ok(());
        }
        if let Some(index) = self.archive_ids.iter().position(|id| id == block_id) {
            self.archive_ids.remove(index);
            if self.archive.is_some() {
                if self.archive_ids.is_empty() {
                    self.archive = None;
                } else {
                    let archived: Vec<TranscriptBlock> = self
                        .archive_ids
                        .iter()
                        .map(|archive_id| self.blocks[archive_id].clone())
                        .collect();
                    let width = self.paint_width();
                    if let Some(archive) = self.archive.as_mut() {
                        archive.update_blocks(archived, width);
                    }
                }
            }
        }
        Ok(())
    }

    fn schedule_compaction(&mut self) {
        if self.widgets.len() <= HISTORY_COMPACT_TRIGGER || self.compaction_pending {
            return;
        }
        // Python: `self.call_later(self._compact_history)` — app assembly
        // must call `compact_history()` on its next idle tick.
        self.compaction_pending = true;
    }

    /// A compaction pass has been scheduled and not yet run.
    pub fn compaction_pending(&self) -> bool {
        self.compaction_pending
    }

    /// Move the old prefix into one visual without changing its text.
    pub fn compact_history(&mut self) {
        let done = |view: &mut Self| view.compaction_pending = false;
        if self.widgets.len() <= HISTORY_COMPACT_TRIGGER {
            done(self);
            return;
        }
        let archive_count = self.order.len().saturating_sub(HISTORY_WIDGET_LIMIT);
        let archive_ids: Vec<String> = self.order[..archive_count].to_vec();
        let newly_archived: Vec<&String> = archive_ids
            .iter()
            .filter(|block_id| self.widgets.contains_key(*block_id))
            .collect();
        if newly_archived.is_empty() {
            done(self);
            return;
        }
        let archived_blocks: Vec<TranscriptBlock> = archive_ids
            .iter()
            .map(|block_id| self.blocks[block_id].clone())
            .collect();
        let width = self.paint_width();
        self.archive
            .get_or_insert_with(HistoryArchive::new)
            .update_blocks(archived_blocks, width);
        for block_id in &archive_ids {
            self.widgets.remove(block_id);
        }
        self.archive_ids = archive_ids;
        done(self);
    }

    /// Reveal a mounted or archived block without rehydrating history.
    pub fn scroll_block_visible(&self, block_id: &str) -> Option<ScrollRequest> {
        if self.widgets.contains_key(block_id) {
            return Some(ScrollRequest::Widget(block_id.to_string()));
        }
        let offset = self.archive.as_ref()?.block_offset(block_id)?;
        Some(ScrollRequest::ArchiveRow(offset))
    }

    /// Keep canonical history aligned with a tail widget's local toggle.
    pub fn on_tool_line_toggled(&mut self, block_id: &str) {
        if let Some(TranscriptWidget::Block(widget)) = self.widgets.get(block_id) {
            if matches!(widget.block(), TranscriptBlock::ToolLine(_)) {
                self.blocks
                    .insert(block_id.to_string(), widget.block().clone());
            }
        }
    }

    /// Keep canonical history aligned with a tail widget's local toggle.
    pub fn on_delegate_summary_toggled(&mut self, block_id: &str) {
        if let Some(TranscriptWidget::Block(widget)) = self.widgets.get(block_id) {
            if matches!(widget.block(), TranscriptBlock::DelegateSummary(_)) {
                self.blocks
                    .insert(block_id.to_string(), widget.block().clone());
            }
        }
    }

    /// Keep canonical history aligned with a tail widget's local toggle.
    pub fn on_thinking_toggled(&mut self, block_id: &str) {
        if let Some(TranscriptWidget::Block(widget)) = self.widgets.get(block_id) {
            if matches!(widget.block(), TranscriptBlock::Thinking(_)) {
                self.blocks
                    .insert(block_id.to_string(), widget.block().clone());
            }
        }
    }

    // -- archive actions (Python `HistoryArchive.action_*`, which read the
    // owner view; the archive is owned here, so the actions live on the view)

    /// An archive `@click=archive_activate('<id>')` action fired.
    pub fn archive_activate(&mut self, block_id: &str, now: f64) -> Option<TranscriptMsg> {
        let block = self.get_block(block_id)?;
        match block {
            TranscriptBlock::ToolLine(tool) if !tool.body.is_empty() => {
                let toggled = ToolLine {
                    expanded: !tool.expanded,
                    ..tool
                };
                let msg = TranscriptMsg::ToolLineToggled {
                    block_id: toggled.id.clone(),
                    expanded: toggled.expanded,
                };
                self.replace(TranscriptBlock::ToolLine(toggled), now).ok()?;
                Some(msg)
            }
            TranscriptBlock::DelegateSummary(summary) if !summary.entries.is_empty() => {
                let toggled = DelegateSummaryBlock {
                    expanded: !summary.expanded,
                    ..summary
                };
                let msg = TranscriptMsg::DelegateSummaryToggled {
                    block_id: toggled.id.clone(),
                    expanded: toggled.expanded,
                };
                // An explicit user toggle: bypass the reducer-replace
                // expansion merge, or collapsing would be undone by
                // preserve_expansion.
                self.replace_with(TranscriptBlock::DelegateSummary(toggled), false, now)
                    .ok()?;
                Some(msg)
            }
            TranscriptBlock::Thinking(thinking) if !thinking.text.is_empty() => {
                let mut toggled = thinking;
                toggled.expanded = !toggled.expanded;
                let msg = TranscriptMsg::ThinkingToggled {
                    block_id: toggled.id.clone(),
                    expanded: toggled.expanded,
                };
                self.replace(TranscriptBlock::Thinking(toggled), now).ok()?;
                Some(msg)
            }
            TranscriptBlock::Answer(answer) if answer.clickable => {
                Some(TranscriptMsg::ShowEvidence {
                    block_id: answer.id,
                    links: answer.evidence_refs,
                })
            }
            TranscriptBlock::TurnRule(rule) => Some(TranscriptMsg::OpenRewind {
                checkpoint_id: rule.checkpoint_id,
            }),
            TranscriptBlock::Evidence(evidence) => {
                // Python also takes keyboard focus; focus is host wiring.
                if let Some(archive) = self.archive.as_mut() {
                    archive.active_evidence_id = Some(evidence.id);
                }
                None
            }
            _ => None,
        }
    }

    /// An archive `@click=archive_decision('<id>', item, choice)` action fired.
    pub fn archive_decision(
        &self,
        block_id: &str,
        item_index: usize,
        choice_index: usize,
    ) -> Option<DecisionTaken> {
        let TranscriptBlock::NeedsYou(block) = self.get_block(block_id)? else {
            return None;
        };
        let entry = block.items.get(item_index)?;
        let choice = entry.choices.get(choice_index)?;
        Some(DecisionTaken::new(&entry.decision_id, &choice.answer))
    }

    fn active_archive_evidence(&self) -> Option<crate::model::blocks::EvidenceBlock> {
        let active = self.archive.as_ref()?.active_evidence_id.clone()?;
        match self.get_block(&active)? {
            TranscriptBlock::Evidence(block) => Some(block),
            _ => None,
        }
    }

    fn move_archive_evidence_selection(&mut self, delta: isize, now: f64) {
        let Some(block) = self.active_archive_evidence() else {
            return;
        };
        if block.links.is_empty() {
            return;
        }
        let selected = (block.selected as isize + delta)
            .clamp(0, block.links.len() as isize - 1) as usize;
        if selected != block.selected {
            let mut updated = block;
            updated.selected = selected;
            let _ = self.replace(TranscriptBlock::Evidence(updated), now);
        }
    }

    pub fn archive_evidence_prev(&mut self, now: f64) {
        self.move_archive_evidence_selection(-1, now);
    }

    pub fn archive_evidence_next(&mut self, now: f64) {
        self.move_archive_evidence_selection(1, now);
    }

    pub fn archive_evidence_expand(&self) -> Option<TranscriptMsg> {
        let block = self.active_archive_evidence()?;
        if block.links.is_empty() {
            return None;
        }
        Some(TranscriptMsg::ExpandEvidenceClaim {
            block_id: block.id.clone(),
            link: block.links[block.selected].clone(),
        })
    }

    pub fn archive_close_evidence(&self) -> Option<TranscriptMsg> {
        let block = self.active_archive_evidence()?;
        Some(TranscriptMsg::CloseEvidence { block_id: block.id })
    }

    // -- tail-follow anchor --------------------------------------------------

    /// True while the view is anchored to the bottom (anchor engaged).
    pub fn follow(&self) -> bool {
        self.anchored && !self.anchor_released
    }

    /// Engage the standing tail anchor (re-arms a released anchor).
    pub fn anchor(&mut self) {
        self.anchored = true;
        self.anchor_released = false;
    }

    /// The user scrolled up: follow disengages until back at the bottom.
    pub fn release_anchor(&mut self) {
        self.anchor_released = true;
    }

    pub fn on_mouse_scroll_up(&mut self) {
        self.release_anchor();
    }

    /// Wheel-down re-arms following once the host reports the view is back
    /// at the bottom (Python `call_after_refresh(_check_reanchor)` +
    /// `is_vertical_scroll_end`).
    pub fn on_mouse_scroll_down(&mut self, is_vertical_scroll_end: bool) {
        if is_vertical_scroll_end {
            self.anchor();
        }
    }

    // -- lane focus (DESIGN-SPEC §8) -----------------------------------------

    pub fn focused_lane(&self) -> Option<&str> {
        self.focused_lane.as_deref()
    }

    /// Swap the transcript to a subagent's own block list.
    pub fn focus_lane(
        &mut self,
        lane_id: &str,
        blocks: Vec<TranscriptBlock>,
        now: f64,
    ) -> TranscriptMsg {
        if self.focused_lane.is_none() {
            self.main_stash = Some(self.blocks());
        }
        self.focused_lane = Some(lane_id.to_string());
        self.swap(blocks, now);
        TranscriptMsg::LaneFocusChanged {
            lane_id: Some(lane_id.to_string()),
        }
    }

    /// Esc from a focused lane: restore the parent transcript.
    pub fn restore_main(&mut self, now: f64) -> Option<TranscriptMsg> {
        self.focused_lane.as_ref()?;
        let stash = self.main_stash.take().unwrap_or_default();
        self.focused_lane = None;
        self.swap(stash, now);
        Some(TranscriptMsg::LaneFocusChanged { lane_id: None })
    }

    fn swap(&mut self, blocks: Vec<TranscriptBlock>, now: f64) {
        self.blocks.clear();
        self.widgets.clear();
        self.order.clear();
        self.archive = None;
        self.archive_ids.clear();
        self.compaction_pending = false;
        for block in &blocks {
            self.blocks.insert(block.id().to_string(), block.clone());
            self.order.push(block.id().to_string());
        }
        let archive_count = if blocks.len() > HISTORY_COMPACT_TRIGGER {
            blocks.len() - HISTORY_WIDGET_LIMIT
        } else {
            0
        };
        let width = self.paint_width();
        if archive_count > 0 {
            let mut archive = HistoryArchive::new();
            archive.update_blocks(blocks[..archive_count].to_vec(), width);
            self.archive = Some(archive);
            self.archive_ids = blocks[..archive_count]
                .iter()
                .map(|block| block.id().to_string())
                .collect();
        }
        for block in &blocks[archive_count..] {
            let mut widget = build_block_widget(block.clone(), now);
            widget.repaint_block(width, now);
            self.widgets.insert(block.id().to_string(), widget);
        }
        self.anchor(); // a lane swap always lands anchored at the bottom
    }

    // -- resize reflow (75ms trailing debounce; streaming deferral) -----------

    pub fn streaming(&self) -> bool {
        self.streaming
    }

    /// Mark the live tail active/idle.
    ///
    /// Turning streaming off releases any deferred reflow — exactly one
    /// forced reflow after consolidation (RESEARCH-BRIEF risk 3).
    pub fn set_streaming(&mut self, streaming: bool, now: f64) {
        self.streaming = streaming;
        if !streaming && self.reflow_deferred {
            self.flush_reflow(now);
        }
    }

    /// The view's width changed. Returns the trailing-debounce delay the
    /// app must (re)schedule — a later resize inside the window restarts
    /// the timer; when it elapses, call [`TranscriptView::debounce_fired`].
    /// `None` means no timer: the width was unchanged/zero, or this was
    /// the first layout (not a reflow — children repaint immediately via
    /// their own resize instead of waiting out the debounce).
    pub fn on_resize(&mut self, width: usize, _now: f64) -> Option<f64> {
        if width == 0 || Some(width) == self.last_width {
            return None;
        }
        let initial_layout = self.last_width.is_none();
        self.last_width = Some(width);
        if initial_layout {
            return None;
        }
        self.reflow_hold = true;
        Some(REFLOW_DEBOUNCE_SECONDS)
    }

    /// The debounce timer elapsed (Python `_debounce_fired`).
    pub fn debounce_fired(&mut self, now: f64) {
        if self.streaming {
            self.reflow_deferred = true;
            return;
        }
        self.flush_reflow(now);
    }

    /// Repaint every block at the current width (pure fn of width).
    fn flush_reflow(&mut self, now: f64) {
        self.reflow_hold = false;
        self.reflow_deferred = false;
        let width = self.paint_width();
        if let Some(archive) = self.archive.as_mut() {
            archive.repaint_archive(width);
        }
        for widget in self.widgets.values_mut() {
            widget.repaint_block(width, now);
        }
    }

    /// BlockWidget resize hook: true = deferred to the debounced flush.
    ///
    /// Streaming always defers (independently of resize event ordering
    /// between the view and its children); otherwise a repaint is held
    /// only inside the view's debounce window.
    fn route_reflow(&mut self) -> bool {
        if self.streaming {
            self.reflow_deferred = true;
            return true;
        }
        self.reflow_hold
    }

    /// A mounted block's own width changed (Python `BlockWidget.on_resize`
    /// consulting the view's reflow router).
    pub fn block_resized(&mut self, block_id: &str, width: usize, now: f64) {
        let painted = match self.widgets.get(block_id) {
            Some(TranscriptWidget::Block(widget)) => widget.painted_width(),
            _ => return, // needs-you rows re-layout themselves (repaint no-op)
        };
        if width == 0 || Some(width) == painted {
            return;
        }
        if self.route_reflow() {
            return; // deferred: the TranscriptView owns the debounce
        }
        if let Some(TranscriptWidget::Block(widget)) = self.widgets.get_mut(block_id) {
            widget.repaint_block(width, now);
        }
    }

    /// The archive's width changed (Python `HistoryArchive.on_resize`).
    pub fn archive_resized(&mut self, width: usize) {
        let painted = match self.archive.as_ref() {
            Some(archive) => archive.painted_width,
            None => return,
        };
        if width == 0 || Some(width) == painted {
            return;
        }
        if self.route_reflow() {
            return;
        }
        if let Some(archive) = self.archive.as_mut() {
            archive.repaint_archive(width);
        }
    }
}

#[cfg(test)]
mod tests {
    //! Pins `tests/test_ui_transcript_view.py`. Each test is named after
    //! the Python case it ports; Textual pilot mechanics are replaced by
    //! direct calls to the ported state machine (clicks → `click(row)`,
    //! key presses → `handle_key`, timers → explicit fire methods).

    use super::*;
    use crate::model::blocks::{
        Answer, EvidenceBlock, Narration, NeedsYouChoice, NeedsYouEntry, SessionBanner,
        ToolLineStatus, TurnRule, UserLine, WorkingStatus,
    };
    use crate::model::turn::TurnTelemetry;
    use crate::ui::live_tail::answer_spans;

    fn tool() -> ToolLine {
        ToolLine {
            body: vec!["1214 passed".to_string(), "build succeeded".to_string()],
            status: ToolLineStatus::Completed,
            ..ToolLine::new("b2", "Ran 2 shell commands")
        }
    }

    /// The stored block, asserted to be a ToolLine (Python `_block`).
    fn tool_block(view: &TranscriptView, block_id: &str) -> ToolLine {
        match view.get_block(block_id) {
            Some(TranscriptBlock::ToolLine(block)) => block,
            other => panic!("expected tool line, got {other:?}"),
        }
    }

    fn narration_text(view: &TranscriptView, block_id: &str) -> String {
        match view.get_block(block_id) {
            Some(TranscriptBlock::Narration(block)) => block.text,
            other => panic!("expected narration, got {other:?}"),
        }
    }

    #[test]
    fn test_append_replace_remove_keyed_by_block_id() {
        let mut view = TranscriptView::new();
        view.append(
            TranscriptBlock::UserLine(UserLine::new("b1", "hello")),
            0.0,
        )
        .unwrap();
        view.append(
            TranscriptBlock::Narration(Narration::new("b2", "working on it")),
            0.0,
        )
        .unwrap();
        assert_eq!(view.block_ids(), ["b1", "b2"]);

        view.replace(
            TranscriptBlock::Narration(Narration::new("b2", "revised narration")),
            0.0,
        )
        .unwrap();
        assert_eq!(narration_text(&view, "b2"), "revised narration");
        assert_eq!(view.block_ids(), ["b1", "b2"]); // replace is in place

        view.remove_block("b1").unwrap();
        assert_eq!(view.block_ids(), ["b2"]);
        assert_eq!(
            view.replace(TranscriptBlock::Narration(Narration::new("b1", "gone")), 0.0),
            Err("unknown block id: 'b1'".to_string())
        );
        assert_eq!(
            view.append(
                TranscriptBlock::Narration(Narration::new("b2", "duplicate")),
                0.0
            )
            .err(),
            Some("duplicate block id: 'b2'".to_string())
        );
    }

    #[test]
    fn test_tool_line_click_toggles_body_in_place() {
        let mut view = TranscriptView::new();
        view.append(TranscriptBlock::ToolLine(tool()), 0.0).unwrap();
        assert!(!tool_block(&view, "b2").expanded);

        let msg = view
            .get_widget_mut("b2")
            .and_then(TranscriptWidget::as_block_mut)
            .expect("flat BlockWidget")
            .click(0, 0.0);
        view.on_tool_line_toggled("b2"); // the view's message handler
        assert!(tool_block(&view, "b2").expanded);
        assert_eq!(
            msg,
            Some(TranscriptMsg::ToolLineToggled {
                block_id: "b2".to_string(),
                expanded: true
            })
        );
        // Same widget, same block id — toggled IN PLACE.
        assert_eq!(view.block_ids(), ["b2"]);

        let msg = view
            .get_widget_mut("b2")
            .and_then(TranscriptWidget::as_block_mut)
            .expect("flat BlockWidget")
            .click(0, 0.0);
        view.on_tool_line_toggled("b2");
        assert!(!tool_block(&view, "b2").expanded);
        assert_eq!(
            msg,
            Some(TranscriptMsg::ToolLineToggled {
                block_id: "b2".to_string(),
                expanded: false
            })
        );
    }

    #[test]
    fn test_tool_line_enter_toggles_when_focused() {
        let mut view = TranscriptView::new();
        view.append(TranscriptBlock::ToolLine(tool()), 0.0).unwrap();
        let widget = view
            .get_widget_mut("b2")
            .and_then(TranscriptWidget::as_block_mut)
            .expect("flat BlockWidget");
        assert!(widget.can_focus()); // widget.focus() is host wiring
        let msg = widget.handle_key("enter", 0.0);
        view.on_tool_line_toggled("b2");
        assert!(tool_block(&view, "b2").expanded);
        assert_eq!(
            msg,
            Some(TranscriptMsg::ToolLineToggled {
                block_id: "b2".to_string(),
                expanded: true
            })
        );
    }

    #[test]
    fn test_answer_click_posts_show_evidence() {
        let mut view = TranscriptView::new();
        let links = vec![EvidenceLink::new("tests pass", "pytest run")];
        view.append(
            TranscriptBlock::Answer(Answer {
                evidence_refs: links.clone(),
                ..Answer::new("b3", vec![Segment::new("All done.")])
            }),
            0.0,
        )
        .unwrap();
        let msg = view
            .get_widget_mut("b3")
            .and_then(TranscriptWidget::as_block_mut)
            .expect("flat BlockWidget")
            .click(0, 0.0);
        assert_eq!(
            msg,
            Some(TranscriptMsg::ShowEvidence {
                block_id: "b3".to_string(),
                links
            })
        );
    }

    /// A click on a fenced code row posts CopyCodeFence with the dedented
    /// fence source (finer-grained than /copy's whole-answer grab); a click
    /// anywhere else on the answer still opens evidence.
    #[test]
    fn test_clicking_a_code_fence_copies_just_that_fence() {
        let mut view = TranscriptView::new();
        assert_eq!(view.on_resize(80, 0.0), None);
        let src = "Intro line.\n\n```python\nprint('hi')\nx = 1\n```";
        let links = vec![EvidenceLink::new("c", "r")];
        view.append(
            TranscriptBlock::Answer(Answer {
                evidence_refs: links,
                ..Answer::new("b7", answer_spans(src))
            }),
            0.0,
        )
        .unwrap();
        let widget = view
            .get_widget_mut("b7")
            .and_then(TranscriptWidget::as_block_mut)
            .expect("flat BlockWidget");
        let lines = render_block(widget.block(), widget.painted_width().unwrap());
        let fence_row = (0..lines.len())
            .find(|row| fence_text_at_row(&lines, *row as isize).is_some())
            .expect("a fence row renders");
        let msg = widget.click(fence_row as isize, 0.0);
        assert_eq!(
            msg,
            Some(TranscriptMsg::CopyCodeFence {
                block_id: "b7".to_string(),
                text: "print('hi')\nx = 1".to_string()
            })
        );
        // A fence click never opens evidence; a click on the intro (row 0)
        // is not a fence — evidence still opens.
        let msg = widget.click(0, 0.0);
        assert!(matches!(
            msg,
            Some(TranscriptMsg::ShowEvidence { ref block_id, .. }) if block_id == "b7"
        ));
    }

    /// Mockup click: null lines (agent tree rows, ✳ recap-shaped lines)
    /// are NOT evidence click targets — clicking them posts nothing.
    #[test]
    fn test_inert_answer_lines_ignore_clicks() {
        let mut view = TranscriptView::new();
        view.append(
            TranscriptBlock::Answer(Answer {
                clickable: false,
                ..Answer::new(
                    "b5",
                    vec![
                        Segment {
                            style_token: StyleToken::Green,
                            ..Segment::new("  ├─ ✔ ")
                        },
                        Segment {
                            style_token: StyleToken::Dim,
                            ..Segment::new("researcher · done")
                        },
                    ],
                )
            }),
            0.0,
        )
        .unwrap();
        view.append(
            TranscriptBlock::Answer(Answer {
                clickable: false,
                ..Answer::new(
                    "b6",
                    vec![
                        Segment {
                            style_token: StyleToken::Dimmer,
                            ..Segment::new("✳ ")
                        },
                        Segment {
                            style_token: StyleToken::Dim,
                            italic: true,
                            ..Segment::new("Plan ready.")
                        },
                    ],
                )
            }),
            0.0,
        )
        .unwrap();
        for block_id in ["b5", "b6"] {
            let msg = view
                .get_widget_mut(block_id)
                .and_then(TranscriptWidget::as_block_mut)
                .expect("flat BlockWidget")
                .click(0, 0.0);
            assert_eq!(msg, None);
        }
    }

    #[test]
    fn test_turn_rule_click_posts_open_rewind_with_checkpoint_id() {
        let mut view = TranscriptView::new();
        view.append(
            TranscriptBlock::TurnRule(TurnRule::new(
                "b4",
                "t7",
                "12s · 3.1k tok · $0.08 · answer",
            )),
            0.0,
        )
        .unwrap();
        let msg = view
            .get_widget_mut("b4")
            .and_then(TranscriptWidget::as_block_mut)
            .expect("flat BlockWidget")
            .click(0, 0.0);
        assert_eq!(
            msg,
            Some(TranscriptMsg::OpenRewind {
                checkpoint_id: "t7".to_string()
            })
        );
    }

    #[test]
    fn test_lane_focus_swaps_block_list_and_restore_brings_main_back() {
        let mut view = TranscriptView::new();
        view.append(
            TranscriptBlock::UserLine(UserLine {
                mode: "build".to_string(),
                ..UserLine::new("b1", "parent turn")
            }),
            0.0,
        )
        .unwrap();
        view.append(
            TranscriptBlock::Narration(Narration::new("b2", "spawning agents")),
            0.0,
        )
        .unwrap();

        let child_blocks = vec![
            TranscriptBlock::SessionBanner(SessionBanner {
                focus_note: "focused: test-writer · subagent of a1b2c3 · own context window \
                             · results report back to parent · esc back"
                    .to_string(),
                ..SessionBanner::new("c1", "")
            }),
            TranscriptBlock::UserLine(UserLine {
                mode: "delegated".to_string(),
                ..UserLine::new("c2", "write the tests")
            }),
        ];
        let msg = view.focus_lane("lane-1", child_blocks, 0.0);
        assert_eq!(view.focused_lane(), Some("lane-1"));
        assert_eq!(view.block_ids(), ["c1", "c2"]);
        assert_eq!(
            msg,
            TranscriptMsg::LaneFocusChanged {
                lane_id: Some("lane-1".to_string())
            }
        );

        // Mutations while focused address the stashed PARENT list (spec §8:
        // the parent turn keeps accumulating during focus; the child view
        // is a read-only snapshot) — mockup this.lines vs focusLines.
        view.replace(
            TranscriptBlock::Narration(Narration::new("b2", "agents finishing up")),
            0.0,
        )
        .unwrap();
        assert!(view
            .append(
                TranscriptBlock::Narration(Narration::new("b3", "final answer landed")),
                0.0
            )
            .unwrap()
            .is_none());
        view.remove_block("b1").unwrap();
        assert_eq!(
            view.replace(
                TranscriptBlock::UserLine(UserLine {
                    mode: "delegated".to_string(),
                    ..UserLine::new("c2", "child ids are not addressable")
                }),
                0.0
            ),
            Err("unknown block id: 'c2'".to_string())
        );
        assert_eq!(view.block_ids(), ["c1", "c2"]); // visible child list untouched

        let msg = view.restore_main(0.0); // the app's esc handler
        assert_eq!(view.focused_lane(), None);
        assert_eq!(view.block_ids(), ["b2", "b3"]);
        assert_eq!(narration_text(&view, "b2"), "agents finishing up");
        assert_eq!(narration_text(&view, "b3"), "final answer landed");
        assert_eq!(
            msg,
            Some(TranscriptMsg::LaneFocusChanged { lane_id: None })
        );
    }

    /// Adapted from the Python pilot test: the scroll geometry
    /// (`scroll_to`, `is_vertical_scroll_end`) is the render host's; the
    /// ported state machine pins the follow flag's transitions.
    #[test]
    fn test_tail_follow_sticks_to_bottom_until_user_scrolls_up() {
        let mut view = TranscriptView::new();
        for index in 0..30 {
            view.append(
                TranscriptBlock::Narration(Narration::new(
                    format!("b{index}"),
                    format!("line {index}"),
                )),
                0.0,
            )
            .unwrap();
        }
        assert!(view.follow());

        // User scrolls up: follow disengages, appends stop moving the view.
        view.on_mouse_scroll_up();
        assert!(!view.follow());
        view.append(
            TranscriptBlock::Narration(Narration::new("b99", "new line while scrolled up")),
            0.0,
        )
        .unwrap();
        assert!(!view.follow());
        // Wheel-down mid-scroll (not at the bottom) does not re-arm …
        view.on_mouse_scroll_down(false);
        assert!(!view.follow());
        // … scrolling back to the bottom re-arms following.
        view.on_mouse_scroll_down(true);
        assert!(view.follow());
    }

    #[test]
    fn test_resize_reflow_debounced_and_width_pure() {
        let mut view = TranscriptView::new();
        // First layout is not a reflow: no debounce timer.
        assert_eq!(view.on_resize(100, 0.0), None);
        view.append(
            TranscriptBlock::TurnRule(TurnRule::new("b1", "t1", "1s · 10 tok · $0.01 · answer")),
            0.0,
        )
        .unwrap();
        let painted = |view: &TranscriptView| {
            view.get_widget("b1")
                .and_then(TranscriptWidget::as_block)
                .expect("flat BlockWidget")
                .painted_width()
        };
        let first_width = painted(&view);
        assert_eq!(first_width, Some(100));

        let delay = view.on_resize(60, 1.0);
        assert_eq!(delay, Some(REFLOW_DEBOUNCE_SECONDS));
        // The child's own resize inside the debounce window is deferred.
        view.block_resized("b1", 60, 1.0);
        assert_eq!(painted(&view), first_width);

        view.debounce_fired(1.0 + REFLOW_DEBOUNCE_SECONDS); // > 75ms trailing debounce
        assert_eq!(painted(&view), Some(60));
        assert_ne!(painted(&view), first_width);
    }

    #[test]
    fn test_resize_reflow_deferred_while_streaming_then_forced_once() {
        let mut view = TranscriptView::new();
        assert_eq!(view.on_resize(100, 0.0), None);
        view.append(
            TranscriptBlock::TurnRule(TurnRule::new("b1", "t1", "1s · 10 tok · $0.01 · answer")),
            0.0,
        )
        .unwrap();
        let painted = |view: &TranscriptView| {
            view.get_widget("b1")
                .and_then(TranscriptWidget::as_block)
                .expect("flat BlockWidget")
                .painted_width()
        };
        let streamed_width = painted(&view);

        view.set_streaming(true, 0.5);
        assert!(view.on_resize(60, 1.0).is_some());
        view.block_resized("b1", 60, 1.0);
        view.debounce_fired(1.0 + REFLOW_DEBOUNCE_SECONDS);
        // Deferred: painted width is stale while the stream is active.
        assert_eq!(painted(&view), streamed_width);
        assert_ne!(Some(60), streamed_width);

        view.set_streaming(false, 2.0); // consolidation → exactly one forced reflow
        assert_eq!(painted(&view), Some(60));
    }

    /// Timer mechanics adapted: the app's 1s interval drives
    /// `advance_spinner` explicitly; timer teardown on unmount is host
    /// wiring (the widget simply drops).
    #[test]
    fn test_working_status_widget_pulses_spinner() {
        // Mockup runTurn: the working-line glyph advances once per second
        // inside the 1000ms tick (the 260ms spinTimer is the title bar's).
        assert!((SPINNER_INTERVAL_SECONDS - 1.0).abs() < 1e-9);

        let mut view = TranscriptView::new();
        view.append(
            TranscriptBlock::WorkingStatus(WorkingStatus {
                agent_count: 0,
                ..WorkingStatus::new(
                    "b1",
                    TurnTelemetry {
                        tokens_down: 100,
                        ..TurnTelemetry::new(1.0)
                    },
                )
            }),
            0.0,
        )
        .unwrap();
        let widget = view
            .get_widget_mut("b1")
            .and_then(TranscriptWidget::as_block_mut)
            .expect("flat BlockWidget");
        widget.advance_spinner(SPINNER_INTERVAL_SECONDS + 0.2); // > one 1s glyph interval
        assert!(widget.spinner_offset() >= 1);
        // The paint-time derivation advances the glyph frame AND the
        // wall-clock secs between event-driven replaces.
        match widget.display_block(SPINNER_INTERVAL_SECONDS + 0.2) {
            TranscriptBlock::WorkingStatus(status) => {
                assert_eq!(status.spinner_frame, 1);
                assert_eq!(status.telemetry.secs, 2.0);
            }
            other => panic!("expected working status, got {other:?}"),
        }
        // Removing the block (turn end) drops the widget — the host stops
        // the pulse timer with it.
        view.remove_block("b1").unwrap();
        assert!(view.get_widget("b1").is_none());
    }

    /// The `_motion_timer` sibling of
    /// `test_working_status_widget_pulses_spinner`: the app's shimmer-cadence
    /// clock drives `advance_working_motion`, and `display_blocks` folds the
    /// moved band into the paint-time block — with the chasing highlight
    /// changing styles but never text
    /// (`test_ui_transcript_render.py::test_working_label_has_a_chasing_highlight_without_changing_text`).
    #[test]
    fn test_working_status_widget_advances_motion_without_changing_text() {
        // Python: MOTION_INTERVAL_SECONDS = SHIMMER_INTERVAL_SECONDS (0.08s).
        assert!((MOTION_INTERVAL_SECONDS - SHIMMER_INTERVAL_SECONDS).abs() < 1e-9);

        let mut view = TranscriptView::new();
        view.append(
            TranscriptBlock::WorkingStatus(WorkingStatus {
                agent_count: 1,
                ..WorkingStatus::new("b1", TurnTelemetry::new(1.0))
            }),
            10.0,
        )
        .unwrap();
        let motion_frame = |view: &TranscriptView, now: f64| -> u32 {
            match view.display_blocks(now).pop() {
                Some(TranscriptBlock::WorkingStatus(status)) => status.motion_frame,
                other => panic!("expected working status, got {other:?}"),
            }
        };
        assert_eq!(motion_frame(&view, 10.0), 0);
        let before = render_block(&view.display_blocks(10.0).pop().unwrap(), 80);

        // Three shimmer intervals → the band's peak moved three cells.
        assert!(view.advance_working_motion(10.0 + MOTION_INTERVAL_SECONDS));
        assert!(view.advance_working_motion(10.0 + 2.0 * MOTION_INTERVAL_SECONDS));
        assert!(view.advance_working_motion(10.0 + 3.0 * MOTION_INTERVAL_SECONDS));
        let now = 10.0 + 3.0 * MOTION_INTERVAL_SECONDS;
        assert_eq!(motion_frame(&view, now), 3);
        let after = render_block(&view.display_blocks(now).pop().unwrap(), 80);
        assert_ne!(before[0], after[0], "the shimmer band swept the label");
        let text = |line: &[Segment]| -> String {
            line.iter().map(|segment| segment.text.as_str()).collect()
        };
        assert_eq!(text(&before[0]), text(&after[0]), "motion never mutates text");

        // Removing the block (turn end) stops the motion with the widget —
        // and a transcript without a working line is a no-op advance.
        view.remove_block("b1").unwrap();
        view.append(
            TranscriptBlock::Answer(Answer::new("b2", vec![Segment::new("done")])),
            11.0,
        )
        .unwrap();
        assert!(!view.advance_working_motion(11.0 + MOTION_INTERVAL_SECONDS));
    }

    /// The archive preserves infinite scroll/copy and old tool interactivity.
    #[test]
    fn test_old_history_compacts_without_losing_text_or_actions() {
        let mut view = TranscriptView::new();
        assert_eq!(view.on_resize(100, 0.0), None);
        let old_tool = ToolLine {
            body: vec!["README.md".to_string(), "config.yaml".to_string()],
            status: ToolLineStatus::Completed,
            ..ToolLine::new("old-tool", "Read the original setup")
        };
        view.append(TranscriptBlock::ToolLine(old_tool.clone()), 0.0)
            .unwrap();
        for index in 0..(HISTORY_COMPACT_TRIGGER + 20) {
            view.append(
                TranscriptBlock::Narration(Narration::new(
                    format!("archive-{index}"),
                    format!("history line {index}"),
                )),
                0.0,
            )
            .unwrap();
        }
        assert!(view.compaction_pending());
        view.compact_history(); // Python: the scheduled call_later fires

        assert!(view.widget_count() <= HISTORY_WIDGET_LIMIT);
        assert!(view.get_widget("old-tool").is_none());
        assert_eq!(
            view.get_block("old-tool"),
            Some(TranscriptBlock::ToolLine(old_tool))
        );
        // Selection/copy source stays intact (Python get_selection(SELECT_ALL)).
        let selected = view.archive().expect("archive exists").plain_text();
        assert!(selected.contains("Read the original setup"));
        assert!(selected.contains("history line 0"));
        // The consolidated markup preserves the click action metadata.
        assert!(view
            .archive()
            .unwrap()
            .markup()
            .contains("[@click=archive_activate('old-tool')]"));

        let msg = view.archive_activate("old-tool", 0.0);
        assert!(tool_block(&view, "old-tool").expanded);
        assert!(view.archive().unwrap().plain_text().contains("README.md"));
        assert_eq!(
            msg,
            Some(TranscriptMsg::ToolLineToggled {
                block_id: "old-tool".to_string(),
                expanded: true
            })
        );

        view.remove_block("old-tool").unwrap();
        assert_eq!(view.get_block("old-tool"), None);
        assert!(!view
            .archive()
            .unwrap()
            .plain_text()
            .contains("Read the original setup"));
    }

    /// Not a pinned Python test: wire-parity oracle. Each expected string
    /// is the exact `HistoryArchive._block_markup(...)` output of the real
    /// Python widget (captured from `uv run python`), pinning the `@click`
    /// action metadata and theme-token markup byte-for-byte.
    #[test]
    fn oracle_archive_block_markup_matches_python() {
        let tool = TranscriptBlock::ToolLine(ToolLine {
            body: vec!["README.md".to_string(), "config.yaml".to_string()],
            status: ToolLineStatus::Completed,
            ..ToolLine::new("old-tool", "Read the original setup")
        });
        let (markup, _) = HistoryArchive::block_markup(&tool, 100);
        assert_eq!(
            markup,
            "[@click=archive_activate('old-tool')][$dim]  ● [/][/]\
             [@click=archive_activate('old-tool')][$dim]Read the original setup[/][/]\
             [@click=archive_activate('old-tool')][$dimmer] · click to expand[/][/]"
        );
        let needs_you = TranscriptBlock::NeedsYou(NeedsYouBlock::new(
            "old-decision",
            vec![NeedsYouEntry {
                choices: vec![NeedsYouChoice::new("yes", "apply it")],
                ..NeedsYouEntry::new("d1", "Apply the safe change?")
            }],
        ));
        let (markup, _) = HistoryArchive::block_markup(&needs_you, 100);
        assert_eq!(
            markup,
            "[$orange]· [/][$orange]Needs you  1 deferred decision[/]\n\
             [@click=archive_decision('old-decision', 0, 0)][$orange]  1 [/][/]\
             [@click=archive_decision('old-decision', 0, 0)][$fg]Apply the safe change?[/][/]\
             [@click=archive_decision('old-decision', 0, 0)][$fg]  [/][/]\
             [@click=archive_decision('old-decision', 0, 0)][$green on $bg-tab]\\[yes][/][/]"
        );
    }

    /// Consolidation retains every non-tool interaction contract.
    #[test]
    fn test_archived_history_retains_answer_rewind_evidence_and_decisions() {
        let mut view = TranscriptView::new();
        assert_eq!(view.on_resize(100, 0.0), None);
        let link = EvidenceLink::new("the claim", "read_file · source.py");
        view.append(
            TranscriptBlock::Answer(Answer {
                evidence_refs: vec![link.clone()],
                ..Answer::new("old-answer", vec![Segment::new("Grounded answer")])
            }),
            0.0,
        )
        .unwrap();
        view.append(
            TranscriptBlock::TurnRule(TurnRule::new("old-turn", "checkpoint-7", "7s · answer")),
            0.0,
        )
        .unwrap();
        view.append(
            TranscriptBlock::Evidence(EvidenceBlock::new("old-evidence", vec![link.clone()])),
            0.0,
        )
        .unwrap();
        view.append(
            TranscriptBlock::NeedsYou(NeedsYouBlock::new(
                "old-decision",
                vec![NeedsYouEntry {
                    choices: vec![NeedsYouChoice::new("yes", "apply it")],
                    ..NeedsYouEntry::new("decision-1", "Apply the safe change?")
                }],
            )),
            0.0,
        )
        .unwrap();
        for index in 0..(HISTORY_COMPACT_TRIGGER + 20) {
            view.append(
                TranscriptBlock::Narration(Narration::new(
                    format!("tail-{index}"),
                    format!("tail line {index}"),
                )),
                0.0,
            )
            .unwrap();
        }
        view.compact_history();
        assert!(view.archive().is_some());

        let evidence_msg = view.archive_activate("old-answer", 0.0);
        let rewind_msg = view.archive_activate("old-turn", 0.0);
        assert_eq!(view.archive_activate("old-evidence", 0.0), None);
        let expand_msg = view.archive_evidence_expand();
        let close_msg = view.archive_close_evidence();
        let decision = view.archive_decision("old-decision", 0, 0);

        assert_eq!(
            evidence_msg,
            Some(TranscriptMsg::ShowEvidence {
                block_id: "old-answer".to_string(),
                links: vec![link.clone()]
            })
        );
        assert_eq!(
            rewind_msg,
            Some(TranscriptMsg::OpenRewind {
                checkpoint_id: "checkpoint-7".to_string()
            })
        );
        assert_eq!(
            expand_msg,
            Some(TranscriptMsg::ExpandEvidenceClaim {
                block_id: "old-evidence".to_string(),
                link
            })
        );
        assert_eq!(
            close_msg,
            Some(TranscriptMsg::CloseEvidence {
                block_id: "old-evidence".to_string()
            })
        );
        let decision = decision.expect("decision chip acts");
        assert_eq!(decision.item_id, "decision-1");
        assert_eq!(decision.choice, "apply it");
    }
}
