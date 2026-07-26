//! `/export` — transcript → markdown export.
//!
//! Pure rendering: user lines become `> ` blockquotes, answers become
//! prose (span texts joined), tool lines become fenced code blocks; every
//! other block kind is UI chrome and is skipped. File I/O lives in
//! [`write_export`] with an injectable root and clock so the app-side
//! `export_transcript` action stays a one-liner.
//!
//! Ported from `commands/export.py`.

use std::io;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use crate::model::blocks::TranscriptBlock;
use crate::model::redaction::scrub_text;

/// Wall-clock stamp for export filenames.
///
/// Python passes a `datetime` into `export_filename`/`write_export` so tests
/// can pin the clock; this struct is the injectable equivalent — construct a
/// fixed one in tests, or [`ExportStamp::now`] at the call site.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ExportStamp {
    pub year: i64,
    pub month: u32,
    pub day: u32,
    pub hour: u32,
    pub minute: u32,
    pub second: u32,
}

impl ExportStamp {
    pub fn new(year: i64, month: u32, day: u32, hour: u32, minute: u32, second: u32) -> Self {
        Self {
            year,
            month,
            day,
            hour,
            minute,
            second,
        }
    }

    /// The current wall clock.
    ///
    /// Divergence noted honestly: Python stamps `datetime.now()` (local
    /// time); without a timezone dependency this stamps UTC. The stamp only
    /// feeds the export filename.
    pub fn now() -> Self {
        let secs = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_secs();
        let (year, month, day) = civil_from_days((secs / 86_400) as i64);
        let rem = secs % 86_400;
        Self {
            year,
            month,
            day,
            hour: (rem / 3600) as u32,
            minute: ((rem % 3600) / 60) as u32,
            second: (rem % 60) as u32,
        }
    }
}

/// Days-since-epoch → (year, month, day) — Howard Hinnant's civil_from_days.
fn civil_from_days(z: i64) -> (i64, u32, u32) {
    let z = z + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097; // [0, 146096]
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365; // [0, 399]
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100); // [0, 365]
    let mp = (5 * doy + 2) / 153; // [0, 11]
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32; // [1, 31]
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32; // [1, 12]
    (if m <= 2 { y + 1 } else { y }, m, d)
}

/// Markdown for one block; `None` for kinds the export skips.
fn render_block(block: &TranscriptBlock) -> Option<String> {
    match block {
        TranscriptBlock::UserLine(user) => Some(
            user.text
                .lines()
                .map(|line| format!("> {line}"))
                .collect::<Vec<_>>()
                .join("\n"),
        ),
        TranscriptBlock::Answer(answer) => Some(
            answer
                .spans
                .iter()
                .map(|segment| segment.text.as_str())
                .collect::<String>(),
        ),
        TranscriptBlock::ToolLine(tool) => {
            let mut lines: Vec<&str> = Vec::with_capacity(tool.body.len() + 3);
            lines.push("```");
            lines.push(&tool.summary);
            lines.extend(tool.body.iter().map(String::as_str));
            lines.push("```");
            Some(lines.join("\n"))
        }
        _ => None,
    }
}

/// Markdown for the exportable blocks; `""` for an empty transcript.
///
/// Sections are blank-line separated; the document ends with a newline.
pub fn render_transcript_markdown<'a, I>(blocks: I) -> String
where
    I: IntoIterator<Item = &'a TranscriptBlock>,
{
    let sections: Vec<String> = blocks.into_iter().filter_map(render_block).collect();
    if sections.is_empty() {
        return String::new();
    }
    // Scrub secret-shaped values at the sink so every block kind is covered
    // (issue #23) with the same rules the transcript/copy/metadata sinks use.
    scrub_text(&format!("{}\n", sections.join("\n\n")))
}

/// `<session-short>-<YYYYMMDD-HHMMSS>.md`.
pub fn export_filename(session_short: &str, now: ExportStamp) -> String {
    format!(
        "{session_short}-{:04}{:02}{:02}-{:02}{:02}{:02}.md",
        now.year, now.month, now.day, now.hour, now.minute, now.second
    )
}

/// Write the markdown export under *root* (created if missing); return the path.
pub fn write_export<'a, I>(
    blocks: I,
    session_short: &str,
    now: ExportStamp,
    root: &Path,
) -> io::Result<PathBuf>
where
    I: IntoIterator<Item = &'a TranscriptBlock>,
{
    std::fs::create_dir_all(root)?;
    let path = root.join(export_filename(session_short, now));
    std::fs::write(&path, render_transcript_markdown(blocks))?;
    Ok(path)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::blocks::{
        Answer, Narration, Segment, SessionBanner, StyleToken, ToolLine, ToolLineStatus, UserLine,
    };

    const NOW: ExportStamp = ExportStamp {
        year: 2026,
        month: 1,
        day: 1,
        hour: 12,
        minute: 34,
        second: 56,
    };

    fn user_line(id: &str, text: &str) -> TranscriptBlock {
        UserLine::new(id, text).into()
    }

    #[test]
    fn test_user_line_renders_as_blockquote() {
        let mut user = UserLine::new("b1", "fix the flaky test");
        user.mode = "build".to_string();
        let blocks = [TranscriptBlock::from(user)];
        assert_eq!(render_transcript_markdown(&blocks), "> fix the flaky test\n");
    }

    #[test]
    fn test_multiline_user_line_prefixes_every_line() {
        let blocks = [user_line("b1", "line one\nline two")];
        assert_eq!(
            render_transcript_markdown(&blocks),
            "> line one\n> line two\n"
        );
    }

    #[test]
    fn test_answer_joins_spans_as_prose() {
        let mut code = Segment::new("app.py");
        code.style_token = StyleToken::Teal;
        let blocks = [TranscriptBlock::from(Answer::new(
            "b2",
            vec![
                Segment::new("The fix is in "),
                code,
                Segment::new(", shipped."),
            ],
        ))];
        assert_eq!(
            render_transcript_markdown(&blocks),
            "The fix is in app.py, shipped.\n"
        );
    }

    #[test]
    fn test_tool_line_renders_fenced_with_body() {
        let mut tool = ToolLine::new("b3", "Ran uv run pytest");
        tool.body = vec!["42 passed".to_string(), "0 failed".to_string()];
        tool.status = ToolLineStatus::Completed;
        let blocks = [TranscriptBlock::from(tool)];
        assert_eq!(
            render_transcript_markdown(&blocks),
            "```\nRan uv run pytest\n42 passed\n0 failed\n```\n"
        );
    }

    #[test]
    fn test_tool_line_renders_fenced_without_body() {
        let blocks = [TranscriptBlock::from(ToolLine::new("b3", "Read blocks.py"))];
        assert_eq!(
            render_transcript_markdown(&blocks),
            "```\nRead blocks.py\n```\n"
        );
    }

    #[test]
    fn test_non_exported_kinds_are_skipped() {
        let blocks = [
            TranscriptBlock::from(SessionBanner::new("b0", "Amplifier 1.0")),
            user_line("b1", "hello"),
            TranscriptBlock::from(Narration::new("b2", "scanning the repo")),
        ];
        assert_eq!(render_transcript_markdown(&blocks), "> hello\n");
    }

    #[test]
    fn test_blocks_separated_by_blank_lines() {
        let mut tool = ToolLine::new("b3", "Ran ls");
        tool.status = ToolLineStatus::Completed;
        let blocks = [
            user_line("b1", "hello"),
            TranscriptBlock::from(Answer::new("b2", vec![Segment::new("hi there")])),
            TranscriptBlock::from(tool),
        ];
        assert_eq!(
            render_transcript_markdown(&blocks),
            "> hello\n\nhi there\n\n```\nRan ls\n```\n"
        );
    }

    #[test]
    fn test_empty_transcript_renders_empty_string() {
        assert_eq!(render_transcript_markdown([]), "");
    }

    #[test]
    fn test_export_filename_format() {
        assert_eq!(export_filename("a1b2c3", NOW), "a1b2c3-20260101-123456.md");
    }

    #[test]
    fn test_write_export_creates_root_and_returns_path() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let root = tmp.path().join("exports"); // does not exist yet
        let blocks = [user_line("b1", "hello")];
        let path = write_export(&blocks, "a1b2c3", NOW, &root).expect("write_export");
        assert_eq!(path, root.join("a1b2c3-20260101-123456.md"));
        assert_eq!(
            std::fs::read_to_string(&path).expect("read export"),
            "> hello\n"
        );
    }

    // ----------------------------------------------------------------------
    // secret scrubbing at the export sink (issue #23)
    // ----------------------------------------------------------------------

    const AWS_KEY: &str = "AKIAIOSFODNN7EXAMPLE";

    #[test]
    fn test_export_redacts_aws_key_in_answer() {
        let blocks = [TranscriptBlock::from(Answer::new(
            "b1",
            vec![Segment::new(format!("your key is {AWS_KEY} keep it"))],
        ))];
        let out = render_transcript_markdown(&blocks);
        assert!(!out.contains(AWS_KEY));
        assert_eq!(out, "your key is [REDACTED] keep it\n");
    }

    #[test]
    fn test_export_redacts_bearer_token_in_tool_body() {
        let mut tool = ToolLine::new("b1", "curl the API");
        tool.body = vec!["Authorization: Bearer sk-live-abcdef123456".to_string()];
        tool.status = ToolLineStatus::Completed;
        let blocks = [TranscriptBlock::from(tool)];
        let out = render_transcript_markdown(&blocks);
        assert!(!out.contains("sk-live-abcdef123456"));
        assert!(out.contains("Bearer [REDACTED]"));
    }

    #[test]
    fn test_export_redacts_secret_in_user_line() {
        let blocks = [user_line("b1", &format!("here: {AWS_KEY}"))];
        let out = render_transcript_markdown(&blocks);
        assert!(!out.contains(AWS_KEY));
        assert_eq!(out, "> here: [REDACTED]\n");
    }

    #[test]
    fn test_write_export_persists_redacted_markdown() {
        let tmp = tempfile::tempdir().expect("tempdir");
        let blocks = [TranscriptBlock::from(Answer::new(
            "b1",
            vec![Segment::new(format!("key {AWS_KEY}"))],
        ))];
        let path = write_export(&blocks, "a1b2c3", NOW, &tmp.path().join("exports"))
            .expect("write_export");
        let written = std::fs::read_to_string(&path).expect("read export");
        assert!(!written.contains(AWS_KEY));
        assert_eq!(written, "key [REDACTED]\n");
    }
}
