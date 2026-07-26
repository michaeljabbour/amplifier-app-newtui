//! `/copy` — extract the last assistant answer for the clipboard.
//!
//! Pure extraction only: the newest `clickable == true` [`Answer`]
//! (`clickable == false` marks answer-*shaped* recap/agent-tree lines that
//! are not real answers). Clipboard I/O (OSC 52) lives in the app-side
//! `copy_answer` action so this stays model-only.

use crate::model::blocks::TranscriptBlock;
use crate::model::redaction::scrub_text;

/// Span-joined text of the last real answer; `None` if there is none.
pub fn last_answer_text(blocks: &[TranscriptBlock]) -> Option<String> {
    for block in blocks.iter().rev() {
        if let TranscriptBlock::Answer(answer) = block {
            if answer.clickable {
                // Scrub before the text leaves for the clipboard (issue #23),
                // shared rules with the transcript/export/metadata sinks.
                let joined: String = answer
                    .spans
                    .iter()
                    .map(|segment| segment.text.as_str())
                    .collect();
                return Some(scrub_text(&joined));
            }
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::blocks::{Answer, Segment, ToolLine, ToolLineStatus, UserLine};

    fn segment_with_style(text: &str, style_token: crate::model::blocks::StyleToken) -> Segment {
        Segment {
            style_token,
            ..Segment::new(text)
        }
    }

    #[test]
    fn test_returns_span_joined_text_of_last_answer() {
        let blocks: Vec<TranscriptBlock> = vec![
            Answer::new("b1", vec![Segment::new("first answer")]).into(),
            Answer::new(
                "b2",
                vec![
                    Segment::new("The fix is in "),
                    segment_with_style("app.py", crate::model::blocks::StyleToken::Teal),
                    Segment::new(", shipped."),
                ],
            )
            .into(),
        ];
        assert_eq!(
            last_answer_text(&blocks).as_deref(),
            Some("The fix is in app.py, shipped.")
        );
    }

    #[test]
    fn test_skips_trailing_non_answer_blocks() {
        let blocks: Vec<TranscriptBlock> = vec![
            Answer::new("b1", vec![Segment::new("the real answer")]).into(),
            UserLine::new("b2", "thanks").into(),
            ToolLine {
                status: ToolLineStatus::Completed,
                ..ToolLine::new("b3", "Ran ls")
            }
            .into(),
        ];
        assert_eq!(last_answer_text(&blocks).as_deref(), Some("the real answer"));
    }

    #[test]
    fn test_skips_non_clickable_answers() {
        let blocks: Vec<TranscriptBlock> = vec![
            Answer::new("b1", vec![Segment::new("true answer")]).into(),
            Answer {
                clickable: false,
                ..Answer::new("b2", vec![Segment::new("recap-shaped line")])
            }
            .into(),
        ];
        assert_eq!(last_answer_text(&blocks).as_deref(), Some("true answer"));
    }

    #[test]
    fn test_returns_none_for_empty_blocks() {
        assert_eq!(last_answer_text(&[]), None);
    }

    #[test]
    fn test_returns_none_when_no_answers() {
        let blocks: Vec<TranscriptBlock> = vec![
            UserLine::new("b1", "hello").into(),
            ToolLine::new("b2", "Read blocks.py").into(),
        ];
        assert_eq!(last_answer_text(&blocks), None);
    }

    // ----------------------------------------------------------------------
    // secret scrubbing at the copy sink (issue #23)
    // ----------------------------------------------------------------------

    #[test]
    fn test_copy_redacts_aws_key_in_last_answer() {
        let blocks: Vec<TranscriptBlock> = vec![Answer::new(
            "b1",
            vec![
                Segment::new("use "),
                Segment::new("AKIAIOSFODNN7EXAMPLE"),
                Segment::new(" then done"),
            ],
        )
        .into()];
        let text = last_answer_text(&blocks).expect("text is not None");
        assert!(!text.contains("AKIAIOSFODNN7EXAMPLE"));
        assert_eq!(text, "use [REDACTED] then done");
    }

    #[test]
    fn test_copy_redacts_bearer_token() {
        let blocks: Vec<TranscriptBlock> = vec![Answer::new(
            "b1",
            vec![Segment::new("Authorization: Bearer abc123def456ghi")],
        )
        .into()];
        let text = last_answer_text(&blocks).expect("text is not None");
        assert!(!text.contains("abc123def456ghi"));
        assert_eq!(text, "Authorization: Bearer [REDACTED]");
    }
}
