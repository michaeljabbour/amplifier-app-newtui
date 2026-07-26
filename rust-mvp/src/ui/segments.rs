//! Segment lists → paintable form, styled ONLY by theme tokens.
//!
//! Port of `src/amplifier_app_newtui/ui/segments.py`.
//!
//! The transcript renderer produces lines of
//! [`crate::model::blocks::Segment`] — plain data naming DESIGN-SPEC §1
//! tokens. This module converts those segments into paintable form without
//! ever touching a color value:
//!
//! - [`segment_style`] / [`line_markup`] / [`lines_markup`] emit Textual
//!   *content markup* whose styles reference theme **variables**
//!   (`[bold $green]…[/]`). The strings are byte-identical to the Python
//!   emitters — they remain the wire form for golden pins and for any
//!   surface that still speaks Textual markup (e.g. replay comparisons).
//! - [`to_ratatui_line`] is the ratatui bridge (Python's `to_rich_text`):
//!   callers holding a resolved token→color mapping get a
//!   `ratatui::text::Line` of styled spans; the mapping is the only place a
//!   concrete color ever appears, and it comes from the theme, never from
//!   this module.
//! - [`line_plain`] / [`lines_plain`] are the style-free projections the
//!   golden tests assert exact glyph/label text against.
//!
//! No hex values appear here.

use std::collections::HashMap;
use std::sync::OnceLock;

use ratatui::style::{Color, Modifier, Style};
use ratatui::text::Span;
use regex::Regex;

use crate::model::blocks::{Segment, StyleToken};

/// One rendered transcript line: a run of styled segments.
pub type Line = Vec<Segment>;

/// Escape text so it won't be interpreted as Textual content markup.
///
/// Faithful port of `textual.markup.escape`: a backslash is inserted before
/// any complete `[tag]` opening with `[a-z#/@]` (preceding backslashes are
/// doubled), and a lone trailing backslash is doubled so it can't swallow a
/// following tag.
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

/// The Textual style string for a segment: `bold italic $teal on $bg-tab`.
///
/// Tokens are referenced by variable name (`$<token>`) — never by value.
pub fn segment_style(segment: &Segment) -> String {
    let mut parts: Vec<String> = Vec::new();
    if segment.bold {
        parts.push("bold".to_string());
    }
    if segment.italic {
        parts.push("italic".to_string());
    }
    parts.push(format!("${}", segment.style_token.as_str()));
    if let Some(bg) = segment.bg_token {
        parts.push(format!("on ${}", bg.as_str()));
    }
    parts.join(" ")
}

/// One segment as Textual content markup (text escaped, style by token).
///
/// A segment carrying a `link` nests a `[link="…"]` tag so the terminal
/// paints a real OSC 8 hyperlink. The URL is QUOTED: an unquoted
/// `[link=https://…]` breaks Textual's markup parser on the `://`
/// ("Expected markup value") — which crashed transcript rendering (e.g.
/// resuming a session whose answer contained a PR link). A stray `"` in the
/// URL is escaped so the quoting itself can't be broken.
pub fn segment_markup(segment: &Segment) -> String {
    if segment.text.is_empty() {
        return String::new();
    }
    let mut body = escape(&segment.text);
    if let Some(link) = &segment.link {
        let safe_link = link.replace('"', "%22");
        body = format!("[link=\"{safe_link}\"]{body}[/link]");
    }
    format!("[{}]{}[/]", segment_style(segment), body)
}

/// A whole line of segments as one markup string.
pub fn line_markup(line: &[Segment]) -> String {
    line.iter().map(segment_markup).collect()
}

/// Multiple lines joined with newlines — the form widgets paint.
pub fn lines_markup<L: AsRef<[Segment]>>(lines: &[L]) -> String {
    lines
        .iter()
        .map(|line| line_markup(line.as_ref()))
        .collect::<Vec<_>>()
        .join("\n")
}

/// Style-free text of a line (what golden tests assert against).
pub fn line_plain(line: &[Segment]) -> String {
    line.iter().map(|segment| segment.text.as_str()).collect()
}

/// Style-free text of many lines, newline-joined.
pub fn lines_plain<L: AsRef<[Segment]>>(lines: &[L]) -> String {
    lines
        .iter()
        .map(|line| line_plain(line.as_ref()))
        .collect::<Vec<_>>()
        .join("\n")
}

/// A line as a `ratatui` [`ratatui::text::Line`] (Python's `to_rich_text`).
///
/// `variables` maps token → resolved color (pass the active theme's
/// variable table); with `None` the line carries structure (text +
/// bold/italic) but no colors — useful for width measurement and tests.
/// Colors resolved this way still come exclusively from the theme.
///
/// Rich's `Style(link=…)` has no ratatui counterpart: ratatui styles carry
/// no hyperlink, so `segment.link` is not painted here — the app-assembly
/// layer must emit OSC 8 itself if clickable links are wanted.
pub fn to_ratatui_line(
    line: &[Segment],
    variables: Option<&HashMap<StyleToken, Color>>,
) -> ratatui::text::Line<'static> {
    let mut spans: Vec<Span<'static>> = Vec::new();
    for segment in line {
        if segment.text.is_empty() {
            continue;
        }
        let mut style = Style::default();
        if let Some(vars) = variables {
            if let Some(color) = vars.get(&segment.style_token) {
                style = style.fg(*color);
            }
            if let Some(bg) = segment.bg_token {
                if let Some(color) = vars.get(&bg) {
                    style = style.bg(*color);
                }
            }
        }
        if segment.bold {
            style = style.add_modifier(Modifier::BOLD);
        }
        if segment.italic {
            style = style.add_modifier(Modifier::ITALIC);
        }
        spans.push(Span::styled(segment.text.clone(), style));
    }
    ratatui::text::Line::from(spans)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn seg(text: &str, token: StyleToken) -> Segment {
        Segment {
            style_token: token,
            ..Segment::new(text)
        }
    }

    /// The user block's first rendered line (`render_block(_blocks()["user"], 80)[0]`),
    /// captured from the Python oracle segment-by-segment.
    fn user_line() -> Line {
        vec![
            Segment {
                bold: true,
                ..seg("❯ ", StyleToken::Green)
            },
            seg("[build] ", StyleToken::Green),
            seg("Please verify the persistence boundary", StyleToken::Bright),
        ]
    }

    // Python: tests/test_ui_transcript_render.py::test_segment_style_token_variables
    #[test]
    fn test_segment_style_token_variables() {
        assert_eq!(segment_style(&Segment::new("x")), "$fg");
        assert_eq!(
            segment_style(&Segment {
                bold: true,
                ..seg("x", StyleToken::Teal)
            }),
            "bold $teal"
        );
        assert_eq!(
            segment_style(&Segment {
                italic: true,
                bg_token: Some(StyleToken::BgTab),
                ..seg("x", StyleToken::Green)
            }),
            "italic $green on $bg-tab"
        );
    }

    // Python: tests/test_ui_transcript_render.py::test_markup_uses_theme_variables_and_escapes_brackets
    // (Textual's Content.from_markup round-trip is replaced by the exact markup
    // string pinned from the Python oracle — same segments, same bytes.)
    #[test]
    fn test_markup_uses_theme_variables_and_escapes_brackets() {
        let lines = vec![user_line()];
        let markup = lines_markup(&lines);
        assert_eq!(
            markup,
            "[bold $green]❯ [/][$green]\\[build] [/][$bright]Please verify the persistence boundary[/]"
        );
        assert!(markup.contains("[bold $green]"));
        assert!(!markup.contains('#')); // never a color value
        // The literal "[build]" badge is escaped, not parsed as markup.
        assert_eq!(
            lines_plain(&lines),
            "❯ [build] Please verify the persistence boundary"
        );
    }

    // Pins textual.markup.escape edge cases against the Python oracle:
    //   escape('[build] hi [/] [x') == '\\[build] hi \\[/] [x'
    //   escape('a\\') == 'a\\\\' (lone trailing backslash doubled)
    #[test]
    fn test_escape_matches_textual_markup_escape() {
        let markup = segment_markup(&Segment {
            bold: true,
            ..seg("[build] hi [/] [x", StyleToken::Green)
        });
        assert_eq!(markup, "[bold $green]\\[build] hi \\[/] [x[/]");
        assert_eq!(
            segment_markup(&seg("a\\", StyleToken::Dim)),
            "[$dim]a\\\\[/]"
        );
        // Empty text renders nothing at all.
        assert_eq!(segment_markup(&Segment::new("")), "");
    }

    // Python: tests/test_ui_transcript_render.py::TestAnswerMarkdown::test_link_url_is_quoted_and_parses
    // ("must parse cleanly" is Textual's parser — here the exact markup emitted
    // is pinned instead, including the %22 escaping of quotes in the URL.)
    #[test]
    fn test_link_url_is_quoted_and_parses() {
        let url = "https://github.com/microsoft/amplifier-app-team-pulse/pull/304";
        let markup = segment_markup(&Segment {
            link: Some(url.to_string()),
            ..seg("team-pulse#304", StyleToken::Teal)
        });
        assert!(markup.contains(&format!("[link=\"{url}\"]"))); // quoted, not bare [link=https://…]
        assert_eq!(
            markup,
            "[$teal][link=\"https://github.com/microsoft/amplifier-app-team-pulse/pull/304\"]team-pulse#304[/link][/]"
        );
        // A stray quote in the URL is escaped so the quoting can't be broken.
        let quoted = segment_markup(&Segment {
            link: Some("https://ex.com/a?q=\"v\"#f".to_string()),
            ..seg("lnk", StyleToken::Teal)
        });
        assert_eq!(
            quoted,
            "[$teal][link=\"https://ex.com/a?q=%22v%22#f\"]lnk[/link][/]"
        );
    }

    // Python: tests/test_ui_transcript_render.py::test_to_rich_text_resolves_tokens_from_mapping_only
    #[test]
    fn test_to_rich_text_resolves_tokens_from_mapping_only() {
        let variables: HashMap<StyleToken, Color> = HashMap::from([
            (StyleToken::Green, Color::Cyan),
            (StyleToken::Bright, Color::Magenta),
            (StyleToken::Dim, Color::Yellow),
        ]);
        let line = user_line();
        let text = to_ratatui_line(&line, Some(&variables));
        let plain: String = text.spans.iter().map(|s| s.content.as_ref()).collect();
        assert_eq!(plain, "❯ [build] Please verify the persistence boundary");
        // Token resolved via mapping only.
        assert_eq!(text.spans[0].style.fg, Some(Color::Cyan));
        // Without a mapping, no colors at all.
        let uncolored = to_ratatui_line(&line, None);
        assert!(uncolored
            .spans
            .iter()
            .all(|span| span.style.fg.is_none() && span.style.bg.is_none()));
        // Structure (bold) survives even uncolored; empty segments are skipped.
        assert!(uncolored.spans[0].style.add_modifier.contains(Modifier::BOLD));
        let with_empty = vec![seg("a", StyleToken::Dim), Segment::new(""), Segment::new("b")];
        assert_eq!(to_ratatui_line(&with_empty, None).spans.len(), 2);
    }

    // Oracle pins for the joiners:
    //   line_markup skips empty segments; lines_plain joins with newlines.
    #[test]
    fn test_line_and_lines_joiners() {
        let line = vec![
            seg("a", StyleToken::Dim),
            Segment::new(""),
            Segment {
                bold: true,
                ..Segment::new("b")
            },
        ];
        assert_eq!(line_markup(&line), "[$dim]a[/][bold $fg]b[/]");
        assert_eq!(line_plain(&line), "ab");
        let lines = vec![vec![Segment::new("one")], vec![Segment::new("two")]];
        assert_eq!(lines_plain(&lines), "one\ntwo");
        assert_eq!(lines_markup(&lines), "[$fg]one[/]\n[$fg]two[/]");
    }
}
