//! Pure transcript renderers: `(block, width)` → lines of Segments.
//!
//! Port of `src/amplifier_app_newtui/ui/transcript_render.py` (issue #33):
//! the 21 pure `_render_*` block→segment transforms and the dispatch table
//! have no widget state, so they live here as plain functions unit-tested by
//! the golden width matrix (ADR-0007: pure renderers are golden-tested). The
//! widget layer imports [`render_block`] / [`render_block_markup`]; nothing
//! here touches ratatui widgets.
//!
//! Rendering emits [`Segment`] runs — exact spec glyphs and strings, no
//! toolkit objects — so every visual detail is testable as plain text.
//! Styles are theme-token references (`$dim` …), never colors.

use std::sync::OnceLock;

use ratatui::text::Span;
use regex::Regex;

use crate::model::blocks::{
    Answer, Blocked, BrainstormIdea, ContextBlock, DelegateState, DelegateSummaryBlock,
    DoctorBlock, EvidenceBlock, ImproveBlock, LiveCommand, Narration, NeedsYouBlock, NeedsYouEntry,
    PlanBlock, PlanItemState, Recap, Segment, SessionBanner, SteerEcho, StyleToken, Thinking,
    TodoStatus, ToolLine, ToolLineBodyStyle, ToolLineStatus, TranscriptBlock, TurnRule, UserLine,
    WorkingStatus, GLYPH_BLOCKED, GLYPH_CHECKBOX_CHECKED, GLYPH_CHECKBOX_EMPTY,
    GLYPH_CHEVRON_COLLAPSED, GLYPH_CHEVRON_EXPANDED, GLYPH_ERROR, GLYPH_LANE_RUNNING,
    GLYPH_PLAN_DONE, GLYPH_QUOTE_GUTTER, GLYPH_SPINNER_FRAMES,
};
use crate::model::modes::get_mode;
use crate::ui::motion::shimmer_band;
use crate::ui::segments::{lines_markup, Line};

/// Exact collapsed-tool-line hint (DESIGN-SPEC §3).
pub const TOOL_EXPAND_HINT: &str = " · click to expand";

/// Collapsed thinking-block reveal hint (issue #129) — the reveal chord
/// rhymes with PR #128's live-tail `ctrl-g`.
pub const THINKING_EXPAND_HINT: &str = "ctrl-g/click to expand";

/// Shown when core withholds the reasoning prose (`ThinkingBlock.visibility`
/// LLM_ONLY/USER_ONLY) — the `content_block:end` payload arrives empty.
pub const THINKING_WITHHELD: &str = "(content withheld by provider)";

const SUPERSCRIPTS: [&str; 10] = ["⁰", "¹", "²", "³", "⁴", "⁵", "⁶", "⁷", "⁸", "⁹"];

/// Exact `/improve` header suffix (mockup cmdImprove, verbatim).
pub const IMPROVE_HEADER: &str = "from ledger + denial log · proposes, never applies silently";

/// Prose wrap cap in cells (a comfortable reading measure). Answers word-
/// wrap at `min(width, READING_MEASURE)` so a wide terminal doesn't stretch
/// paragraphs into unreadably long lines; code and table rows keep full width
/// (they are emitted verbatim, never re-wrapped) so alignment survives.
pub const READING_MEASURE: usize = 100;

/// Terminal cell width of `s` (Python: `rich.cells.cell_len`).
fn cell_len(s: &str) -> usize {
    Span::raw(s).width()
}

/// Segment constructor shorthand: text + style token, other fields default.
fn seg(text: impl Into<String>, style_token: StyleToken) -> Segment {
    Segment {
        style_token,
        ..Segment::new(text)
    }
}

/// Split segments containing newlines into per-line segment runs.
fn split_lines(segments: &[Segment]) -> Vec<Line> {
    let mut lines: Vec<Line> = vec![Vec::new()];
    for segment in segments {
        for (index, part) in segment.text.split('\n').enumerate() {
            if index > 0 {
                lines.push(Vec::new());
            }
            if !part.is_empty() {
                lines.last_mut().expect("lines never empty").push(Segment {
                    text: part.to_string(),
                    ..segment.clone()
                });
            }
        }
    }
    lines
}

fn superscript(number: usize) -> String {
    number
        .to_string()
        .chars()
        .map(|digit| SUPERSCRIPTS[digit.to_digit(10).expect("decimal digit") as usize])
        .collect()
}

fn render_session_banner(block: &SessionBanner, _width: usize) -> Vec<Line> {
    if !block.focus_note.is_empty() {
        // Mockup focusLane: 'focused: <name> ' bright bold + '· subagent of
        // …' dim — split at the first '·' of the joined banner string.
        if let Some(position) = block.focus_note.find('·') {
            let head = &block.focus_note[..position];
            let tail = &block.focus_note[position..];
            return vec![vec![
                Segment {
                    bold: true,
                    ..seg(head, StyleToken::Bright)
                },
                seg(tail, StyleToken::Dim),
            ]];
        }
        return vec![vec![seg(&block.focus_note, StyleToken::Dim)]];
    }
    let mut lines: Vec<Line> = vec![vec![Segment {
        bold: true,
        ..seg(&block.headline, StyleToken::Bright)
    }]];
    if !block.detail.is_empty() {
        lines.push(vec![seg(&block.detail, StyleToken::Dim)]);
    }
    lines
}

fn render_user_line(block: &UserLine, _width: usize) -> Vec<Line> {
    // '[delegated]' (focused-subagent brief) is teal per the mockup;
    // any other non-mode badge falls back to the chat profile (dim).
    let mode_token = if block.mode == "delegated" {
        StyleToken::Teal
    } else {
        get_mode(Some(&block.mode)).color_token
    };
    vec![vec![
        Segment {
            bold: true,
            ..seg("❯ ", StyleToken::Green)
        },
        seg(format!("[{}] ", block.mode), mode_token),
        seg(&block.text, StyleToken::Bright),
    ]]
}

fn render_narration(block: &Narration, _width: usize) -> Vec<Line> {
    vec![vec![
        seg("● ", StyleToken::Bright),
        seg(&block.text, StyleToken::Fg),
    ]]
}

fn render_tool_line(block: &ToolLine, _width: usize) -> Vec<Line> {
    let summary_token = if block.status == ToolLineStatus::Failed {
        StyleToken::Red
    } else {
        StyleToken::Dim
    };
    let mut head: Vec<Segment> = vec![seg("  ● ", summary_token), seg(&block.summary, summary_token)];
    // The mockup never mutates the head on toggle: the hint stays visible
    // while the body is expanded.
    if !block.body.is_empty() {
        head.push(seg(TOOL_EXPAND_HINT, StyleToken::Dimmer));
    }
    let mut lines: Vec<Line> = vec![head];
    if block.expanded {
        for body_line in &block.body {
            let mut token = StyleToken::Dimmer;
            let mut background = None;
            let mut bold = false;
            if block.body_style == ToolLineBodyStyle::Diff {
                if body_line.starts_with("@@") {
                    token = StyleToken::Blue;
                    bold = true;
                } else if body_line.starts_with("--- ") || body_line.starts_with("+++ ") {
                    token = StyleToken::Teal;
                } else if body_line.starts_with('+') {
                    token = StyleToken::Green;
                    background = Some(StyleToken::BgTab);
                } else if body_line.starts_with('-') {
                    token = StyleToken::Red;
                    background = Some(StyleToken::BgTab);
                } else if body_line.contains(" · ") {
                    token = StyleToken::Dim;
                }
            }
            lines.push(vec![Segment {
                bold,
                bg_token: background,
                ..seg(format!("      {body_line}"), token)
            }]);
        }
    }
    lines
}

fn render_live_command(block: &LiveCommand, _width: usize) -> Vec<Line> {
    vec![vec![
        seg("  └ ", StyleToken::Dimmer),
        seg(format!("$ {}", block.command), StyleToken::Dim),
    ]]
}

fn render_plan(block: &PlanBlock, _width: usize) -> Vec<Line> {
    let mut header: Vec<Segment> = vec![seg("· ", StyleToken::Orange), seg(&block.title, StyleToken::Fg)];
    if block.read_only {
        header.push(seg(" (read-only)", StyleToken::Dim));
    }
    if let Some(telemetry) = &block.telemetry {
        header.push(seg(format!(" {}", telemetry.suffix()), StyleToken::Dim));
    }
    let mut lines: Vec<Line> = vec![header];
    for item in &block.items {
        match item.state {
            PlanItemState::Done => lines.push(vec![
                seg("  ✔ ", StyleToken::Green),
                seg(&item.text, StyleToken::Dim),
            ]),
            // Mockup L331: the '  ■ ' prefix is plain orange (weight 400);
            // only the step text is bright bold.
            PlanItemState::Active => lines.push(vec![
                seg("  ■ ", StyleToken::Orange),
                Segment {
                    bold: true,
                    ..seg(&item.text, StyleToken::Bright)
                },
            ]),
            PlanItemState::Pending => lines.push(vec![
                seg("  □ ", StyleToken::Dimmer),
                seg(&item.text, StyleToken::Dim),
            ]),
        }
    }
    lines
}

fn render_blocked(block: &Blocked, _width: usize) -> Vec<Line> {
    let mut line: Vec<Segment> = vec![
        seg("  ⊘ blocked · ", StyleToken::Red),
        seg(&block.cmd, StyleToken::Red),
    ];
    if !block.reason.is_empty() {
        line.push(seg(format!(" · {}", block.reason), StyleToken::Dim));
    }
    if !block.continuation.is_empty() {
        line.push(seg(format!(" · {}", block.continuation), StyleToken::Dim));
    }
    vec![line]
}

/// A soft five-cell highlight band that travels across `label`.
///
/// The quiet gap keeps this from reading like a marquee; plain text never
/// changes, so copy/paste and snapshots remain stable.
fn shimmer_segments(label: &str, frame: usize) -> Vec<Segment> {
    let chars: Vec<char> = label.chars().collect();
    let band = shimmer_band(chars.len(), frame);
    let base_style = (StyleToken::Dim, false);
    let mut segments: Vec<Segment> = Vec::new();
    for (index, character) in chars.iter().enumerate() {
        let (token, bold) = band
            .iter()
            .find(|(cell, _, _)| *cell == index)
            .map(|&(_, token, bold)| (token, bold))
            .unwrap_or(base_style);
        if let Some(previous) = segments.last_mut() {
            if previous.style_token == token && previous.bold == bold {
                previous.text.push(*character);
                continue;
            }
        }
        segments.push(Segment {
            bold,
            ..seg(character.to_string(), token)
        });
    }
    segments
}

fn render_working_status(block: &WorkingStatus, _width: usize) -> Vec<Line> {
    let frame =
        GLYPH_SPINNER_FRAMES[(block.spinner_frame as usize) % GLYPH_SPINNER_FRAMES.len()];
    let suffix = block.telemetry.suffix();
    let inner = &suffix[1..suffix.len() - 1]; // "(8s · ↓ 3.2k tok)" -> "8s · ↓ 3.2k tok"
    if block.agent_count > 1 {
        // Fan-out turn (mockup runAgentsTurn): 'Coordinating N agents · …'
        // dim + 'esc to interrupt' dimmer — no 'working ·', no steer hint.
        let label = format!("Coordinating {} agents", block.agent_count);
        let mut line: Vec<Segment> = vec![seg(format!("{frame} "), StyleToken::Orange)];
        line.extend(shimmer_segments(&label, block.motion_frame as usize));
        line.push(seg(format!(" · {inner} · "), StyleToken::Dim));
        line.push(seg(&block.interrupt_hint, StyleToken::Dimmer));
        return vec![line];
    }
    // Single-agent pulse: the live activity tree beneath carries the ops
    // (spec §3). Before any tool runs, fall back to the inline note
    // (``thinking``) so the supervisor still sees the turn breathing.
    //
    // Deliberate divergence from the Python app: Python (and the mockup
    // runTurn line) falls back to '1 agent' here, which reads as a spawned
    // subagent when nothing was spawned. We show 'thinking' instead — an
    // honest label for a turn with no activity yet.
    let mut pulse: Vec<Segment> = vec![seg(format!("{frame} "), StyleToken::Orange)];
    pulse.extend(shimmer_segments("working", block.motion_frame as usize));
    if !block.activity_lines.is_empty() {
        pulse.push(seg(format!(" · {inner} · "), StyleToken::Dim));
    } else {
        let note = if block.activity.is_empty() {
            "thinking"
        } else {
            block.activity.as_str()
        };
        pulse.push(seg(format!(" · {inner} · {note} · "), StyleToken::Dim));
    }
    pulse.push(seg(
        format!("{} · {}", block.interrupt_hint, block.steer_hint),
        StyleToken::Dimmer,
    ));
    let mut lines: Vec<Line> = vec![pulse];
    let last = block.activity_lines.len().wrapping_sub(1);
    for (i, branch) in block.activity_lines.iter().enumerate() {
        let glyph = if i == last { "  └ " } else { "  ├ " };
        let text_token = if branch.running {
            StyleToken::Dim
        } else {
            StyleToken::Dimmer
        };
        lines.push(vec![
            seg(glyph, StyleToken::Dimmer),
            seg(&branch.text, text_token),
        ]);
    }
    lines
}

fn render_recap(block: &Recap, _width: usize) -> Vec<Line> {
    vec![vec![
        seg("✳ ", StyleToken::Dimmer),
        Segment {
            italic: true,
            ..seg(
                format!("Goal: {}. Next: {}.", block.goal, block.next),
                StyleToken::Dim,
            )
        },
    ]]
}

/// A list marker segment (`• ` / `✓ ` / `☐ ` / `1. ` / indented) at
/// the head of a logical answer line — its cell width becomes the hanging
/// indent for continuation lines.
fn answer_marker_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(&format!(
            r"^\s*(?:•|{GLYPH_CHECKBOX_CHECKED}|{GLYPH_CHECKBOX_EMPTY}|\d+[.)])\s+$"
        ))
        .expect("static regex compiles")
    })
}

/// Cell width of a leading list marker or blockquote gutter, or 0 if
/// the line is neither (continuation lines wrap under the body, not the
/// marker).
fn answer_marker_hang(first: &Segment) -> usize {
    if matches!(first.style_token, StyleToken::Dim | StyleToken::Green)
        && answer_marker_re().is_match(&first.text)
    {
        return cell_len(&first.text);
    }
    if first.style_token == StyleToken::Blue && first.text == GLYPH_QUOTE_GUTTER {
        return cell_len(GLYPH_QUOTE_GUTTER);
    }
    0
}

/// Lines the wrapper must not touch: fenced code (teal, 2-space indent)
/// and table rows/rules (grid separators destroy alignment if re-wrapped).
fn answer_line_is_verbatim(line: &[Segment]) -> bool {
    let first = &line[0];
    if first.style_token == StyleToken::Teal && first.text.starts_with("  ") {
        return true;
    }
    line.iter()
        .any(|segment| segment.text.contains('│') || segment.text.contains('┼'))
}

/// Merge adjacent segments that share a style so wrapped lines emit one
/// run per style rather than one per word (readable markup, small goldens).
fn coalesce(segs: Vec<Segment>) -> Line {
    let mut merged: Vec<Segment> = Vec::new();
    for segment in segs {
        if let Some(previous) = merged.last_mut() {
            if previous.style_token == segment.style_token
                && previous.bold == segment.bold
                && previous.italic == segment.italic
                && previous.bg_token == segment.bg_token
            {
                previous.text.push_str(&segment.text);
                continue;
            }
        }
        merged.push(segment);
    }
    merged
}

/// Alternating whitespace/word runs of `text` (Python `re.split(r"(\s+)")`
/// keeping the separators): `(run, is_whitespace)` pairs, no empties.
fn ws_tokens(text: &str) -> Vec<(String, bool)> {
    let mut out: Vec<(String, bool)> = Vec::new();
    for ch in text.chars() {
        let is_space = ch.is_whitespace();
        match out.last_mut() {
            Some((run, last_space)) if *last_space == is_space => run.push(ch),
            _ => out.push((ch.to_string(), is_space)),
        }
    }
    out
}

/// Greedy word-wrap a run of styled segments to `width` cells.
///
/// Continuation lines are left-padded by `hang` spaces so list-item bodies
/// stay flush under their first word (hanging indent). Styles are preserved
/// per token; a single word wider than `width` sits alone rather than looping.
fn wrap_line(segs: &[Segment], width: usize, hang: usize) -> Vec<Line> {
    let total: usize = segs.iter().map(|s| cell_len(&s.text)).sum();
    if width == 0 || total <= width {
        return vec![segs.to_vec()]; // fits as-is — keep the original segment runs
    }
    let pad = " ".repeat(hang);
    let mut lines: Vec<Vec<Segment>> = vec![Vec::new()];
    let mut widths: Vec<usize> = vec![0];
    let mut pending: Option<Segment> = None; // a whitespace run awaiting its next word

    for segment in segs {
        for (token, is_space) in ws_tokens(&segment.text) {
            let baseline = if lines.len() > 1 { hang } else { 0 };
            if is_space {
                if *widths.last().expect("non-empty") > baseline {
                    pending = Some(Segment {
                        text: token,
                        ..segment.clone()
                    });
                }
                continue; // drop leading whitespace on a fresh line
            }
            let tok_w = cell_len(&token);
            let mut space_w = pending
                .as_ref()
                .map(|p| cell_len(&p.text))
                .unwrap_or(0);
            let current = *widths.last().expect("non-empty");
            if current > baseline && current + space_w + tok_w > width {
                // word does not fit — start a continuation line
                lines.push(if hang > 0 {
                    vec![Segment::new(pad.clone())]
                } else {
                    Vec::new()
                });
                widths.push(hang);
                pending = None;
                space_w = 0;
            }
            if let Some(space) = pending.take() {
                lines.last_mut().expect("non-empty").push(space);
                *widths.last_mut().expect("non-empty") += space_w;
            }
            lines.last_mut().expect("non-empty").push(Segment {
                text: token,
                ..segment.clone()
            });
            *widths.last_mut().expect("non-empty") += tok_w;
        }
    }
    lines.into_iter().map(coalesce).collect()
}

/// Collapsible inline thinking block (issue #129).
///
/// Collapsed: one dim `▸ thinking · N lines · ctrl-g/click to expand`
/// row. Expanded: a `▾ thinking` header + the reasoning prose in dim
/// italic. When core withholds the reasoning (empty `text` —
/// `ThinkingBlock.visibility` LLM_ONLY/USER_ONLY), the block degrades to
/// a single dim `· thinking · (content withheld by provider)` line that
/// cannot be expanded — honest about the gap rather than rendering nothing.
fn render_thinking(block: &Thinking, _width: usize) -> Vec<Line> {
    if block.text.is_empty() {
        return vec![vec![
            seg("· ", StyleToken::Dimmer),
            seg(format!("thinking · {THINKING_WITHHELD}"), StyleToken::Dim),
        ]];
    }
    let mut body_lines: Vec<&str> = block.text.lines().collect();
    if body_lines.is_empty() {
        body_lines.push(&block.text);
    }
    if !block.expanded {
        let count = body_lines.len();
        let noun = if count == 1 { "line" } else { "lines" };
        return vec![vec![
            seg(format!("{GLYPH_CHEVRON_COLLAPSED} "), StyleToken::Dimmer),
            seg(format!("thinking · {count} {noun}"), StyleToken::Dim),
            seg(format!(" · {THINKING_EXPAND_HINT}"), StyleToken::Dimmer),
        ]];
    }
    let mut lines: Vec<Line> = vec![vec![
        seg(format!("{GLYPH_CHEVRON_EXPANDED} "), StyleToken::Dimmer),
        seg("thinking", StyleToken::Dim),
    ]];
    for body_line in body_lines {
        lines.push(vec![Segment {
            italic: true,
            ..seg(format!("  {body_line}"), StyleToken::Dim)
        }]);
    }
    lines
}

/// Long answers read like a document: prose word-wraps at a comfortable
/// reading measure (`min(width, READING_MEASURE)`) with hanging indents on
/// list continuations, so a wide terminal never stretches a paragraph into
/// an unreadably long line.
///
/// Code and table lines pass through verbatim at full width; every other
/// logical line is greedy-wrapped, list items keeping their body aligned
/// under the marker.
fn render_answer(block: &Answer, width: usize) -> Vec<Line> {
    let prose_width = width.min(READING_MEASURE);
    let mut out: Vec<Line> = Vec::new();
    for line in split_lines(&block.spans) {
        if line.is_empty() {
            // Inter-block spacing: one blank line max — the block sentinel
            // and a source blank line must not stack into a double gap.
            if matches!(out.last(), Some(last) if last.is_empty()) {
                continue;
            }
            out.push(line);
            continue;
        }
        if answer_line_is_verbatim(&line) {
            out.push(line);
            continue;
        }
        let hang = answer_marker_hang(&line[0]);
        out.extend(wrap_line(&line, prose_width, hang));
    }
    // Drop a leading/trailing blank the collapsing may leave.
    while matches!(out.first(), Some(line) if line.is_empty()) {
        out.remove(0);
    }
    while matches!(out.last(), Some(line) if line.is_empty()) {
        out.pop();
    }
    out
}

/// A verbatim fenced-code line in a rendered answer: a lone teal run
/// indented two spaces (`answer_spans` emits code that way). Inline code
/// is teal too but never opens a line with the 2-space code indent, and
/// table rows carry `│`/`┼` box glyphs — so neither is mistaken here.
fn is_fence_line(line: &[Segment]) -> bool {
    line.first()
        .is_some_and(|first| first.style_token == StyleToken::Teal && first.text.starts_with("  "))
}

/// The dedented source of the fenced code block covering `row`, or
/// `None` when `row` is not inside a fence.
///
/// Pure and click-independent: the answer widget maps a click's content-y
/// to a rendered row and copies exactly this fence (the whole answer is
/// what `/copy` grabs — this is the finer-grained affordance).
pub fn fence_text_at_row(lines: &[Line], row: isize) -> Option<String> {
    if row < 0 || row as usize >= lines.len() || !is_fence_line(&lines[row as usize]) {
        return None;
    }
    let row = row as usize;
    let mut start = row;
    while start > 0 && is_fence_line(&lines[start - 1]) {
        start -= 1;
    }
    let mut end = row;
    while end + 1 < lines.len() && is_fence_line(&lines[end + 1]) {
        end += 1;
    }
    let out: Vec<String> = (start..=end)
        .map(|index| {
            let text: String = lines[index].iter().map(|s| s.text.as_str()).collect();
            match text.strip_prefix("  ") {
                Some(stripped) => stripped.to_string(),
                None => text,
            }
        })
        .collect();
    Some(out.join("\n"))
}

fn render_steer_echo(block: &SteerEcho, _width: usize) -> Vec<Line> {
    vec![vec![
        seg("  ↳ ", StyleToken::Teal),
        seg(
            format!("steer queued: \"{}\" ", block.text),
            StyleToken::Teal,
        ),
        seg(format!("· {}", block.note), StyleToken::Dimmer),
    ]]
}

/// Full-width 1px rule + right-aligned label; dim/dimmer by shipped.
fn render_turn_rule(block: &TurnRule, width: usize) -> Vec<Line> {
    let label_token = if block.shipped {
        StyleToken::Dim
    } else {
        StyleToken::Dimmer
    };
    let label_width = cell_len(&block.label);
    if width >= label_width + 4 {
        let fill = width - label_width - 1;
        return vec![vec![
            seg("─".repeat(fill), StyleToken::Rule),
            seg(" ", StyleToken::Rule),
            seg(&block.label, label_token),
        ]];
    }
    // Too narrow to share a line: full rule, then the label right-aligned.
    let pad = width.saturating_sub(label_width);
    vec![
        vec![seg("─".repeat(width.max(1)), StyleToken::Rule)],
        vec![
            seg(" ".repeat(pad), StyleToken::Rule),
            seg(&block.label, label_token),
        ],
    ]
}

fn render_evidence(block: &EvidenceBlock, _width: usize) -> Vec<Line> {
    let total = block.links.len();
    let mut lines: Vec<Line> = vec![vec![
        seg("· ", StyleToken::Teal),
        Segment {
            bold: true,
            ..seg("Evidence", StyleToken::Teal)
        },
        seg(
            format!(
                "  {}/{total} · ←/→ select · enter expand · esc close",
                block.selected + 1
            ),
            StyleToken::Dimmer,
        ),
    ]];
    for (index, link) in block.links.iter().enumerate() {
        lines.push(vec![
            seg(format!("  {} ", superscript(index + 1)), StyleToken::Teal),
            seg(format!("\"{}\"", link.claim_quote), StyleToken::Fg),
            seg(" → ", StyleToken::Dim),
            seg(&link.tool_ref, StyleToken::Dim),
        ]);
    }
    lines
}

/// Two-decimal dollar figure matching Python's `f"{Decimal:.2f}"`.
fn format_spend_2dp(spend: rust_decimal::Decimal) -> String {
    format!("{:.2}", spend.round_dp(2))
}

fn render_ledger(block: &crate::model::blocks::LedgerBlock, _width: usize) -> Vec<Line> {
    vec![
        vec![
            seg("· ", StyleToken::Blue),
            seg(
                format!("Session ledger  {} · {}", block.session, block.bundle),
                StyleToken::Fg,
            ),
        ],
        vec![seg(
            format!(
                "  {} turns · ${} · {} shipped · {} answer-only · cache hit {}%",
                block.turns,
                format_spend_2dp(block.spend),
                block.shipped,
                block.answer_only,
                block.cache_hit_pct
            ),
            StyleToken::Dim,
        )],
    ]
}

fn render_context(block: &ContextBlock, _width: usize) -> Vec<Line> {
    let mut lines: Vec<Line> = vec![vec![
        seg("· ", StyleToken::Blue),
        seg(
            format!("Context  {}% of {}", block.used_pct, block.window_label),
            StyleToken::Fg,
        ),
    ]];
    if !block.segments.is_empty() {
        // Mockup cmdContext: ONE dim line — '  ████████░░░░  <legend>'.
        // Labels carry the legend value ("free 116k"); the first word is
        // the bucket name — free renders ░, used buckets █.
        let bar: String = block
            .segments
            .iter()
            .filter(|(_, cells)| *cells > 0)
            .map(|(label, cells)| {
                let glyph = if label.split(' ').next().unwrap_or("") == "free" {
                    "░"
                } else {
                    "█"
                };
                glyph.repeat(*cells as usize)
            })
            .collect();
        let legend: Vec<&str> = block.segments.iter().map(|(label, _)| label.as_str()).collect();
        lines.push(vec![seg(
            format!("  {bar}  {}", legend.join(" · ")),
            StyleToken::Dim,
        )]);
    }
    lines
}

/// The fg question text, with the entry's highlight run in teal
/// (mockup: 'Push to fork ' fg + 'mj/waypoint' teal + ' instead?' fg).
fn needs_you_question_segments(entry: &NeedsYouEntry) -> Vec<Segment> {
    let question = &entry.question;
    if !entry.highlight.is_empty() {
        if let Some(position) = question.find(&entry.highlight) {
            let before = &question[..position];
            let after = &question[position + entry.highlight.len()..];
            let mut segments: Vec<Segment> = Vec::new();
            if !before.is_empty() {
                segments.push(seg(before, StyleToken::Fg));
            }
            segments.push(seg(&entry.highlight, StyleToken::Teal));
            if !after.is_empty() {
                segments.push(seg(after, StyleToken::Fg));
            }
            return segments;
        }
    }
    vec![seg(question, StyleToken::Fg)]
}

fn render_needs_you(block: &NeedsYouBlock, _width: usize) -> Vec<Line> {
    // Header is ONE plain orange run, count never pluralized (mockup
    // showNeedsYou: 'Needs you  N deferred decision').
    let count = block.items.len();
    let mut lines: Vec<Line> = vec![vec![
        seg("· ", StyleToken::Orange),
        seg(
            format!("Needs you  {count} deferred decision"),
            StyleToken::Orange,
        ),
    ]];
    for (index, entry) in block.items.iter().enumerate() {
        let mut row: Vec<Segment> = vec![seg(format!("  {} ", index + 1), StyleToken::Orange)];
        row.extend(needs_you_question_segments(entry));
        if !entry.reason.is_empty() {
            row.push(seg(format!(" · {}", entry.reason), StyleToken::Dim));
        }
        for choice in &entry.choices {
            row.push(seg("  ", StyleToken::Fg));
            row.push(Segment {
                bg_token: Some(StyleToken::BgTab),
                ..seg(format!("[{}]", choice.label), StyleToken::Green)
            });
        }
        lines.push(row);
    }
    lines
}

fn render_doctor(block: &DoctorBlock, _width: usize) -> Vec<Line> {
    let mut lines: Vec<Line> = Vec::new();
    if !block.headline.is_empty() {
        lines.push(vec![
            seg("· ", StyleToken::Blue),
            seg(format!("Doctor  {}", block.headline), StyleToken::Fg),
        ]);
    }
    for healthy in &block.healthy {
        lines.push(vec![
            seg("  ✔ ", StyleToken::Green),
            seg(healthy, StyleToken::Dim),
        ]);
    }
    for finding in &block.findings {
        lines.push(vec![
            seg(format!("  {} ", finding.number), StyleToken::Orange),
            seg(&finding.text, StyleToken::Dim),
        ]);
    }
    lines
}

fn render_improve(block: &ImproveBlock, _width: usize) -> Vec<Line> {
    let mut lines: Vec<Line> = vec![vec![
        seg("· ", StyleToken::Blue),
        seg(format!("Improve  {IMPROVE_HEADER}"), StyleToken::Fg),
    ]];
    if block.proposals.is_empty() {
        lines.push(vec![seg(
            "  no proposals yet · repeated approvals and overridden denials become evidence here",
            StyleToken::Dimmer,
        )]);
    }
    for (index, proposal) in block.proposals.iter().enumerate() {
        let number = index + 1;
        if !proposal.action.is_empty() {
            // 'allowlist:' rows name the action once, in green (mockup
            // cmdImprove: dim '  1 allowlist: ' + green action + dim tail).
            lines.push(vec![
                seg(format!("  {number} {} ", proposal.title), StyleToken::Dim),
                seg(&proposal.action, StyleToken::Green),
                seg(format!(" {}", proposal.rationale), StyleToken::Dim),
            ]);
        } else {
            lines.push(vec![seg(
                format!("  {number} {} {}", proposal.title, proposal.rationale),
                StyleToken::Dim,
            )]);
        }
    }
    lines
}

fn render_brainstorm_idea(block: &BrainstormIdea, _width: usize) -> Vec<Line> {
    // Mockup brainstorm ideas are single fg runs: '  1 Ambient tab color: …'
    // (number + space, no period, no accent color).
    let prefix = if block.number > 0 {
        format!("  {} ", block.number)
    } else {
        "  ".to_string()
    };
    vec![vec![seg(format!("{prefix}{}", block.text), StyleToken::Fg)]]
}

/// `42s` under a minute, `1m 42s` above (lane-panel zero-pad style).
fn format_span(seconds: f64) -> String {
    let total = seconds as i64;
    if total < 60 {
        return format!("{total}s");
    }
    let (minutes, secs) = (total / 60, total % 60);
    format!("{minutes}m {secs:02}s")
}

/// Cell-width truncation with a trailing ellipsis; '' when it can't fit.
fn clip(text: &str, budget: i64) -> String {
    if budget <= 1 {
        return String::new();
    }
    if (cell_len(text) as i64) <= budget {
        return text.to_string();
    }
    let mut out = String::new();
    let mut width: i64 = 0;
    for ch in text.chars() {
        let mut buf = [0u8; 4];
        let ch_w = cell_len(ch.encode_utf8(&mut buf)) as i64;
        if width + ch_w > budget - 1 {
            break;
        }
        out.push(ch);
        width += ch_w;
    }
    out.push('…');
    out
}

fn delegate_glyph(state: DelegateState) -> (&'static str, StyleToken) {
    match state {
        DelegateState::Running => (GLYPH_LANE_RUNNING, StyleToken::Dimmer),
        DelegateState::Done => (GLYPH_PLAN_DONE, StyleToken::Green),
        DelegateState::Error => (GLYPH_ERROR, StyleToken::Red),
        DelegateState::Cancelled => (GLYPH_BLOCKED, StyleToken::Red),
    }
}

// Reuse the plan-panel checklist glyphs (ui/plan_panel.py:_GLYPHS) — goldens pin both.
fn delegate_plan_glyph(status: TodoStatus) -> (&'static str, StyleToken) {
    match status {
        TodoStatus::Completed => ("✔", StyleToken::Green),
        TodoStatus::InProgress => ("▶", StyleToken::Orange),
        TodoStatus::Pending => ("○", StyleToken::Dim),
    }
}

/// Ambient-progress D5: one-line summary, expandable to the agent tree.
fn render_delegate_summary(block: &DelegateSummaryBlock, width: usize) -> Vec<Line> {
    let width = width as i64;
    // Python `if block.plan_final:` — falsy for both None and an empty list.
    let plan_final = block
        .plan_final
        .as_deref()
        .filter(|plan| !plan.is_empty());
    let running = block
        .entries
        .iter()
        .filter(|entry| entry.state == DelegateState::Running)
        .count();
    let mut head: Vec<Segment> = vec![seg("● ", StyleToken::Bright)];
    if running > 0 {
        let noun = if running == 1 { "delegate" } else { "delegates" };
        head.push(seg(format!("{running} {noun} running…"), StyleToken::Dim));
    } else {
        let total = block.entries.len();
        let noun = if total == 1 { "delegate" } else { "delegates" };
        head.push(seg(format!("Used {total} {noun}"), StyleToken::Fg));
        let mut detail = String::new();
        if let Some(plan) = plan_final {
            let done = plan
                .iter()
                .filter(|item| item.status == TodoStatus::Completed)
                .count();
            detail.push_str(&format!(" · Plan {done}/{}", plan.len()));
        }
        detail.push_str(&format!(" · {}", format_span(block.duration_s)));
        head.push(seg(detail, StyleToken::Dim));
        let chevron = if block.expanded {
            GLYPH_CHEVRON_EXPANDED
        } else {
            GLYPH_CHEVRON_COLLAPSED
        };
        head.push(seg(format!(" {chevron}"), StyleToken::Dimmer));
    }
    let mut lines: Vec<Line> = vec![head];
    if !block.expanded {
        return lines;
    }
    let name_width = block
        .entries
        .iter()
        .map(|entry| cell_len(&entry.agent))
        .max()
        .unwrap_or(0);
    for (index, entry) in block.entries.iter().enumerate() {
        let branch = if index == block.entries.len() - 1 {
            "└─"
        } else {
            "├─"
        };
        let (glyph, token) = delegate_glyph(entry.state);
        // Python str.ljust pads by code points to name_width.
        let mut padded = entry.agent.clone();
        while padded.chars().count() < name_width {
            padded.push(' ');
        }
        let mut row: Vec<Segment> = vec![
            seg(format!("    {branch} "), StyleToken::Dimmer),
            seg(format!("{glyph} "), token),
            seg(format!("{padded}  "), StyleToken::Dim),
        ];
        if entry.state == DelegateState::Running {
            row.push(seg("running", StyleToken::Dimmer));
        } else {
            let mut tail = format_span(entry.elapsed_s);
            if !entry.snippet.is_empty() {
                let used: i64 = row.iter().map(|s| cell_len(&s.text) as i64).sum::<i64>()
                    + cell_len(&tail) as i64;
                let snippet = clip(&entry.snippet, width - used - 5);
                if !snippet.is_empty() {
                    tail.push_str(&format!(" · \"{snippet}\""));
                }
            }
            row.push(seg(tail, StyleToken::Dim));
        }
        lines.push(row);
    }
    if let Some(plan) = plan_final {
        // One line, clipped to width — real plans carry long items that
        // would otherwise soft-wrap mid-word into an unaligned blob.
        let mut plan_row: Vec<Segment> = vec![seg("    Plan  ", StyleToken::Dim)];
        let mut used: i64 = 10;
        let mut shown = 0usize;
        for item in plan {
            let (glyph, token) = delegate_plan_glyph(item.status);
            let content = clip(&item.content, width - used - 4); // glyph + trail
            if content.is_empty() {
                break;
            }
            plan_row.push(seg(format!("{glyph} "), token));
            let trail = if content == item.content { "  " } else { "" };
            plan_row.push(seg(format!("{content}{trail}"), StyleToken::Dim));
            used += 2 + cell_len(&content) as i64 + trail.len() as i64;
            shown += 1;
            if trail.is_empty() {
                break;
            }
        }
        let ends_with_trail = plan_row
            .last()
            .is_some_and(|last| last.text.ends_with("  "));
        if shown < plan.len() && ends_with_trail {
            // Items were dropped whole: spend the reserved trail on a
            // visible "there's more" marker (same width, no overflow).
            let last = plan_row.last_mut().expect("plan row non-empty");
            let kept = last.text[..last.text.len() - 2].to_string();
            last.text = format!("{kept} …");
        }
        lines.push(plan_row);
    }
    lines
}

/// Render one block to lines of Segments — a pure function of (block, width).
///
/// Every block kind in the union is supported; the match is exhaustive, so
/// the Python "unknown kind fails loudly" path is a compile error here.
pub fn render_block(block: &TranscriptBlock, width: usize) -> Vec<Line> {
    match block {
        TranscriptBlock::SessionBanner(b) => render_session_banner(b, width),
        TranscriptBlock::UserLine(b) => render_user_line(b, width),
        TranscriptBlock::Narration(b) => render_narration(b, width),
        TranscriptBlock::ToolLine(b) => render_tool_line(b, width),
        TranscriptBlock::LiveCommand(b) => render_live_command(b, width),
        TranscriptBlock::Plan(b) => render_plan(b, width),
        TranscriptBlock::Blocked(b) => render_blocked(b, width),
        TranscriptBlock::WorkingStatus(b) => render_working_status(b, width),
        TranscriptBlock::Recap(b) => render_recap(b, width),
        TranscriptBlock::Thinking(b) => render_thinking(b, width),
        TranscriptBlock::Answer(b) => render_answer(b, width),
        TranscriptBlock::SteerEcho(b) => render_steer_echo(b, width),
        TranscriptBlock::TurnRule(b) => render_turn_rule(b, width),
        TranscriptBlock::Evidence(b) => render_evidence(b, width),
        TranscriptBlock::Ledger(b) => render_ledger(b, width),
        TranscriptBlock::Context(b) => render_context(b, width),
        TranscriptBlock::NeedsYou(b) => render_needs_you(b, width),
        TranscriptBlock::Doctor(b) => render_doctor(b, width),
        TranscriptBlock::Improve(b) => render_improve(b, width),
        TranscriptBlock::BrainstormIdea(b) => render_brainstorm_idea(b, width),
        TranscriptBlock::DelegateSummary(b) => render_delegate_summary(b, width),
    }
}

/// Markup form of [`render_block`] (styles = `$token` variables).
pub fn render_block_markup(block: &TranscriptBlock, width: usize) -> String {
    lines_markup(&render_block(block, width))
}

#[cfg(test)]
mod tests {
    //! Pins `tests/test_ui_transcript_render.py`, `tests/test_ui_render_thinking.py`
    //! and `tests/test_ui_render_delegate_summary.py`. Each test is named after
    //! the Python case it ports.
    //!
    //! Inputs the Python tests build via `ui/live_tail.answer_spans` (an
    //! unported unit) are pinned here as the exact segment lists the real
    //! Python `answer_spans` produced (oracle dump, 2026-07-26) — the render
    //! path under test is identical.
    //!
    //! Not ported (with reasons):
    //! - `test_segment_style_token_variables`, `test_link_url_is_quoted_and_parses`,
    //!   `test_to_rich_text_resolves_tokens_from_mapping_only` — already ported
    //!   with `ui/segments.rs`.
    //! - `test_markup_roundtrip_matches_plain` — needs Textual's
    //!   `Content.from_markup` parser; the markup emitters are byte-pinned in
    //!   `ui/segments.rs` instead (see the adapted markup test below).
    //! - `TestAnswerMarkdown` cases that only assert on `answer_spans` output
    //!   (plain round-trip, heading, pipe table, code fence spans, bullets and
    //!   links, wide-table fallback, heading blank line) — they test
    //!   `ui/live_tail.answer_spans`, not this renderer.
    //! - `test_todo_tool_reroutes_to_plan_changed_never_the_transcript` —
    //!   reducer/kernel-event wiring, not a pure renderer case.

    use rust_decimal::Decimal;

    use super::*;
    use crate::model::blocks::{
        DelegateEntry, DoctorFinding, ImproveProposal, LedgerBlock, NeedsYouChoice, PlanItem,
        TodoItem,
    };
    use crate::model::evidence::EvidenceLink;
    use crate::model::turn::TurnTelemetry;
    use crate::ui::segments::{line_plain, lines_plain};

    const GOLDEN_WIDTHS: [usize; 3] = [40, 80, 120];

    fn dec(value: &str) -> Decimal {
        value.parse().expect("valid decimal literal")
    }

    fn tel() -> TurnTelemetry {
        TurnTelemetry {
            secs: 68.0,
            tokens_down: 83_900,
            cached_pct: Some(91),
            cost: dec("0.17"),
            estimated: false,
        }
    }

    fn live_tel() -> TurnTelemetry {
        TurnTelemetry {
            tokens_down: 3_200,
            ..TurnTelemetry::new(8.0)
        }
    }

    fn working() -> WorkingStatus {
        WorkingStatus {
            agent_count: 3,
            ..WorkingStatus::new("b11", live_tel())
        }
    }

    fn answer_fixture() -> Answer {
        Answer {
            evidence_refs: vec![EvidenceLink::new("it is done", "pytest run")],
            ..Answer::new(
                "b13",
                vec![
                    Segment::new("Run "),
                    seg("pytest", StyleToken::Teal),
                    seg(" — it is ", StyleToken::Fg),
                    Segment {
                        bold: true,
                        ..seg("done", StyleToken::Bright)
                    },
                    seg(".\nSecond line.", StyleToken::Fg),
                ],
            )
        }
    }

    fn rule_shipped() -> TurnRule {
        TurnRule {
            shipped: true,
            ..TurnRule::new(
                "b15",
                "t1",
                format!("{} · 3 files · +142/−38 · tests ✔", tel().label()),
            )
        }
    }

    fn rule_answer() -> TurnRule {
        TurnRule::new("b16", "t2", format!("{} · answer", tel().label()))
    }

    /// The `_blocks()` fixture, one block per golden name.
    fn blocks(name: &str) -> TranscriptBlock {
        match name {
            "session_banner" => SessionBanner {
                detail: "Bundle: dev | Provider: anthropic | claude-fable-5 · session a1b2c3"
                    .to_string(),
                ..SessionBanner::new("b1", "Amplifier 0.1.0 · core 1.6.0")
            }
            .into(),
            "user" => UserLine {
                mode: "build".to_string(),
                ..UserLine::new("b2", "Please verify the persistence boundary")
            }
            .into(),
            "narration" => Narration::new("b3", "Checking the durable session store").into(),
            "tool_collapsed" => ToolLine {
                body: vec!["1214 passed".to_string(), "build succeeded".to_string()],
                status: ToolLineStatus::Completed,
                ..ToolLine::new("b4", "Ran 2 shell commands")
            }
            .into(),
            "tool_expanded" => ToolLine {
                body: vec!["1214 passed".to_string(), "build succeeded".to_string()],
                expanded: true,
                status: ToolLineStatus::Completed,
                ..ToolLine::new("b5", "Ran 2 shell commands")
            }
            .into(),
            "tool_failed" => ToolLine {
                body: vec!["1 failed".to_string()],
                status: ToolLineStatus::Failed,
                ..ToolLine::new("b6", "Test suite failed")
            }
            .into(),
            "live_command" => LiveCommand::new("b7", "uv run pytest tests -q").into(),
            "plan" => PlanBlock {
                telemetry: Some(tel()),
                items: vec![
                    PlanItem {
                        state: PlanItemState::Done,
                        ..PlanItem::new("Audit persistence paths")
                    },
                    PlanItem {
                        state: PlanItemState::Active,
                        ..PlanItem::new("Migrate durable history")
                    },
                    PlanItem::new("Add reconciliation"),
                ],
                ..PlanBlock::new("b8", "Refactor session store")
            }
            .into(),
            "plan_read_only" => PlanBlock {
                read_only: true,
                ..PlanBlock::new("b9", "Ship checklist")
            }
            .into(),
            "blocked" => Blocked {
                continuation: "continuing without push".to_string(),
                ..Blocked::new("b10", "git push --force origin main", "denied by user")
            }
            .into(),
            "working" => working().into(),
            "recap" => Recap::new("b12", "durable chat history", "resume migration").into(),
            "answer" => answer_fixture().into(),
            "steer" => SteerEcho::new("b14", "focus on the tests").into(),
            "rule_shipped" => rule_shipped().into(),
            "rule_answer" => rule_answer().into(),
            "evidence" => EvidenceBlock::new(
                "b17",
                vec![
                    EvidenceLink::new("all tests pass", "pytest run · 34 passed"),
                    EvidenceLink::new("3 files changed", "git diff --stat"),
                ],
            )
            .into(),
            "ledger" => LedgerBlock {
                id: "b18".to_string(),
                session: "a1b2c3".to_string(),
                bundle: "dev-bundle".to_string(),
                turns: 3,
                spend: dec("1.24"),
                shipped: 2,
                answer_only: 1,
                cache_hit_pct: 91,
            }
            .into(),
            "context" => ContextBlock {
                segments: vec![
                    ("conversation".to_string(), 5),
                    ("tools".to_string(), 2),
                    ("memory".to_string(), 1),
                    ("free".to_string(), 2),
                ],
                ..ContextBlock::new("b19", 42)
            }
            .into(),
            "needs_you" => NeedsYouBlock::new(
                "b20",
                vec![NeedsYouEntry {
                    reason: "net access denied".to_string(),
                    choices: vec![NeedsYouChoice::new("yes · push to fork", "push")],
                    ..NeedsYouEntry::new("d1", "push branch to fork?")
                }],
            )
            .into(),
            "doctor" => DoctorBlock {
                headline: "1 finding · nothing changed yet".to_string(),
                healthy: vec!["provider mounted".to_string(), "bundle resolves".to_string()],
                findings: vec![DoctorFinding::new(1, "bundle override unused")],
                ..DoctorBlock::new("b21")
            }
            .into(),
            "improve" => ImproveBlock {
                proposals: vec![
                    ImproveProposal {
                        action: "uv run pytest".to_string(),
                        ..ImproveProposal::new("allowlist:", "approved 22/22 times · add to auto")
                    },
                    ImproveProposal::new(
                        "trust slot:",
                        "3 denials on push-to-fork all overridden · add fork remote to boundary",
                    ),
                ],
                ..ImproveBlock::new("b22")
            }
            .into(),
            "brainstorm" => BrainstormIdea {
                number: 2,
                ..BrainstormIdea::new("b23", "event-sourced transcript")
            }
            .into(),
            other => panic!("unknown fixture name: {other}"),
        }
    }

    const GOLDEN_MARKERS: &[(&str, &[&str])] = &[
        (
            "session_banner",
            &["Amplifier 0.1.0", "core 1.6.0", "session a1b2c3"],
        ),
        ("user", &["❯", "[build]", "persistence boundary"]),
        ("narration", &["●", "durable session store"]),
        (
            "tool_collapsed",
            &["●", "Ran 2 shell commands", "· click to expand"],
        ),
        ("tool_expanded", &["●", "1214 passed", "build succeeded"]),
        ("tool_failed", &["●", "Test suite failed"]),
        ("live_command", &["└", "$ uv run pytest tests -q"]),
        (
            "plan",
            &["·", "Refactor session store", "✔", "■", "□", "↓ 83.9k tok"],
        ),
        ("plan_read_only", &["(read-only)"]),
        (
            "blocked",
            &["⊘", "git push --force", "continuing without push"],
        ),
        ("working", &["✳", "Coordinating 3 agents", "esc to interrupt"]),
        ("recap", &["✳", "Goal:", "Next:"]),
        ("answer", &["pytest", "done", "Second line."]),
        (
            "steer",
            &["↳", "steer queued:", "applies at next step boundary"],
        ),
        ("rule_shipped", &["tests ✔", "$0.17", "91% cached"]),
        ("rule_answer", &["· answer"]),
        ("evidence", &["Evidence", "1/2", "¹", "²", "→", "esc close"]),
        (
            "ledger",
            &["Session ledger", "a1b2c3", "$1.24", "cache hit 91%"],
        ),
        ("context", &["Context", "42% of 200k", "████████░░"]),
        (
            "needs_you",
            &["Needs you", "1 deferred decision", "[yes · push to fork]"],
        ),
        (
            "doctor",
            &["Doctor", "✔", "provider mounted", "1 bundle override unused"],
        ),
        (
            "improve",
            &["Improve", "allowlist:", "uv run pytest", "trust slot:"],
        ),
        ("brainstorm", &["2 event-sourced transcript"]),
    ];

    // Python: test_block_golden_markers_at_width (parametrized name × width)
    #[test]
    fn test_block_golden_markers_at_width() {
        for &(name, markers) in GOLDEN_MARKERS {
            for width in GOLDEN_WIDTHS {
                let rendered = lines_plain(&render_block(&blocks(name), width));
                let normalized = rendered.split_whitespace().collect::<Vec<_>>().join(" ");
                for marker in markers {
                    assert!(
                        normalized.contains(marker),
                        "({name}, {width}, {marker:?}, {rendered:?})"
                    );
                }
            }
        }
    }

    // -- exact spec strings (DESIGN-SPEC §3) ----------------------------------

    #[test]
    fn test_user_line_exact() {
        let lines = render_block(&blocks("user"), 80);
        assert_eq!(
            line_plain(&lines[0]),
            "❯ [build] Please verify the persistence boundary"
        );
        let (prompt, badge, text) = (&lines[0][0], &lines[0][1], &lines[0][2]);
        assert_eq!((prompt.style_token, prompt.bold), (StyleToken::Green, true));
        assert_eq!(badge.style_token, StyleToken::Green); // build mode badge is green
        assert_eq!(text.style_token, StyleToken::Bright);
    }

    #[test]
    fn test_user_line_mode_badge_colors() {
        let cases = [
            ("chat", StyleToken::Dim),
            ("plan", StyleToken::Blue),
            ("brainstorm", StyleToken::Teal),
            ("build", StyleToken::Green),
            ("auto", StyleToken::Orange),
            ("delegated", StyleToken::Teal), // focused-subagent brief badge (mockup §8)
        ];
        for (mode, token) in cases {
            let block = UserLine {
                mode: mode.to_string(),
                ..UserLine::new("x", "t")
            };
            let lines = render_block(&block.into(), 80);
            assert_eq!(lines[0][1].style_token, token, "{mode}");
        }
    }

    #[test]
    fn test_narration_exact() {
        let lines = render_block(&blocks("narration"), 80);
        assert_eq!(line_plain(&lines[0]), "● Checking the durable session store");
        assert_eq!(lines[0][0].style_token, StyleToken::Bright);
        assert_eq!(lines[0][1].style_token, StyleToken::Fg);
    }

    #[test]
    fn test_tool_line_collapsed_exact() {
        let lines = render_block(&blocks("tool_collapsed"), 80);
        assert_eq!(
            lines_plain(&lines),
            "  ● Ran 2 shell commands · click to expand"
        );
        assert_eq!(
            lines[0].last().expect("head segments").style_token,
            StyleToken::Dimmer
        );
    }

    #[test]
    fn test_expanded_change_line_uses_theme_aware_diff_styles() {
        let block = ToolLine {
            body: vec![
                "foundation:coder · edit file · src/app.py".to_string(),
                "--- src/app.py".to_string(),
                "+++ src/app.py".to_string(),
                "@@ replaced text @@".to_string(),
                "-old".to_string(),
                "+new".to_string(),
            ],
            expanded: true,
            status: ToolLineStatus::Completed,
            body_style: ToolLineBodyStyle::Diff,
            ..ToolLine::new("changes", "Changed 1 file")
        };
        let lines = render_block(&block.into(), 100);
        assert_eq!(line_plain(&lines[0]), "  ● Changed 1 file · click to expand");
        let body_tokens: Vec<StyleToken> = lines[2..].iter().map(|line| line[0].style_token).collect();
        assert_eq!(
            body_tokens,
            vec![
                StyleToken::Teal,
                StyleToken::Teal,
                StyleToken::Blue,
                StyleToken::Red,
                StyleToken::Green,
            ]
        );
        assert_eq!(lines[lines.len() - 2][0].bg_token, Some(StyleToken::BgTab));
        assert_eq!(lines[lines.len() - 1][0].bg_token, Some(StyleToken::BgTab));
        assert_eq!(TOOL_EXPAND_HINT, " · click to expand");
    }

    #[test]
    fn test_tool_line_expanded_shows_indented_body_and_keeps_hint() {
        // Mockup toolLine never mutates its head on toggle: the '· click to
        // expand' hint stays visible while the body is expanded.
        let lines = render_block(&blocks("tool_expanded"), 80);
        assert_eq!(line_plain(&lines[0]), "  ● Ran 2 shell commands · click to expand");
        assert_eq!(line_plain(&lines[1]), "      1214 passed");
        assert_eq!(line_plain(&lines[2]), "      build succeeded");
        assert!(lines[1]
            .iter()
            .all(|segment| segment.style_token == StyleToken::Dimmer));
    }

    #[test]
    fn test_tool_line_failed_is_red() {
        let lines = render_block(&blocks("tool_failed"), 80);
        assert_eq!(lines[0][0].style_token, StyleToken::Red);
    }

    #[test]
    fn test_live_command_exact() {
        let lines = render_block(&blocks("live_command"), 80);
        assert_eq!(line_plain(&lines[0]), "  └ $ uv run pytest tests -q");
        assert_eq!(lines[0][0].style_token, StyleToken::Dimmer);
        assert_eq!(lines[0][1].style_token, StyleToken::Dim);
    }

    #[test]
    fn test_plan_exact() {
        let lines = render_block(&blocks("plan"), 80);
        // One space between the title and the telemetry paren (mockup: the
        // title segment carries the trailing space).
        assert_eq!(
            line_plain(&lines[0]),
            format!("· Refactor session store {}", tel().suffix())
        );
        assert_eq!(lines[0][0].style_token, StyleToken::Orange);
        assert_eq!(line_plain(&lines[1]), "  ✔ Audit persistence paths");
        assert_eq!(lines[1][0].style_token, StyleToken::Green);
        assert_eq!(line_plain(&lines[2]), "  ■ Migrate durable history");
        // Mockup L331: plain orange prefix — only the step text is bright bold.
        assert_eq!(lines[2][0], seg("  ■ ", StyleToken::Orange));
        assert!(lines[2][1].bold);
        assert_eq!(lines[2][1].style_token, StyleToken::Bright);
        assert_eq!(line_plain(&lines[3]), "  □ Add reconciliation");
        assert_eq!(lines[3][0].style_token, StyleToken::Dimmer);
    }

    #[test]
    fn test_plan_read_only_suffix() {
        let lines = render_block(&blocks("plan_read_only"), 80);
        assert_eq!(line_plain(&lines[0]), "· Ship checklist (read-only)");
    }

    #[test]
    fn test_blocked_exact() {
        let lines = render_block(&blocks("blocked"), 80);
        assert_eq!(
            line_plain(&lines[0]),
            "  ⊘ blocked · git push --force origin main · denied by user · continuing without push"
        );
        assert_eq!(lines[0][0].style_token, StyleToken::Red);
        assert_eq!(
            lines[0].last().expect("segments").style_token,
            StyleToken::Dim
        );
    }

    #[test]
    fn test_working_status_exact_and_spinner_frames() {
        // Fan-out turn (mockup runAgentsTurn): 'Coordinating N agents · Ns ·
        // ↓ X.Xk tok · esc to interrupt' — integer secs, always one-decimal k.
        let lines = render_block(&blocks("working"), 80);
        assert_eq!(
            line_plain(&lines[0]),
            "✳ Coordinating 3 agents · 8s · ↓ 3.2k tok · esc to interrupt"
        );
        assert_eq!(lines[0][0].style_token, StyleToken::Orange);
        assert_eq!(
            lines[0].last().expect("segments").style_token,
            StyleToken::Dimmer
        );
        for (frame, glyph) in ["✳", "✦", "✧", "✦", "✳"].iter().enumerate() {
            let block = WorkingStatus {
                spinner_frame: frame as u32,
                ..working()
            };
            let lines = render_block(&block.into(), 80);
            assert_eq!(lines[0][0].text, format!("{glyph} "));
        }
    }

    #[test]
    fn test_working_label_has_a_chasing_highlight_without_changing_text() {
        let first = render_block(
            &WorkingStatus {
                motion_frame: 0,
                ..working()
            }
            .into(),
            80,
        );
        let second = render_block(
            &WorkingStatus {
                motion_frame: 1,
                ..working()
            }
            .into(),
            80,
        );
        assert_eq!(line_plain(&first[0]), line_plain(&second[0]));
        let bright = |line: &Line| -> Vec<String> {
            line.iter()
                .filter(|segment| segment.style_token == StyleToken::Bright)
                .map(|segment| segment.text.clone())
                .collect()
        };
        let first_bright = bright(&first[0]);
        let second_bright = bright(&second[0]);
        assert!(!first_bright.is_empty());
        assert!(!second_bright.is_empty());
        assert_ne!(first_bright, second_bright);
    }

    #[test]
    fn test_working_label_shimmer_is_a_soft_multi_cell_band() {
        let lines = render_block(
            &WorkingStatus {
                motion_frame: 2,
                ..working()
            }
            .into(),
            80,
        );
        let line = &lines[0];
        let label = &line[1..line.len() - 2];
        let bright: Vec<&Segment> = label
            .iter()
            .filter(|segment| segment.style_token == StyleToken::Bright)
            .collect();
        let bright_text: String = bright.iter().map(|segment| segment.text.as_str()).collect();
        assert!(bright_text.chars().count() >= 3);
        assert!(bright.iter().any(|segment| segment.bold));
        assert!(bright.iter().any(|segment| !segment.bold));
        assert!(label
            .iter()
            .any(|segment| segment.style_token == StyleToken::Fg));
    }

    #[test]
    fn test_working_status_single_agent_exact() {
        // Single-agent turns with no activity yet show '· thinking ·'
        // (deliberate divergence from the mockup/Python '1 agent', which
        // misreads as a spawned subagent).
        let block = WorkingStatus {
            agent_count: 1,
            ..working()
        };
        let lines = render_block(&block.clone().into(), 80);
        assert_eq!(
            line_plain(&lines[0]),
            "✳ working · 8s · ↓ 3.2k tok · thinking · esc to interrupt · type to steer"
        );
        let zero = WorkingStatus {
            agent_count: 0,
            ..block.clone()
        };
        assert_eq!(
            render_block(&zero.into(), 80),
            render_block(&block.into(), 80)
        );
    }

    #[test]
    fn test_recap_exact_italic_dim() {
        let lines = render_block(&blocks("recap"), 80);
        assert_eq!(
            line_plain(&lines[0]),
            "✳ Goal: durable chat history. Next: resume migration."
        );
        assert_eq!(lines[0][0].style_token, StyleToken::Dimmer);
        assert!(lines[0][1].italic);
        assert_eq!(lines[0][1].style_token, StyleToken::Dim);
    }

    #[test]
    fn test_steer_echo_exact() {
        let lines = render_block(&blocks("steer"), 80);
        assert_eq!(
            line_plain(&lines[0]),
            "  ↳ steer queued: \"focus on the tests\" · applies at next step boundary"
        );
        assert_eq!(lines[0][0].style_token, StyleToken::Teal);
        assert_eq!(
            lines[0].last().expect("segments").style_token,
            StyleToken::Dimmer
        );
    }

    #[test]
    fn test_turn_rule_fills_width_exactly() {
        for width in GOLDEN_WIDTHS {
            for (block, label) in [
                (rule_shipped(), rule_shipped().label),
                (rule_answer(), rule_answer().label),
            ] {
                let lines = render_block(&block.into(), width);
                if lines.len() == 1 {
                    assert_eq!(cell_len(&line_plain(&lines[0])), width);
                    assert!(line_plain(&lines[0]).ends_with(&label));
                } else {
                    // narrow fallback: full rule line + right-aligned label line
                    assert_eq!(line_plain(&lines[0]), "─".repeat(width));
                    assert!(line_plain(&lines[1]).ends_with(&label));
                }
            }
        }
    }

    #[test]
    fn test_turn_rule_label_dim_when_shipped_dimmer_otherwise() {
        let shipped = render_block(&blocks("rule_shipped"), 200);
        let answer = render_block(&blocks("rule_answer"), 200);
        assert_eq!(
            shipped[0].last().expect("segments").style_token,
            StyleToken::Dim
        );
        assert_eq!(
            answer[0].last().expect("segments").style_token,
            StyleToken::Dimmer
        );
        assert_eq!(shipped[0][0].style_token, StyleToken::Rule);
    }

    #[test]
    fn test_evidence_exact() {
        let lines = render_block(&blocks("evidence"), 80);
        assert_eq!(
            line_plain(&lines[0]),
            "· Evidence  1/2 · ←/→ select · enter expand · esc close"
        );
        // Header counter + hints are ONE dimmer run (mockup showEvidence).
        assert_eq!(
            lines[0].last().expect("segments").style_token,
            StyleToken::Dimmer
        );
        assert_eq!(
            line_plain(&lines[1]),
            "  ¹ \"all tests pass\" → pytest run · 34 passed"
        );
        assert_eq!(line_plain(&lines[2]), "  ² \"3 files changed\" → git diff --stat");
        // No background highlight on claims (mockup renders them plain).
        assert!(lines
            .iter()
            .all(|line| line.iter().all(|segment| segment.bg_token.is_none())));
    }

    #[test]
    fn test_ledger_exact() {
        let lines = render_block(&blocks("ledger"), 80);
        assert_eq!(line_plain(&lines[0]), "· Session ledger  a1b2c3 · dev-bundle");
        // Header after the blue '· ' is one plain fg run; stats line is dim.
        assert_eq!(lines[0][1].style_token, StyleToken::Fg);
        assert!(!lines[0][1].bold);
        assert_eq!(
            line_plain(&lines[1]),
            "  3 turns · $1.24 · 2 shipped · 1 answer-only · cache hit 91%"
        );
        assert_eq!(lines[1][0].style_token, StyleToken::Dim);
    }

    #[test]
    fn test_context_exact_bar() {
        let lines = render_block(&blocks("context"), 80);
        assert_eq!(line_plain(&lines[0]), "· Context  42% of 200k");
        assert_eq!(lines[0][1].style_token, StyleToken::Fg);
        assert!(!lines[0][1].bold);
        // ONE dim line combining bar + legend (mockup cmdContext).
        assert_eq!(lines.len(), 2);
        assert_eq!(
            line_plain(&lines[1]),
            "  ████████░░  conversation · tools · memory · free"
        );
        assert!(lines[1]
            .iter()
            .all(|segment| segment.style_token == StyleToken::Dim));
    }

    #[test]
    fn test_needs_you_exact_chip_styling() {
        let lines = render_block(&blocks("needs_you"), 80);
        // Header is one plain orange run, count never pluralized (mockup).
        assert_eq!(line_plain(&lines[0]), "· Needs you  1 deferred decision");
        assert_eq!(lines[0][1].style_token, StyleToken::Orange);
        assert!(!lines[0][1].bold);
        // Row number: '  1 ' orange, no period; two spaces before the chip.
        assert_eq!(lines[1][0], seg("  1 ", StyleToken::Orange));
        assert_eq!(
            line_plain(&lines[1]),
            "  1 push branch to fork? · net access denied  [yes · push to fork]"
        );
        let chip = lines[1].last().expect("segments");
        assert_eq!(chip.text, "[yes · push to fork]");
        assert_eq!(chip.style_token, StyleToken::Green);
        assert_eq!(chip.bg_token, Some(StyleToken::BgTab));
    }

    #[test]
    fn test_needs_you_highlight_renders_teal() {
        let block = NeedsYouBlock::new(
            "x",
            vec![NeedsYouEntry {
                highlight: "mj/waypoint".to_string(),
                ..NeedsYouEntry::new("d1", "Push to fork mj/waypoint instead?")
            }],
        );
        let lines = render_block(&block.into(), 80);
        let row = &lines[1];
        assert_eq!(line_plain(row), "  1 Push to fork mj/waypoint instead?");
        let accent = &row[2];
        assert_eq!(accent.text, "mj/waypoint");
        assert_eq!(accent.style_token, StyleToken::Teal);
    }

    #[test]
    fn test_doctor_exact() {
        let lines = render_block(&blocks("doctor"), 80);
        assert_eq!(line_plain(&lines[0]), "· Doctor  1 finding · nothing changed yet");
        assert_eq!(lines[0][0].style_token, StyleToken::Blue);
        assert_eq!(lines[0][1].style_token, StyleToken::Fg);
        assert_eq!(line_plain(&lines[1]), "  ✔ provider mounted");
        assert_eq!(lines[1][0].style_token, StyleToken::Green);
        // Finding rows: orange number (no period) + dim text.
        assert_eq!(line_plain(&lines[3]), "  1 bundle override unused");
        assert_eq!(lines[3][0].style_token, StyleToken::Orange);
        assert_eq!(lines[3][1].style_token, StyleToken::Dim);
    }

    #[test]
    fn test_improve_exact() {
        let lines = render_block(&blocks("improve"), 80);
        assert_eq!(
            line_plain(&lines[0]),
            "· Improve  from ledger + denial log · proposes, never applies silently"
        );
        assert_eq!(lines[0][1].style_token, StyleToken::Fg);
        // Allowlist row: dim '  1 allowlist: ' + green action + dim tail.
        assert_eq!(
            line_plain(&lines[1]),
            "  1 allowlist: uv run pytest approved 22/22 times · add to auto"
        );
        assert_eq!(lines[1][1], seg("uv run pytest", StyleToken::Green));
        // Trust-slot row: one dim run, the action named exactly once.
        assert_eq!(
            line_plain(&lines[2]),
            "  2 trust slot: 3 denials on push-to-fork all overridden · add fork remote to boundary"
        );
        assert!(lines[2]
            .iter()
            .all(|segment| segment.style_token == StyleToken::Dim));
    }

    #[test]
    fn test_answer_splits_newlines_and_keeps_span_styles() {
        let lines = render_block(&blocks("answer"), 80);
        assert_eq!(lines.len(), 2);
        assert_eq!(line_plain(&lines[0]), "Run pytest — it is done.");
        assert_eq!(line_plain(&lines[1]), "Second line.");
        let code = &lines[0][1];
        assert_eq!(code.style_token, StyleToken::Teal);
        assert_eq!(code.text, "pytest");
        let emphasis = &lines[0][3];
        assert_eq!(emphasis.style_token, StyleToken::Bright);
        assert!(emphasis.bold);
    }

    /// A long callout blockquote wraps like body text: gutter on the first
    /// line, continuations hang under the quoted text (2 cells), everything
    /// within width — never a verbatim overflow line.
    ///
    /// Input pinned from the Python oracle:
    /// `answer_spans("> ★ **Insight:** " + " ".join(["insight"] * 12))`.
    #[test]
    fn test_answer_blockquote_wraps_under_the_gutter() {
        let spans = vec![
            seg("▌ ", StyleToken::Blue),
            seg("★ ", StyleToken::Fg),
            Segment {
                bold: true,
                ..seg("Insight:", StyleToken::Bright)
            },
            seg(
                " insight insight insight insight insight insight insight insight insight insight insight insight",
                StyleToken::Fg,
            ),
        ];
        let block = Answer::new("a-quote", spans);
        let lines = render_block(&block.into(), 40);
        let plains: Vec<String> = lines.iter().map(|line| line_plain(line)).collect();
        assert!(plains.len() > 1);
        assert!(plains[0].starts_with("▌ ★ Insight:"));
        assert_eq!(lines[0][0], seg("▌ ", StyleToken::Blue));
        for continuation in &plains[1..] {
            assert!(continuation.starts_with("  "));
            assert!(!continuation.starts_with("   "));
        }
        assert!(plains.iter().all(|plain| cell_len(plain) <= 40));
        // Extra pin: the exact wrapped lines from the Python oracle.
        assert_eq!(
            plains,
            vec![
                "▌ ★ Insight: insight insight insight",
                "  insight insight insight insight",
                "  insight insight insight insight",
                "  insight",
            ]
        );
    }

    #[test]
    fn test_session_banner_focus_note_replaces_headline() {
        let banner = SessionBanner {
            focus_note: "focused: test-writer · subagent of a1b2c3 · own context window \
                         · results report back to parent · esc back"
                .to_string(),
            ..SessionBanner::new("x", "Amplifier 0.1.0")
        };
        let lines = render_block(&banner.into(), 80);
        assert_eq!(lines.len(), 1);
        assert!(line_plain(&lines[0]).starts_with("focused: test-writer · subagent of"));
        // 'focused: <name> ' bright bold, the remainder dim (mockup focusLane).
        assert_eq!(
            lines[0][0],
            Segment {
                bold: true,
                ..seg("focused: test-writer ", StyleToken::Bright)
            }
        );
        assert_eq!(lines[0][1].style_token, StyleToken::Dim);
    }

    // -- segments: markup bridge ----------------------------------------------

    // Python: test_markup_uses_theme_variables_and_escapes_brackets.
    // Textual's Content.from_markup round-trip is replaced by the exact markup
    // string pinned from the Python oracle (same segments, same bytes).
    #[test]
    fn test_markup_uses_theme_variables_and_escapes_brackets() {
        let markup = render_block_markup(&blocks("user"), 80);
        assert!(markup.contains("[bold $green]"));
        assert!(!markup.contains('#')); // never a color value
        // The literal "[build]" badge is escaped, not parsed as markup.
        assert_eq!(
            markup,
            "[bold $green]❯ [/][$green]\\[build] [/][$bright]Please verify the persistence boundary[/]"
        );
    }

    // -- answer markdown render path (inputs pinned from answer_spans) --------

    /// Numbered items render a dim `N. ` marker; wrapped continuation
    /// lines hang-indent under the body (3 cells for `1. `).
    #[test]
    fn test_numbered_list_marker_and_hanging_indent() {
        // Oracle: answer_spans("1. Configure the provider, …") ==
        //   [("1. ", dim), ("Configure … operator.", fg)]
        let spans = vec![
            seg("1. ", StyleToken::Dim),
            seg(
                "Configure the provider, load the bundle, and render the terminal UI cleanly for the operator.",
                StyleToken::Fg,
            ),
        ];
        assert_eq!(spans[0].text, "1. ");
        assert_eq!(spans[0].style_token, StyleToken::Dim);
        let block = Answer::new("a1", spans);
        let plains: Vec<String> = render_block(&block.into(), 40)
            .iter()
            .map(|line| line_plain(line))
            .collect();
        assert!(plains[0].starts_with("1. "));
        assert!(plains.len() > 1); // wrapped at width 40
        assert!(plains[1].starts_with("   ")); // 3-cell hanging indent
        assert_ne!(plains[1].chars().nth(3), Some(' ')); // continuation body, no fabricated padding
    }

    #[test]
    fn test_bullet_hanging_indent_when_wrapped() {
        // Oracle: answer_spans("- Configure the provider, …") ==
        //   [("• ", dim), ("Configure … operator.", fg)]
        let spans = vec![
            seg("• ", StyleToken::Dim),
            seg(
                "Configure the provider, load the bundle, and render the terminal UI cleanly for the operator.",
                StyleToken::Fg,
            ),
        ];
        let block = Answer::new("a2", spans);
        let plains: Vec<String> = render_block(&block.into(), 40)
            .iter()
            .map(|line| line_plain(line))
            .collect();
        assert!(plains[0].starts_with("• "));
        assert!(plains.len() > 1);
        assert!(plains[1].starts_with("  ")); // 2-cell hang for "• "
        assert_ne!(plains[1].chars().nth(2), Some(' '));
    }

    // -- rendering polish (issue #34) ------------------------------------------

    /// Prose word-wraps at min(width, READING_MEASURE): a wide terminal
    /// never stretches a paragraph past the reading measure, but a narrow one
    /// still wraps at its own width.
    #[test]
    fn test_render_answer_caps_prose_at_reading_measure() {
        // Oracle: answer_spans("word " * 60) == one fg segment of the source.
        let block = Answer::new("a", vec![Segment::new("word ".repeat(60))]);
        let union: TranscriptBlock = block.into();

        let wide = render_block(&union, 200);
        let widest = wide
            .iter()
            .filter(|line| !line.is_empty())
            .map(|line| cell_len(&line_plain(line)))
            .max()
            .expect("non-empty render");
        assert!(widest <= READING_MEASURE);
        assert!(wide.len() > 1); // the cap actually forced a wrap

        let narrow = render_block(&union, 60);
        let narrow_widest = narrow
            .iter()
            .filter(|line| !line.is_empty())
            .map(|line| cell_len(&line_plain(line)))
            .max()
            .expect("non-empty render");
        assert!(narrow_widest <= 60);
    }

    /// Code fences and table rows are emitted verbatim — never re-wrapped —
    /// so a long line survives past the reading measure (alignment intact).
    #[test]
    fn test_render_answer_code_and_tables_keep_full_width() {
        let long_code = "x".repeat(130);
        // Oracle: answer_spans("```\n<x*130>\n```") == one teal "  <x*130>" segment.
        let block = Answer::new("a", vec![seg(format!("  {long_code}"), StyleToken::Teal)]);
        let lines = render_block(&block.into(), 200);
        assert!(lines
            .iter()
            .any(|line| line_plain(line) == format!("  {long_code}")));
    }

    /// A click on any fence row yields the whole fence, dedented, markers
    /// dropped; non-fence rows and out-of-range indices yield None.
    #[test]
    fn test_fence_text_at_row_extracts_dedented_fence() {
        // Oracle: answer_spans("Intro line.\n\n```python\nprint('hi')\nx = 1\n```\n\nOutro.")
        let spans = vec![
            Segment::new("Intro line."),
            Segment::new("\n"),
            Segment::new("\n"),
            seg("  print('hi')", StyleToken::Teal),
            Segment::new("\n"),
            seg("  x = 1", StyleToken::Teal),
            Segment::new("\n"),
            Segment::new("\n"),
            Segment::new("\n"),
            Segment::new("Outro."),
        ];
        let lines = render_block(&Answer::new("a", spans).into(), 80);
        let fenced: Vec<Option<String>> = (0..lines.len())
            .map(|i| fence_text_at_row(&lines, i as isize))
            .collect();
        assert!(fenced.contains(&Some("print('hi')\nx = 1".to_string())));
        assert_eq!(fence_text_at_row(&lines, 0), None); // the intro prose line
        assert_eq!(fence_text_at_row(&lines, -1), None);
        assert_eq!(fence_text_at_row(&lines, lines.len() as isize), None);
    }

    // -- thinking renderer (tests/test_ui_render_thinking.py) ------------------

    fn thinking_plain(block: Thinking, width: usize) -> Vec<String> {
        render_block(&block.into(), width)
            .iter()
            .map(|line| line_plain(line))
            .collect()
    }

    #[test]
    fn test_collapsed_single_line_exact() {
        let block = Thinking {
            text: "one line only".to_string(),
            ..Thinking::new("b1")
        };
        assert_eq!(
            thinking_plain(block, 97),
            vec![format!(
                "{GLYPH_CHEVRON_COLLAPSED} thinking · 1 line · ctrl-g/click to expand"
            )]
        );
    }

    #[test]
    fn test_collapsed_pluralizes_line_count() {
        let block = Thinking {
            text: "first\nsecond\nthird".to_string(),
            ..Thinking::new("b1")
        };
        assert_eq!(
            thinking_plain(block, 97),
            vec![format!(
                "{GLYPH_CHEVRON_COLLAPSED} thinking · 3 lines · ctrl-g/click to expand"
            )]
        );
    }

    #[test]
    fn test_expanded_shows_prose_under_header() {
        let block = Thinking {
            text: "weigh options\npick the safe one".to_string(),
            expanded: true,
            ..Thinking::new("b1")
        };
        let lines = thinking_plain(block, 97);
        assert_eq!(lines[0], format!("{GLYPH_CHEVRON_EXPANDED} thinking"));
        assert_eq!(lines[1], "  weigh options");
        assert_eq!(lines[2], "  pick the safe one");
        assert_eq!(lines.len(), 3);
    }

    #[test]
    fn test_expanded_body_is_dim_italic() {
        let block = Thinking {
            text: "reasoning".to_string(),
            expanded: true,
            ..Thinking::new("b1")
        };
        let lines = render_block(&block.into(), 97);
        let body = &lines[1];
        assert_eq!(body[0].style_token, StyleToken::Dim);
        assert!(body[0].italic);
    }

    /// Empty text (core withheld the prose) → one honest line, no crash.
    #[test]
    fn test_withheld_thinking_degrades_honestly() {
        let block = Thinking::new("b1");
        assert_eq!(
            thinking_plain(block, 97),
            vec!["· thinking · (content withheld by provider)"]
        );
    }

    /// An expanded withheld block still renders the single withheld line.
    #[test]
    fn test_withheld_thinking_ignores_expanded_flag() {
        let block = Thinking {
            expanded: true,
            ..Thinking::new("b1")
        };
        assert_eq!(
            thinking_plain(block, 97),
            vec!["· thinking · (content withheld by provider)"]
        );
    }

    /// The discriminated union serializes/deserializes losslessly (replay).
    #[test]
    fn test_block_round_trips_through_json() {
        let block: TranscriptBlock = Thinking {
            text: "a\nb".to_string(),
            expanded: true,
            ..Thinking::new("b7")
        }
        .into();
        let json = serde_json::to_string(&block).expect("thinking serializes");
        let restored: TranscriptBlock = serde_json::from_str(&json).expect("thinking deserializes");
        assert_eq!(restored, block);
    }

    // -- delegate summary (tests/test_ui_render_delegate_summary.py) ----------

    fn delegate_plain(block: DelegateSummaryBlock, width: usize) -> Vec<String> {
        render_block(&block.into(), width)
            .iter()
            .map(|line| line_plain(line))
            .collect()
    }

    fn done_entry(agent: &str, elapsed_s: f64, snippet: &str) -> DelegateEntry {
        DelegateEntry {
            state: DelegateState::Done,
            elapsed_s,
            snippet: snippet.to_string(),
            ..DelegateEntry::new(agent)
        }
    }

    fn done_entries() -> Vec<DelegateEntry> {
        vec![
            done_entry("researcher", 4.4, "3 findings"),
            done_entry("coder", 6.0, "2 files"),
            done_entry("tester", 2.6, "tests ✔"),
        ]
    }

    fn plan_items() -> Vec<TodoItem> {
        ["scan provider docs", "migrate session store", "run store tests", "synthesize findings"]
            .iter()
            .map(|content| TodoItem {
                status: TodoStatus::Completed,
                ..TodoItem::new(*content)
            })
            .collect()
    }

    #[test]
    fn test_running_header_is_single_line_no_chevron() {
        let block = DelegateSummaryBlock {
            entries: vec![
                DelegateEntry::new("researcher"),
                DelegateEntry::new("coder"),
                DelegateEntry::new("tester"),
            ],
            ..DelegateSummaryBlock::new("b1")
        };
        assert_eq!(delegate_plain(block, 97), vec!["● 3 delegates running…"]);
    }

    #[test]
    fn test_single_running_delegate_is_singular() {
        let block = DelegateSummaryBlock {
            entries: vec![DelegateEntry::new("coder")],
            ..DelegateSummaryBlock::new("b1")
        };
        assert_eq!(delegate_plain(block, 97), vec!["● 1 delegate running…"]);
    }

    #[test]
    fn test_collapsed_final_header_exact() {
        let block = DelegateSummaryBlock {
            entries: done_entries(),
            plan_final: Some(plan_items()),
            duration_s: 102.0,
            ..DelegateSummaryBlock::new("b1")
        };
        assert_eq!(
            delegate_plain(block, 97),
            vec!["● Used 3 delegates · Plan 4/4 · 1m 42s ▸"]
        );
    }

    #[test]
    fn test_collapsed_header_omits_plan_when_none() {
        let block = DelegateSummaryBlock {
            entries: done_entries(),
            duration_s: 42.0,
            ..DelegateSummaryBlock::new("b1")
        };
        assert_eq!(delegate_plain(block, 97), vec!["● Used 3 delegates · 42s ▸"]);
    }

    #[test]
    fn test_expanded_rows_and_plan_line() {
        let block = DelegateSummaryBlock {
            entries: done_entries(),
            plan_final: Some(plan_items()),
            duration_s: 102.0,
            expanded: true,
            ..DelegateSummaryBlock::new("b1")
        };
        let lines = delegate_plain(block, 97);
        assert_eq!(lines[0], "● Used 3 delegates · Plan 4/4 · 1m 42s ▾");
        assert!(lines[1].starts_with("    ├─ ✔ researcher"));
        assert!(lines[1].contains("4s · \"3 findings\""));
        assert!(lines[3].starts_with("    └─ ✔ tester")); // last row gets the corner glyph
        assert!(lines[4].starts_with("    Plan  "));
        assert!(lines[4].contains("✔ scan provider docs"));
        assert_eq!(lines.len(), 5);
    }

    #[test]
    fn test_error_and_cancelled_glyphs() {
        let block = DelegateSummaryBlock {
            entries: vec![
                DelegateEntry {
                    state: DelegateState::Error,
                    elapsed_s: 3.0,
                    snippet: "failed".to_string(),
                    ..DelegateEntry::new("coder")
                },
                DelegateEntry {
                    state: DelegateState::Cancelled,
                    elapsed_s: 1.0,
                    ..DelegateEntry::new("tester")
                },
            ],
            duration_s: 3.0,
            expanded: true,
            ..DelegateSummaryBlock::new("b1")
        };
        let lines = delegate_plain(block, 97);
        assert!(lines[1].contains("✖ coder"));
        assert!(lines[2].contains("⊘ tester"));
    }

    #[test]
    fn test_expanded_running_row_shows_running() {
        let block = DelegateSummaryBlock {
            entries: vec![DelegateEntry::new("coder")],
            expanded: true,
            ..DelegateSummaryBlock::new("b1")
        };
        let lines = delegate_plain(block, 97);
        assert_eq!(lines[0], "● 1 delegate running…");
        assert_eq!(lines[1], "    └─ ◐ coder  running");
    }

    #[test]
    fn test_snippet_truncated_to_width() {
        let long = DelegateEntry {
            state: DelegateState::Done,
            elapsed_s: 1.0,
            snippet: "x".repeat(200),
            ..DelegateEntry::new("a")
        };
        let block = DelegateSummaryBlock {
            entries: vec![long],
            duration_s: 1.0,
            expanded: true,
            ..DelegateSummaryBlock::new("b1")
        };
        let row = delegate_plain(block, 40)[1].clone();
        assert!(row.chars().count() <= 40);
        assert!(row.ends_with("…\""));
    }

    /// Review finding: the plan fold was one uncapped line — real plans
    /// with long items wrapped mid-word into an unaligned blob.
    #[test]
    fn test_expanded_plan_row_clips_to_width() {
        let block = DelegateSummaryBlock {
            entries: done_entries(),
            plan_final: Some(plan_items()),
            duration_s: 102.0,
            expanded: true,
            ..DelegateSummaryBlock::new("b1")
        };
        let plan_line = delegate_plain(block, 40).last().expect("plan line").clone();
        assert!(plan_line.chars().count() <= 40);
        assert!(plan_line.starts_with("    Plan  "));
        assert!(plan_line.trim_end().ends_with('…')); // clipped, visibly
    }

    #[test]
    fn test_expanded_plan_row_marks_whole_dropped_items() {
        let block = DelegateSummaryBlock {
            entries: done_entries(),
            plan_final: Some(plan_items()),
            duration_s: 102.0,
            expanded: true,
            ..DelegateSummaryBlock::new("b1")
        };
        let plan_line = delegate_plain(block, 35).last().expect("plan line").clone();
        assert!(plan_line.chars().count() <= 35);
        assert!(plan_line.ends_with(" …")); // later items dropped whole, marked
    }
}
