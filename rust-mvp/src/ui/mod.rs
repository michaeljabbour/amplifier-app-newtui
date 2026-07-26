//! Pure rendering — mirrors `ui/transcript.py`'s `render_block`. `draw` is a pure
//! function of `App` state: title bar, transcript, live tail, composer/approval,
//! footer. Colors are theme tokens, not literals scattered through logic.

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

use crate::app::{App, TurnState};
use crate::model::Block;
use ratatui::prelude::*;
use ratatui::widgets::{Block as WBlock, Borders, Paragraph, Wrap};

// Theme tokens (the one place hex/ansi lives — cf. ui/themes.py).
const ACCENT: Color = Color::Rgb(122, 162, 247);
const MUTED: Color = Color::Rgb(120, 128, 148);
const OK: Color = Color::Rgb(158, 206, 106);
const WARN: Color = Color::Rgb(224, 175, 104);
const USER: Color = Color::Rgb(187, 194, 207);

pub fn draw(f: &mut Frame, app: &App) {
    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(1), // title bar
            Constraint::Min(3),    // transcript + live tail
            Constraint::Length(3), // composer / approval bar
            Constraint::Length(1), // footer
        ])
        .split(f.area());

    title_bar(f, chunks[0], app);
    transcript(f, chunks[1], app);
    if app.state == TurnState::AwaitingApproval {
        approval_bar(f, chunks[2], app);
    } else {
        composer(f, chunks[2], app);
    }
    footer(f, chunks[3], app);
}

fn title_bar(f: &mut Frame, area: Rect, app: &App) {
    let spin = if app.state == TurnState::Running {
        format!("{} ", app.spinner_frame())
    } else {
        "★ ".to_string()
    };
    let line = Line::from(vec![
        Span::styled(spin, Style::default().fg(ACCENT)),
        Span::styled(
            format!("{} — {} — {}", app.state_label(), app.bundle, app.session),
            Style::default().fg(USER).add_modifier(Modifier::BOLD),
        ),
    ]);
    f.render_widget(Paragraph::new(line).style(Style::default().bg(Color::Rgb(26, 28, 38))), area);
}

fn transcript(f: &mut Frame, area: Rect, app: &App) {
    let mut lines: Vec<Line> = Vec::new();
    for block in &app.blocks {
        render_block(&mut lines, block);
    }
    if let Some(live) = &app.live {
        lines.push(Line::from(vec![
            Span::styled("  ", Style::default()),
            Span::styled(live.clone(), Style::default().fg(USER)),
            Span::styled("▌", Style::default().fg(ACCENT)),
        ]));
    }
    let para = Paragraph::new(lines)
        .wrap(Wrap { trim: false })
        .scroll((app.scroll, 0));
    f.render_widget(para, area);
}

/// The per-kind renderer — one arm per block variant (cf. `_render_*`).
fn render_block(out: &mut Vec<Line>, block: &Block) {
    match block {
        Block::SessionBanner { bundle, session } => {
            out.push(Line::from(Span::styled(
                format!("┌─ session {}  ·  bundle {} ", session, bundle),
                Style::default().fg(MUTED),
            )));
        }
        Block::User(text) => {
            out.push(Line::from(vec![
                Span::styled("› ", Style::default().fg(ACCENT).add_modifier(Modifier::BOLD)),
                Span::styled(text.clone(), Style::default().fg(USER).add_modifier(Modifier::BOLD)),
            ]));
        }
        Block::Narration(text) => {
            out.push(Line::from(Span::styled(
                format!("  {}", text),
                Style::default().fg(MUTED).add_modifier(Modifier::ITALIC),
            )));
        }
        Block::Tool { summary, ok } => {
            let (glyph, color) = if *ok { ("✓", OK) } else { ("✗", WARN) };
            out.push(Line::from(vec![
                Span::styled(format!("  {} ", glyph), Style::default().fg(color)),
                Span::styled(summary.clone(), Style::default().fg(MUTED)),
            ]));
        }
        Block::Answer(text) => {
            out.push(Line::from(Span::styled(text.clone(), Style::default().fg(USER))));
            out.push(Line::from(""));
        }
        Block::TurnRule { files, added, removed, cost } => {
            out.push(Line::from(Span::styled(
                format!("└─ files {} · +{}/−{} · ${:.4}", files, added, removed, cost),
                Style::default().fg(MUTED),
            )));
            out.push(Line::from(""));
        }
    }
}

fn composer(f: &mut Frame, area: Rect, app: &App) {
    let hint = if app.state == TurnState::Running { " (Enter steers)" } else { "" };
    let block = WBlock::default()
        .borders(Borders::ALL)
        .border_style(Style::default().fg(MUTED))
        .title(Span::styled(format!(" compose{} ", hint), Style::default().fg(MUTED)));
    let text = if app.composer.is_empty() {
        Span::styled("type a message…", Style::default().fg(MUTED).add_modifier(Modifier::DIM))
    } else {
        Span::styled(format!("{}▏", app.composer), Style::default().fg(USER))
    };
    f.render_widget(Paragraph::new(Line::from(text)).block(block), area);
}

fn approval_bar(f: &mut Frame, area: Rect, app: &App) {
    let action = app.pending_action.clone().unwrap_or_default();
    let block = WBlock::default()
        .borders(Borders::ALL)
        .border_style(Style::default().fg(WARN))
        .title(Span::styled(" approval required ", Style::default().fg(WARN).add_modifier(Modifier::BOLD)));
    let line = Line::from(vec![
        Span::styled(format!("{}   ", action), Style::default().fg(USER)),
        Span::styled("[y]", Style::default().fg(OK).add_modifier(Modifier::BOLD)),
        Span::styled(" allow   ", Style::default().fg(MUTED)),
        Span::styled("[n]", Style::default().fg(WARN).add_modifier(Modifier::BOLD)),
        Span::styled(" deny", Style::default().fg(MUTED)),
    ]);
    f.render_widget(Paragraph::new(line).block(block), area);
}

fn footer(f: &mut Frame, area: Rect, app: &App) {
    let left = format!(
        " {} · {} tok · ${:.4}",
        app.mode.label(),
        app.tallies.tokens,
        app.tallies.cost
    );
    let right = match app.state {
        TurnState::AwaitingApproval => "y allow · n deny",
        TurnState::Running => "esc interrupt",
        TurnState::Idle => "enter send · shift+tab mode · ctrl+c quit",
    };
    let notice = app.notice.clone().unwrap_or_default();
    let spans = vec![
        Span::styled(left, Style::default().fg(ACCENT)),
        Span::styled(format!("   {}", notice), Style::default().fg(WARN)),
        Span::styled(
            format!("{:>width$}", right, width = (area.width as usize).saturating_sub(40)),
            Style::default().fg(MUTED),
        ),
    ];
    f.render_widget(Paragraph::new(Line::from(spans)).style(Style::default().bg(Color::Rgb(26, 28, 38))), area);
}
