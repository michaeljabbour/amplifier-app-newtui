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
    draw_transcript_region(f, chunks[1], &ui, colors);
    if palette_rows > 0 {
        draw_palette(f, chunks[2], &palette_rows_all, colors);
    }
    if strip_rows > 0 {
        draw_bottom_strip(f, chunks[3], &ui, &plan_lines, colors);
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
    if ui.approval.is_some() {
        f.render_widget(Paragraph::new(approval_lines), chunks[7]);
    } else {
        draw_composer(f, chunks[7], &ui, colors);
    }
    draw_footer(f, chunks[8], &footer_state, colors);
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
        for line in render_block(&block, width.max(20)) {
            lines.push(to_ratatui_line(&line, Some(colors)));
        }
    }

    // Live tail (region two): the mutable streaming peek under history.
    let tail_source = ui.live_tail.visible_source();
    if !tail_source.is_empty() {
        lines.push(Line::default());
        for row in segments_to_rows(&streaming_spans(&tail_source)) {
            lines.push(to_ratatui_line(&row, Some(colors)));
        }
    }

    // Tail anchor: keep the newest rows visible (transcript.follow()).
    let visible = area.height as usize;
    let scroll = if ui.transcript.follow() {
        lines.len().saturating_sub(visible)
    } else {
        0
    } as u16;
    f.render_widget(
        Paragraph::new(lines).scroll((scroll, 0)),
        Rect {
            x: area.x + 1,
            width: area.width.saturating_sub(2),
            ..area
        },
    );

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
        let tail_after = ui.lanes_panel.tail_row_index();
        for index in 0..ui.lanes_panel.records().len() {
            let row = ui
                .lanes_panel
                .row_segments(index, Some(lanes_area.width.saturating_sub(2) as usize));
            lines.push(to_ratatui_line(&row, Some(colors)));
            if tail_after == Some(index) {
                let tail = strip_markup(&ui.lanes_panel.tail_markup());
                for tail_line in tail.lines() {
                    lines.push(to_ratatui_line(
                        &[seg(tail_line.to_string(), StyleToken::Dim)],
                        Some(colors),
                    ));
                }
            }
        }
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
) {
    let width = area.width as usize;
    let wrap = footer_wrap(state, width);
    let mut left = footer_left_segments(state, width);
    let waiting = footer_waiting_text(state);
    if !waiting.is_empty() && !wrap.badge_wrapped {
        left.push(seg(" · ", StyleToken::Dimmer));
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
