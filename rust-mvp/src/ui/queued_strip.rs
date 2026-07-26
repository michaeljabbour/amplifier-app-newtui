//! Queued-message overlay strip (DESIGN-SPEC §2/§5) — port of `ui/queued_strip.py`.
//!
//! A one-line orange strip docked ABOVE the composer, shown while a full
//! next-turn message is queued (Shift+Enter while running, or a second
//! steer):
//!
//! `▹ queued next: "<text>" · runs when this turn ends`
//!
//! The strip is display-only: the `SteeringQueue` owns the state, the footer
//! shows the `· q1` badge, and the app clears the strip when the queued
//! message is picked up at turn end.
//!
//! Textual widget mechanics (`Static` subclass, `DEFAULT_CSS`, `update()`)
//! do not port; this is the pure show/clear state machine plus the exact
//! strip line, with a `display` flag for the app-assembly layer to honor
//! when laying out the strip above the composer (orange, top rule).

use crate::model::blocks::GLYPH_QUEUED;

/// Exact strip text: `▹ queued next: "<text>" · runs when this turn ends`.
pub fn queued_text(text: &str) -> String {
    format!("{GLYPH_QUEUED} queued next: \"{text}\" · runs when this turn ends")
}

/// The queued-next-message strip (orange, bordered, above composer).
#[derive(Debug, Clone, Default)]
pub struct QueuedStrip {
    queued: Option<String>,
    display: bool,
}

impl QueuedStrip {
    /// A new strip: hidden, nothing queued.
    pub fn new() -> Self {
        Self::default()
    }

    /// Whether the strip is currently displayed.
    pub fn display(&self) -> bool {
        self.display
    }

    /// The queued message text, or `None` when nothing is queued.
    pub fn queued(&self) -> Option<&str> {
        self.queued.as_deref()
    }

    /// The exact strip line currently displayed (empty when hidden).
    pub fn text(&self) -> String {
        match &self.queued {
            Some(queued) => queued_text(queued),
            None => String::new(),
        }
    }

    /// Show the strip for a queued next-turn message.
    pub fn show_queued(&mut self, text: &str) {
        self.queued = Some(text.to_string());
        self.display = true;
    }

    /// Hide the strip (queued message picked up or cancelled).
    pub fn clear_queued(&mut self) {
        self.queued = None;
        self.display = false;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // Pins tests/test_ui_rewind_queued.py::test_queued_text_exact_string
    #[test]
    fn test_queued_text_exact_string() {
        assert_eq!(
            queued_text("also update the changelog"),
            "▹ queued next: \"also update the changelog\" · runs when this turn ends"
        );
    }

    // Pins tests/test_ui_rewind_queued.py::test_hidden_until_message_queued
    // (app host / pilot mounting is Textual mechanics; the pinned semantics
    // are the fresh strip's display/queued/text state).
    #[test]
    fn test_hidden_until_message_queued() {
        let strip = QueuedStrip::new();
        assert!(!strip.display());
        assert!(strip.queued().is_none());
        assert_eq!(strip.text(), "");
    }

    // Pins tests/test_ui_rewind_queued.py::test_show_queued_displays_exact_line
    #[test]
    fn test_show_queued_displays_exact_line() {
        let mut strip = QueuedStrip::new();
        strip.show_queued("also update the changelog");
        assert!(strip.display());
        assert_eq!(strip.queued(), Some("also update the changelog"));
        assert_eq!(
            strip.text(),
            "▹ queued next: \"also update the changelog\" · runs when this turn ends"
        );
    }

    // Pins tests/test_ui_rewind_queued.py::test_clear_queued_hides_strip
    #[test]
    fn test_clear_queued_hides_strip() {
        let mut strip = QueuedStrip::new();
        strip.show_queued("ship it");
        strip.clear_queued();
        assert!(!strip.display());
        assert!(strip.queued().is_none());
        assert_eq!(strip.text(), "");
    }
}
