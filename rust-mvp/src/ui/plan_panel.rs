//! Ambient plan strip (design 2026-07-21 D1/D2): the `todo` tool's live
//! checklist, rendered in the bottom strip's right column instead of the
//! transcript.
//!
//! Port of `src/amplifier_app_newtui/ui/plan_panel.py`.
//!
//! Header: `Plan N/M` (`Plan` bright bold, counts dim). Rows: `✔` green
//! done (dim text), `▶` orange bold in-progress (bright bold text), `○`
//! dimmer pending (dim text). Overflow: at most [`PLAN_MAX_ROWS`] item
//! rows, windowed around the in-progress item, then one `⋮ +N more` dimmer
//! line. All complete: collapses to the header line alone (completion stays
//! visible — same "done stays visible" rule as the lanes panel). Formatting
//! is a pure function of the items (like `ui/transcript.py` renderers) so
//! tests pin plain strings via [`crate::ui::segments::line_plain`].
//!
//! Textual widget mechanics (mount/refresh/CSS `display`) do not port; the
//! [`PlanPanel`] state holder keeps the widget's data surface (`items`,
//! `plan_lines`, `update_plan`, show/hide) and renders through
//! [`crate::ui::segments::to_ratatui_line`] — app assembly owns layout and
//! the responsive ladder (`app_support.sync_plan_surfaces`).

use std::collections::HashMap;

use ratatui::style::Color;
use ratatui::text::Span;

use crate::model::blocks::{Segment, StyleToken, TodoItem, TodoStatus};
use crate::ui::segments::{line_plain, to_ratatui_line, Line};

/// Max item rows before collapsing the rest into `⋮ +N more`.
pub const PLAN_MAX_ROWS: usize = 5;

/// Fixed column width of the panel in the bottom strip (design §1 mockup).
pub const PLAN_PANEL_WIDTH: usize = 37;

/// Terminal cell width of `s` (Python: `rich.cells.cell_len`).
fn cell_len(s: &str) -> usize {
    Span::raw(s).width()
}

/// status -> (prefix, content token, content bold)
fn glyph(status: TodoStatus) -> (&'static str, StyleToken, bool) {
    match status {
        TodoStatus::Completed => ("  ✔ ", StyleToken::Dim, false),
        TodoStatus::InProgress => ("  ▶ ", StyleToken::Bright, true),
        TodoStatus::Pending => ("  ○ ", StyleToken::Dim, false),
    }
}

fn prefix_token(status: TodoStatus) -> StyleToken {
    match status {
        TodoStatus::Completed => StyleToken::Green,
        TodoStatus::InProgress => StyleToken::Orange,
        TodoStatus::Pending => StyleToken::Dimmer,
    }
}

/// `(done, total)` for the header and the footer fallback.
pub fn plan_counts(items: &[TodoItem]) -> (usize, usize) {
    (
        items
            .iter()
            .filter(|item| item.status == TodoStatus::Completed)
            .count(),
        items.len(),
    )
}

/// Bottom-strip panel width: the mockup's 37 minimum, grown to the
/// widest rendered row, capped at a third of the strip.
///
/// Found live in a 198-col real fan-out: fixed 37 wraps real plan items
/// while the lanes half sits mostly empty. The cap keeps lanes dominant;
/// the floor keeps the demo/goldens geometry unchanged.
pub fn plan_panel_width(items: &[TodoItem], strip_width: usize) -> usize {
    let chrome = 4; // PlanPanel CSS `padding: 0 2` — content width is panel − 4
    let needed = chrome
        + format_plan_lines(items)
            .iter()
            .map(|line| cell_len(&line_plain(line)))
            .max()
            .unwrap_or(0);
    PLAN_PANEL_WIDTH.max(needed.min(strip_width / 3))
}

/// Render the plan as Segment lines — a pure function of the items.
///
/// (Python's `format_plan_lines(items, *, max_rows=PLAN_MAX_ROWS)` with the
/// default `max_rows`; use [`format_plan_lines_max`] to override.)
pub fn format_plan_lines(items: &[TodoItem]) -> Vec<Line> {
    format_plan_lines_max(items, PLAN_MAX_ROWS)
}

/// [`format_plan_lines`] with an explicit `max_rows`.
pub fn format_plan_lines_max(items: &[TodoItem], max_rows: usize) -> Vec<Line> {
    if items.is_empty() {
        return Vec::new();
    }
    let (done, total) = plan_counts(items);
    let header: Line = vec![
        Segment {
            style_token: StyleToken::Bright,
            bold: true,
            ..Segment::new("Plan")
        },
        Segment {
            style_token: StyleToken::Dim,
            ..Segment::new(format!(" {done}/{total}"))
        },
    ];
    if done == total {
        return vec![header]; // collapse: completion stays visible as one line
    }
    let active = items
        .iter()
        .position(|item| item.status == TodoStatus::InProgress)
        .unwrap_or(0);
    let start = 0isize.max((active as isize - 1).min(total as isize - max_rows as isize)) as usize;
    let visible = &items[start..total.min(start + max_rows)];
    let mut lines: Vec<Line> = vec![header];
    for item in visible {
        let (prefix, token, bold) = glyph(item.status);
        lines.push(vec![
            Segment {
                style_token: prefix_token(item.status),
                ..Segment::new(prefix)
            },
            Segment {
                style_token: token,
                bold,
                ..Segment::new(item.content.clone())
            },
        ]);
    }
    let hidden = total - visible.len();
    if hidden > 0 {
        lines.push(vec![Segment {
            style_token: StyleToken::Dimmer,
            ..Segment::new(format!("  ⋮ +{hidden} more"))
        }]);
    }
    lines
}

/// The plan strip state (`#plan-panel`) — bottom strip, right column.
///
/// Feed it with [`PlanPanel::update_plan`]; the app decides visibility via
/// [`PlanPanel::show_panel`] / [`PlanPanel::hide_panel`] (responsive ladder
/// lives in app assembly, not here). Rendering is [`format_plan_lines`]
/// painted with theme tokens — no interaction, no focus, no timers.
#[derive(Clone, Debug, Default)]
pub struct PlanPanel {
    items: Vec<TodoItem>,
    /// Python `display` (CSS starts `display: none`).
    display: bool,
}

impl PlanPanel {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn items(&self) -> &[TodoItem] {
        &self.items
    }

    /// The exact plain-text lines currently displayed (test surface).
    pub fn plan_lines(&self) -> Vec<String> {
        format_plan_lines(&self.items)
            .iter()
            .map(|line| line_plain(line))
            .collect()
    }

    /// Replace the listing (the `todo` tool replaces the whole list).
    pub fn update_plan(&mut self, items: Vec<TodoItem>) {
        self.items = items;
    }

    pub fn show_panel(&mut self) {
        self.display = true;
    }

    pub fn hide_panel(&mut self) {
        self.display = false;
    }

    /// Whether the panel is shown (Python `widget.display`).
    pub fn display(&self) -> bool {
        self.display
    }

    /// The panel's lines as ratatui text, colors resolved from the theme's
    /// token table (Python `render()` painting with `app.theme_variables`).
    pub fn render(
        &self,
        variables: Option<&HashMap<StyleToken, Color>>,
    ) -> Vec<ratatui::text::Line<'static>> {
        format_plan_lines(&self.items)
            .iter()
            .map(|line| to_ratatui_line(line, variables))
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn item(i: usize, status: TodoStatus) -> TodoItem {
        TodoItem {
            content: format!("step {i}"),
            status,
        }
    }

    fn items(statuses: &[TodoStatus]) -> Vec<TodoItem> {
        statuses
            .iter()
            .enumerate()
            .map(|(i, status)| item(i, *status))
            .collect()
    }

    fn plains(items: &[TodoItem]) -> Vec<String> {
        format_plan_lines(items)
            .iter()
            .map(|line| line_plain(line))
            .collect()
    }

    // Python: tests/test_ui_plan_panel.py::test_no_items_renders_nothing
    #[test]
    fn test_no_items_renders_nothing() {
        assert_eq!(format_plan_lines(&[]), Vec::<Line>::new());
    }

    // Python: tests/test_ui_plan_panel.py::test_header_counts_and_glyph_rows
    #[test]
    fn test_header_counts_and_glyph_rows() {
        let items = items(&[
            TodoStatus::Completed,
            TodoStatus::InProgress,
            TodoStatus::Pending,
            TodoStatus::Pending,
        ]);
        assert_eq!(
            plains(&items),
            vec![
                "Plan 1/4",
                "  ✔ step 0",
                "  ▶ step 1",
                "  ○ step 2",
                "  ○ step 3",
            ]
        );
    }

    // Python: tests/test_ui_plan_panel.py::test_all_complete_collapses_to_header_only
    #[test]
    fn test_all_complete_collapses_to_header_only() {
        let items = items(&[
            TodoStatus::Completed,
            TodoStatus::Completed,
            TodoStatus::Completed,
        ]);
        assert_eq!(plains(&items), vec!["Plan 3/3"]);
    }

    // Python: tests/test_ui_plan_panel.py::test_overflow_windows_around_active_item_with_more_marker
    #[test]
    fn test_overflow_windows_around_active_item_with_more_marker() {
        // 8 items, active at index 4 → window starts one above the active row.
        let items = items(&[
            TodoStatus::Completed,
            TodoStatus::Completed,
            TodoStatus::Pending,
            TodoStatus::Pending,
            TodoStatus::InProgress,
            TodoStatus::Pending,
            TodoStatus::Pending,
            TodoStatus::Pending,
        ]);
        assert_eq!(PLAN_MAX_ROWS, 5);
        assert_eq!(
            plains(&items),
            vec![
                "Plan 2/8",
                "  ○ step 3",
                "  ▶ step 4",
                "  ○ step 5",
                "  ○ step 6",
                "  ○ step 7",
                "  ⋮ +3 more",
            ]
        );
    }

    // Python: tests/test_ui_plan_panel.py::test_overflow_with_no_active_item_shows_first_rows
    #[test]
    fn test_overflow_with_no_active_item_shows_first_rows() {
        let items = items(&[TodoStatus::Pending; 6]);
        let lines = plains(&items);
        assert_eq!(lines[0], "Plan 0/6");
        assert_eq!(lines[1], "  ○ step 0");
        assert_eq!(lines[lines.len() - 1], "  ⋮ +1 more");
        assert_eq!(lines.len(), 1 + PLAN_MAX_ROWS + 1); // header + rows + marker
    }

    // -- responsive width (found live: 198-col real fan-out, wrapping plan items) --

    // Python: tests/test_ui_plan_panel.py::test_plan_panel_width_grows_to_fit_long_items_capped_at_a_third
    #[test]
    fn test_plan_panel_width_grows_to_fit_long_items_capped_at_a_third() {
        // At 198 cols the fixed 37-col panel wrapped real plan items while the
        // lanes half sat mostly empty — the panel should fit its content, capped
        // at a third of the strip so the lanes stay dominant.
        let long_items = vec![
            TodoItem {
                status: TodoStatus::InProgress,
                ..TodoItem::new("Fan out parallel agents to survey repo state")
            },
            TodoItem {
                status: TodoStatus::Pending,
                ..TodoItem::new("Synthesize findings into recommended next steps")
            },
        ];
        let width = plan_panel_width(&long_items, 198);
        // widest row (4-char glyph prefix + content) + 4 cells panel padding
        assert_eq!(width, 4 + long_items[1].content.len() + 4);
        assert!(width <= 198 / 3);
        // Very long content still respects the one-third cap.
        let huge = vec![TodoItem {
            status: TodoStatus::Pending,
            ..TodoItem::new("x".repeat(200))
        }];
        assert_eq!(plan_panel_width(&huge, 198), 198 / 3);
    }

    // Python: tests/test_ui_plan_panel.py::test_plan_panel_width_never_shrinks_below_the_mockup_37
    #[test]
    fn test_plan_panel_width_never_shrinks_below_the_mockup_37() {
        let short_items = vec![
            TodoItem {
                status: TodoStatus::Completed,
                ..TodoItem::new("scan provider docs")
            },
            TodoItem {
                status: TodoStatus::Pending,
                ..TodoItem::new("run store tests")
            },
        ];
        // Demo-length content at the snapshot width: unchanged 37 (goldens hold).
        assert_eq!(plan_panel_width(&short_items, 120), PLAN_PANEL_WIDTH);
        assert_eq!(plan_panel_width(&[], 198), PLAN_PANEL_WIDTH);
    }
}
