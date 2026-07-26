//! Needs-you block rendering + focused-lane banner helpers (DESIGN-SPEC §7/§8).
//!
//! Port of `ui/needs_you.py`. The needs-you list renders
//! transcript-block-style (it is printed into the transcript flow on
//! ctrl-y / footer-badge click, not a modal):
//!
//! - Header (orange): `· Needs you  N deferred decision`
//! - One numbered row per deferred decision: orange number + fg question +
//!   inline actionable chips like `[yes · push to fork]` (green on
//!   bg-tab). Activating a chip yields a [`DecisionTaken`]; the app then
//!   logs the `Applying decision: …` narration and clears the footer
//!   badge.
//!
//! Also provides the focused-lane banner line helper (spec §8): the bright
//! `focused: <name>` prefix plus the dim
//! `· subagent of <parent> · own context window · results report back to
//! parent · esc back` tail.
//!
//! Ratatui adaptation: the Textual widgets (`_NeedsYouHeader`,
//! `_ChoiceChip`, `_DecisionText`, `_DecisionRow`, `NeedsYouList`) become
//! pure segment production plus a [`NeedsYouList`] state holder. Widget
//! mechanics — mounting, message pumps, CSS, mouse hit-testing — are the
//! app-assembly layer's job: it maps a click on a chip's screen region to
//! [`NeedsYouList::activate_chip`] (or a click anywhere else on the row to
//! [`NeedsYouList::activate_row`]) and dispatches the returned
//! [`DecisionTaken`].

use crate::model::blocks::{NeedsYouBlock, NeedsYouChoice, NeedsYouEntry, Segment, StyleToken};
use ratatui::text::Span;

/// Terminal cell width of `s` (Python: `rich.cells.cell_len`).
fn cell_len(s: &str) -> usize {
    Span::raw(s).width()
}

/// Header text: `Needs you  N deferred decision` (spec §7, verbatim).
pub fn needs_you_header(count: usize) -> String {
    format!("Needs you  {count} deferred decision")
}

/// The full header line including the leading `· ` marker.
pub fn needs_you_header_line(count: usize) -> String {
    format!("· {}", needs_you_header(count))
}

/// The orange row-number prefix: `  1 ` (two-space indent, mockup).
pub fn decision_number_text(number: usize) -> String {
    format!("  {number} ")
}

/// Inline chip text: `[<label>]` e.g. `[yes · push to fork]`.
pub fn chip_text(choice: &NeedsYouChoice) -> String {
    format!("[{}]", choice.label)
}

/// Narration logged when a decision is acted on: `Applying decision: …`.
pub fn applying_decision_line(detail: &str) -> String {
    format!("Applying decision: {detail}")
}

/// (bright bold prefix, dim tail) of the focused-lane banner (spec §8).
pub fn focused_lane_banner_parts(name: &str, parent_session: &str) -> (String, String) {
    (
        format!("focused: {name} "),
        format!(
            "· subagent of {parent_session} · own context window \
             · results report back to parent · esc back"
        ),
    )
}

/// The full focused-lane banner line as plain text.
pub fn focused_lane_banner(name: &str, parent_session: &str) -> String {
    let (prefix, tail) = focused_lane_banner_parts(name, parent_session);
    format!("{prefix}{tail}")
}

/// Orange `· Needs you  N deferred decision` header line (was `_NeedsYouHeader`).
pub fn header_segments(count: usize) -> Vec<Segment> {
    vec![Segment {
        style_token: StyleToken::Orange,
        ..Segment::new(needs_you_header_line(count))
    }]
}

/// One actionable chip: `[<label>]` green on bg-tab (was `_ChoiceChip`).
pub fn chip_segment(choice: &NeedsYouChoice) -> Segment {
    Segment {
        style_token: StyleToken::Green,
        bg_token: Some(StyleToken::BgTab),
        ..Segment::new(chip_text(choice))
    }
}

/// Orange number + fg question (+ dim reason) segments of one decision row
/// (was `_DecisionText.render`).
pub fn decision_text_segments(entry: &NeedsYouEntry, number: usize) -> Vec<Segment> {
    let mut segments = vec![Segment {
        style_token: StyleToken::Orange,
        ..Segment::new(decision_number_text(number))
    }];
    let question = entry.question.as_str();
    let highlight = entry.highlight.as_str();
    if let Some(at) = (!highlight.is_empty())
        .then(|| question.find(highlight))
        .flatten()
    {
        // Mockup: 'Push to fork ' fg + 'mj/waypoint' teal + ' instead?' fg.
        let before = &question[..at];
        let after = &question[at + highlight.len()..];
        if !before.is_empty() {
            segments.push(Segment::new(before));
        }
        segments.push(Segment {
            style_token: StyleToken::Teal,
            ..Segment::new(highlight)
        });
        if !after.is_empty() {
            segments.push(Segment::new(after));
        }
    } else {
        segments.push(Segment::new(question));
    }
    if !entry.reason.is_empty() {
        segments.push(Segment {
            style_token: StyleToken::Dim,
            ..Segment::new(format!(" · {}", entry.reason))
        });
    }
    segments
}

/// The cells one decision row needs to fit its text plus every inline chip
/// (each chip carries a 2-cell left margin), mirroring
/// `_DecisionRow._update_wrap`'s arithmetic.
pub fn decision_row_needed_width(entry: &NeedsYouEntry, number: usize) -> usize {
    let mut needed = cell_len(&decision_number_text(number)) + cell_len(&entry.question);
    if !entry.reason.is_empty() {
        needed += cell_len(&format!(" · {}", entry.reason));
    }
    // each chip carries ``margin-left: 2``
    needed += entry
        .choices
        .iter()
        .map(|choice| cell_len(&chip_text(choice)) + 2)
        .sum::<usize>();
    needed
}

/// Whether the row must drop its chips onto their own lines at `width`.
///
/// Mirrors the mockup's showNeedsYou row (normal HTML flow — it wraps, so
/// chips never clip) and the ApprovalBar `-wrapped` treatment: every chip
/// stays visible and clickable at narrow terminal widths (spec §7 inline
/// actionable chips, §12 mouse click targets). Python's `_update_wrap`
/// no-ops at width <= 0 (keeps the previous class); here `width == 0`
/// simply reports `true` and callers pass real container widths.
pub fn decision_row_wraps(entry: &NeedsYouEntry, number: usize, width: usize) -> bool {
    decision_row_needed_width(entry, number) > width
}

/// The human acted on a deferred decision chip (was `NeedsYouList.DecisionTaken`).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct DecisionTaken {
    pub item_id: String,
    pub choice: String,
}

impl DecisionTaken {
    pub fn new(item_id: impl Into<String>, choice: impl Into<String>) -> Self {
        Self {
            item_id: item_id.into(),
            choice: choice.into(),
        }
    }
}

/// Transcript-block-style needs-you list (DESIGN-SPEC §7).
///
/// Feed it a [`NeedsYouBlock`] via [`NeedsYouList::update_block`]. Chip
/// activation (or [`NeedsYouList::take_decision`] for keyboard paths)
/// yields a [`DecisionTaken`]; the app applies the answer, logs
/// `Applying decision: …` and clears the footer badge.
#[derive(Clone, Debug, Default)]
pub struct NeedsYouList {
    block: Option<NeedsYouBlock>,
}

impl NeedsYouList {
    pub fn new(block: Option<NeedsYouBlock>) -> Self {
        Self { block }
    }

    pub fn block(&self) -> Option<&NeedsYouBlock> {
        self.block.as_ref()
    }

    /// The exact header line currently displayed (empty when no block).
    pub fn header_text(&self) -> String {
        match &self.block {
            None => String::new(),
            Some(block) => needs_you_header_line(block.items.len()),
        }
    }

    /// Replace the rendered decision list.
    pub fn update_block(&mut self, block: NeedsYouBlock) {
        self.block = Some(block);
    }

    /// Numbered rows currently displayed (numbering starts at 1).
    pub fn rows(&self) -> Vec<(usize, &NeedsYouEntry)> {
        match &self.block {
            None => Vec::new(),
            Some(block) => block.items.iter().enumerate().map(|(i, e)| (i + 1, e)).collect(),
        }
    }

    /// Programmatic chip activation (keyboard/number paths).
    pub fn take_decision(&self, item_id: impl Into<String>, choice: impl Into<String>) -> DecisionTaken {
        DecisionTaken::new(item_id, choice)
    }

    /// Chip click: acts with THAT chip's answer (was `_ChoiceChip.on_click`).
    pub fn activate_chip(&self, decision_id: &str, chip_index: usize) -> Option<DecisionTaken> {
        let entry = self.entry(decision_id)?;
        let choice = entry.choices.get(chip_index)?;
        Some(DecisionTaken::new(&entry.decision_id, &choice.answer))
    }

    /// Row click: clicking anywhere on the row acts on THIS decision — its
    /// first choice; the header is not a click target (mockup showNeedsYou,
    /// was `_DecisionRow.on_click`).
    pub fn activate_row(&self, decision_id: &str) -> Option<DecisionTaken> {
        let entry = self.entry(decision_id)?;
        let first = entry.choices.first()?;
        Some(DecisionTaken::new(&entry.decision_id, &first.answer))
    }

    /// Segment lines of the whole block at `width` (was `_remount_rows`):
    /// header + one row per decision; a row that can't fit its chips inline
    /// wraps them onto their own indented lines (`-wrapped` treatment) so
    /// every chip stays visible and clickable.
    pub fn render_lines(&self, width: usize) -> Vec<Vec<Segment>> {
        let block = match &self.block {
            Some(block) if !block.items.is_empty() => block,
            _ => return Vec::new(),
        };
        let mut lines = vec![header_segments(block.items.len())];
        for (number, entry) in block.items.iter().enumerate().map(|(i, e)| (i + 1, e)) {
            let mut line = decision_text_segments(entry, number);
            if decision_row_wraps(entry, number, width) {
                lines.push(line);
                for choice in &entry.choices {
                    lines.push(vec![Segment::new("  "), chip_segment(choice)]);
                }
            } else {
                for choice in &entry.choices {
                    line.push(Segment::new("  "));
                    line.push(chip_segment(choice));
                }
                lines.push(line);
            }
        }
        lines
    }

    fn entry(&self, decision_id: &str) -> Option<&NeedsYouEntry> {
        self.block
            .as_ref()?
            .items
            .iter()
            .find(|entry| entry.decision_id == decision_id)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// The mockup's deferred decision, verbatim (test_ui_lanes_needs_you.BLOCK).
    fn block() -> NeedsYouBlock {
        NeedsYouBlock::new(
            "b9",
            vec![NeedsYouEntry {
                choices: vec![NeedsYouChoice::new(
                    "yes · push to fork",
                    "push to fork mj/waypoint",
                )],
                ..NeedsYouEntry::new(
                    "decision-1",
                    "Push branch to origin was blocked (outside trust boundary). \
                     Push to fork mj/waypoint instead?",
                )
            }],
        )
    }

    fn plain(segments: &[Segment]) -> String {
        segments.iter().map(|s| s.text.as_str()).collect()
    }

    // -- pure helpers ---------------------------------------------------

    #[test]
    fn header_exact_strings() {
        assert_eq!(needs_you_header(1), "Needs you  1 deferred decision");
        assert_eq!(needs_you_header_line(2), "· Needs you  2 deferred decision");
    }

    #[test]
    fn chip_and_number_text() {
        assert_eq!(chip_text(&block().items[0].choices[0]), "[yes · push to fork]");
        assert_eq!(decision_number_text(1), "  1 ");
    }

    #[test]
    fn applying_decision_line_exact() {
        assert_eq!(
            applying_decision_line("pushing to fork mj/waypoint"),
            "Applying decision: pushing to fork mj/waypoint"
        );
    }

    #[test]
    fn focused_lane_banner_exact_string() {
        assert_eq!(
            focused_lane_banner("researcher", "e07de0"),
            "focused: researcher · subagent of e07de0 · own context window \
             · results report back to parent · esc back"
        );
        let (prefix, tail) = focused_lane_banner_parts("coder", "e07de0");
        assert_eq!(prefix, "focused: coder ");
        assert_eq!(
            tail,
            "· subagent of e07de0 · own context window · results report back to parent · esc back"
        );
    }

    // -- widget behavior (state-level; Textual pilot mechanics skipped) --

    #[test]
    fn block_renders_header_and_numbered_rows() {
        let widget = NeedsYouList::new(Some(block()));
        assert_eq!(widget.header_text(), "· Needs you  1 deferred decision");
        let rows = widget.rows();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].0, 1);
        let chips = &rows[0].1.choices;
        assert_eq!(chips.len(), 1);
        assert_eq!(chip_segment(&chips[0]).text, "[yes · push to fork]");
        // Rendered lines: orange header first, then the numbered row.
        let lines = widget.render_lines(160);
        assert_eq!(lines.len(), 2);
        assert_eq!(plain(&lines[0]), "· Needs you  1 deferred decision");
        assert_eq!(lines[0][0].style_token, StyleToken::Orange);
        assert!(plain(&lines[1]).starts_with("  1 "));
        assert_eq!(lines[1][0].style_token, StyleToken::Orange);
    }

    #[test]
    fn chip_click_posts_decision_taken() {
        let widget = NeedsYouList::new(Some(block()));
        assert_eq!(
            widget.activate_chip("decision-1", 0),
            Some(DecisionTaken::new("decision-1", "push to fork mj/waypoint"))
        );
        // Unknown chip / decision never fires.
        assert_eq!(widget.activate_chip("decision-1", 1), None);
        assert_eq!(widget.activate_chip("decision-999", 0), None);
    }

    #[test]
    fn row_click_acts_on_first_choice() {
        let widget = NeedsYouList::new(Some(block()));
        assert_eq!(
            widget.activate_row("decision-1"),
            Some(DecisionTaken::new("decision-1", "push to fork mj/waypoint"))
        );
    }

    #[test]
    fn row_wraps_at_narrow_width_so_chip_stays_visible() {
        // Spec §7/§12: chips are inline actionable click targets — the row
        // wraps (mockup: normal HTML flow) instead of clipping the chip off
        // the right edge at narrow terminal widths.
        let widget = NeedsYouList::new(Some(block()));
        let entry = &block().items[0];
        // Oracle (rich.cells.cell_len): 4 (number) + 93 (question) + 20 + 2 (chip).
        assert_eq!(decision_row_needed_width(entry, 1), 119);
        assert!(decision_row_wraps(entry, 1, 80));
        let lines = widget.render_lines(80);
        // header + question line + chip on its own line
        assert_eq!(lines.len(), 3);
        let chip_line = &lines[2];
        assert_eq!(plain(chip_line), "  [yes · push to fork]");
        // The chip stays whole → fully visible → clickable.
        assert_eq!(chip_line[1].text.chars().count(), "[yes · push to fork]".chars().count());
        assert_eq!(chip_line[1].style_token, StyleToken::Green);
        assert_eq!(chip_line[1].bg_token, Some(StyleToken::BgTab));
        assert_eq!(
            widget.activate_chip("decision-1", 0),
            Some(DecisionTaken::new("decision-1", "push to fork mj/waypoint"))
        );
    }

    #[test]
    fn row_stays_single_line_when_it_fits() {
        let widget = NeedsYouList::new(Some(block()));
        let entry = &block().items[0];
        assert!(!decision_row_wraps(entry, 1, 160));
        // header + one combined text+chip line
        let lines = widget.render_lines(160);
        assert_eq!(lines.len(), 2);
        assert!(plain(&lines[1]).ends_with("  [yes · push to fork]"));
    }

    #[test]
    fn take_decision_programmatic_path() {
        let widget = NeedsYouList::new(Some(block()));
        assert_eq!(
            widget.take_decision("decision-1", "push to fork mj/waypoint"),
            DecisionTaken::new("decision-1", "push to fork mj/waypoint")
        );
    }

    #[test]
    fn update_block_rerenders() {
        let mut widget = NeedsYouList::new(Some(block()));
        let two = NeedsYouBlock::new(
            "b10",
            vec![
                block().items[0].clone(),
                NeedsYouEntry {
                    choices: vec![NeedsYouChoice::new("yes · retry", "retry with lockfile")],
                    ..NeedsYouEntry::new(
                        "decision-2",
                        "Install dependency left unresolved. Retry with lockfile?",
                    )
                },
            ],
        );
        widget.update_block(two);
        assert_eq!(widget.header_text(), "· Needs you  2 deferred decision");
        assert_eq!(
            widget.rows().iter().map(|(n, _)| *n).collect::<Vec<_>>(),
            vec![1, 2]
        );
    }

    // -- segments beyond the Python widget tests (pin _DecisionText.render) --

    #[test]
    fn decision_text_highlight_and_reason_segments() {
        let entry = NeedsYouEntry {
            reason: "not authorized".into(),
            highlight: "mj/waypoint".into(),
            ..NeedsYouEntry::new("decision-1", "Push to fork mj/waypoint instead?")
        };
        let segments = decision_text_segments(&entry, 1);
        let rendered: Vec<(&str, StyleToken)> = segments
            .iter()
            .map(|s| (s.text.as_str(), s.style_token))
            .collect();
        assert_eq!(
            rendered,
            vec![
                ("  1 ", StyleToken::Orange),
                ("Push to fork ", StyleToken::Fg),
                ("mj/waypoint", StyleToken::Teal),
                (" instead?", StyleToken::Fg),
                (" · not authorized", StyleToken::Dim),
            ]
        );
        // Absent highlight: the whole question is one fg segment.
        let plain_entry = NeedsYouEntry::new("decision-2", "Allow x?");
        let segments = decision_text_segments(&plain_entry, 2);
        assert_eq!(segments[1].text, "Allow x?");
        assert_eq!(segments[1].style_token, StyleToken::Fg);
        assert_eq!(segments.len(), 2);
    }

    #[test]
    fn empty_or_missing_block_renders_nothing() {
        let widget = NeedsYouList::default();
        assert_eq!(widget.header_text(), "");
        assert!(widget.render_lines(80).is_empty());
        let empty = NeedsYouList::new(Some(NeedsYouBlock::new("b1", Vec::new())));
        assert!(empty.render_lines(80).is_empty());
    }
}
