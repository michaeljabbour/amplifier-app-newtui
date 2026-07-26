//! Rewind picker overlay strip (DESIGN-SPEC §9, §2 overlay strips) — port of
//! `ui/rewind_strip.py`.
//!
//! A bordered orange strip docked ABOVE the composer, opened by ctrl-r /
//! `/rewind` / clicking a turn rule:
//!
//! `‹ rewind · pick a turn · turn N · $X.XX · <label> › [enter fork] [esc close]`
//!
//! - `‹` / `›` (click or `←`/`→`) navigate checkpoints, clamped at the ends
//!   (mockup `Math.max/Math.min` — no wrap-around).
//! - `enter fork` (chip, bright on bg-tab; Enter or click) emits
//!   [`RewindMsg::ForkRequested`] with the current checkpoint id — the app
//!   performs the actual session fork (confirm-then-trim, ADR-0007) and only
//!   then trims the transcript.
//! - `esc close` (dimmer; Esc or click) emits [`RewindMsg::Closed`].
//!
//! The strip hides itself after fork/close.
//!
//! Textual mechanics translated for ratatui: the widget's posted `Message`
//! classes become the [`RewindMsg`] enum returned by the action/click entry
//! points; `display`/focus are plain state the app-assembly layer reads when
//! laying out and routing keys. The Textual BINDINGS (`left`/`right`/`enter`,
//! esc via the app's ESC_CHAIN) map onto [`RewindStrip::action_prev`],
//! [`RewindStrip::action_next`], [`RewindStrip::action_fork`], and
//! [`RewindStrip::action_close`].

use crate::model::blocks::{GLYPH_REWIND_LEFT, GLYPH_REWIND_RIGHT};
use crate::model::turn::Checkpoint;

pub const FORK_HINT: &str = "enter fork";
pub const CLOSE_HINT: &str = "esc close";

/// `turn N · $X.XX · <label>` — the picker's checkpoint description.
///
/// The turn is spelled out (`turn 3`, not the cryptic `t3`) so the marker
/// reads legibly (S5 discoverability: users could not tell what `t3` meant).
pub fn rewind_label(checkpoint: &Checkpoint) -> String {
    // round_dp uses banker's rounding (MidpointNearestEven), matching the
    // default decimal context Python's `f"{Decimal:.2f}"` formats with;
    // `{:.2}` then only pads.
    format!(
        "turn {} · ${:.2} · {}",
        checkpoint.turn_id,
        checkpoint.cost_at.round_dp(2),
        checkpoint.label
    )
}

/// The strip's center text: `rewind · pick a turn · turn N · $X.XX · <label>`.
///
/// The `pick a turn` phrase turns the strip into a self-explaining header: it
/// names the feature (`rewind`), states the action, then shows the currently
/// selected turn — flanked by the ‹ › nav glyphs and the `enter fork` /
/// `esc close` chips the strip composes alongside.
pub fn rewind_line(checkpoint: &Checkpoint) -> String {
    format!("rewind · pick a turn · {}", rewind_label(checkpoint))
}

/// What the Textual widget posted as `RewindStrip.ForkRequested` /
/// `.Closed` / `.TypeThrough` messages; the ratatui app-assembly layer
/// receives these as return values from the strip's entry points.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum RewindMsg {
    /// The user asked to fork from a checkpoint (Enter / chip click).
    ForkRequested { checkpoint_id: String },
    /// Esc pressed / `esc close` clicked.
    Closed,
    /// A printable key pressed while the strip held focus.
    ///
    /// Mockup ground truth (document-level keydown, composer input keeps
    /// focus while `rewindOpen`): typing is never swallowed by the rewind
    /// picker — the app forwards the character to the composer, so `/`
    /// opens the palette live-filtered and the text lands in the input
    /// (spec §5).
    TypeThrough { character: String },
}

/// The rewind picker strip (DESIGN-SPEC §9).
///
/// Open with [`RewindStrip::show_checkpoints`] (defaults to the newest
/// checkpoint, or the clicked rule's). Emits:
///
/// - [`RewindMsg::ForkRequested`] — Enter / `enter fork` chip click.
/// - [`RewindMsg::Closed`] — Esc / `esc close` click.
#[derive(Clone, Debug, Default)]
pub struct RewindStrip {
    checkpoints: Vec<Checkpoint>,
    index: usize,
    display: bool,
}

impl RewindStrip {
    pub fn new() -> Self {
        Self::default()
    }

    // -- public API ----------------------------------------------------

    pub fn checkpoints(&self) -> &[Checkpoint] {
        &self.checkpoints
    }

    pub fn index(&self) -> usize {
        self.index
    }

    /// Whether the strip is visible (Textual `display`).
    pub fn display(&self) -> bool {
        self.display
    }

    pub fn current(&self) -> Option<&Checkpoint> {
        self.checkpoints.get(self.index)
    }

    /// The exact center text currently displayed.
    pub fn label_text(&self) -> String {
        match self.current() {
            Some(current) => rewind_line(current),
            None => String::new(),
        }
    }

    /// The strip's five children in compose order, as `(id, text)` pairs —
    /// what Textual mounted as Statics (`‹`, label, `›`, chips). The
    /// app-assembly layer styles them per DEFAULT_CSS (orange strip, bright
    /// fork chip on bg-tab, dimmer close hint).
    pub fn segments(&self) -> [(&'static str, String); 5] {
        [
            ("rewind-prev", GLYPH_REWIND_LEFT.to_string()),
            ("rewind-label", self.label_text()),
            ("rewind-next", GLYPH_REWIND_RIGHT.to_string()),
            ("rewind-fork", FORK_HINT.to_string()),
            ("rewind-close", CLOSE_HINT.to_string()),
        ]
    }

    /// Open the picker on `checkpoints` (newest selected by default).
    ///
    /// An empty checkpoint list keeps the strip hidden — the app shows the
    /// `no rewind checkpoints yet` notice instead.
    pub fn show_checkpoints(&mut self, checkpoints: &[Checkpoint], index: Option<usize>) {
        self.checkpoints = checkpoints.to_vec();
        if self.checkpoints.is_empty() {
            self.display = false;
            return;
        }
        let last = self.checkpoints.len() - 1;
        self.index = match index {
            None => last,
            Some(index) => index.min(last),
        };
        self.display = true;
        // Textual `self.focus()` is app-assembly wiring in ratatui.
    }

    /// Refresh the open picker's list in place (mockup openRewind /
    /// rewindNext read the live `this.checkpoints` array — a checkpoint cut
    /// while the picker is open is immediately navigable with ›).
    ///
    /// The cursor position is preserved (clamped); focus is untouched.
    pub fn sync_checkpoints(&mut self, checkpoints: &[Checkpoint]) {
        if !self.display {
            return;
        }
        self.checkpoints = checkpoints.to_vec();
        if self.checkpoints.is_empty() {
            self.display = false;
            return;
        }
        self.index = self.index.min(self.checkpoints.len() - 1);
    }

    /// Move the checkpoint cursor by `delta`, clamped at both ends.
    pub fn nav(&mut self, delta: i64) {
        if self.checkpoints.is_empty() {
            return;
        }
        let last = (self.checkpoints.len() - 1) as i64;
        self.index = (self.index as i64 + delta).clamp(0, last) as usize;
    }

    /// Request the fork for the current checkpoint and close the strip.
    pub fn fork(&mut self) -> Option<RewindMsg> {
        let checkpoint_id = self.current()?.id.clone();
        self.display = false;
        Some(RewindMsg::ForkRequested { checkpoint_id })
    }

    pub fn close_strip(&mut self) -> RewindMsg {
        self.display = false;
        RewindMsg::Closed
    }

    // -- key actions ----------------------------------------------------

    /// Printable keys pass through to the composer (mockup: the composer
    /// keeps typing rights while `rewindOpen`); ←→/enter stay with the strip
    /// via its bindings, esc bubbles to the app's ESC_CHAIN.
    pub fn on_printable_key(&self, character: &str) -> RewindMsg {
        RewindMsg::TypeThrough {
            character: character.to_string(),
        }
    }

    pub fn action_prev(&mut self) {
        self.nav(-1);
    }

    pub fn action_next(&mut self) {
        self.nav(1);
    }

    pub fn action_fork(&mut self) -> Option<RewindMsg> {
        self.fork()
    }

    pub fn action_close(&mut self) -> RewindMsg {
        self.close_strip()
    }

    // -- clicks ----------------------------------------------------------

    /// Dispatch a click on one of the strip's children by id (the Textual
    /// `on_click` widget-id table); unknown ids are ignored.
    pub fn on_click(&mut self, widget_id: &str) -> Option<RewindMsg> {
        match widget_id {
            "rewind-prev" => {
                self.nav(-1);
                None
            }
            "rewind-next" => {
                self.nav(1);
                None
            }
            "rewind-fork" => self.fork(),
            "rewind-close" => Some(self.close_strip()),
            _ => None,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use rust_decimal::Decimal;
    use std::str::FromStr;

    fn checkpoints() -> Vec<Checkpoint> {
        vec![
            Checkpoint {
                id: "t1".to_string(),
                turn_id: 1,
                message_index: 4,
                cost_at: Decimal::from_str("0.18").unwrap(),
                label: "store refactor · shipped".to_string(),
            },
            Checkpoint {
                id: "t2".to_string(),
                turn_id: 2,
                message_index: 9,
                cost_at: Decimal::from_str("0.47").unwrap(),
                label: "auto run · shipped locally".to_string(),
            },
            Checkpoint {
                id: "t3".to_string(),
                turn_id: 3,
                message_index: 13,
                cost_at: Decimal::from_str("1.12").unwrap(),
                label: "plan ready".to_string(),
            },
        ]
    }

    // -- pure formatting -----------------------------------------------

    #[test]
    fn test_rewind_label_exact_string() {
        let cps = checkpoints();
        assert_eq!(rewind_label(&cps[2]), "turn 3 · $1.12 · plan ready");
        assert_eq!(
            rewind_line(&cps[0]),
            "rewind · pick a turn · turn 1 · $0.18 · store refactor · shipped"
        );
    }

    #[test]
    fn test_hint_strings() {
        assert_eq!(FORK_HINT, "enter fork");
        assert_eq!(CLOSE_HINT, "esc close");
    }

    // -- widget behavior -------------------------------------------------

    #[test]
    fn test_opens_on_newest_checkpoint_by_default() {
        let mut strip = RewindStrip::new();
        assert!(!strip.display());
        strip.show_checkpoints(&checkpoints(), None);
        assert!(strip.display());
        assert_eq!(strip.index(), 2);
        assert_eq!(
            strip.label_text(),
            "rewind · pick a turn · turn 3 · $1.12 · plan ready"
        );
    }

    #[test]
    fn test_opens_at_clicked_rule_checkpoint() {
        let mut strip = RewindStrip::new();
        strip.show_checkpoints(&checkpoints(), Some(0));
        assert_eq!(
            strip.label_text(),
            "rewind · pick a turn · turn 1 · $0.18 · store refactor · shipped"
        );
    }

    #[test]
    fn test_arrow_navigation_is_clamped() {
        let mut strip = RewindStrip::new();
        strip.show_checkpoints(&checkpoints(), None);
        strip.action_prev();
        strip.action_prev();
        assert_eq!(strip.index(), 0);
        strip.action_prev(); // clamped at the oldest
        assert_eq!(strip.index(), 0);
        // clamped at newest
        strip.action_next();
        strip.action_next();
        strip.action_next();
        strip.action_next();
        assert_eq!(strip.index(), 2);
        assert_eq!(
            strip.label_text(),
            "rewind · pick a turn · turn 3 · $1.12 · plan ready"
        );
    }

    #[test]
    fn test_enter_requests_fork_for_current_checkpoint_and_closes() {
        let mut strip = RewindStrip::new();
        strip.show_checkpoints(&checkpoints(), None);
        strip.action_prev();
        let msg = strip.action_fork();
        assert_eq!(
            msg,
            Some(RewindMsg::ForkRequested {
                checkpoint_id: "t2".to_string()
            })
        );
        assert!(!strip.display());
    }

    #[test]
    fn test_close_action_posts_closed_and_hides() {
        // Esc is resolved by the app via keymap.ESC_CHAIN (spec §5) — the
        // strip has no local escape binding; the chain invokes `action_close`.
        let mut strip = RewindStrip::new();
        strip.show_checkpoints(&checkpoints(), None);
        let msg = strip.action_close();
        assert_eq!(msg, RewindMsg::Closed);
        assert!(!strip.display());
    }

    #[test]
    fn test_click_glyphs_navigate_and_fork_chip_forks() {
        let mut strip = RewindStrip::new();
        strip.show_checkpoints(&checkpoints(), None);
        assert_eq!(strip.on_click("rewind-prev"), None);
        assert_eq!(strip.index(), 1);
        assert_eq!(strip.on_click("rewind-next"), None);
        assert_eq!(strip.index(), 2);
        assert_eq!(
            strip.on_click("rewind-fork"),
            Some(RewindMsg::ForkRequested {
                checkpoint_id: "t3".to_string()
            })
        );
        assert!(!strip.display());
    }

    #[test]
    fn test_empty_checkpoints_keep_strip_hidden() {
        let mut strip = RewindStrip::new();
        strip.show_checkpoints(&[], None);
        assert!(!strip.display());
    }

    // -- non-pinned behavior kept faithful to the Python source ----------

    #[test]
    fn test_close_click_posts_closed() {
        let mut strip = RewindStrip::new();
        strip.show_checkpoints(&checkpoints(), None);
        assert_eq!(strip.on_click("rewind-close"), Some(RewindMsg::Closed));
        assert!(!strip.display());
    }

    #[test]
    fn test_printable_key_types_through() {
        let strip = RewindStrip::new();
        assert_eq!(
            strip.on_printable_key("/"),
            RewindMsg::TypeThrough {
                character: "/".to_string()
            }
        );
    }

    #[test]
    fn test_sync_checkpoints_clamps_cursor_and_hides_on_empty() {
        let cps = checkpoints();
        let mut strip = RewindStrip::new();
        // Ignored while hidden.
        strip.sync_checkpoints(&cps);
        assert!(strip.checkpoints().is_empty());
        strip.show_checkpoints(&cps, None);
        assert_eq!(strip.index(), 2);
        strip.sync_checkpoints(&cps[..1]);
        assert!(strip.display());
        assert_eq!(strip.index(), 0);
        strip.sync_checkpoints(&[]);
        assert!(!strip.display());
    }

    #[test]
    fn test_segments_compose_order_and_glyphs() {
        let mut strip = RewindStrip::new();
        strip.show_checkpoints(&checkpoints(), None);
        let segments = strip.segments();
        assert_eq!(segments[0], ("rewind-prev", "‹".to_string()));
        assert_eq!(segments[1].0, "rewind-label");
        assert_eq!(
            segments[1].1,
            "rewind · pick a turn · turn 3 · $1.12 · plan ready"
        );
        assert_eq!(segments[2], ("rewind-next", "›".to_string()));
        assert_eq!(segments[3], ("rewind-fork", FORK_HINT.to_string()));
        assert_eq!(segments[4], ("rewind-close", CLOSE_HINT.to_string()));
    }
}
