//! The mutable streaming tail — region two of the two-region transcript.
//!
//! Port of `src/amplifier_app_newtui/ui/live_tail.py`.
//!
//! ADR-0007: durable history is pure and immutable; THIS state machine is the
//! single mutable region. It accumulates raw text deltas (Channel A
//! `llm:stream_block_delta`), repaints at a throttled 30–60Hz, and on
//! `llm:stream_block_end` consolidates the accumulated source into one
//! durable [`Answer`] block the app appends to the transcript.
//!
//! Table holdback (RESEARCH-BRIEF risk 1): a markdown table cannot be laid
//! out until all rows are known, so a *trailing* table run is withheld from
//! the painted tail until either a paragraph break completes it or the
//! stream ends — the consolidated Answer always carries the full source.
//!
//! Ratatui adaptation: the pure logic — the markdown span pipeline
//! ([`answer_spans`] / [`streaming_spans`] / [`visible_length`] /
//! [`lane_tail_markup`]) and the [`LiveTail`] throttle/holdback/reveal state
//! machine — ports; Textual widget mechanics do not:
//!
//! - `Static.update()` becomes the stored [`LiveTail::painted`] markup string
//!   (the exact bytes Python passed to `update()`); the app-assembly layer
//!   renders it (via [`crate::ui::segments::to_ratatui_line`] once parsed).
//! - `set_timer` becomes an injected clock: [`LiveTail::feed`] takes `now`
//!   (monotonic seconds) and returns the trailing-paint delay to schedule;
//!   the app calls [`LiveTail::fire_timer`] when it elapses.
//! - `post_message(Consolidated)` does not port: [`LiveTail::consolidate`]
//!   returns the durable Answer and the caller appends it.
//! - The async off-thread render path (`run_worker` + `asyncio.to_thread`
//!   above [`ASYNC_RENDER_THRESHOLD`]) does not port — rendering here is
//!   synchronous; app assembly must offload long parses if the paint stalls.
//! - `on_click` does not port; the app maps a click on the tail to
//!   [`LiveTail::toggle_reveal`].

use std::sync::OnceLock;

use regex::Regex;

use crate::model::blocks::{
    Answer, Segment, StyleToken, GLYPH_CHECKBOX_CHECKED, GLYPH_CHECKBOX_EMPTY, GLYPH_QUOTE_GUTTER,
};
use crate::model::evidence::EvidenceLink;

use super::segments::segment_markup;

/// Minimum interval between tail repaints (30Hz — inside the 30–60Hz budget).
pub const THROTTLE_SECONDS: f64 = 1.0 / 30.0;

/// Max painted lines of a focused lane's live tail (design doc D4).
pub const LANE_TAIL_LINES: usize = 3;

/// Max painted lines of the revealed root stream (thinking/response preview).
///
/// The live box is a *peek*, not the transcript: it shows the last few lines
/// of the in-flight block. The durable, full-length text arrives on the
/// consolidated Answer (Channel B) — never truncated here.
pub const MAX_ROOT_LINES: usize = 6;

/// Chord advertised in the collapsed-stream hint (mirrors the keymap).
pub const REVEAL_HINT_KEY: &str = "ctrl-g";

/// Long streams parse off-thread so markdown can never stall the UI loop.
///
/// (Python offloads via `asyncio.to_thread` past this length; the Rust state
/// machine renders synchronously — the threshold is exported so the
/// app-assembly layer can make the same offload decision.)
pub const ASYNC_RENDER_THRESHOLD: usize = 100_000;

/// Padded-grid tables wider than this fall back to a definition list —
/// wrapped cells destroy column alignment (user screenshot, /about run).
const TABLE_GRID_MAX_WIDTH: usize = 96;

fn answer_span_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        // Python's italic alternative uses lookarounds
        // (`\*(?![*\s])[^*\n]+?(?<![*\s])\*`); the `regex` crate has none, so
        // it is rewritten as "one non-space/non-star char, or such a char on
        // each end of any non-star run" — the same language.
        Regex::new(concat!(
            r"(?s:\*\*.+?\*\*)", // **bold**
            r"|\*(?:[^*\s]|[^*\s][^*\n]*?[^*\s])\*", // *italic* — no space adjacent to a marker, never **
            r"|`[^`\n]+`",       // `inline code`
            r"|\[[^\]\n]+\]\((?:https?|file)://[^)\s]+\)", // [text](url)
            r"|(?:https?|file)://[^\s)]*[^\s).,;:!?]", // bare url (trailing sentence punctuation excluded)
        ))
        .expect("static regex compiles")
    })
}

fn link_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| {
        Regex::new(r"^\[([^\]\n]+)\]\(((?:https?|file)://[^)\s]+)\)$").expect("static regex compiles")
    })
}

fn heading_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"^(#{1,6})\s+(.*)$").expect("static regex compiles"))
}

fn bullet_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"^(\s*)[-*+]\s+(.*)$").expect("static regex compiles"))
}

/// A GitHub task-list marker at the head of a bullet body: `[x] ` / `[ ] `.
fn checkbox_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"^\[([ xX])\]\s+").expect("static regex compiles"))
}

fn numbered_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"^(\s*)(\d+)[.)]\s+(.*)$").expect("static regex compiles"))
}

fn table_sep_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"^\s*\|?[\s:\-|]+\|?\s*$").expect("static regex compiles"))
}

/// Markdown blockquote line. The insight/machete callouts that the
/// hooks-inline-blocks module (occams-machete bundle) teaches the model to
/// emit are blockquotes — `> ★ **Insight:** …` / `> ✂ **MJ:** …` —
/// because the line-mode CLI's Rich renderer frames them with a colored
/// `▌` gutter. This parser is that frame's TUI-native equivalent.
fn quote_re() -> &'static Regex {
    static RE: OnceLock<Regex> = OnceLock::new();
    RE.get_or_init(|| Regex::new(r"^\s*>\s?(.*)$").expect("static regex compiles"))
}

/// Escape text so it won't be interpreted as Textual content markup.
///
/// Faithful port of `textual.markup.escape` (mirrors the private helper in
/// [`crate::ui::segments`] — Python imports it from Textual directly).
fn escape(markup: &str) -> String {
    static ESCAPE_RE: OnceLock<Regex> = OnceLock::new();
    let re = ESCAPE_RE
        .get_or_init(|| Regex::new(r"(\\*)(\[[a-z#/@][^\[]*?\])").expect("static regex compiles"));
    let escaped = re.replace_all(markup, |caps: &regex::Captures<'_>| {
        format!("{0}{0}\\{1}", &caps[1], &caps[2])
    });
    let mut escaped = escaped.into_owned();
    if escaped.ends_with('\\') && !escaped.ends_with("\\\\") {
        escaped.push('\\');
    }
    escaped
}

fn seg_token(text: &str, token: StyleToken) -> Segment {
    Segment {
        style_token: token,
        ..Segment::new(text)
    }
}

/// Inline emphasis: `**…**` bright bold, `*…*` italic, `` `…` `` teal code,
/// `[text](url)` teal text + dimmer url, bare `https://` URLs teal — links
/// carry an OSC 8 target so they click through — and everything else fg (§3).
fn inline(text: &str) -> Vec<Segment> {
    let mut spans: Vec<Segment> = Vec::new();
    let mut position = 0;
    for m in answer_span_re().find_iter(text) {
        if m.start() > position {
            spans.push(Segment::new(&text[position..m.start()]));
        }
        let token = m.as_str();
        if let Some(inner) = token.strip_prefix("**").and_then(|t| t.strip_suffix("**")) {
            spans.push(Segment {
                bold: true,
                ..seg_token(inner, StyleToken::Bright)
            });
        } else if token.starts_with('*') {
            spans.push(Segment {
                italic: true,
                ..Segment::new(&token[1..token.len() - 1])
            });
        } else if token.starts_with('`') {
            spans.push(seg_token(&token[1..token.len() - 1], StyleToken::Teal));
        } else if token.starts_with("http://")
            || token.starts_with("https://")
            || token.starts_with("file://")
        {
            // Bare URL: collapse the raw text into one real hyperlink.
            spans.push(Segment {
                link: Some(token.to_string()),
                ..seg_token(token, StyleToken::Teal)
            });
        } else {
            let link = link_re()
                .captures(token)
                .expect("the alternation guarantees the shape");
            let url = link.get(2).expect("group 2 exists").as_str();
            spans.push(Segment {
                link: Some(url.to_string()),
                ..seg_token(link.get(1).expect("group 1 exists").as_str(), StyleToken::Teal)
            });
            spans.push(Segment {
                link: Some(url.to_string()),
                ..seg_token(&format!(" ({url})"), StyleToken::Dimmer)
            });
        }
        position = m.end();
    }
    if position < text.len() {
        spans.push(Segment::new(&text[position..]));
    }
    spans
}

/// Visible cell width of *text* once inline markers are stripped.
fn plain_len(text: &str) -> usize {
    inline(text)
        .iter()
        .map(|segment| segment.text.chars().count())
        .sum()
}

fn table_cells(line: &str) -> Vec<String> {
    line.trim()
        .trim_matches('|')
        .split('|')
        .map(|cell| cell.trim().to_string())
        .collect()
}

/// Render a pipe table (header · rule · rows) with aligned columns.
///
/// Raw `| a | b |` lines read terribly in the transcript (user report);
/// columns are padded to their widest cell, the `|---|` separator becomes a
/// dim rule, and the header row renders bright. Returns the index of the
/// first line after the table.
fn emit_table(spans: &mut Vec<Segment>, lines: &[&str], start: usize) -> usize {
    let mut end = start;
    while end < lines.len() && lines[end].trim_start().starts_with('|') {
        end += 1;
    }
    let rows: Vec<Vec<String>> = (start..end).map(|i| table_cells(lines[i])).collect();
    let body: Vec<&Vec<String>> = rows
        .iter()
        .enumerate()
        .filter(|(i, _)| !table_sep_re().is_match(lines[start + i]))
        .map(|(_, row)| row)
        .collect();
    let columns = body.iter().map(|row| row.len()).max().unwrap_or(0);
    let widths: Vec<usize> = (0..columns)
        .map(|col| {
            body.iter()
                .filter(|row| col < row.len())
                .map(|row| plain_len(&row[col]))
                .max()
                .unwrap_or(0)
        })
        .collect();
    if widths.iter().sum::<usize>() + 3 * columns.saturating_sub(1) > TABLE_GRID_MAX_WIDTH {
        // Wide cells wrap and shred a padded grid (found live: the
        // /about run's Piece/Location table). Fall back to a definition
        // list — header dim, cell inline — which reads at any width.
        let headers = body[0];
        for row in &body[1..] {
            for col in 0..columns {
                let cell = row.get(col).map(String::as_str).unwrap_or("");
                if cell.is_empty() {
                    continue;
                }
                let header = headers.get(col).map(String::as_str).unwrap_or("");
                let label = if header.is_empty() { "·" } else { header };
                spans.push(seg_token(&format!("  {label}: "), StyleToken::Dimmer));
                spans.extend(inline(cell));
                spans.push(Segment::new("\n"));
            }
            spans.push(Segment::new("\n"));
        }
        if spans.last().is_some_and(|segment| segment.text == "\n") {
            spans.pop();
        }
        return end;
    }
    for (index, row) in body.iter().enumerate() {
        for (col, width) in widths.iter().enumerate() {
            let cell = row.get(col).map(String::as_str).unwrap_or("");
            if col > 0 {
                spans.push(seg_token(" │ ", StyleToken::Dimmer));
            }
            if index == 0 {
                spans.push(Segment {
                    bold: true,
                    ..seg_token(cell, StyleToken::Bright)
                });
            } else {
                spans.extend(inline(cell));
            }
            spans.push(Segment::new(" ".repeat(width.saturating_sub(plain_len(cell)))));
        }
        spans.push(Segment::new("\n"));
        if index == 0 {
            let rule = widths
                .iter()
                .map(|width| "─".repeat(*width))
                .collect::<Vec<_>>()
                .join("─┼─");
            spans.push(seg_token(&rule, StyleToken::Dimmer));
            spans.push(Segment::new("\n"));
        }
    }
    end
}

/// Append a blank line (a lone `\n` sentinel) unless the last emitted line is
/// already blank.
///
/// Inter-block spacing (headings, list runs, tables, fenced code) is the
/// main readability win — a block reads as its own paragraph. Every line the
/// loop emits terminates in a `Segment { text: "\n" }`; a blank line is a
/// second consecutive terminator. Nothing is added at the very start
/// (leading blank) or when a blank already separates the previous block.
fn ensure_blank(spans: &mut Vec<Segment>) {
    match spans.last() {
        None => return,
        Some(last) if last.text != "\n" => return,
        _ => {}
    }
    if spans.len() >= 2 && spans[spans.len() - 2].text == "\n" {
        return; // the last line is already blank — don't stack another
    }
    spans.push(Segment::new("\n"));
}

/// Raw model text → Answer segments (light markdown, theme tokens only).
///
/// Inline: `**…**` bright bold, `` `…` `` teal code, links teal+dim —
/// the selective emphasis DESIGN-SPEC §3 specifies. Real model output
/// also carries block structure the mockup never had, rendered here so
/// it doesn't leak raw (user report): `#` headings → bright bold,
/// pipe tables → aligned columns with a dim rule, fenced code → teal
/// indented block (fence lines dropped), `- ` bullets → `• `, and
/// `> ` blockquotes → a colored `▌` gutter (the TUI-native frame for
/// the insight/machete callouts, matching the line-mode CLI's Rich edge).
pub fn answer_spans(source: &str) -> Vec<Segment> {
    let mut spans: Vec<Segment> = Vec::new();
    let lines: Vec<&str> = source.split('\n').collect();
    let mut index = 0;
    let mut in_code = false;
    let mut in_list = false; // inside a run of consecutive bullet/numbered items
    let mut in_quote = false; // inside a run of consecutive `> ` blockquote lines
    while index < lines.len() {
        let line = lines[index];
        let stripped = line.trim();
        if stripped.starts_with("```") || stripped.starts_with("~~~") {
            if !in_code {
                in_list = false;
                in_quote = false;
                ensure_blank(&mut spans); // fenced code opens its own paragraph
                in_code = true;
            } else {
                in_code = false;
                ensure_blank(&mut spans); // …and closes with a trailing gap
            }
            index += 1;
            continue;
        }
        if in_code {
            spans.push(seg_token(&format!("  {line}"), StyleToken::Teal));
            spans.push(Segment::new("\n"));
            index += 1;
            continue;
        }
        if stripped.starts_with('|')
            && index + 1 < lines.len()
            && lines[index + 1].trim_start().starts_with('|')
            && table_sep_re().is_match(lines[index + 1])
        {
            in_list = false;
            in_quote = false;
            ensure_blank(&mut spans);
            index = emit_table(&mut spans, &lines, index);
            ensure_blank(&mut spans);
            continue;
        }
        if let Some(heading) = heading_re().captures(line) {
            in_list = false;
            in_quote = false;
            ensure_blank(&mut spans); // the blank before is what sets a heading off
            spans.push(Segment {
                bold: true,
                ..seg_token(heading.get(2).expect("group 2 exists").as_str(), StyleToken::Bright)
            });
            spans.push(Segment::new("\n"));
            ensure_blank(&mut spans);
            index += 1;
            continue;
        }
        if let Some(numbered) = numbered_re().captures(line) {
            if !in_list {
                ensure_blank(&mut spans);
                in_list = true;
            }
            in_quote = false;
            let marker = format!("{}{}. ", &numbered[1], &numbered[2]);
            spans.push(seg_token(&marker, StyleToken::Dim));
            spans.extend(inline(&numbered[3]));
            spans.push(Segment::new("\n"));
            index += 1;
            continue;
        }
        if let Some(bullet) = bullet_re().captures(line) {
            if !in_list {
                ensure_blank(&mut spans);
                in_list = true;
            }
            in_quote = false;
            let (indent, body) = (&bullet[1], bullet.get(2).expect("group 2 exists").as_str());
            if let Some(checkbox) = checkbox_re().captures(body) {
                // `- [x]` / `- [ ]` render as a task-list glyph (green
                // done / dim pending), not the raw `• [x]` the bullet path
                // would otherwise leak.
                let checked = checkbox[1].to_lowercase() == "x";
                let glyph = if checked {
                    GLYPH_CHECKBOX_CHECKED
                } else {
                    GLYPH_CHECKBOX_EMPTY
                };
                spans.push(seg_token(
                    &format!("{indent}{glyph} "),
                    if checked { StyleToken::Green } else { StyleToken::Dim },
                ));
                spans.extend(inline(&body[checkbox.get(0).expect("match exists").end()..]));
            } else {
                spans.push(seg_token(&format!("{indent}• "), StyleToken::Dim));
                spans.extend(inline(body));
            }
            spans.push(Segment::new("\n"));
            index += 1;
            continue;
        }
        if let Some(quote) = quote_re().captures(line) {
            // Insight/machete callouts and any other blockquote: a colored
            // left gutter frames the quote, inline emphasis still applies.
            in_list = false;
            if !in_quote {
                ensure_blank(&mut spans); // a quote run reads as its own paragraph
                in_quote = true;
            }
            spans.push(seg_token(GLYPH_QUOTE_GUTTER, StyleToken::Blue));
            spans.extend(inline(quote.get(1).expect("group 1 exists").as_str()));
            spans.push(Segment::new("\n"));
            index += 1;
            continue;
        }
        if in_list {
            ensure_blank(&mut spans); // a plain line closes the list run
            in_list = false;
        }
        if in_quote {
            ensure_blank(&mut spans); // a plain line closes the quote run
            in_quote = false;
        }
        spans.extend(inline(line));
        spans.push(Segment::new("\n"));
        index += 1;
    }
    // The per-line loop appends one newline per source line; the final one
    // would fabricate a trailing blank line — drop it. A block that ends the
    // answer (heading/list/table/code) also left a trailing blank sentinel
    // from ensure_blank; pop that too so answers never end on empty lines.
    if spans.len() >= 2
        && spans[spans.len() - 1].text == "\n"
        && spans[spans.len() - 2].text == "\n"
    {
        spans.pop();
    }
    if spans
        .last()
        .is_some_and(|last| last.text == "\n" && last.style_token == StyleToken::Fg)
    {
        spans.pop();
    }
    if spans.is_empty() {
        spans.push(Segment::new(""));
    }
    spans
}

/// Number of leading lines that may be painted mid-stream.
///
/// A trailing run of table lines (`|`-prefixed) is withheld. One final
/// empty element (the artifact of a source ending in `\n`) is ignored
/// when locating the run; a *blank line* after the table (paragraph
/// break) means the table is complete and paintable.
pub fn visible_length(lines: &[&str]) -> usize {
    let mut scan = lines.len();
    if scan > 0 && lines[scan - 1].is_empty() {
        scan -= 1;
    }
    let mut cut = scan;
    while cut > 0 && lines[cut - 1].trim_start().starts_with('|') {
        cut -= 1;
    }
    if cut == scan {
        return lines.len(); // no trailing table run
    }
    cut
}

/// Return the active fence marker after all completed lines.
fn open_fence(source: &str) -> Option<&'static str> {
    let mut active: Option<&'static str> = None;
    for line in source.split('\n') {
        let stripped = line.trim();
        let marker = if stripped.starts_with("```") {
            "```"
        } else if stripped.starts_with("~~~") {
            "~~~"
        } else {
            continue;
        };
        match active {
            None => active = Some(marker),
            Some(current) if current == marker => active = None,
            Some(_) => {}
        }
    }
    active
}

/// Render completed streaming lines through the final answer pipeline.
///
/// Only the trailing partial line remains plain. A trailing table run is
/// held back until its paragraph break arrives, and a partial line inside
/// an open fence uses the same indented teal treatment as the final answer.
pub fn streaming_spans(source: &str) -> Vec<Segment> {
    let lines: Vec<&str> = source.split('\n').collect();
    let cut = visible_length(&lines);
    let table_held = cut < lines.len();
    let visible = if table_held {
        lines[..cut].join("\n")
    } else {
        source.to_string()
    };
    if visible.is_empty() {
        return Vec::new();
    }

    let (committed, partial) = if table_held {
        (visible, String::new())
    } else if let Some(split_at) = visible.rfind('\n') {
        (
            visible[..split_at].to_string(),
            visible[split_at + 1..].to_string(),
        )
    } else {
        (String::new(), visible)
    };

    let mut spans: Vec<Segment> = if committed.is_empty() {
        Vec::new()
    } else {
        answer_spans(&committed)
    };
    if !committed.is_empty() && !partial.is_empty() {
        spans.push(Segment::new("\n"));
    }
    if !partial.is_empty() {
        if open_fence(&committed).is_some() {
            spans.push(seg_token(&format!("  {partial}"), StyleToken::Teal));
        } else {
            spans.push(Segment::new(partial.as_str()));
        }
    }
    spans
}

/// The last `max_lines` lines of `text` (all of it when `max_lines` is None).
///
/// Blank lines are kept — the root peek mirrors the model's own line breaks,
/// unlike [`lane_tail_markup`] which drops blanks to pack three dense lines.
fn last_lines(text: &str, max_lines: Option<usize>) -> String {
    let Some(max_lines) = max_lines else {
        return text.to_string();
    };
    let lines: Vec<&str> = text.split('\n').collect();
    if lines.len() > max_lines {
        lines[lines.len() - max_lines..].join("\n")
    } else {
        text.to_string()
    }
}

/// Markup for a focused lane's tail: the last [`LANE_TAIL_LINES`] non-blank
/// lines, `┆`-guttered, dim (DESIGN-SPEC §8). Pure function — unit-testable
/// without a widget; content is escaped, never interpreted.
pub fn lane_tail_markup(text: &str) -> String {
    let lines: Vec<&str> = text
        .split('\n')
        .filter(|line| !line.trim().is_empty())
        .collect();
    let lines = if lines.len() > LANE_TAIL_LINES {
        &lines[lines.len() - LANE_TAIL_LINES..]
    } else {
        &lines[..]
    };
    if lines.is_empty() {
        return String::new();
    }
    let body = lines
        .iter()
        .map(|line| format!("┆ {}", escape(line)))
        .collect::<Vec<_>>()
        .join("\n");
    format!("[$dim]{body}[/]")
}

/// Streaming tail state machine: accumulate deltas, throttle paints,
/// consolidate.
///
/// Contract with the app layer:
///
/// - [`LiveTail::open_stream`] on `stream_block_start`;
/// - [`LiveTail::feed`] per `stream_block_delta` (repaints coalesce to ≤30Hz
///   via a trailing timer — high-frequency deltas cost one paint; `feed`
///   returns the delay for the app to schedule, [`LiveTail::fire_timer`]
///   runs the trailing paint);
/// - [`LiveTail::consolidate`] on `stream_block_end` → returns the durable
///   Answer for the caller to append (Python also posted a
///   `LiveTail.Consolidated` message; that wiring does not port).
///
/// The `now` parameters are monotonic seconds (Python `time.monotonic()`).
#[derive(Debug, Clone)]
pub struct LiveTail {
    source: String,
    block_type: String,
    timer_pending: bool,
    last_paint: f64,
    paint_count: usize,
    lane_mode: bool,
    root_open: bool,
    // Reveal is a session-level preference: the box defaults to hidden
    // (a one-line peek hint), and once the user shows it, it stays shown
    // across subsequent blocks until they hide it again.
    revealed: bool,
    painted: String,
}

impl Default for LiveTail {
    fn default() -> Self {
        Self::new()
    }
}

impl LiveTail {
    pub fn new() -> Self {
        Self {
            source: String::new(),
            block_type: "text".to_string(),
            timer_pending: false,
            last_paint: 0.0,
            paint_count: 0,
            lane_mode: false,
            root_open: false,
            revealed: false,
            painted: String::new(),
        }
    }

    /// The full accumulated raw text (holdback never applies here).
    pub fn source(&self) -> &str {
        &self.source
    }

    pub fn block_type(&self) -> &str {
        &self.block_type
    }

    /// Paints performed so far (throttle tests observe this).
    pub fn paint_count(&self) -> usize {
        self.paint_count
    }

    /// True while the root stream box shows its content (not the peek hint).
    pub fn revealed(&self) -> bool {
        self.revealed
    }

    /// The last painted markup (Python's `Static.update()` argument).
    pub fn painted(&self) -> &str {
        &self.painted
    }

    /// Flip the reveal preference; repaint the open root stream. Returns new state.
    pub fn toggle_reveal(&mut self, now: f64) -> bool {
        self.revealed = !self.revealed;
        if self.root_open {
            self.paint_now(now);
        }
        self.revealed
    }

    /// The collapsed-stream peek: one dim line naming the activity + how to show.
    pub fn reveal_hint(&self) -> String {
        let label = if self.block_type == "thinking" {
            "thinking"
        } else {
            "responding"
        };
        format!("[$dim]▸ {label}… — {REVEAL_HINT_KEY} or click to show[/]")
    }

    /// Reset for a new streaming block (`llm:stream_block_start`).
    pub fn open_stream(&mut self, block_type: &str, now: f64) {
        self.lane_mode = false; // root always preempts the lane tail (D4)
        self.root_open = true;
        self.timer_pending = false;
        self.source.clear();
        self.block_type = block_type.to_string();
        self.last_paint = 0.0;
        self.paint_now(now);
    }

    /// Accumulate one delta; schedule a throttled repaint.
    ///
    /// Returns `Some(delay_seconds)` when a trailing paint must be scheduled
    /// (Python's `set_timer`) — the app calls [`LiveTail::fire_timer`] once
    /// it elapses. `None` means the paint already happened or one is pending.
    pub fn feed(&mut self, text: &str, now: f64) -> Option<f64> {
        if !text.is_empty() {
            self.source.push_str(text);
        }
        if self.timer_pending {
            return None; // a trailing paint is already scheduled
        }
        let due = self.last_paint + THROTTLE_SECONDS;
        if now >= due {
            self.paint_now(now);
            None
        } else {
            self.timer_pending = true;
            Some(due - now)
        }
    }

    /// The scheduled trailing paint fired (Python's timer callback).
    pub fn fire_timer(&mut self, now: f64) {
        self.paint_now(now);
    }

    /// Close the stream: emit the durable Answer, clear the tail.
    ///
    /// The full source (including any held-back trailing table) becomes
    /// the Answer's spans. Evidence refs are attached later by the app
    /// (they need tool correlation) — the block id is stable for that.
    pub fn consolidate(&mut self, block_id: &str) -> Answer {
        let source = self.source.trim_end_matches('\n').to_string();
        let answer = Answer::new(block_id, answer_spans(&source));
        self.timer_pending = false;
        self.root_open = false;
        self.source.clear();
        self.last_paint = 0.0;
        self.painted.clear();
        answer
    }

    /// True while the tail shows a focused lane's stream, not the root's.
    pub fn lane_mode(&self) -> bool {
        self.lane_mode
    }

    /// Paint the focused lane's accumulated tail (dim, `┆`-guttered).
    ///
    /// The root always preempts: refused while a root stream is open. The
    /// reducer owns accumulation and the ~0.05s throttle
    /// (`LANE_TAIL_NOTIFY_SECONDS`); this widget just paints the last
    /// [`LANE_TAIL_LINES`] lines. Lane content is ephemeral — it is
    /// never consolidated into a transcript block.
    pub fn show_lane_tail(&mut self, text: &str) {
        if self.root_open {
            return;
        }
        self.lane_mode = true;
        self.painted = lane_tail_markup(text);
    }

    /// Drop the lane tail (root preemption / lane done / turn end).
    pub fn clear_lane_tail(&mut self) {
        if !self.lane_mode {
            return;
        }
        self.lane_mode = false;
        self.painted.clear();
    }

    /// Convenience: the consolidated Answer with evidence refs attached.
    pub fn attach_evidence(&self, answer: &Answer, links: Vec<EvidenceLink>) -> Answer {
        let mut updated = answer.clone();
        updated.evidence_refs = links;
        updated
    }

    /// The paintable portion of the source (trailing tables withheld).
    pub fn visible_source(&self) -> String {
        let lines: Vec<&str> = self.source.split('\n').collect();
        let cut = visible_length(&lines);
        if cut >= lines.len() {
            return self.source.clone();
        }
        lines[..cut].join("\n")
    }

    // -- painting ------------------------------------------------------------

    fn paint_now(&mut self, now: f64) {
        self.timer_pending = false;
        self.last_paint = now;
        // Collapsed: show a single-line peek hint instead of the content.
        // Deltas still accumulate into `source` so a mid-stream reveal
        // snaps straight to the current tail.
        if self.root_open && !self.revealed {
            self.paint_count += 1;
            self.painted = self.reveal_hint();
            return;
        }
        self.paint_count += 1;
        self.painted = Self::markup_for(&self.source, &self.block_type, Some(MAX_ROOT_LINES));
    }

    pub fn markup(&self) -> String {
        Self::markup_for(&self.source, &self.block_type, None)
    }

    pub fn markup_for(source: &str, block_type: &str, max_lines: Option<usize>) -> String {
        let lines: Vec<&str> = source.split('\n').collect();
        let cut = visible_length(&lines);
        let visible = if cut >= lines.len() {
            source.to_string()
        } else {
            lines[..cut].join("\n")
        };
        if visible.is_empty() {
            return String::new();
        }
        if block_type == "thinking" {
            let text = last_lines(&visible, max_lines);
            return format!("[italic $dim]{}[/]", escape(&text));
        }
        let render_source = if max_lines.is_some() {
            last_lines(source, max_lines)
        } else {
            source.to_string()
        };
        streaming_spans(&render_source)
            .iter()
            .map(segment_markup)
            .collect()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn seg(text: &str) -> Segment {
        Segment::new(text)
    }

    // -- pure helpers ----------------------------------------------------------

    // Python: tests/test_ui_transcript_live_tail.py::test_answer_spans_selective_emphasis
    #[test]
    fn test_answer_spans_selective_emphasis() {
        let spans = answer_spans("Run `pytest` now — **done**.");
        assert_eq!(
            spans,
            vec![
                seg("Run "),
                seg_token("pytest", StyleToken::Teal),
                seg(" now — "),
                Segment {
                    bold: true,
                    ..seg_token("done", StyleToken::Bright)
                },
                seg("."),
            ]
        );
    }

    // Python: test_answer_spans_plain_and_empty
    #[test]
    fn test_answer_spans_plain_and_empty() {
        assert_eq!(answer_spans("just text"), vec![seg("just text")]);
        assert_eq!(answer_spans(""), vec![seg("")]);
    }

    // Python: test_answer_spans_blockquote_callout_gutter
    #[test]
    fn test_answer_spans_blockquote_callout_gutter() {
        let spans = answer_spans("> ★ **Insight:** one owner per concern.");
        assert_eq!(
            spans,
            vec![
                seg_token("▌ ", StyleToken::Blue),
                seg("★ "),
                Segment {
                    bold: true,
                    ..seg_token("Insight:", StyleToken::Bright)
                },
                seg(" one owner per concern."),
            ]
        );
    }

    // Python: test_answer_spans_blockquote_run_reads_as_its_own_paragraph
    #[test]
    fn test_answer_spans_blockquote_run_reads_as_its_own_paragraph() {
        let spans = answer_spans("intro\n> ★ **Insight:** a\n>b\ntail");
        assert_eq!(
            spans,
            vec![
                seg("intro"),
                seg("\n"),
                seg("\n"),
                seg_token("▌ ", StyleToken::Blue),
                seg("★ "),
                Segment {
                    bold: true,
                    ..seg_token("Insight:", StyleToken::Bright)
                },
                seg(" a"),
                seg("\n"),
                seg_token("▌ ", StyleToken::Blue),
                seg("b"),
                seg("\n"),
                seg("\n"),
                seg("tail"),
            ]
        );
    }

    // Python: test_answer_spans_italic_emphasis
    #[test]
    fn test_answer_spans_italic_emphasis() {
        assert_eq!(
            answer_spans("plain *emph* and **bold** and `code`"),
            vec![
                seg("plain "),
                Segment {
                    italic: true,
                    ..seg("emph")
                },
                seg(" and "),
                Segment {
                    bold: true,
                    ..seg_token("bold", StyleToken::Bright)
                },
                seg(" and "),
                seg_token("code", StyleToken::Teal),
            ]
        );
        // A star with whitespace on the inside is not emphasis (2 * 3 * 4).
        assert_eq!(answer_spans("2 * 3 * 4"), vec![seg("2 * 3 * 4")]);
    }

    // Python: test_answer_spans_task_list_checkboxes
    #[test]
    fn test_answer_spans_task_list_checkboxes() {
        assert_eq!(
            answer_spans("- [x] shipped\n- [ ] todo\n- plain"),
            vec![
                seg_token("✓ ", StyleToken::Green),
                seg("shipped"),
                seg("\n"),
                seg_token("☐ ", StyleToken::Dim),
                seg("todo"),
                seg("\n"),
                seg_token("• ", StyleToken::Dim),
                seg("plain"),
            ]
        );
    }

    // Python: test_answer_spans_markdown_link_carries_osc8_target
    #[test]
    fn test_answer_spans_markdown_link_carries_osc8_target() {
        assert_eq!(
            answer_spans("see [docs](https://example.com/g) now"),
            vec![
                seg("see "),
                Segment {
                    link: Some("https://example.com/g".to_string()),
                    ..seg_token("docs", StyleToken::Teal)
                },
                Segment {
                    link: Some("https://example.com/g".to_string()),
                    ..seg_token(" (https://example.com/g)", StyleToken::Dimmer)
                },
                seg(" now"),
            ]
        );
    }

    // Python: test_answer_spans_bare_url_collapses_to_hyperlink
    #[test]
    fn test_answer_spans_bare_url_collapses_to_hyperlink() {
        assert_eq!(
            answer_spans("visit https://amplifier.dev. thanks"),
            vec![
                seg("visit "),
                Segment {
                    link: Some("https://amplifier.dev".to_string()),
                    ..seg_token("https://amplifier.dev", StyleToken::Teal)
                },
                seg(". thanks"),
            ]
        );
    }

    // Python: test_visible_length_holds_back_trailing_table
    #[test]
    fn test_visible_length_holds_back_trailing_table() {
        // Trailing table run (with streaming-newline artifact) is withheld.
        assert_eq!(visible_length(&["Results:", "| a | b |", "| 1 | 2 |"]), 1);
        assert_eq!(visible_length(&["Results:", "| a | b |", ""]), 1);
        // No table → everything paints.
        assert_eq!(visible_length(&["Results:", "done"]), 2);
        // A paragraph break after the table completes it → paintable.
        assert_eq!(visible_length(&["Results:", "| a | b |", "", "Done"]), 4);
    }

    // Python: test_streaming_spans_commit_complete_lines_only
    #[test]
    fn test_streaming_spans_commit_complete_lines_only() {
        let spans = streaming_spans("# Result\nRun `pytest` — **done**.\npartial **mar");
        assert_eq!(
            spans,
            vec![
                Segment {
                    bold: true,
                    ..seg_token("Result", StyleToken::Bright)
                },
                seg("\n"),
                seg("\n"),
                seg("Run "),
                seg_token("pytest", StyleToken::Teal),
                seg(" — "),
                Segment {
                    bold: true,
                    ..seg_token("done", StyleToken::Bright)
                },
                seg("."),
                seg("\n"),
                seg("partial **mar"),
            ]
        );
    }

    // Python: test_streaming_spans_hold_table_and_track_open_fence
    #[test]
    fn test_streaming_spans_hold_table_and_track_open_fence() {
        let table = streaming_spans("Results:\n| Check | State |\n| tests | pass |");
        assert_eq!(table, vec![seg("Results:")]);

        let code = streaming_spans("```python\nprint('ok')\nret");
        assert_eq!(code.last().unwrap(), &seg_token("  ret", StyleToken::Teal));
        assert!(code.iter().all(|segment| !segment.text.contains("```")));
    }

    // -- widget behavior (state machine, injected clock) ------------------------

    // Python: test_feed_accumulates_and_visible_source_tracks
    // (pilot.pause → fire_timer with the injected clock)
    #[test]
    fn test_feed_accumulates_and_visible_source_tracks() {
        let mut tail = LiveTail::new();
        tail.open_stream("text", 0.0);
        let delay = tail.feed("Hello ", 0.0);
        tail.feed("world", 0.0);
        if delay.is_some() {
            tail.fire_timer(0.1);
        }
        assert_eq!(tail.source(), "Hello world");
        assert_eq!(tail.visible_source(), "Hello world");
    }

    // Python: test_paints_throttle_to_one_per_interval
    #[test]
    fn test_paints_throttle_to_one_per_interval() {
        let mut tail = LiveTail::new();
        tail.open_stream("text", 1.0);
        let base = tail.paint_count(); // open_stream paints once
        let mut trailing = None;
        for index in 0..50 {
            // a burst far faster than 30Hz
            if let Some(delay) = tail.feed(&format!("chunk{index} "), 1.0) {
                trailing = Some(delay);
            }
        }
        // The burst may cost at most one immediate paint + one trailing timer.
        assert!(tail.paint_count() <= base + 1);
        assert!(trailing.is_some()); // one trailing paint was scheduled
        tail.fire_timer(1.0 + THROTTLE_SECONDS * 4.0);
        assert!(tail.paint_count() <= base + 2);
        assert!(tail.source().ends_with("chunk49 "));
        // The trailing paint flushed the full accumulated source.
        assert_eq!(tail.visible_source(), tail.source());
    }

    // Python: test_trailing_table_withheld_until_stream_end
    #[test]
    fn test_trailing_table_withheld_until_stream_end() {
        let mut tail = LiveTail::new();
        tail.open_stream("text", 0.0);
        tail.feed("Results:\n| Check | State |\n| tests | pass |", 0.0);
        assert_eq!(tail.visible_source(), "Results:"); // table held back

        let answer = tail.consolidate("b9");
        // Consolidation carries the FULL source, holdback never loses text.
        let full: String = answer.spans.iter().map(|span| span.text.as_str()).collect();
        assert_eq!(full, "Results:\n| Check | State |\n| tests | pass |");
    }

    // Python: test_consolidate_emits_answer_block_and_message_then_resets
    // (the LiveTail.Consolidated message does not port — the returned Answer
    // is the wiring; `app.consolidated == [answer]` has no Rust counterpart)
    #[test]
    fn test_consolidate_emits_answer_block_and_message_then_resets() {
        let mut tail = LiveTail::new();
        tail.open_stream("text", 0.0);
        tail.feed("Run `pytest` — **34 passed**.\n", 0.0);

        let answer = tail.consolidate("b42");
        assert_eq!(answer.id, "b42");
        assert_eq!(
            answer.spans,
            vec![
                seg("Run "),
                seg_token("pytest", StyleToken::Teal),
                seg(" — "),
                Segment {
                    bold: true,
                    ..seg_token("34 passed", StyleToken::Bright)
                },
                seg("."),
            ]
        );
        assert_eq!(tail.source(), ""); // tail cleared for the next stream

        let with_refs = tail.attach_evidence(
            &answer,
            vec![EvidenceLink::new("34 passed", "pytest run")],
        );
        assert_eq!(with_refs.evidence_refs[0].tool_ref, "pytest run");
        assert_eq!(with_refs.id, "b42");
    }

    // Python: test_thinking_blocks_paint_italic_dim
    #[test]
    fn test_thinking_blocks_paint_italic_dim() {
        let mut tail = LiveTail::new();
        tail.open_stream("thinking", 0.0);
        tail.feed("considering the store layout", 0.0);
        assert_eq!(tail.block_type(), "thinking");
        assert!(tail.markup().starts_with("[italic $dim]"));
    }

    // Python: test_markup_for_caps_revealed_stream_to_max_lines
    #[test]
    fn test_markup_for_caps_revealed_stream_to_max_lines() {
        let src = (0..20)
            .map(|index| format!("row{index}"))
            .collect::<Vec<_>>()
            .join("\n");
        let out = LiveTail::markup_for(&src, "thinking", Some(MAX_ROOT_LINES));
        assert!(out.contains("row19")); // newest line kept
        assert!(!out.contains("row0\n") && !out.ends_with("row0")); // oldest trimmed
        let inner = &out["[italic $dim]".len()..out.len() - "[/]".len()];
        assert_eq!(inner.matches('\n').count(), MAX_ROOT_LINES - 1); // exactly the last N lines
    }

    // Python: test_markup_for_without_cap_is_unchanged
    #[test]
    fn test_markup_for_without_cap_is_unchanged() {
        let src = "# Head\nRun `pytest`\nbody text";
        assert_eq!(
            LiveTail::markup_for(src, "text", None),
            LiveTail::markup_for(src, "text", None)
        );
    }

    // Python: test_toggle_reveal_returns_state_and_persists
    #[test]
    fn test_toggle_reveal_returns_state_and_persists() {
        let mut tail = LiveTail::new();
        assert!(!tail.revealed()); // default hidden
        assert!(tail.toggle_reveal(0.0));
        assert!(tail.revealed());
        assert!(!tail.toggle_reveal(0.0));
        assert!(!tail.revealed());
    }

    // Python: test_hidden_root_stream_paints_peek_hint
    // (monkeypatched `update` → the stored `painted` markup)
    #[test]
    fn test_hidden_root_stream_paints_peek_hint() {
        let mut tail = LiveTail::new();
        tail.open_stream("thinking", 0.0);
        let delay = tail.feed("secret line one\nsecret line two", 0.0);
        if delay.is_some() {
            tail.fire_timer(0.1);
        }
        assert!(!tail.revealed());
        assert_eq!(tail.painted(), tail.reveal_hint());
        assert!(tail.painted().contains("click to show"));
        assert!(!tail.painted().contains("secret line")); // content stays hidden
    }

    // Python: test_revealed_root_stream_paints_capped_content
    #[test]
    fn test_revealed_root_stream_paints_capped_content() {
        let mut tail = LiveTail::new();
        tail.toggle_reveal(0.0); // user shows the box
        tail.open_stream("thinking", 0.0);
        let payload = (0..10)
            .map(|index| format!("line{index}"))
            .collect::<Vec<_>>()
            .join("\n");
        let delay = tail.feed(&payload, 0.0);
        if delay.is_some() {
            tail.fire_timer(THROTTLE_SECONDS * 4.0);
        }
        assert!(tail.revealed());
        assert!(tail.painted().contains("line9")); // newest content shown
        assert!(!tail.painted().contains("click to show")); // not the hint
    }

    // Python: test_completed_stream_lines_use_final_markup_before_consolidation
    #[test]
    fn test_completed_stream_lines_use_final_markup_before_consolidation() {
        let mut tail = LiveTail::new();
        tail.open_stream("text", 0.0);
        tail.feed("# Result\nRun `pytest`\npartial **mar", 0.0);
        let markup = tail.markup();
        assert!(!markup.contains("# Result"));
        assert!(markup.contains("[bold $bright]Result[/]"));
        assert!(markup.contains("[$teal]pytest[/]"));
        assert!(markup.contains("partial **mar"));
    }

    // Python: test_open_stream_resets_previous_source
    #[test]
    fn test_open_stream_resets_previous_source() {
        let mut tail = LiveTail::new();
        tail.open_stream("text", 0.0);
        tail.feed("first stream", 0.0);
        tail.open_stream("text", 0.1);
        assert_eq!(tail.source(), "");
        assert_eq!(tail.visible_source(), "");
    }

    // -- lane mode (design doc D4: focused-lane live tail) ----------------------

    // Python: test_lane_tail_markup_gutters_dims_and_caps_at_three_lines
    #[test]
    fn test_lane_tail_markup_gutters_dims_and_caps_at_three_lines() {
        let markup = lane_tail_markup("one\ntwo\nthree\nfour\n");
        assert_eq!(markup, "[$dim]┆ two\n┆ three\n┆ four[/]");
    }

    // Python: test_lane_tail_markup_escapes_and_handles_empty
    #[test]
    fn test_lane_tail_markup_escapes_and_handles_empty() {
        assert_eq!(lane_tail_markup(""), "");
        assert_eq!(lane_tail_markup("   \n"), "");
        let markup = lane_tail_markup("[red]not markup");
        assert!(markup.starts_with("[$dim]"));
        assert!(markup.contains("┆ \\[red]not markup")); // escaped — content is never interpreted
    }

    // Python: test_lane_mode_yields_to_root_stream_and_clears
    #[test]
    fn test_lane_mode_yields_to_root_stream_and_clears() {
        let mut tail = LiveTail::new();
        tail.show_lane_tail("agent prose");
        assert!(tail.lane_mode());
        tail.open_stream("text", 0.0); // root preempts instantly
        assert!(!tail.lane_mode());
        tail.show_lane_tail("ignored while root streams");
        assert!(!tail.lane_mode()); // refused: root owns the tail
        tail.feed("root text", 0.0);
        tail.consolidate("blk-1"); // root stream closed
        tail.show_lane_tail("agent prose again");
        assert!(tail.lane_mode()); // lanes may resume after the root goes idle
        tail.clear_lane_tail();
        assert!(!tail.lane_mode());
    }
}
