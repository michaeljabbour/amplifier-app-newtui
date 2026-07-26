//! Inline approval bar (DESIGN-SPEC §2 item 4, §7) — pure-logic port of
//! `ui/approval_bar.py`.
//!
//! Replaces the composer while an approval is pending: ``Approval required
//! ·`` (orange bold) + the prompt + the options, selected option prefixed
//! ``› `` and shown bright on ``bg-tab``; Deny is red while unselected.
//!
//! Keyboard (the bar owns the keyboard while open — keymap ``approval``
//! context): left/up and right/down/tab cycle, Enter confirms, Esc = Deny,
//! ctrl-y parks the ticket into the needs-you queue WITHOUT resolving it
//! (deny-and-continue, ADR-0007 resolution 5 — answerable later).
//! Clicking an option confirms it directly. Resolution is emitted as
//! [`ApprovalMsg::Resolved`]; a park as [`ApprovalMsg::Deferred`] — the
//! app-assembly layer routes each back to the kernel approval broker.
//!
//! Textual widget mechanics (mount/compose/focus, CSS, message pump,
//! reactive `selected`, region geometry) do not port. This module keeps the
//! state machine (selection cycling, key dispatch, click-confirm, wrap
//! decision) and the rendered text/style-token surface; the ratatui
//! app-assembly layer feeds it key names, click indices and the container
//! width, and paints [`ApprovalBar::render_lines`].

use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};

pub const APPROVAL_LABEL: &str = "Approval required ·";
/// Verbatim Rust fail-closed option strings (ADR-0007 approvals).
pub const DEFAULT_OPTIONS: [&str; 3] = ["Allow once", "Allow always", "Deny"];

pub const SELECTED_PREFIX: &str = "› ";
pub const DENY_OPTION: &str = "Deny";

const _PREV_KEYS: [&str; 2] = ["left", "up"];
// Mockup keydown: ``e.key === "Tab"`` cycles with or without shift.
const _NEXT_KEYS: [&str; 4] = ["right", "down", "tab", "shift+tab"];
// ctrl-y parks the live ticket into the needs-you queue (ADR-0007
// approvals: "ctrl-y defers head to NeedsYouQueue"). The global ctrl-y
// (show_needs_you) is suppressed while the bar owns the keyboard, so the
// same chord means "defer THIS ticket" here.
const _PARK_KEYS: [&str; 1] = ["ctrl+y"];

/// Display cell width (Rich `cell_len` equivalent; wide glyphs count 2).
fn cell_len(text: &str) -> usize {
    Span::raw(text).width()
}

/// What the bar posts back to the app (Python `ApprovalBar.Resolved` /
/// `ApprovalBar.Deferred` messages).
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ApprovalMsg {
    /// The user answered: `choice` is the verbatim option string.
    Resolved { ticket_id: String, choice: String },
    /// The user parked the ticket into the needs-you queue (ctrl-y).
    ///
    /// Unlike `Resolved`, this does NOT answer the ticket: the app routes
    /// it to the kernel broker's `defer` so the future keeps its default
    /// (deny-and-continue) while the decision stays retro-answerable in
    /// the needs-you queue (ADR-0007 resolution 5).
    Deferred { ticket_id: String },
}

/// Result of feeding a key to the bar (Python `on_key`): whether the key
/// was consumed (`event.stop()` + `event.prevent_default()`) and whether a
/// message was posted.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum KeyOutcome {
    /// Key not owned by the bar — falls through.
    Ignored,
    /// Consumed (selection moved), nothing posted.
    Handled,
    /// Consumed and a message posted to the app.
    Emit(ApprovalMsg),
}

/// Semantic style of one option chip — the port of the Python CSS classes
/// set by `ApprovalOption.paint` (`-selected` / `-deny` / plain `$dim`).
///
/// Color tokens come from the theme (`$bright` on `$bg-tab`, `$red`,
/// `$dim`) and are resolved by the app-assembly layer once `ui/themes`
/// lands; [`OptionStyle::style`] carries the theme-independent part
/// (selected renders bold — mockup font-weight 700).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum OptionStyle {
    /// `-selected`: bright on bg-tab, bold.
    Selected,
    /// `-deny`: red while unselected.
    Deny,
    /// Unselected non-Deny: dim.
    Plain,
}

impl OptionStyle {
    /// Theme-independent ratatui style for the chip.
    pub fn style(self) -> Style {
        match self {
            OptionStyle::Selected => Style::default().add_modifier(Modifier::BOLD),
            OptionStyle::Deny | OptionStyle::Plain => Style::default(),
        }
    }
}

/// The approval strip's pure state. The app focuses it while shown; it
/// owns the keyboard (every key goes through [`ApprovalBar::handle_key`]).
#[derive(Debug, Clone)]
pub struct ApprovalBar {
    pub ticket_id: String,
    pub prompt: String,
    pub options: Vec<String>,
    /// Index of the highlighted option (Python reactive `selected`).
    pub selected: usize,
    wrapped: bool,
}

impl ApprovalBar {
    /// Build a bar; errors with the exact Python message when `options`
    /// is empty.
    pub fn new(
        ticket_id: impl Into<String>,
        prompt: impl Into<String>,
        options: Vec<String>,
    ) -> Result<Self, String> {
        if options.is_empty() {
            return Err("ApprovalBar needs at least one option".to_string());
        }
        Ok(Self {
            ticket_id: ticket_id.into(),
            prompt: prompt.into(),
            options,
            selected: 0,
            wrapped: false,
        })
    }

    /// Convenience mirroring the Python default `options=DEFAULT_OPTIONS`.
    pub fn with_default_options(ticket_id: impl Into<String>, prompt: impl Into<String>) -> Self {
        Self::new(
            ticket_id,
            prompt,
            DEFAULT_OPTIONS.iter().map(|s| s.to_string()).collect(),
        )
        .expect("DEFAULT_OPTIONS is non-empty")
    }

    // -- rendered strings (tests assert on these) ----------------------------

    /// Plain option strings as rendered (``› `` prefix on selected).
    pub fn option_texts(&self) -> Vec<String> {
        self.options
            .iter()
            .enumerate()
            .map(|(index, label)| {
                let prefix = if index == self.selected { SELECTED_PREFIX } else { "" };
                format!("{prefix}{label}")
            })
            .collect()
    }

    /// Style token per option — the port of `ApprovalOption.paint`'s
    /// class flips (`-selected` wins; Deny is red only while unselected).
    pub fn option_style(&self, index: usize) -> OptionStyle {
        if index == self.selected {
            OptionStyle::Selected
        } else if self.options[index] == DENY_OPTION {
            OptionStyle::Deny
        } else {
            OptionStyle::Plain
        }
    }

    /// Plain-text render: head line (label, one pad cell, prompt), then
    /// the option chips — all on one row normally, one full-width row per
    /// chip when wrapped (Python `-wrapped` vertical stack, #122). Each
    /// chip carries its `padding: 0 1`.
    pub fn lines_plain(&self) -> Vec<String> {
        let mut lines = vec![format!("{APPROVAL_LABEL} {}", self.prompt)];
        let chips: Vec<String> = self.option_texts().iter().map(|t| format!(" {t} ")).collect();
        if self.wrapped {
            lines.extend(chips);
        } else {
            lines.push(chips.concat());
        }
        lines
    }

    /// Styled render for ratatui: same shape as [`Self::lines_plain`] with
    /// [`OptionStyle::style`] on each chip and the label bold (its
    /// `$orange` and the chips' theme colors are applied by the assembly
    /// layer once themes are ported).
    pub fn render_lines(&self) -> Vec<Line<'static>> {
        let mut lines = vec![Line::from(vec![
            Span::styled(
                format!("{APPROVAL_LABEL} "),
                Style::default().add_modifier(Modifier::BOLD),
            ),
            Span::raw(self.prompt.clone()),
        ])];
        let chips: Vec<Span<'static>> = self
            .option_texts()
            .into_iter()
            .enumerate()
            .map(|(index, text)| Span::styled(format!(" {text} "), self.option_style(index).style()))
            .collect();
        if self.wrapped {
            lines.extend(chips.into_iter().map(Line::from));
        } else {
            lines.push(Line::from(chips));
        }
        lines
    }

    // -- wrap decision ---------------------------------------------------------

    /// One-row cell budget: label (padding-right 1) + prompt (padding-right
    /// 2) + each option chip (`padding: 0 1`) + the one ``› `` carried by
    /// the selected chip.
    pub fn needed_width(&self) -> usize {
        let mut needed = cell_len(APPROVAL_LABEL) + 1 + cell_len(&self.prompt) + 2;
        needed += self.options.iter().map(|label| cell_len(label) + 2).sum::<usize>();
        needed + cell_len(SELECTED_PREFIX)
    }

    /// Drop the options onto their own rows when one row can't fit all.
    ///
    /// Mirrors the mockup approval strip's ``flex-wrap: wrap`` — every
    /// option stays visible and clickable instead of clipping off-screen
    /// at narrow terminal widths (spec §7: options are clickable). A zero
    /// width (no layout yet) leaves the state untouched, as in Python.
    pub fn update_wrap(&mut self, width: usize) {
        if width == 0 {
            return;
        }
        self.wrapped = self.needed_width() > width;
    }

    /// Whether the bar is in the stacked (`-wrapped`) layout.
    pub fn is_wrapped(&self) -> bool {
        self.wrapped
    }

    // -- interaction ----------------------------------------------------------

    /// Feed one key (Textual key names: "left", "tab", "shift+tab",
    /// "enter", "escape", "ctrl+y", ...). Port of `on_key`.
    pub fn handle_key(&mut self, key: &str) -> KeyOutcome {
        let count = self.options.len();
        if _PREV_KEYS.contains(&key) {
            self.selected = (self.selected + count - 1) % count;
            KeyOutcome::Handled
        } else if _NEXT_KEYS.contains(&key) {
            self.selected = (self.selected + 1) % count;
            KeyOutcome::Handled
        } else if key == "enter" {
            KeyOutcome::Emit(self.resolve(self.options[self.selected].clone()))
        } else if _PARK_KEYS.contains(&key) {
            KeyOutcome::Emit(ApprovalMsg::Deferred {
                ticket_id: self.ticket_id.clone(),
            })
        } else if key == "escape" {
            let choice = self.deny_choice().to_string();
            KeyOutcome::Emit(self.resolve(choice))
        } else {
            KeyOutcome::Ignored
        }
    }

    /// An option chip was clicked: select it and confirm it directly.
    /// Port of `on_approval_bar_option_clicked`.
    pub fn click(&mut self, index: usize) -> ApprovalMsg {
        self.selected = index;
        self.resolve(self.options[index].clone())
    }

    // -- internals ---------------------------------------------------------------

    /// Esc target: "Deny" when present, otherwise the last option.
    fn deny_choice(&self) -> &str {
        if self.options.iter().any(|label| label == DENY_OPTION) {
            DENY_OPTION
        } else {
            self.options.last().expect("options is non-empty")
        }
    }

    fn resolve(&self, choice: String) -> ApprovalMsg {
        ApprovalMsg::Resolved {
            ticket_id: self.ticket_id.clone(),
            choice,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const TICKET: &str = "ticket-42";
    const PROMPT: &str = "Run `pytest -q` in /repo?";

    fn bar() -> ApprovalBar {
        ApprovalBar::with_default_options(TICKET, PROMPT)
    }

    // -- tests/test_ui_approval.py -------------------------------------------

    #[test]
    fn test_default_options_are_verbatim_fail_closed_strings() {
        assert_eq!(DEFAULT_OPTIONS, ["Allow once", "Allow always", "Deny"]);
    }

    #[test]
    fn test_label_is_exact_spec_string() {
        assert_eq!(APPROVAL_LABEL, "Approval required ·");
    }

    #[test]
    fn test_option_texts_selected_prefix() {
        assert_eq!(
            bar().option_texts(),
            vec!["› Allow once", "Allow always", "Deny"]
        );
    }

    #[test]
    fn test_rendered_strings_and_selection_styling() {
        // Text/style-token half of the Python test; widget query/render
        // mechanics are Textual and do not port.
        let mut bar = bar();
        let lines = bar.lines_plain();
        assert!(lines.iter().any(|text| text.contains(APPROVAL_LABEL)));
        assert!(lines.iter().any(|text| text.contains(PROMPT)));

        assert_eq!(
            bar.option_texts(),
            vec!["› Allow once", "Allow always", "Deny"]
        );
        // Selected bright-on-bg-tab; Deny red while unselected.
        assert_eq!(bar.option_style(0), OptionStyle::Selected);
        assert_eq!(bar.option_style(2), OptionStyle::Deny);

        assert_eq!(bar.handle_key("right"), KeyOutcome::Handled);
        assert_eq!(bar.selected, 1);
        assert_eq!(
            bar.option_texts(),
            vec!["Allow once", "› Allow always", "Deny"]
        );
    }

    #[test]
    fn test_arrows_and_tab_cycle_with_wraparound() {
        let mut bar = bar();
        bar.handle_key("left"); // wraps 0 -> 2
        assert_eq!(bar.selected, 2);
        bar.handle_key("tab"); // wraps 2 -> 0
        assert_eq!(bar.selected, 0);
        bar.handle_key("down");
        assert_eq!(bar.selected, 1);
        bar.handle_key("up");
        assert_eq!(bar.selected, 0);
    }

    #[test]
    fn test_shift_tab_cycles_forward_like_mockup() {
        // _NEXT_KEYS includes shift+tab (mockup cycles with or without shift).
        let mut bar = bar();
        bar.handle_key("shift+tab");
        assert_eq!(bar.selected, 1);
    }

    #[test]
    fn test_enter_confirms_selected_option() {
        let mut bar = bar();
        bar.handle_key("right");
        let outcome = bar.handle_key("enter");
        assert_eq!(
            outcome,
            KeyOutcome::Emit(ApprovalMsg::Resolved {
                ticket_id: TICKET.to_string(),
                choice: "Allow always".to_string(),
            })
        );
    }

    #[test]
    fn test_escape_resolves_to_deny() {
        let mut bar = bar();
        let outcome = bar.handle_key("escape");
        assert_eq!(
            outcome,
            KeyOutcome::Emit(ApprovalMsg::Resolved {
                ticket_id: TICKET.to_string(),
                choice: "Deny".to_string(),
            })
        );
    }

    #[test]
    fn test_escape_without_deny_option_resolves_to_last() {
        // Pins `_deny_choice`'s fallback branch (options without "Deny").
        let mut bar = ApprovalBar::new(
            TICKET,
            PROMPT,
            vec!["Yes".to_string(), "No".to_string()],
        )
        .unwrap();
        assert_eq!(
            bar.handle_key("escape"),
            KeyOutcome::Emit(ApprovalMsg::Resolved {
                ticket_id: TICKET.to_string(),
                choice: "No".to_string(),
            })
        );
    }

    #[test]
    fn test_click_confirms_that_option() {
        // Click dispatch (on_approval_bar_option_clicked); the mouse-hit
        // geometry itself is Textual and does not port.
        let mut bar = bar();
        let msg = bar.click(2);
        assert_eq!(
            msg,
            ApprovalMsg::Resolved {
                ticket_id: TICKET.to_string(),
                choice: "Deny".to_string(),
            }
        );
        assert_eq!(bar.selected, 2);
    }

    #[test]
    fn test_selecting_deny_swaps_red_for_selected_styling() {
        let mut bar = bar();
        assert_eq!(bar.option_style(2), OptionStyle::Deny);
        bar.handle_key("left"); // select Deny
        assert_eq!(bar.option_style(2), OptionStyle::Selected);
        assert_ne!(bar.option_style(2), OptionStyle::Deny);
        assert_eq!(bar.option_texts()[2], "› Deny");
    }

    #[test]
    fn test_empty_options_rejected() {
        let err = ApprovalBar::new(TICKET, PROMPT, vec![]).unwrap_err();
        assert_eq!(err, "ApprovalBar needs at least one option");
    }

    #[test]
    fn test_selected_option_is_bold() {
        // Mockup approvalOptions: selected renders font-weight 700.
        let bar = bar();
        assert!(bar
            .option_style(0)
            .style()
            .add_modifier
            .contains(Modifier::BOLD));
        assert!(!bar
            .option_style(1)
            .style()
            .add_modifier
            .contains(Modifier::BOLD));
    }

    #[test]
    fn test_options_wrap_onto_second_row_at_narrow_width() {
        // Mockup approval strip has flex-wrap: wrap — at 80 cols every
        // option stays on-screen (visible and clickable) instead of
        // clipping (spec §7). Region-geometry assertions are Textual; the
        // wrap decision + per-row width + click-confirm port.
        let mut bar = bar();
        bar.update_wrap(80);
        assert!(bar.is_wrapped());
        let lines = bar.lines_plain();
        // head + one row per option, each within the terminal width
        assert_eq!(lines.len(), 1 + bar.options.len());
        for line in &lines {
            assert!(cell_len(line) <= 80);
        }
        let msg = bar.click(2);
        assert_eq!(
            msg,
            ApprovalMsg::Resolved {
                ticket_id: TICKET.to_string(),
                choice: "Deny".to_string(),
            }
        );
    }

    #[test]
    fn test_options_stay_on_one_row_at_wide_width() {
        // `bar.size.height == 1` is Textual layout; the single options
        // row is pinned via the render surface instead.
        let mut bar = bar();
        bar.update_wrap(120);
        assert!(!bar.is_wrapped());
        assert_eq!(bar.lines_plain().len(), 2); // head + one options row
    }

    #[test]
    fn test_zero_width_leaves_wrap_state_untouched() {
        // Python `_update_wrap` returns early when width <= 0.
        let mut bar = bar();
        bar.update_wrap(80);
        assert!(bar.is_wrapped());
        bar.update_wrap(0);
        assert!(bar.is_wrapped());
    }

    #[test]
    fn test_ctrl_y_parks_ticket_without_resolving() {
        // ctrl-y posts Deferred(ticket_id) — the park path (ADR-0007
        // approvals) — and must NOT resolve the ticket (no answer chosen).
        let mut bar = bar();
        let outcome = bar.handle_key("ctrl+y");
        assert_eq!(
            outcome,
            KeyOutcome::Emit(ApprovalMsg::Deferred {
                ticket_id: TICKET.to_string(),
            })
        );
    }

    #[test]
    fn test_ctrl_y_park_leaves_selection_untouched() {
        // Parking does not move the selection or answer — the bar just
        // hands the ticket to the needs-you queue.
        let mut bar = bar();
        bar.handle_key("right"); // select "Allow always"
        assert_eq!(bar.selected, 1);
        let outcome = bar.handle_key("ctrl+y");
        assert_eq!(bar.selected, 1);
        assert!(matches!(
            outcome,
            KeyOutcome::Emit(ApprovalMsg::Deferred { .. })
        ));
    }

    // -- tests/test_ui_approval_wrap.py ---------------------------------------

    const LONG_OPTIONS: [&str; 4] = [
        "Refactor the module first, then add tests",
        "Add tests first, then refactor",
        "Do both in parallel with a subagent",
        "Skip it and document the risk instead",
    ];

    fn long_bar() -> ApprovalBar {
        ApprovalBar::new(
            "t1",
            "Which approach should I take?",
            LONG_OPTIONS.iter().map(|s| s.to_string()).collect(),
        )
        .unwrap()
    }

    #[test]
    fn test_wrapped_options_stack_fullwidth_and_stay_on_screen() {
        // App/footer region geometry is Textual; the stacking (one row
        // per option, all within width) and moving selection port.
        let mut bar = long_bar();
        bar.update_wrap(80);
        assert!(bar.is_wrapped());
        let lines = bar.lines_plain();
        // Each option on its own row, all within the terminal width.
        assert_eq!(lines.len(), 1 + LONG_OPTIONS.len());
        for line in &lines {
            assert!(cell_len(line) <= 80, "an option is clipped off-screen: {line:?}");
        }
        // Selection is visible and moves with arrows.
        assert_eq!(bar.option_style(0), OptionStyle::Selected);
        bar.handle_key("down");
        assert_eq!(bar.selected, 1);
        assert_eq!(bar.option_style(1), OptionStyle::Selected);
    }

    #[test]
    fn test_few_short_options_stay_on_one_row() {
        let mut bar = ApprovalBar::new(
            "t1",
            "ok?",
            DEFAULT_OPTIONS.iter().map(|s| s.to_string()).collect(),
        )
        .unwrap();
        bar.update_wrap(140);
        assert!(!bar.is_wrapped());
        assert_eq!(bar.lines_plain().len(), 2); // all on one row, unchanged
    }
}
