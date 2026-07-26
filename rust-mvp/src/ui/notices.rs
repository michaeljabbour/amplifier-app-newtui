//! Transient notice slot (DESIGN-SPEC §2 item 3) — port of `ui/notices.py`.
//!
//! A single-slot, dim text line floating at the transcript's bottom edge:
//! `mode plan · read-only`, `steer queued · shift+enter queues a full
//! next-turn message`, `approval required · choose below the transcript`, …
//! Auto-dismisses after ~4 seconds (callers may pass a longer per-notice
//! duration, mirroring the mockup's `showNotice(text, ms = 4000)`); showing
//! a new notice replaces the current one and restarts the clock.
//!
//! Textual adaptation: the Python widget is a `Static` on its own compositor
//! layer with CSS positioning and a `Timer`; none of that ports. What ports
//! is the pure single-slot state machine: text, visibility, and the
//! auto-dismiss deadline. Instead of a Textual timer callback, the slot
//! records a deadline against an injected monotonic clock and the host event
//! loop calls [`NoticeSlot::tick`] each frame to expire it. The app-assembly
//! layer is responsible for the rendering the Python CSS declared: dim
//! foreground on the terminal background, one cell of horizontal padding,
//! right-aligned over the transcript's last row without consuming a layout
//! row. Notice text stays literal — the Python widget rendered it via
//! `Content.from_markup("$text", text=text)` precisely so arbitrary text is
//! never parsed as markup.

use std::sync::OnceLock;
use std::time::Instant;

/// Seconds a notice stays visible (DESIGN-SPEC §2: auto-dismiss ~4s).
pub const NOTICE_DURATION: f64 = 4.0;

type Clock = Box<dyn Fn() -> f64 + Send + Sync>;

/// Monotonic clock in fractional seconds (Python's `time.monotonic`),
/// anchored at first use within this process.
fn monotonic() -> f64 {
    static START: OnceLock<Instant> = OnceLock::new();
    START.get_or_init(Instant::now).elapsed().as_secs_f64()
}

/// The one-and-only notice line.
///
/// Owns only its own text/visibility/deadline; placement and styling belong
/// to the draw layer (the Python widget likewise "only manages its own
/// text/visibility/timer").
pub struct NoticeSlot {
    duration: f64,
    current: Option<String>,
    /// Clock reading at which the visible notice auto-dismisses.
    deadline: Option<f64>,
    clock: Clock,
}

impl Default for NoticeSlot {
    fn default() -> Self {
        Self::new()
    }
}

impl NoticeSlot {
    /// Slot with the spec default duration and the process monotonic clock.
    pub fn new() -> Self {
        Self::with_duration(NOTICE_DURATION)
    }

    /// Slot with a custom default duration (Python `NoticeSlot(duration=…)`).
    pub fn with_duration(duration: f64) -> Self {
        Self {
            duration,
            current: None,
            deadline: None,
            clock: Box::new(monotonic),
        }
    }

    /// Replace the clock (tests inject a fake; mirrors `queues.rs`).
    pub fn with_clock(mut self, clock: impl Fn() -> f64 + Send + Sync + 'static) -> Self {
        self.clock = Box::new(clock);
        self
    }

    /// The visible notice text, or `None` when the slot is empty.
    pub fn current(&self) -> Option<&str> {
        self.current.as_deref()
    }

    /// Whether the slot should be drawn (Python's `-visible` class).
    pub fn is_visible(&self) -> bool {
        self.current.is_some()
    }

    /// Show `text`, replacing any current notice and restarting the clock.
    ///
    /// `duration` overrides the slot default for this notice only (mockup
    /// `showNotice(text, ms = 4000)`; approval notices pass 6s).
    pub fn show_notice(&mut self, text: &str, duration: Option<f64>) {
        self.current = Some(text.to_string());
        self.deadline = Some((self.clock)() + duration.unwrap_or(self.duration));
    }

    /// Clear the slot immediately.
    pub fn dismiss_notice(&mut self) {
        self.current = None;
        self.deadline = None;
    }

    /// Expire the notice once its deadline passes (stands in for the Textual
    /// timer firing). Call each frame; returns `true` when the slot changed
    /// and the host should redraw.
    pub fn tick(&mut self) -> bool {
        match self.deadline {
            Some(deadline) if (self.clock)() >= deadline => {
                self.dismiss_notice();
                true
            }
            _ => false,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::sync::Arc;

    /// Fake clock in milliseconds shared with the slot under test.
    fn slot_with_fake_clock(duration: f64) -> (NoticeSlot, Arc<AtomicU64>) {
        let now_ms = Arc::new(AtomicU64::new(0));
        let clock_ms = Arc::clone(&now_ms);
        let slot = NoticeSlot::with_duration(duration)
            .with_clock(move || clock_ms.load(Ordering::SeqCst) as f64 / 1000.0);
        (slot, now_ms)
    }

    // Pins tests/test_ui_chrome.py::test_notice_shows_and_auto_dismisses
    // (test app uses NoticeSlot(duration=0.05)).
    #[test]
    fn test_notice_shows_and_auto_dismisses() {
        let (mut slot, now_ms) = slot_with_fake_clock(0.05);
        slot.show_notice("mode plan · read-only", None);
        now_ms.store(10, Ordering::SeqCst); // stay well inside the 0.05s TTL
        assert!(!slot.tick());
        assert_eq!(slot.current(), Some("mode plan · read-only"));
        assert!(slot.is_visible());
        now_ms.store(300, Ordering::SeqCst); // duration is 0.05s here
        assert!(slot.tick());
        assert_eq!(slot.current(), None);
        assert!(!slot.is_visible());
    }

    // Pins tests/test_ui_chrome.py::test_notice_is_single_slot_and_replaces
    #[test]
    fn test_notice_is_single_slot_and_replaces() {
        let (mut slot, now_ms) = slot_with_fake_clock(0.05);
        slot.show_notice("first", None);
        slot.show_notice(
            "steer queued · shift+enter queues a full next-turn message",
            None,
        );
        now_ms.store(10, Ordering::SeqCst); // stay well inside the 0.05s TTL
        slot.tick();
        assert_eq!(
            slot.current(),
            Some("steer queued · shift+enter queues a full next-turn message")
        );
    }

    // Pins tests/test_ui_chrome.py::test_notice_per_call_duration_overrides_default
    // "Mockup showNotice(text, ms): approval notices pass 6000 over the 4000 default."
    #[test]
    fn test_notice_per_call_duration_overrides_default() {
        let (mut slot, now_ms) = slot_with_fake_clock(0.05);
        slot.show_notice("approval required · choose below the transcript", Some(0.4));
        now_ms.store(200, Ordering::SeqCst); // past the 0.05s default, before the override
        slot.tick();
        assert_eq!(
            slot.current(),
            Some("approval required · choose below the transcript")
        );
        now_ms.store(600, Ordering::SeqCst);
        slot.tick();
        assert_eq!(slot.current(), None);
    }

    // Pins tests/test_ui_chrome.py::test_notice_manual_dismiss
    #[test]
    fn test_notice_manual_dismiss() {
        let (mut slot, now_ms) = slot_with_fake_clock(0.05);
        slot.show_notice("approval required · choose below the transcript", None);
        now_ms.store(1, Ordering::SeqCst);
        slot.tick();
        slot.dismiss_notice();
        assert_eq!(slot.current(), None);
        assert!(!slot.is_visible());
    }

    // Source behavior: docstring — "showing a new notice replaces the current
    // one and restarts the clock" (the Python stops the old Timer and starts a
    // fresh one).
    #[test]
    fn replacing_a_notice_restarts_the_clock() {
        let (mut slot, now_ms) = slot_with_fake_clock(0.05);
        slot.show_notice("first", None);
        now_ms.store(40, Ordering::SeqCst); // 0.04s in: first would die at 0.05s
        slot.show_notice("second", None);
        now_ms.store(60, Ordering::SeqCst); // past first's deadline, inside second's
        assert!(!slot.tick());
        assert_eq!(slot.current(), Some("second"));
        now_ms.store(90, Ordering::SeqCst); // 0.04 + 0.05 = 0.09s: second expires
        assert!(slot.tick());
        assert_eq!(slot.current(), None);
    }

    // Source behavior: a per-call override applies to that notice only; the
    // slot default is unchanged for the next notice.
    #[test]
    fn per_call_duration_does_not_change_the_default() {
        let (mut slot, now_ms) = slot_with_fake_clock(0.05);
        slot.show_notice("long one", Some(0.4));
        slot.dismiss_notice();
        slot.show_notice("back to default", None);
        now_ms.store(60, Ordering::SeqCst); // past the 0.05s default again
        assert!(slot.tick());
        assert_eq!(slot.current(), None);
    }

    // Source behavior: NOTICE_DURATION is 4.0s and is the constructor default
    // (Python `NoticeSlot()` with no duration argument).
    #[test]
    fn default_duration_is_notice_duration() {
        assert_eq!(NOTICE_DURATION, 4.0);
        let now_ms = Arc::new(AtomicU64::new(0));
        let clock_ms = Arc::clone(&now_ms);
        let mut slot =
            NoticeSlot::new().with_clock(move || clock_ms.load(Ordering::SeqCst) as f64 / 1000.0);
        slot.show_notice("mode plan · read-only", None);
        now_ms.store(3_999, Ordering::SeqCst);
        assert!(!slot.tick());
        assert!(slot.is_visible());
        now_ms.store(4_000, Ordering::SeqCst);
        assert!(slot.tick());
        assert!(!slot.is_visible());
    }

    // Source behavior: notice text is kept literal (the Python renders via
    // Content.from_markup("$text", text=text) substitution so markup-looking
    // text is never parsed).
    #[test]
    fn notice_text_stays_literal() {
        let (mut slot, _now_ms) = slot_with_fake_clock(0.05);
        slot.show_notice("[bold]not markup[/bold] · 100% literal", None);
        assert_eq!(
            slot.current(),
            Some("[bold]not markup[/bold] · 100% literal")
        );
    }

    // Source behavior: an empty slot's tick is a no-op (no timer to fire).
    #[test]
    fn tick_on_empty_slot_is_noop() {
        let (mut slot, now_ms) = slot_with_fake_clock(0.05);
        now_ms.store(10_000, Ordering::SeqCst);
        assert!(!slot.tick());
        assert_eq!(slot.current(), None);
        assert!(!slot.is_visible());
    }
}
