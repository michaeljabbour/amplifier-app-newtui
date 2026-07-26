//! Agent lanes overlay strip (DESIGN-SPEC §8, §2 overlay strips).
//!
//! Port of `src/amplifier_app_newtui/ui/lanes_panel.py`.
//!
//! A bordered strip docked ABOVE the composer, toggled by ctrl-t / `/tasks`:
//!
//! - Header: `Agent lanes · ↑↓ select · enter focus · ctrl-o tail · esc close`
//!   (`Agent lanes` bright bold, the hint dimmer).
//! - One aligned line per subagent (Claude Code's live agent panel):
//!   `  <glyph> <name> · <activity> · <elapsed> · ↓ Nk tokens · $<cost>` —
//!   name / activity / elapsed / token columns padded to their widest entry
//!   so the `·` separators line up exactly like the mockup. Line color
//!   comes from the lane state's theme token (`◐` teal running, `■` fg
//!   working, `✔` dim done).
//!
//! `↑`/`↓` move the selection (highlighted `bg-tab`), Enter or a click
//! yields [`LanesMsg::FocusLane`]; Esc yields [`LanesMsg::Closed`] and
//! hides the panel. The panel never swaps transcripts itself — focusing a
//! lane is the app's job.
//!
//! Ratatui adaptation: the pure logic ([`format_lane_lines`],
//! [`lane_elapsed`], the selection/tail/motion state machine) ports;
//! Textual widget mechanics do not:
//!
//! - Messages (`FocusLane` / `Closed` / `TypeThrough`) become the returned
//!   [`LanesMsg`] values (same pattern as `ui/rewind_strip.rs`).
//! - The `set_interval` motion timer becomes the exported
//!   [`LANE_MOTION_INTERVAL_SECONDS`] cadence + [`LanesPanel::motion_running`]:
//!   app assembly schedules the tick and calls [`LanesPanel::advance_motion`]
//!   each interval while the flag is true (injected-clock pattern of
//!   `ui/live_tail.rs`).
//! - Row/header `render()` become [`LanesPanel::row_segments`] /
//!   [`LanesPanel::header_segments`]; the `-selected` CSS class becomes
//!   [`LanesPanel::is_selected`] (the app paints `bg-tab` on that row).
//! - `container_size.width` becomes the `width` argument of
//!   [`LanesPanel::lane_lines`] (rows refit on resize by re-rendering);
//!   the remount/patch machinery (`_refresh_or_rebuild_rows`,
//!   `_remount_rows`, `call_later`) does not port — state is plain data and
//!   every render is a fresh pure projection, so motion frames survive
//!   updates by construction.
//! - The `_LaneTail` child widget becomes the mounted-tail state
//!   ([`LanesPanel::show_lane_tail`] / [`LanesPanel::has_lane_tail`] /
//!   [`LanesPanel::tail_row_index`] / [`LanesPanel::tail_markup`]); app
//!   assembly renders the markup directly under that row.

use std::collections::HashMap;

use crate::model::blocks::{Segment, StyleToken};
use crate::model::formatting::format_tokens_k;
use crate::model::lanes::{lane_labels, LaneRecord, LaneState, LaneStateName};
use crate::ui::live_tail::lane_tail_markup;
use crate::ui::motion::{shimmer_band, SHIMMER_INTERVAL_SECONDS};

pub const LANES_HEADER_TITLE: &str = "Agent lanes";
pub const LANES_HEADER_HINT: &str = "· ↑↓ select · enter focus · ctrl-o tail · esc close";
/// Exact header line per DESIGN-SPEC §8.
pub const LANES_HEADER: &str = "Agent lanes · ↑↓ select · enter focus · ctrl-o tail · esc close";

/// Active-only soft-band cadence for agent names.
pub const LANE_MOTION_INTERVAL_SECONDS: f64 = SHIMMER_INTERVAL_SECONDS;

/// Claude-Code lane elapsed precision: `41s` / `5m 48s`.
///
/// Under a minute renders whole seconds (`41s`); at or above, minutes
/// plus zero-padded seconds (`348` → `5m 48s`, `124` → `2m 04s`)
/// so the live per-agent clock reads like Claude Code's agent panel.
pub fn lane_elapsed(seconds: f64) -> String {
    // Python `round()` is banker's rounding (ties to even).
    let total = seconds.round_ties_even() as i64;
    if total < 60 {
        format!("{total}s")
    } else {
        format!("{}m {:02}s", total / 60, total % 60)
    }
}

/// Floor for the elided activity column — below this, readability is gone
/// and the tokens column is dropped whole instead.
const MIN_ACTIVITY_WIDTH: usize = 8;

/// Python `len(str)` / `str[:n]` are character-based — mirror that.
fn char_len(text: &str) -> usize {
    text.chars().count()
}

/// Left-align pad to `width` characters (Python `f"{text:<{width}}"`).
fn pad(text: &str, width: usize) -> String {
    let len = char_len(text);
    if len >= width {
        text.to_string()
    } else {
        format!("{}{}", text, " ".repeat(width - len))
    }
}

fn elide(text: &str, budget: usize) -> String {
    let chars: Vec<char> = text.chars().collect();
    if chars.len() <= budget {
        return text.to_string();
    }
    let keep = budget.saturating_sub(1).max(1);
    let mut out: String = chars[..keep].iter().collect();
    out.push('…');
    out
}

/// The optional keyword arguments of Python `format_lane_lines`.
#[derive(Clone, Copy, Debug, Default)]
pub struct LaneLineOpts<'a> {
    /// Appends the DESIGN-SPEC §8 `▸` tail marker to that lane's name.
    pub tailed_index: Option<usize>,
    /// Disambiguates same-named agent lanes (LaneRegistry order); absent,
    /// the raw agent name is the label (unique-name fast path).
    pub labels: Option<&'a [String]>,
    /// The row budget (see [`format_lane_lines`]).
    pub width: Option<usize>,
    /// Aligned to *lanes*: appends a `▸ N queued` steer badge (issue #39).
    pub queued_counts: Option<&'a [usize]>,
}

/// Aligned lane lines per Claude Code's live agent panel:
/// `  <glyph> <name> · <activity> · <elapsed> · ↓ Nk tokens · $<cost>`.
///
/// Name, activity, elapsed and token columns are padded to the widest
/// entry so every `·` separator column lines up (mockup alignment).
/// `tailed_index` appends the DESIGN-SPEC §8 `▸` tail marker to that
/// lane's name (inside the padded name column, so alignment holds).
///
/// `queued_counts` (aligned to *lanes*) appends a `▸ N queued` steer
/// badge after the cost when a lane has messages queued for it (issue
/// #39) — it rhymes with the tail-pin `▸` and sits last so it never
/// disturbs the aligned columns.
///
/// `width` is the row budget: rows are height-1 lines, so overflow is
/// CROPPED, and what fell off was the right-side telemetry — the panel's
/// whole point. The elastic activity column is elided first; the tokens
/// column is dropped whole next. Name, elapsed and cost always survive.
pub fn format_lane_lines(lanes: &[LaneState], opts: LaneLineOpts<'_>) -> Vec<String> {
    if lanes.is_empty() {
        return Vec::new();
    }
    let display: Vec<String> = match opts.labels {
        Some(labels) => labels.to_vec(),
        None => lanes.iter().map(|lane| lane.name.clone()).collect(),
    };
    let badges: Vec<String> = (0..lanes.len())
        .map(|index| match opts.queued_counts {
            Some(counts) if index < counts.len() && counts[index] > 0 => {
                format!("▸ {} queued", counts[index])
            }
            _ => String::new(),
        })
        .collect();
    let names: Vec<String> = (0..lanes.len())
        .map(|index| {
            if Some(index) == opts.tailed_index {
                format!("{} ▸", display[index])
            } else {
                display[index].clone()
            }
        })
        .collect();
    let activities: Vec<String> = lanes.iter().map(|lane| lane.activity.clone()).collect();
    let elapsed: Vec<String> = lanes.iter().map(|lane| lane_elapsed(lane.elapsed)).collect();
    let tokens: Vec<String> = lanes
        .iter()
        .map(|lane| format!("↓ {} tokens", format_tokens_k(lane.tokens)))
        .collect();
    // Python `f"${lane.cost:.2f}"` rounds half-even; rust_decimal's `{:.2}`
    // TRUNCATES excess scale, so round first (`round_dp` defaults to
    // banker's rounding — the same strategy).
    let costs: Vec<String> =
        lanes.iter().map(|lane| format!("${:.2}", lane.cost.round_dp(2))).collect();
    let name_w = names.iter().map(|name| char_len(name)).max().unwrap_or(0);
    let el_w = elapsed.iter().map(|text| char_len(text)).max().unwrap_or(0);
    let tok_w = tokens.iter().map(|text| char_len(text)).max().unwrap_or(0);
    let cost_w = costs.iter().map(|text| char_len(text)).max().unwrap_or(0);

    let compose = |acts: &[String], act_w: usize, show_tokens: bool| -> Vec<String> {
        lanes
            .iter()
            .enumerate()
            .map(|(i, lane)| {
                let mut line = format!(
                    "  {} {} · {} · {}",
                    lane.glyph,
                    pad(&names[i], name_w),
                    pad(&acts[i], act_w),
                    pad(&elapsed[i], el_w),
                );
                if show_tokens {
                    line.push_str(&format!(" · {}", pad(&tokens[i], tok_w)));
                }
                line.push_str(&format!(" · {}", costs[i]));
                if !badges[i].is_empty() {
                    line.push_str(&format!(" · {}", badges[i]));
                }
                line
            })
            .collect()
    };

    let act_w = activities.iter().map(|text| char_len(text)).max().unwrap_or(0);
    // Everything but activity/tokens.
    let fixed = (4 + name_w + 3 + 3 + el_w + 3 + cost_w) as isize;
    let Some(width) = opts.width else {
        return compose(&activities, act_w, true);
    };
    let budget = width as isize - fixed - 3 - tok_w as isize;
    if budget >= act_w as isize {
        return compose(&activities, act_w, true);
    }
    if budget >= MIN_ACTIVITY_WIDTH as isize {
        let acts: Vec<String> =
            activities.iter().map(|activity| elide(activity, budget as usize)).collect();
        let act_w = acts.iter().map(|text| char_len(text)).max().unwrap_or(0);
        return compose(&acts, act_w, true);
    }
    let budget = (width as isize - fixed).max(MIN_ACTIVITY_WIDTH as isize) as usize;
    let acts: Vec<String> = activities.iter().map(|activity| elide(activity, budget)).collect();
    let act_w = acts.iter().map(|text| char_len(text)).max().unwrap_or(0);
    compose(&acts, act_w, false)
}

/// What the Textual widget posted as `LanesPanel.FocusLane` / `.Closed` /
/// `.TypeThrough` messages; the ratatui app-assembly layer receives these
/// as return values from the panel's entry points.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum LanesMsg {
    /// The user focused a lane (Enter or click).
    FocusLane { name: String, session_id: String },
    /// Esc pressed while the lanes panel was open.
    Closed,
    /// A printable key pressed while the panel held focus.
    ///
    /// Mockup ground truth (document-level keydown, composer input keeps
    /// focus while `lanesOpen`): typing is never swallowed by the lanes
    /// panel — the app forwards the character to the composer, so `/`
    /// opens the palette and mid-turn steering text lands in the input.
    TypeThrough { character: String },
}

/// The agent-lanes overlay strip (DESIGN-SPEC §8).
///
/// Feed it with [`LanesPanel::update_lanes`] (LaneRegistry records) and
/// toggle it with [`LanesPanel::show_panel`] / [`LanesPanel::hide_panel`].
/// Emits:
///
/// - [`LanesMsg::FocusLane`] — Enter on the selection or click on a row.
/// - [`LanesMsg::Closed`] — Esc (the panel also hides itself).
#[derive(Debug, Default)]
pub struct LanesPanel {
    records: Vec<LaneRecord>,
    selected: usize,
    tailed: Option<String>,
    queued: HashMap<String, usize>,
    motion_frame: usize,
    motion_running: bool,
    display: bool,
    tail_text: String,
    tail_mounted: bool,
}

impl LanesPanel {
    pub fn new() -> Self {
        Self::default()
    }

    // -- public API ----------------------------------------------------

    pub fn records(&self) -> &[LaneRecord] {
        &self.records
    }

    /// Whether the panel is visible (Textual `display`).
    pub fn display(&self) -> bool {
        self.display
    }

    /// The exact aligned lane line strings currently displayed.
    ///
    /// `width` is the panel's content width (Python read
    /// `container_size.width`); pre-layout `0` (or `None`) means no budget —
    /// rows refit when the app re-renders with the real width.
    pub fn lane_lines(&self, width: Option<usize>) -> Vec<String> {
        let lanes: Vec<LaneState> = self.records.iter().map(|record| record.lane.clone()).collect();
        let labels = lane_labels(&self.records);
        let queued_counts: Vec<usize> = self
            .records
            .iter()
            .map(|record| self.queued.get(&record.session_id).copied().unwrap_or(0))
            .collect();
        format_lane_lines(
            &lanes,
            LaneLineOpts {
                tailed_index: self.tailed_index(),
                labels: Some(&labels),
                width: width.filter(|w| *w > 0),
                queued_counts: Some(&queued_counts),
            },
        )
    }

    /// Index of the highlighted row (the `-selected` CSS class in Python).
    pub fn selected_index(&self) -> usize {
        self.selected
    }

    pub fn is_selected(&self, index: usize) -> bool {
        index == self.selected
    }

    pub fn selected_record(&self) -> Option<&LaneRecord> {
        self.records.get(self.selected)
    }

    /// Replace the lane listing (registration order, per LaneRegistry).
    ///
    /// `queued_counts` (`{session_id: depth}`) drives each lane row's
    /// `▸ N queued` steer badge (issue #39); `None` leaves it unchanged.
    pub fn update_lanes(
        &mut self,
        records: &[LaneRecord],
        tailed_session_id: Option<&str>,
        queued_counts: Option<&HashMap<String, usize>>,
    ) {
        self.records = records.to_vec();
        let tailed = tailed_session_id.map(str::to_string);
        let focus_changed = tailed != self.tailed;
        self.tailed = tailed;
        if focus_changed {
            // ctrl+o moved the ▸ focus — drop the old row's tail; the reducer
            // re-feeds show_lane_tail for the newly focused lane.
            self.tail_mounted = false;
        }
        if let Some(counts) = queued_counts {
            self.queued = counts.clone();
        }
        self.selected = self.selected.min(self.records.len().saturating_sub(1));
        self.sync_motion();
    }

    // -- focused-lane live tail (issue #90) ----------------------------------

    fn tailed_index(&self) -> Option<usize> {
        let tailed = self.tailed.as_deref()?;
        self.records.iter().position(|record| record.session_id == tailed)
    }

    /// Paint the focused lane's accumulated tail directly under its row.
    pub fn show_lane_tail(&mut self, text: &str) {
        self.tail_text = text.to_string();
        if self.tailed_index().is_none() {
            return; // focused lane not listed
        }
        self.tail_mounted = true;
    }

    /// Drop the lane tail (root preemption / lane done / turn end).
    pub fn clear_lane_tail(&mut self) {
        self.tail_text.clear();
        self.tail_mounted = false;
    }

    /// True while a focused-lane tail is mounted under its row.
    pub fn has_lane_tail(&self) -> bool {
        self.tail_mounted
    }

    /// The row index the mounted tail renders directly under (Python
    /// mounted the `_LaneTail` widget `after=row`).
    pub fn tail_row_index(&self) -> Option<usize> {
        if self.tail_mounted {
            self.tailed_index()
        } else {
            None
        }
    }

    /// The mounted tail's markup (Python `_LaneTail.set_text` →
    /// [`lane_tail_markup`]): dim, `┆`-guttered, last 3 non-blank lines.
    pub fn tail_markup(&self) -> String {
        lane_tail_markup(&self.tail_text)
    }

    /// Show the panel. (Python's `focus=True` keyboard-focus grab is app
    /// assembly's job in ratatui — route keys here while visible.)
    pub fn show_panel(&mut self) {
        self.display = true;
        self.sync_motion();
    }

    pub fn hide_panel(&mut self) {
        self.display = false;
        self.motion_running = false;
    }

    /// Snap the highlight to the currently focused lane (or leave as-is).
    pub fn set_focused(&mut self, name: Option<&str>) {
        let Some(name) = name else { return };
        if let Some(index) = self.records.iter().position(|record| record.lane.name == name) {
            self.selected = index;
        }
    }

    pub fn move_selection(&mut self, delta: isize) {
        if self.records.is_empty() {
            return;
        }
        let last = self.records.len() as isize - 1;
        self.selected = (self.selected as isize + delta).clamp(0, last) as usize;
    }

    /// Emit [`LanesMsg::FocusLane`] for the highlighted lane.
    pub fn focus_selected(&self) -> Option<LanesMsg> {
        self.selected_record().map(|record| LanesMsg::FocusLane {
            name: record.lane.name.clone(),
            session_id: record.session_id.clone(),
        })
    }

    // -- key actions ----------------------------------------------------

    /// Printable keys pass through to the composer (mockup: the composer
    /// keeps typing rights while `lanesOpen`); ↑↓/enter stay with the panel
    /// via its bindings, esc bubbles to the app's ESC_CHAIN (spec §5 —
    /// palette/rewind close before lanes even while this panel holds
    /// keyboard focus; the chain calls [`LanesPanel::action_close`] when
    /// the lanes step is reached).
    pub fn on_printable_key(&self, character: &str) -> LanesMsg {
        LanesMsg::TypeThrough { character: character.to_string() }
    }

    pub fn action_cursor_up(&mut self) {
        self.move_selection(-1);
    }

    pub fn action_cursor_down(&mut self) {
        self.move_selection(1);
    }

    pub fn action_focus_lane(&self) -> Option<LanesMsg> {
        self.focus_selected()
    }

    pub fn action_close(&mut self) -> LanesMsg {
        self.hide_panel();
        LanesMsg::Closed
    }

    // -- clicks ----------------------------------------------------------

    /// A click on lane row *index* (Python `_LaneRow.on_click`): focuses
    /// that lane without moving the selection highlight.
    pub fn on_click(&self, index: usize) -> Option<LanesMsg> {
        self.records.get(index).map(|record| LanesMsg::FocusLane {
            name: record.lane.name.clone(),
            session_id: record.session_id.clone(),
        })
    }

    // -- motion (Python set_interval timer → app-driven ticks) -----------

    /// True while the shimmer timer would be running in Python: the panel
    /// is shown and any lane is not done. App assembly calls
    /// [`LanesPanel::advance_motion`] every [`LANE_MOTION_INTERVAL_SECONDS`]
    /// while this is true.
    pub fn motion_running(&self) -> bool {
        self.motion_running
    }

    pub fn motion_frame(&self) -> usize {
        self.motion_frame
    }

    /// One motion-timer tick (Python `_advance_motion`).
    pub fn advance_motion(&mut self) {
        if !self.motion_running {
            return; // the Python timer never fires once stopped
        }
        self.motion_frame += 1;
    }

    fn sync_motion(&mut self) {
        self.motion_running = self.display
            && self.records.iter().any(|record| record.lane.state != LaneStateName::Done);
    }

    // -- rendering (Python `_LanesHeader.render` / `_LaneRow.render`) ----

    /// `Agent lanes` bright bold + dimmer hint.
    pub fn header_segments(&self) -> Vec<Segment> {
        vec![
            Segment {
                style_token: StyleToken::Bright,
                bold: true,
                ..Segment::new(LANES_HEADER_TITLE)
            },
            Segment::new(" "),
            Segment {
                style_token: StyleToken::Dimmer,
                ..Segment::new(LANES_HEADER_HINT)
            },
        ]
    }

    /// One aligned lane line, colored by lane state, with the active-lane
    /// shimmer band overlaid on the agent name for the current motion frame.
    pub fn row_segments(&self, index: usize, width: Option<usize>) -> Vec<Segment> {
        let lines = self.lane_lines(width);
        let (Some(line), Some(record)) = (lines.get(index), self.records.get(index)) else {
            return Vec::new();
        };
        let base = record.lane.color_token;
        let lane = &record.lane;
        let mut overlays: HashMap<usize, (StyleToken, bool)> = HashMap::new();
        if lane.state != LaneStateName::Done && !lane.name.is_empty() {
            if let Some(byte_pos) = line.find(&lane.name) {
                let name_start = line[..byte_pos].chars().count();
                for (offset, token, bold) in
                    shimmer_band(char_len(&lane.name), self.motion_frame)
                {
                    overlays.insert(name_start + offset, (token, bold));
                }
            }
        }
        let mut segments: Vec<Segment> = Vec::new();
        let mut current = String::new();
        let mut current_style = (base, false);
        for (position, ch) in line.chars().enumerate() {
            let style = overlays.get(&position).copied().unwrap_or((base, false));
            if style != current_style && !current.is_empty() {
                segments.push(Segment {
                    style_token: current_style.0,
                    bold: current_style.1,
                    ..Segment::new(std::mem::take(&mut current))
                });
            }
            current_style = style;
            current.push(ch);
        }
        if !current.is_empty() {
            segments.push(Segment {
                style_token: current_style.0,
                bold: current_style.1,
                ..Segment::new(current)
            });
        }
        segments
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::lanes::{LaneRegistry, LaneUpdate, RegisterOptions};
    use rust_decimal::Decimal;

    fn dec(value: &str) -> Decimal {
        value.parse().expect("valid decimal literal")
    }

    fn record(
        session_id: &str,
        name: &str,
        state: LaneStateName,
        activity: &str,
        elapsed: f64,
        cost: &str,
        tokens: u64,
    ) -> LaneRecord {
        LaneRecord {
            session_id: session_id.to_string(),
            parent_id: Some("root".to_string()),
            depth: 1,
            started_at: 0.0,
            lane: LaneState::for_state_with(name, state, activity, elapsed, tokens, dec(cost)),
        }
    }

    /// The mockup's three demo lanes, verbatim (Python `RECORDS`).
    fn mockup_records() -> Vec<LaneRecord> {
        vec![
            record(
                "s1",
                "researcher",
                LaneStateName::Running,
                "scanning provider docs",
                41.0,
                "0.09",
                100_100,
            ),
            record("s2", "coder", LaneStateName::Working, "migrating store", 124.0, "0.31", 48_300),
            record("s3", "tester", LaneStateName::Done, "done · tests ✔", 55.0, "0.07", 3_200),
        ]
    }

    fn lanes_of(records: &[LaneRecord]) -> Vec<LaneState> {
        records.iter().map(|r| r.lane.clone()).collect()
    }

    /// Python `line.index(" · ")` — character index of the first separator.
    fn sep_index(line: &str) -> usize {
        let byte = line.find(" · ").expect("separator present");
        line[..byte].chars().count()
    }

    fn row_plain(panel: &LanesPanel, index: usize) -> String {
        panel
            .row_segments(index, None)
            .iter()
            .map(|segment| segment.text.as_str())
            .collect()
    }

    // -- pure formatting (tests/test_ui_lanes.py) -----------------------------

    /// Pins Python `test_header_exact_string`.
    #[test]
    fn test_header_exact_string() {
        assert_eq!(
            LANES_HEADER,
            "Agent lanes · ↑↓ select · enter focus · ctrl-o tail · esc close"
        );
        assert_eq!(LANES_HEADER, format!("{LANES_HEADER_TITLE} {LANES_HEADER_HINT}"));
        assert_eq!(LANE_MOTION_INTERVAL_SECONDS, SHIMMER_INTERVAL_SECONDS);
    }

    /// Pins Python `test_lane_elapsed_format`.
    #[test]
    fn test_lane_elapsed_format() {
        assert_eq!(lane_elapsed(41.0), "41s");
        assert_eq!(lane_elapsed(55.0), "55s");
        assert_eq!(lane_elapsed(124.0), "2m 04s");
        assert_eq!(lane_elapsed(348.0), "5m 48s");
        assert_eq!(lane_elapsed(0.0), "0s");
        // Oracle (real Python `round` is banker's rounding): 0.5→0s,
        // 1.5→2s, 41.4→41s, 59.6→1m 00s.
        assert_eq!(lane_elapsed(0.5), "0s");
        assert_eq!(lane_elapsed(1.5), "2s");
        assert_eq!(lane_elapsed(41.4), "41s");
        assert_eq!(lane_elapsed(59.6), "1m 00s");
    }

    /// Pins Python `test_lane_lines_align_exactly_like_mockup`.
    #[test]
    fn test_lane_lines_align_exactly_like_mockup() {
        let lines = format_lane_lines(&lanes_of(&mockup_records()), LaneLineOpts::default());
        assert_eq!(
            lines,
            vec![
                "  ◐ researcher · scanning provider docs · 41s    · ↓ 100.1k tokens · $0.09",
                "  ■ coder      · migrating store        · 2m 04s · ↓ 48.3k tokens  · $0.31",
                "  ✔ tester     · done · tests ✔         · 55s    · ↓ 3.2k tokens   · $0.07",
            ]
        );
    }

    /// Pins Python `test_lane_glyphs_and_colors_per_state`.
    #[test]
    fn test_lane_glyphs_and_colors_per_state() {
        let records = mockup_records();
        let (running, working, done) = (&records[0].lane, &records[1].lane, &records[2].lane);
        assert_eq!((running.glyph.as_str(), running.color_token), ("◐", StyleToken::Teal));
        assert_eq!((working.glyph.as_str(), working.color_token), ("■", StyleToken::Fg));
        assert_eq!((done.glyph.as_str(), done.color_token), ("✔", StyleToken::Dim));
    }

    /// Pins Python `test_empty_lanes_format_to_nothing`.
    #[test]
    fn test_empty_lanes_format_to_nothing() {
        assert!(format_lane_lines(&[], LaneLineOpts::default()).is_empty());
    }

    // -- widget behavior (adapted: messages are returned, not posted) ---------

    /// Pins Python `test_panel_lists_aligned_lanes_and_selects_first`.
    #[test]
    fn test_panel_lists_aligned_lanes_and_selects_first() {
        let records = mockup_records();
        let mut panel = LanesPanel::new();
        panel.update_lanes(&records, None, None);
        panel.show_panel();
        assert!(panel.display());
        assert_eq!(
            panel.lane_lines(None),
            format_lane_lines(&lanes_of(&records), LaneLineOpts::default())
        );
        assert_eq!(panel.selected_record(), Some(&records[0]));
        let rows: Vec<String> = (0..records.len()).map(|i| row_plain(&panel, i)).collect();
        assert_eq!(rows, panel.lane_lines(None));
        assert!(panel.is_selected(0));
    }

    /// Pins Python `test_active_lane_labels_shimmer_and_stop_when_all_done`.
    /// (`set_interval` → `motion_running` + explicit `advance_motion`.)
    #[test]
    fn test_active_lane_labels_shimmer_and_stop_when_all_done() {
        let records = mockup_records();
        let mut panel = LanesPanel::new();
        panel.update_lanes(&records[..1], None, None);
        panel.show_panel();
        assert!(panel.motion_running());
        let start = panel.motion_frame();
        panel.advance_motion();
        assert!(panel.motion_frame() > start);

        let segments = panel.row_segments(0, None);
        assert!(segments.iter().any(|segment| segment.bold));

        panel.update_lanes(&records[2..3], None, None);
        assert!(!panel.motion_running());
    }

    /// Pins Python `test_live_telemetry_patches_rows_without_remounting_motion`.
    /// (Row-widget identity is a Textual mechanic; the ported substance is
    /// that motion frames survive a telemetry update.)
    #[test]
    fn test_live_telemetry_patches_rows_without_remounting_motion() {
        let records = mockup_records();
        let mut panel = LanesPanel::new();
        panel.update_lanes(&records[..1], None, None);
        panel.show_panel();
        panel.advance_motion();
        let frame = panel.motion_frame();

        let updated = record(
            "s1",
            "researcher",
            LaneStateName::Working,
            "reading README.md",
            42.0,
            "0.10",
            120_000,
        );
        panel.update_lanes(std::slice::from_ref(&updated), None, None);
        assert_eq!(panel.motion_frame(), frame); // motion not reset
        assert!(panel.lane_lines(None)[0].contains("reading README.md"));
    }

    /// Pins Python `test_arrows_move_selection_and_enter_focuses_lane`.
    #[test]
    fn test_arrows_move_selection_and_enter_focuses_lane() {
        let records = mockup_records();
        let mut panel = LanesPanel::new();
        panel.update_lanes(&records, None, None);
        panel.show_panel();
        panel.action_cursor_down();
        assert_eq!(panel.selected_record(), Some(&records[1]));
        panel.action_cursor_down();
        panel.action_cursor_down();
        panel.action_cursor_down(); // clamped at the end
        assert_eq!(panel.selected_record(), Some(&records[2]));
        panel.action_cursor_up();
        panel.action_cursor_up();
        assert_eq!(panel.selected_record(), Some(&records[0]));
        panel.action_cursor_down();
        assert_eq!(
            panel.action_focus_lane(),
            Some(LanesMsg::FocusLane {
                name: "coder".to_string(),
                session_id: "s2".to_string()
            })
        );
    }

    /// Pins Python `test_click_focuses_that_lane`.
    #[test]
    fn test_click_focuses_that_lane() {
        let records = mockup_records();
        let mut panel = LanesPanel::new();
        panel.update_lanes(&records, None, None);
        panel.show_panel();
        assert_eq!(
            panel.on_click(2),
            Some(LanesMsg::FocusLane {
                name: "tester".to_string(),
                session_id: "s3".to_string()
            })
        );
    }

    /// Pins Python `test_close_action_hides_and_posts_closed`.
    /// (Esc is resolved by the app via keymap.ESC_CHAIN — spec §5; the
    /// panel has no local escape binding, the chain invokes `action_close`.)
    #[test]
    fn test_close_action_hides_and_posts_closed() {
        let records = mockup_records();
        let mut panel = LanesPanel::new();
        panel.update_lanes(&records, None, None);
        panel.show_panel();
        assert_eq!(panel.action_close(), LanesMsg::Closed);
        assert!(!panel.display());
    }

    /// Pins Python `test_set_focused_snaps_highlight`.
    #[test]
    fn test_set_focused_snaps_highlight() {
        let records = mockup_records();
        let mut panel = LanesPanel::new();
        panel.update_lanes(&records, None, None);
        panel.show_panel();
        panel.set_focused(Some("tester"));
        assert_eq!(panel.selected_record(), Some(&records[2]));
        panel.set_focused(None); // leave as-is
        assert_eq!(panel.selected_record(), Some(&records[2]));
    }

    /// Pins Python `test_format_lane_lines_marks_the_tailed_lane_and_keeps_alignment`.
    #[test]
    fn test_format_lane_lines_marks_the_tailed_lane_and_keeps_alignment() {
        let lanes = vec![
            LaneState::for_state_with(
                "researcher",
                LaneStateName::Running,
                "scanning docs",
                0.0,
                0,
                Decimal::ZERO,
            ),
            LaneState::for_state_with(
                "coder",
                LaneStateName::Working,
                "migrating store",
                0.0,
                0,
                Decimal::ZERO,
            ),
        ];
        let lines = format_lane_lines(
            &lanes,
            LaneLineOpts { tailed_index: Some(1), ..LaneLineOpts::default() },
        );
        assert!(lines[1].contains("coder ▸"));
        assert!(!lines[0].contains('▸'));
        // The name column still pads to the widest entry (marker included):
        assert_eq!(sep_index(&lines[0]), sep_index(&lines[1]));
        // No marker → identical to today's output shape.
        assert!(!format_lane_lines(&lanes, LaneLineOpts::default()).join("").contains('▸'));
    }

    // -- width budget (review finding: rows clipped their telemetry) ----------

    fn wide_lanes() -> Vec<LaneState> {
        vec![
            LaneState::for_state_with(
                "foundation:zen-architect",
                LaneStateName::Running,
                "Exploring the codebase for relevant files",
                348.0,
                128_000,
                dec("12.34"),
            ),
            LaneState::for_state_with(
                "foundation:git-ops",
                LaneStateName::Running,
                "running",
                19.0,
                0,
                dec("0"),
            ),
        ]
    }

    /// Pins Python `test_format_lane_lines_elides_activity_to_fit_width`:
    /// the row is height-1: anything past the width is CROPPED, and the
    /// dropped part was the telemetry (elapsed/tokens/cost) — the panel's
    /// whole point. The activity column is the elastic one.
    #[test]
    fn test_format_lane_lines_elides_activity_to_fit_width() {
        let lines = format_lane_lines(
            &wide_lanes(),
            LaneLineOpts { width: Some(80), ..LaneLineOpts::default() },
        );
        assert!(lines.iter().all(|line| char_len(line) <= 80));
        assert!(lines[0].contains('…')); // activity elided
        assert!(
            lines[0].contains("5m 48s")
                && lines[0].contains("↓ 128.0k tokens")
                && lines[0].contains("$12.34")
        );
        assert_eq!(sep_index(&lines[0]), sep_index(&lines[1])); // alignment holds
        // Oracle golden (real Python output at width=80):
        assert_eq!(
            lines,
            vec![
                "  ◐ foundation:zen-architect · Exploring th… · 5m 48s · ↓ 128.0k tokens · $12.34",
                "  ◐ foundation:git-ops       · running       · 19s    · ↓ 0.0k tokens   · $0.00",
            ]
        );
    }

    /// Pins Python `test_format_lane_lines_drops_tokens_before_the_essentials`.
    #[test]
    fn test_format_lane_lines_drops_tokens_before_the_essentials() {
        let lines = format_lane_lines(
            &wide_lanes(),
            LaneLineOpts { width: Some(58), ..LaneLineOpts::default() },
        );
        assert!(lines.iter().all(|line| char_len(line) <= 58));
        assert!(!lines[0].contains("tokens")); // tokens column dropped whole
        assert!(lines[0].contains("foundation:zen-architect"));
        assert!(lines[0].contains("5m 48s") && lines[0].contains("$12.34")); // essentials kept
        // Oracle golden (real Python output at width=58):
        assert_eq!(
            lines,
            vec![
                "  ◐ foundation:zen-architect · Explorin… · 5m 48s · $12.34",
                "  ◐ foundation:git-ops       · running   · 19s    · $0.00",
            ]
        );
    }

    /// Pins Python `test_format_lane_lines_without_width_is_unchanged`.
    #[test]
    fn test_format_lane_lines_without_width_is_unchanged() {
        let wide = format_lane_lines(&wide_lanes(), LaneLineOpts::default());
        assert!(wide[0].contains("Exploring the codebase for relevant files"));
        assert_eq!(
            wide,
            format_lane_lines(
                &wide_lanes(),
                LaneLineOpts { width: None, ..LaneLineOpts::default() }
            )
        );
    }

    // -- same-named-agent lane aliasing (runtime parity) -----------------------

    /// Pins Python `test_lane_labels_leave_unique_names_untouched`.
    #[test]
    fn test_lane_labels_leave_unique_names_untouched() {
        assert_eq!(lane_labels(&mockup_records()), vec!["researcher", "coder", "tester"]);
    }

    /// Pins Python `test_lane_labels_disambiguate_same_named_agents`: two
    /// delegates of the same agent get a short session-id tag so their lane
    /// rows stop reading identically (the whole point of the panel).
    #[test]
    fn test_lane_labels_disambiguate_same_named_agents() {
        let records = vec![
            record("sub-aaaa", "test-writer", LaneStateName::Running, "writing tests", 10.0, "0.05", 0),
            record("sub-bbbb", "test-writer", LaneStateName::Working, "writing tests", 20.0, "0.06", 0),
            record("s3", "reviewer", LaneStateName::Done, "done · ok", 5.0, "0.01", 0),
        ];
        assert_eq!(
            lane_labels(&records),
            vec!["test-writer #aaaa", "test-writer #bbbb", "reviewer"]
        );
    }

    /// Pins Python `test_lane_labels_tail_collision_falls_back_to_ordinal`:
    /// two ids sharing the last four usable chars can't disambiguate by tag,
    /// so the group falls back to a stable 1-based ordinal (deterministic).
    #[test]
    fn test_lane_labels_tail_collision_falls_back_to_ordinal() {
        let records = vec![
            record("x-9999", "worker", LaneStateName::Running, "a", 1.0, "0.01", 0),
            record("y-9999", "worker", LaneStateName::Running, "b", 2.0, "0.01", 0),
        ];
        assert_eq!(lane_labels(&records), vec!["worker #9999", "worker #2"]);
    }

    /// Pins Python `test_lane_labels_ignore_blank_names`.
    #[test]
    fn test_lane_labels_ignore_blank_names() {
        let records = vec![
            record("s1", "", LaneStateName::Running, "a", 1.0, "0.01", 0),
            record("s2", "", LaneStateName::Running, "b", 2.0, "0.01", 0),
        ];
        assert_eq!(lane_labels(&records), vec!["", ""]);
    }

    /// Pins Python `test_format_lane_lines_disambiguates_same_named_lanes` —
    /// golden: the aliased labels flow into the aligned rows and the `·`
    /// separator columns still line up exactly.
    #[test]
    fn test_format_lane_lines_disambiguates_same_named_lanes() {
        let records = vec![
            record("sub-aaaa", "test-writer", LaneStateName::Running, "writing tests", 10.0, "0.05", 1_000),
            record("sub-bbbb", "test-writer", LaneStateName::Working, "writing tests", 20.0, "0.06", 2_000),
            record("s3", "reviewer", LaneStateName::Done, "done · ok", 5.0, "0.01", 300),
        ];
        let labels = lane_labels(&records);
        let lines = format_lane_lines(
            &lanes_of(&records),
            LaneLineOpts { labels: Some(&labels), ..LaneLineOpts::default() },
        );
        assert_eq!(
            lines,
            vec![
                "  ◐ test-writer #aaaa · writing tests · 10s · ↓ 1.0k tokens · $0.05",
                "  ■ test-writer #bbbb · writing tests · 20s · ↓ 2.0k tokens · $0.06",
                "  ✔ reviewer          · done · ok     · 5s  · ↓ 0.3k tokens · $0.01",
            ]
        );
        // Alignment holds across the disambiguated (wider) name column.
        assert_eq!(sep_index(&lines[0]), sep_index(&lines[1]));
        assert_eq!(sep_index(&lines[1]), sep_index(&lines[2]));
    }

    /// Pins Python `test_panel_disambiguates_same_named_lanes`.
    #[test]
    fn test_panel_disambiguates_same_named_lanes() {
        let records = vec![
            record("sub-aaaa", "test-writer", LaneStateName::Running, "writing tests", 10.0, "0.05", 1_000),
            record("sub-bbbb", "test-writer", LaneStateName::Working, "writing tests", 20.0, "0.06", 2_000),
        ];
        let mut panel = LanesPanel::new();
        panel.update_lanes(&records, None, None);
        panel.show_panel();
        let joined = panel.lane_lines(Some(96)).join("\n");
        assert!(joined.contains("test-writer #aaaa"));
        assert!(joined.contains("test-writer #bbbb"));
        // Focus routing still carries the raw agent name (session id disambiguates).
        assert_eq!(
            panel.on_click(1),
            Some(LanesMsg::FocusLane {
                name: "test-writer".to_string(),
                session_id: "sub-bbbb".to_string()
            })
        );
    }

    /// Pins Python `test_lane_tail_mounts_under_focused_row_then_drops`
    /// (issue #90): the focused lane's live tail renders directly under
    /// that lane's row (co-located with its agent), and drops on focus
    /// change / clear.
    #[test]
    fn test_lane_tail_mounts_under_focused_row_then_drops() {
        let records = mockup_records();
        let mut panel = LanesPanel::new();
        panel.update_lanes(&records, Some("s2"), None); // coder focused
        panel.show_panel();

        panel.show_lane_tail("scanning the queue bridge\nfeeding the lanes\nnext: trackers");
        assert!(panel.has_lane_tail());
        // The tail sits immediately after the focused (s2 = coder) row.
        assert_eq!(panel.tail_row_index(), Some(1));
        assert_eq!(
            panel.tail_markup(),
            "[$dim]┆ scanning the queue bridge\n┆ feeding the lanes\n┆ next: trackers[/]"
        );

        // Cycling focus drops it (the reducer re-feeds for the newly focused lane).
        panel.update_lanes(&records, Some("s1"), None);
        assert!(!panel.has_lane_tail());

        // Explicit clear (turn end) drops it too.
        panel.show_lane_tail("x");
        assert!(panel.has_lane_tail());
        panel.clear_lane_tail();
        assert!(!panel.has_lane_tail());
    }

    /// Adapted from Python `LanesPanel.TypeThrough` behavior (`on_key`):
    /// printable keys pass through to the composer.
    #[test]
    fn test_printable_key_types_through() {
        let panel = LanesPanel::new();
        assert_eq!(
            panel.on_printable_key("/"),
            LanesMsg::TypeThrough { character: "/".to_string() }
        );
    }

    // -- steer badge (tests/test_ui_lane_steering.py, pure formatting) --------

    fn steer_lane(name: &str) -> LaneState {
        LaneState::for_state_with(name, LaneStateName::Running, "working", 41.0, 0, dec("0.09"))
    }

    /// Oracle check (not a pinned pytest case): Python `f"${cost:.2f}"`
    /// rounds half-even — `Decimal("1.9752735")` → `$1.98`,
    /// `Decimal("1.985")` → `$1.98` (verified against the real module).
    #[test]
    fn oracle_cost_column_rounds_half_even_like_python() {
        let lane = |cost: &str| {
            LaneState::for_state_with("a", LaneStateName::Running, "x", 1.0, 0, dec(cost))
        };
        let lines = format_lane_lines(&[lane("1.9752735")], LaneLineOpts::default());
        assert!(lines[0].ends_with("$1.98"));
        let lines = format_lane_lines(&[lane("1.985")], LaneLineOpts::default());
        assert!(lines[0].ends_with("$1.98"));
    }

    /// Pins Python `test_badge_appended_when_a_lane_has_queued_steers`.
    #[test]
    fn test_badge_appended_when_a_lane_has_queued_steers() {
        let lanes = vec![steer_lane("researcher"), steer_lane("coder")];
        let lines = format_lane_lines(
            &lanes,
            LaneLineOpts { queued_counts: Some(&[1, 0]), ..LaneLineOpts::default() },
        );
        assert!(lines[0].contains("▸ 1 queued"));
        assert!(!lines[1].contains("queued")); // no badge without a queue
    }

    /// Pins Python `test_badge_absent_by_default`.
    #[test]
    fn test_badge_absent_by_default() {
        let lines = format_lane_lines(&[steer_lane("researcher")], LaneLineOpts::default());
        assert!(!lines[0].contains("queued"));
    }

    /// Pins Python `test_badge_pluralises_by_count`.
    #[test]
    fn test_badge_pluralises_by_count() {
        let lines = format_lane_lines(
            &[steer_lane("researcher")],
            LaneLineOpts { queued_counts: Some(&[3]), ..LaneLineOpts::default() },
        );
        assert!(lines[0].contains("▸ 3 queued"));
    }

    // -- per-agent lane telemetry (tests/test_ui_lanes_telemetry.py: the
    //    LaneRegistry cases the panel's live clock/token columns ride on) ----

    /// Pins Python `test_advance_ticks_running_lanes_and_freezes_done`.
    #[test]
    fn test_advance_ticks_running_lanes_and_freezes_done() {
        let mut reg = LaneRegistry::new();
        reg.register(
            "a",
            None,
            "researcher",
            RegisterOptions { now: 100.0, ..RegisterOptions::default() },
        );
        reg.register(
            "b",
            None,
            "coder",
            RegisterOptions { now: 100.0, ..RegisterOptions::default() },
        );
        reg.complete("b", "tests ✔"); // done lanes are frozen

        assert!(reg.advance(110.0));
        assert_eq!(reg.get("a").unwrap().lane.elapsed, 10.0); // running lane ticked
        assert_eq!(reg.get("b").unwrap().lane.elapsed, 0.0); // done lane left alone
        assert_eq!(reg.get("b").unwrap().lane.state, LaneStateName::Done);

        // Idempotent: advancing to the same wall time changes nothing.
        assert!(!reg.advance(110.0));
    }

    /// Pins Python `test_advance_ignores_lanes_without_started_at`.
    #[test]
    fn test_advance_ignores_lanes_without_started_at() {
        let mut reg = LaneRegistry::new();
        reg.register("a", None, "a", RegisterOptions::default()); // no now → started_at 0.0
        assert!(!reg.advance(500.0));
        assert_eq!(reg.get("a").unwrap().lane.elapsed, 0.0);
    }

    /// Pins Python `test_update_sets_lane_tokens`.
    #[test]
    fn test_update_sets_lane_tokens() {
        let mut reg = LaneRegistry::new();
        reg.register("a", None, "coder", RegisterOptions::default());
        assert_eq!(reg.get("a").unwrap().lane.tokens, 0);
        reg.update("a", LaneUpdate { tokens: Some(1234), ..LaneUpdate::default() });
        assert_eq!(reg.get("a").unwrap().lane.tokens, 1234);
        // tokens=None on a later update keeps the existing count.
        reg.update(
            "a",
            LaneUpdate { activity: Some("still going".to_string()), ..LaneUpdate::default() },
        );
        assert_eq!(reg.get("a").unwrap().lane.tokens, 1234);
    }
}
