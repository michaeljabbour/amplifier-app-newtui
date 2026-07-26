//! Top-level draw: a pure function of the assembled [`App`] state.
//!
//! Layout (DESIGN-SPEC §2, top → bottom): TitleBar / TranscriptView /
//! LiveTail / NoticeSlot overlay / palette · lanes+plan · rewind · queued ·
//! file-mentions strips / composer-or-approval-bar / FooterBar. Every color
//! resolves from the active theme's token table (`ui/themes`), never a
//! literal; block text renders through the ported `transcript_render` +
//! `segments::to_ratatui_line`.

pub mod app_support;
pub mod approval_bar;
pub mod demo_wiring;
pub mod command_context;
pub mod config_view;
pub mod directory_admin;
pub mod lanes_panel;
pub mod session_ops_view;
pub mod term_probe;
pub mod transcript;
pub mod composer;
pub mod footer;
pub mod lane_reducer;
pub mod live_tail;
pub mod plan_panel;
pub mod splash;
pub mod transcript_render;
pub mod chrome;
pub mod file_mentions;
pub mod keymap;
pub mod motion;
pub mod needs_you;
pub mod notices;
pub mod notifications;
pub mod palette;
pub mod config_admin;
pub mod queued_strip;
pub mod reducer;
pub mod runtime_adapter;
pub mod session_ops_controller;
pub mod rewind_strip;
pub mod segments;
pub mod themes;

use std::collections::HashMap;
use std::sync::OnceLock;

use ratatui::prelude::*;
use ratatui::widgets::Paragraph;
use regex::Regex;

use crate::app::App;
use crate::model::blocks::{Segment as BlockSegment, StyleToken};
use crate::ui::file_mentions::MentionStyle;
use crate::ui::footer::{footer_left_segments, footer_right_text, footer_waiting_text, footer_wrap};
use crate::ui::live_tail::streaming_spans;
use crate::ui::palette::{command_row_cells, command_row_tokens, group_header_text, PaletteRow};
use crate::ui::segments::to_ratatui_line;
use crate::ui::transcript::block_margin_top;
use crate::ui::transcript_render::render_block;

/// Rows the open palette strip may occupy (rows beyond scroll are clipped).
const PALETTE_MAX_ROWS: usize = 10;

// ---------------------------------------------------------------------------
// Frame layout — the per-frame geometry [`draw`] computed, kept for exact
// mouse hit-testing (the ratatui analogue of Textual's widget tree).
// ---------------------------------------------------------------------------

/// One frame's hit-testing geometry. [`draw`] rebuilds it every frame and
/// stores it on the [`App`]; `App::on_mouse_*` reads it so a click maps to
/// exactly what was painted (never a re-derivation that could drift).
#[derive(Clone, Debug, Default)]
pub struct FrameLayout {
    /// Inner transcript text rect (the region minus its 1-cell gutter).
    pub transcript: Rect,
    /// Total content lines built for the transcript paragraph.
    pub transcript_total_lines: usize,
    /// The scroll offset applied this frame.
    pub transcript_scroll: usize,
    /// Per mounted block: (block id, first content line, line count).
    pub block_lines: Vec<(String, usize, usize)>,
    /// Lanes strip rect (zero-sized while closed).
    pub lanes: Rect,
    /// Per lanes-strip relative row: the lane index (None = header/tail).
    pub lane_rows: Vec<Option<usize>>,
    /// Composer / approval-bar rect.
    pub input: Rect,
    /// Cell width of the composer's `[mode]` badge (0 while the approval
    /// bar replaces the composer).
    pub mode_badge_width: u16,
    /// Footer rect.
    pub footer: Rect,
    /// Waiting-badge x span (start, end-exclusive) on the footer's first
    /// row, when the `N decisions waiting · ctrl-y` badge is painted inline.
    pub badge_span: Option<(u16, u16)>,
}

impl FrameLayout {
    /// Map a transcript content line to (block id, block-local row) — the
    /// screen-y → `BlockWidget::click(row)` half of hit-testing.
    pub fn block_at_line(&self, line: usize) -> Option<(String, usize)> {
        self.block_lines.iter().find_map(|(id, start, len)| {
            (line >= *start && line < start + len).then(|| (id.clone(), line - start))
        })
    }
}

/// Which approval option chip a click at (col, row) inside the approval
/// bar lands on. Row 0 is the label+prompt head line; the chips sit on row
/// 1 (one per row when wrapped). X-ranges reuse the rendered chip widths:
/// each chip paints as ``" {option_text} "`` (`ApprovalBar::render_lines`).
pub fn approval_hit(bar: &crate::ui::approval_bar::ApprovalBar, col: usize, row: usize) -> Option<usize> {
    if row == 0 {
        return None;
    }
    let chips: Vec<usize> = bar
        .option_texts()
        .iter()
        .map(|text| Span::raw(format!(" {text} ")).width())
        .collect();
    if bar.is_wrapped() {
        let index = row - 1;
        return (index < chips.len() && col < chips[index]).then_some(index);
    }
    if row != 1 {
        return None;
    }
    let mut start = 0usize;
    for (index, width) in chips.iter().enumerate() {
        if col >= start && col < start + width {
            return Some(index);
        }
        start += width;
    }
    None
}

type ColorTable = HashMap<StyleToken, Color>;

fn seg(text: impl Into<String>, token: StyleToken) -> BlockSegment {
    BlockSegment {
        style_token: token,
        ..BlockSegment::new(text)
    }
}

/// `StyleToken` from a token *name* (palette/rewind rows carry names).
fn parse_token(name: &str) -> StyleToken {
    match name {
        "bright" => StyleToken::Bright,
        "dim" => StyleToken::Dim,
        "dimmer" => StyleToken::Dimmer,
        "teal" => StyleToken::Teal,
        "green" => StyleToken::Green,
        "orange" => StyleToken::Orange,
        "red" => StyleToken::Red,
        "blue" => StyleToken::Blue,
        "rule" => StyleToken::Rule,
        _ => StyleToken::Fg,
    }
}

/// Split a flat segment run (which may embed `\n`) into per-row lines.
fn segments_to_rows(spans: &[BlockSegment]) -> Vec<Vec<BlockSegment>> {
    let mut rows: Vec<Vec<BlockSegment>> = vec![Vec::new()];
    for span in spans {
        let mut parts = span.text.split('\n').peekable();
        while let Some(part) = parts.next() {
            if !part.is_empty() {
                rows.last_mut().expect("non-empty").push(BlockSegment {
                    text: part.to_string(),
                    ..span.clone()
                });
            }
            if parts.peek().is_some() {
                rows.push(Vec::new());
            }
        }
    }
    rows
}

/// Strip Textual content markup down to plain text (the lane tail is the
/// one surface still carrying markup — styling is dropped, text kept).
fn strip_markup(markup: &str) -> String {
    static TAG_RE: OnceLock<Regex> = OnceLock::new();
    let re = TAG_RE.get_or_init(|| Regex::new(r"\[[^\]]*\]").expect("static regex"));
    re.replace_all(markup, "").replace("\\[", "[")
}

pub fn draw(f: &mut Frame, app: &App) {
    let mut layout = FrameLayout::default();
    let ui = app.ui.borrow();
    let colors = &ui.colors;
    let area = f.area();
    let width = area.width as usize;

    // -- bottom-up height budget ------------------------------------------------
    let footer_state = app.footer_state();
    let wrap = footer_wrap(&footer_state, width);
    let footer_rows: u16 = if wrap.wrapped { 2 } else { 1 };

    let approval_lines: Vec<Line<'static>> = ui
        .approval
        .as_ref()
        .map(|bar| bar.render_lines())
        .unwrap_or_default();
    let input_rows = if ui.approval.is_some() {
        approval_lines.len().max(1) as u16
    } else {
        ui.composer.text().lines().count().clamp(1, 6) as u16
    };

    let mention_lines = if ui.file_mentions.is_open() {
        ui.file_mentions.render_lines()
    } else {
        Vec::new()
    };
    let queued_rows: u16 = if ui.queued_strip.display() { 1 } else { 0 };
    let rewind_rows: u16 = if ui.rewind.display() { 1 } else { 0 };

    let lanes_open = ui.lanes_panel.display();
    let lane_rows = if lanes_open {
        // header + one row per lane + optional tail line
        1 + ui.lanes_panel.records().len()
            + usize::from(ui.lanes_panel.tail_row_index().is_some())
    } else {
        0
    };
    let plan_lines = if ui.plan_panel.display() {
        ui.plan_panel.render(Some(colors))
    } else {
        Vec::new()
    };
    let strip_rows = lane_rows.max(plan_lines.len()) as u16;

    let palette_rows_all = if ui.palette.is_open() {
        ui.palette.rows()
    } else {
        Vec::new()
    };
    let palette_rows: u16 = palette_rows_all.len().min(PALETTE_MAX_ROWS) as u16;

    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(1),                          // title bar
            Constraint::Min(3),                             // transcript + live tail
            Constraint::Length(palette_rows),               // palette strip
            Constraint::Length(strip_rows),                 // lanes + plan strip
            Constraint::Length(rewind_rows),                // rewind strip
            Constraint::Length(queued_rows),                // queued strip
            Constraint::Length(mention_lines.len() as u16), // @file strip
            Constraint::Length(input_rows),                 // composer / approval
            Constraint::Length(footer_rows),                // footer
        ])
        .split(area);

    draw_title(f, chunks[0], &ui, colors);
    draw_transcript_region(f, chunks[1], &ui, colors, &mut layout);
    if palette_rows > 0 {
        draw_palette(f, chunks[2], &palette_rows_all, colors);
    }
    if strip_rows > 0 {
        draw_bottom_strip(f, chunks[3], &ui, &plan_lines, colors, &mut layout);
    }
    if rewind_rows > 0 {
        draw_rewind(f, chunks[4], &ui, colors);
    }
    if queued_rows > 0 {
        let text = ui.queued_strip.text();
        f.render_widget(
            Paragraph::new(to_ratatui_line(&[seg(text, StyleToken::Orange)], Some(colors))),
            chunks[5],
        );
    }
    if !mention_lines.is_empty() {
        draw_mentions(f, chunks[6], &mention_lines, colors);
    }
    layout.input = chunks[7];
    if ui.approval.is_some() {
        f.render_widget(Paragraph::new(approval_lines), chunks[7]);
    } else {
        layout.mode_badge_width =
            ratatui::text::Span::raw(ui.composer.badge_text()).width() as u16;
        draw_composer(f, chunks[7], &ui, colors);
    }
    layout.footer = chunks[8];
    draw_footer(f, chunks[8], &footer_state, colors, &mut layout);
    *app.layout.borrow_mut() = layout;
}

fn draw_title(f: &mut Frame, area: Rect, ui: &crate::app::UiState, colors: &ColorTable) {
    let spans: Vec<BlockSegment> = ui
        .title
        .title_spans()
        .into_iter()
        .map(|(text, token)| seg(text, token.unwrap_or(StyleToken::Dim)))
        .collect();
    let bg = colors
        .get(&StyleToken::BgChrome)
        .copied()
        .unwrap_or(Color::Reset);
    f.render_widget(
        Paragraph::new(to_ratatui_line(&spans, Some(colors))).style(Style::default().bg(bg)),
        area,
    );
}

fn draw_transcript_region(
    f: &mut Frame,
    area: Rect,
    ui: &crate::app::UiState,
    colors: &ColorTable,
    layout: &mut FrameLayout,
) {
    // Boot splash overlays the whole region while active.
    if let Some(splash) = ui.splash.as_ref() {
        let rows = splash.rows();
        let mut lines: Vec<Line<'static>> = Vec::new();
        let pad = (area.height as usize).saturating_sub(rows.len() + 1) / 2;
        for _ in 0..pad {
            lines.push(Line::default());
        }
        for row in &rows {
            lines.push(to_ratatui_line(row, Some(colors)));
        }
        if !splash.status().is_empty() {
            lines.push(Line::from(splash.status().to_string()));
        }
        f.render_widget(Paragraph::new(lines).alignment(Alignment::Center), area);
        return;
    }

    let width = area.width.saturating_sub(2) as usize;
    let mut lines: Vec<Line<'static>> = Vec::new();
    let mut first = true;
    for block in ui.transcript.blocks() {
        let margin = if first { 0 } else { block_margin_top(&block) };
        for _ in 0..margin {
            lines.push(Line::default());
        }
        first = false;
        let start = lines.len();
        for line in render_block(&block, width.max(20)) {
            lines.push(to_ratatui_line(&line, Some(colors)));
        }
        layout
            .block_lines
            .push((block.id().to_string(), start, lines.len() - start));
    }

    // Live tail (region two): the mutable streaming peek under history.
    let tail_source = ui.live_tail.visible_source();
    if !tail_source.is_empty() {
        lines.push(Line::default());
        for row in segments_to_rows(&streaming_spans(&tail_source)) {
            lines.push(to_ratatui_line(&row, Some(colors)));
        }
    }

    // Tail anchor: keep the newest rows visible (transcript.follow());
    // released anchor honors the wheel-scroll offset (clamped to content).
    let visible = area.height as usize;
    let bottom = lines.len().saturating_sub(visible);
    let scroll = if ui.transcript.follow() {
        bottom
    } else {
        ui.transcript_scroll.min(bottom)
    };
    let inner = Rect {
        x: area.x + 1,
        width: area.width.saturating_sub(2),
        ..area
    };
    layout.transcript = inner;
    layout.transcript_total_lines = lines.len();
    layout.transcript_scroll = scroll;
    f.render_widget(Paragraph::new(lines).scroll((scroll as u16, 0)), inner);

    // Transient notice: bottom-right overlay on the region's last row.
    if let Some(notice) = ui.notices.current() {
        let text = format!(" {notice} ");
        let w = text.chars().count().min(area.width as usize) as u16;
        let rect = Rect {
            x: area.x + area.width.saturating_sub(w + 2),
            y: area.y + area.height.saturating_sub(1),
            width: w,
            height: 1,
        };
        f.render_widget(
            Paragraph::new(to_ratatui_line(&[seg(text, StyleToken::Dim)], Some(colors))),
            rect,
        );
    }
}

fn draw_palette(
    f: &mut Frame,
    area: Rect,
    rows: &[PaletteRow<crate::commands::registry::CommandSpec>],
    colors: &ColorTable,
) {
    let mut lines: Vec<Line<'static>> = Vec::new();
    for row in rows.iter().take(PALETTE_MAX_ROWS) {
        match row {
            PaletteRow::GroupHeader { group } => {
                lines.push(to_ratatui_line(
                    &[seg(group_header_text(group), StyleToken::Dimmer)],
                    Some(colors),
                ));
            }
            PaletteRow::Command { spec, selected, .. } => {
                let (name, desc, tag) = command_row_cells(spec);
                let (name_token, desc_token, tag_token) = command_row_tokens(*selected);
                let marker = if *selected { "› " } else { "  " };
                let spans = vec![
                    seg(marker, StyleToken::Teal),
                    seg(format!("{name}  "), parse_token(name_token)),
                    seg(format!("{desc}  "), parse_token(desc_token)),
                    seg(tag, parse_token(tag_token)),
                ];
                lines.push(to_ratatui_line(&spans, Some(colors)));
            }
        }
    }
    f.render_widget(Paragraph::new(lines), area);
}

fn draw_bottom_strip(
    f: &mut Frame,
    area: Rect,
    ui: &crate::app::UiState,
    plan_lines: &[Line<'static>],
    colors: &ColorTable,
    layout: &mut FrameLayout,
) {
    let plan_width = if plan_lines.is_empty() {
        0
    } else {
        crate::ui::plan_panel::PLAN_PANEL_WIDTH as u16
    };
    let lanes_area = Rect {
        width: area.width.saturating_sub(plan_width),
        ..area
    };
    if ui.lanes_panel.display() && lanes_area.width > 0 {
        let mut lines: Vec<Line<'static>> = Vec::new();
        lines.push(to_ratatui_line(&ui.lanes_panel.header_segments(), Some(colors)));
        layout.lane_rows.push(None); // header row
        let tail_after = ui.lanes_panel.tail_row_index();
        for index in 0..ui.lanes_panel.records().len() {
            let row = ui
                .lanes_panel
                .row_segments(index, Some(lanes_area.width.saturating_sub(2) as usize));
            lines.push(to_ratatui_line(&row, Some(colors)));
            layout.lane_rows.push(Some(index));
            if tail_after == Some(index) {
                let tail = strip_markup(&ui.lanes_panel.tail_markup());
                for tail_line in tail.lines() {
                    lines.push(to_ratatui_line(
                        &[seg(tail_line.to_string(), StyleToken::Dim)],
                        Some(colors),
                    ));
                    layout.lane_rows.push(None);
                }
            }
        }
        layout.lanes = lanes_area;
        f.render_widget(Paragraph::new(lines), lanes_area);
    }
    if !plan_lines.is_empty() {
        let plan_area = Rect {
            x: area.x + area.width.saturating_sub(plan_width),
            width: plan_width,
            ..area
        };
        f.render_widget(Paragraph::new(plan_lines.to_vec()), plan_area);
    }
}

fn draw_rewind(f: &mut Frame, area: Rect, ui: &crate::app::UiState, colors: &ColorTable) {
    let spans: Vec<BlockSegment> = ui
        .rewind
        .segments()
        .into_iter()
        .map(|(token, text)| seg(text, parse_token(token)))
        .collect();
    f.render_widget(Paragraph::new(to_ratatui_line(&spans, Some(colors))), area);
}

fn draw_mentions(
    f: &mut Frame,
    area: Rect,
    rows: &[Vec<crate::ui::file_mentions::MentionSpan>],
    colors: &ColorTable,
) {
    let lines: Vec<Line<'static>> = rows
        .iter()
        .map(|row| {
            let spans: Vec<BlockSegment> = row
                .iter()
                .map(|span| {
                    let (token, bold, bg) = match span.style {
                        MentionStyle::Hint => (StyleToken::Dimmer, false, None),
                        MentionStyle::Sigil => (StyleToken::Green, true, None),
                        MentionStyle::Path => (StyleToken::Fg, false, None),
                        MentionStyle::PathSelected => {
                            (StyleToken::Bright, false, Some(StyleToken::BgTab))
                        }
                    };
                    BlockSegment {
                        style_token: token,
                        bold,
                        bg_token: bg,
                        ..BlockSegment::new(span.text.clone())
                    }
                })
                .collect();
            to_ratatui_line(&spans, Some(colors))
        })
        .collect();
    f.render_widget(Paragraph::new(lines), area);
}

fn draw_composer(f: &mut Frame, area: Rect, ui: &crate::app::UiState, colors: &ColorTable) {
    let badge_token = ui.mode.color_token;
    let mut spans = vec![
        seg(format!("{} ", ui.composer.badge_text()), badge_token),
        seg("❯ ", StyleToken::Green),
    ];
    let text = ui.composer.text();
    if text.is_empty() {
        spans.push(seg(
            ui.composer.input().placeholder.to_string(),
            StyleToken::Dimmer,
        ));
    } else {
        spans.push(seg(text.to_string(), StyleToken::Bright));
        spans.push(seg("▎", StyleToken::Dim));
    }
    let mut lines: Vec<Line<'static>> = Vec::new();
    for row in segments_to_rows(&spans) {
        lines.push(to_ratatui_line(&row, Some(colors)));
    }
    f.render_widget(Paragraph::new(lines), area);
}

fn draw_footer(
    f: &mut Frame,
    area: Rect,
    state: &crate::ui::footer::FooterState,
    colors: &ColorTable,
    layout: &mut FrameLayout,
) {
    let width = area.width as usize;
    let wrap = footer_wrap(state, width);
    let mut left = footer_left_segments(state, width);
    let waiting = footer_waiting_text(state);
    if !waiting.is_empty() && !wrap.badge_wrapped {
        left.push(seg(" · ", StyleToken::Dimmer));
        let badge_start = to_ratatui_line(&left, Some(colors)).width();
        let badge_width = ratatui::text::Span::raw(waiting.as_str()).width();
        layout.badge_span = Some((
            area.x + badge_start as u16,
            area.x + (badge_start + badge_width) as u16,
        ));
        left.push(seg(waiting.clone(), StyleToken::Orange));
    }
    let right = footer_right_text(state);
    let bg = colors
        .get(&StyleToken::BgChrome)
        .copied()
        .unwrap_or(Color::Reset);
    let style = Style::default().bg(bg);
    if wrap.wrapped && area.height >= 2 {
        let rows = Layout::default()
            .direction(Direction::Vertical)
            .constraints([Constraint::Length(1), Constraint::Length(1)])
            .split(area);
        f.render_widget(
            Paragraph::new(to_ratatui_line(&left, Some(colors))).style(style),
            rows[0],
        );
        f.render_widget(
            Paragraph::new(to_ratatui_line(&[seg(right, StyleToken::Dim)], Some(colors)))
                .style(style)
                .alignment(Alignment::Right),
            rows[1],
        );
    } else {
        let left_line = to_ratatui_line(&left, Some(colors));
        let left_width = left_line.width() as u16;
        f.render_widget(Paragraph::new(left_line).style(style), area);
        let right_rect = Rect {
            x: area.x + left_width.min(area.width),
            width: area.width.saturating_sub(left_width),
            ..area
        };
        if right_rect.width > 0 {
            f.render_widget(
                Paragraph::new(to_ratatui_line(&[seg(right, StyleToken::Dim)], Some(colors)))
                    .style(style)
                    .alignment(Alignment::Right),
                right_rect,
            );
        }
    }
}

// ---------------------------------------------------------------------------
// Hit-testing math tests (the pure decision halves of the mouse wiring;
// the click flows themselves are exercised in main.rs's flow tests).
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ui::approval_bar::ApprovalBar;

    // Adapts tests/test_ui_approval.py::test_click_confirms_that_option's
    // geometry half: each chip paints " {text} ", so the x-ranges are the
    // cumulative rendered chip widths on the options row (row 1).
    #[test]
    fn test_approval_chip_x_ranges_follow_rendered_widths() {
        let bar = ApprovalBar::new(
            "t1",
            "write_file src/health.py",
            vec!["Allow once".into(), "Allow always".into(), "Deny".into()],
        )
        .unwrap();
        // Chips: " › Allow once " (14) · " Allow always " (14) · " Deny " (6).
        assert_eq!(approval_hit(&bar, 0, 1), Some(0));
        assert_eq!(approval_hit(&bar, 13, 1), Some(0));
        assert_eq!(approval_hit(&bar, 14, 1), Some(1));
        assert_eq!(approval_hit(&bar, 27, 1), Some(1));
        assert_eq!(approval_hit(&bar, 28, 1), Some(2));
        assert_eq!(approval_hit(&bar, 33, 1), Some(2));
        assert_eq!(approval_hit(&bar, 34, 1), None, "past the last chip");
        assert_eq!(approval_hit(&bar, 0, 0), None, "head line is not a chip");
        assert_eq!(approval_hit(&bar, 0, 2), None, "no third row unwrapped");
    }

    // The wrapped (#122) stack: one full-width chip row per option.
    #[test]
    fn test_approval_chip_rows_when_wrapped() {
        let mut bar = ApprovalBar::new(
            "t1",
            "a very long prompt that cannot share a row with the chips",
            vec!["Allow once".into(), "Allow always".into(), "Deny".into()],
        )
        .unwrap();
        bar.update_wrap(40);
        assert!(bar.is_wrapped());
        assert_eq!(approval_hit(&bar, 0, 1), Some(0));
        assert_eq!(approval_hit(&bar, 0, 2), Some(1));
        assert_eq!(approval_hit(&bar, 2, 3), Some(2));
        assert_eq!(approval_hit(&bar, 30, 3), None, "past the chip's width");
        assert_eq!(approval_hit(&bar, 0, 4), None, "below the last chip");
    }

    // The screen-y → (block, block-local row) mapping the transcript click
    // path uses (adapts the hit-testing implicit in Textual's widget tree).
    #[test]
    fn test_block_at_line_maps_content_lines_to_block_rows() {
        let layout = FrameLayout {
            block_lines: vec![
                ("b1".into(), 0, 2),
                ("b2".into(), 3, 1), // one margin row between b1 and b2
                ("b3".into(), 5, 4),
            ],
            ..FrameLayout::default()
        };
        assert_eq!(layout.block_at_line(0), Some(("b1".into(), 0)));
        assert_eq!(layout.block_at_line(1), Some(("b1".into(), 1)));
        assert_eq!(layout.block_at_line(2), None, "margin rows hit nothing");
        assert_eq!(layout.block_at_line(3), Some(("b2".into(), 0)));
        assert_eq!(layout.block_at_line(4), None);
        assert_eq!(layout.block_at_line(8), Some(("b3".into(), 3)));
        assert_eq!(layout.block_at_line(9), None, "below the last block");
    }
}
