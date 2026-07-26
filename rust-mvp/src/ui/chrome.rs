//! Title bar chrome (DESIGN-SPEC §2 item 1).
//!
//! Centered title `amplifier-app-newtui — Amplifier — <state> — <bundle> —
//! <session-short>` on the `bg-chrome` background. While a turn is running
//! the title is prefixed with an orange spinner glyph cycling `✳ ✦ ✧ ✦`
//! every ~260ms (the app drives the tick; see [`SPINNER_INTERVAL`]).
//!
//! The `<state>` text is owned by the app: it reflects the current plan step
//! (lowercased) or `ready` / `planning` / `brainstorming` /
//! `✳ coordinating N agents` — the title bar only displays it.
//!
//! Port of `src/amplifier_app_newtui/ui/chrome.py`. This is the pure logic:
//! text assembly, spinner frame state, OSC-0 sanitization, and change
//! notification. Textual widget mechanics (mount/unmount, `set_interval`
//! timers, reactive watchers, CSS) do not port — the app-assembly layer must
//! call [`TitleBar::advance_spinner`] on a ~260ms cadence while running and
//! forward [`TitleChanged`] notifications to the native terminal via
//! [`write_terminal_title`].

use crate::model::blocks::{StyleToken, GLYPH_SPINNER_FRAMES};

pub const TITLE_SEPARATOR: &str = " — ";

/// Seconds between spinner frames (~260ms per DESIGN-SPEC §2).
pub const SPINNER_INTERVAL: f64 = 0.26;

/// Unmistakable terminal-window spinner; the in-app chrome keeps its stars.
pub const TERMINAL_SPINNER_FRAMES: [&str; 10] =
    ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];

/// Keep macOS terminal tabs useful when a plan step has a long title.
pub const TERMINAL_TITLE_MAX_CHARS: usize = 180;

pub const APP_TITLE_NAME: &str = "amplifier-app-newtui";
pub const PRODUCT_NAME: &str = "Amplifier";

/// True for Unicode general category `Cc` (C0 controls, DEL, C1 controls) —
/// the exact set `unicodedata.category(ch) == "Cc"` matches in the Python.
fn is_control(character: char) -> bool {
    matches!(character, '\u{0000}'..='\u{001f}' | '\u{007f}'..='\u{009f}')
}

/// Build a safe OSC 0 sequence for a native terminal window/tab title.
///
/// Bundle names and plan steps can come from runtime data, so control
/// characters must never reach the OSC payload. Whitespace is collapsed and
/// the result is bounded so a verbose step does not take over the tab bar.
pub fn terminal_title_sequence(title: &str) -> String {
    let without_controls: String = title
        .chars()
        .map(|character| if is_control(character) { ' ' } else { character })
        .collect();
    let safe_title: String = without_controls
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .chars()
        .take(TERMINAL_TITLE_MAX_CHARS)
        .collect();
    format!("\x1b]0;{safe_title}\x07")
}

/// The sink the app writes native terminal titles through — the pure shape of
/// Textual's `Driver` (`is_headless` / `is_web` / `write` / `flush`).
pub trait TerminalTitleDriver {
    fn is_headless(&self) -> bool;
    fn is_web(&self) -> bool;
    fn write(&mut self, data: &str);
    fn flush(&mut self);
}

/// Write `title` to native terminal chrome when a terminal is present.
pub fn write_terminal_title(driver: Option<&mut dyn TerminalTitleDriver>, title: &str) -> bool {
    let Some(driver) = driver else {
        return false;
    };
    if driver.is_headless() || driver.is_web() {
        return false;
    }
    driver.write(&terminal_title_sequence(title));
    driver.flush();
    true
}

/// The rendered title changed, including an active spinner frame.
///
/// Python posts this as a Textual `Message`; here setters and
/// [`TitleBar::advance_spinner`] return it (deduped on the terminal title,
/// exactly like `_repaint`) for the app loop to forward.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TitleChanged {
    pub title: String,
    pub terminal_title: String,
}

/// The top chrome strip.
///
/// State API (the app sets fields via the setters, which return the
/// [`TitleChanged`] notification when the emitted terminal title changed):
///
/// - `state_text`: the `<state>` fragment (`ready`, a plan step, …).
/// - `bundle` / `session_short`: identity fragments (skipped when empty).
/// - `running`: true while a turn executes — the app must then tick
///   [`TitleBar::advance_spinner`] every [`SPINNER_INTERVAL`] seconds.
#[derive(Debug, Clone)]
pub struct TitleBar {
    state_text: String,
    bundle: String,
    session_short: String,
    running: bool,
    frame_index: usize,
    last_emitted_title: String,
}

impl Default for TitleBar {
    fn default() -> Self {
        Self {
            state_text: "ready".to_string(),
            bundle: String::new(),
            session_short: String::new(),
            running: false,
            frame_index: 0,
            last_emitted_title: String::new(),
        }
    }
}

impl TitleBar {
    pub fn new() -> Self {
        Self::default()
    }

    // -- state accessors -----------------------------------------------------

    pub fn state_text(&self) -> &str {
        &self.state_text
    }

    pub fn bundle(&self) -> &str {
        &self.bundle
    }

    pub fn session_short(&self) -> &str {
        &self.session_short
    }

    pub fn running(&self) -> bool {
        self.running
    }

    // -- reactive-watcher equivalents ----------------------------------------

    pub fn set_state_text(&mut self, value: impl Into<String>) -> Option<TitleChanged> {
        self.state_text = value.into();
        self.repaint()
    }

    pub fn set_bundle(&mut self, value: impl Into<String>) -> Option<TitleChanged> {
        self.bundle = value.into();
        self.repaint()
    }

    pub fn set_session_short(&mut self, value: impl Into<String>) -> Option<TitleChanged> {
        self.session_short = value.into();
        self.repaint()
    }

    /// `watch_running` — both edges reset the spinner to frame 0. Starting and
    /// stopping the ~260ms tick itself is the app loop's job.
    pub fn set_running(&mut self, running: bool) -> Option<TitleChanged> {
        self.running = running;
        self.frame_index = 0;
        self.repaint()
    }

    // -- text assembly -------------------------------------------------------

    /// The current spinner frame (`✳`/`✦`/`✧`/`✦`).
    pub fn spinner_glyph(&self) -> &'static str {
        GLYPH_SPINNER_FRAMES[self.frame_index % GLYPH_SPINNER_FRAMES.len()]
    }

    /// The current high-motion braille frame for native terminal chrome.
    pub fn terminal_spinner_glyph(&self) -> &'static str {
        TERMINAL_SPINNER_FRAMES[self.frame_index % TERMINAL_SPINNER_FRAMES.len()]
    }

    /// Plain rendered title, spinner prefix included while running.
    pub fn title_text(&self) -> String {
        let title = self.plain_title();
        if self.running {
            format!("{} {}", self.spinner_glyph(), title)
        } else {
            title
        }
    }

    /// Native terminal title with a visibly rotating braille spinner.
    pub fn terminal_title_text(&self) -> String {
        let title = self.plain_title();
        if self.running {
            format!("{} {}", self.terminal_spinner_glyph(), title)
        } else {
            title
        }
    }

    /// Styled fragments of the rendered title line — the pure equivalent of
    /// `Content.from_markup("[bold $orange]$glyph[/] $title", …)`: while
    /// running the spinner glyph carries the `orange` token (rendered bold);
    /// the rest uses the bar's own style (`$title-fg` bold on `$bg-chrome`,
    /// wired by the app-assembly layer).
    pub fn title_spans(&self) -> Vec<(String, Option<StyleToken>)> {
        if self.running {
            vec![
                (self.spinner_glyph().to_string(), Some(StyleToken::Orange)),
                (format!(" {}", self.plain_title()), None),
            ]
        } else {
            vec![(self.title_text(), None)]
        }
    }

    fn plain_title(&self) -> String {
        let mut parts: Vec<&str> = vec![APP_TITLE_NAME, PRODUCT_NAME, &self.state_text];
        if !self.bundle.is_empty() {
            parts.push(&self.bundle);
        }
        if !self.session_short.is_empty() {
            parts.push(&self.session_short);
        }
        parts.join(TITLE_SEPARATOR)
    }

    /// Step to the next spinner frame (the app's ~260ms timer callback).
    pub fn advance_spinner(&mut self) -> Option<TitleChanged> {
        self.frame_index += 1;
        self.repaint()
    }

    /// `_repaint`'s notification half: emit [`TitleChanged`] only when the
    /// terminal title actually differs from the last one emitted.
    fn repaint(&mut self) -> Option<TitleChanged> {
        let title = self.title_text();
        let terminal_title = self.terminal_title_text();
        if terminal_title != self.last_emitted_title {
            self.last_emitted_title = terminal_title.clone();
            Some(TitleChanged {
                title,
                terminal_title,
            })
        } else {
            None
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // -- title text (pinned from tests/test_ui_chrome.py) ---------------------

    #[test]
    fn test_idle_title_exact_format() {
        let mut bar = TitleBar::new();
        bar.set_state_text("ready");
        bar.set_bundle("dev-bundle");
        bar.set_session_short("a1b2c3");
        assert_eq!(
            bar.title_text(),
            "amplifier-app-newtui — Amplifier — ready — dev-bundle — a1b2c3"
        );
    }

    #[test]
    fn test_empty_identity_fragments_are_skipped() {
        let mut bar = TitleBar::new();
        bar.set_state_text("planning");
        assert_eq!(bar.title_text(), "amplifier-app-newtui — Amplifier — planning");
    }

    #[test]
    fn test_running_title_prefixes_spinner_and_cycles_frames() {
        let mut bar = TitleBar::new();
        bar.set_running(true);
        bar.set_state_text("ready");
        assert!(bar.title_text().starts_with("✳ "));
        let mut seen = vec![bar.spinner_glyph()];
        for _ in 0..3 {
            bar.advance_spinner();
            seen.push(bar.spinner_glyph());
        }
        assert_eq!(seen, ["✳", "✦", "✧", "✦"]);
    }

    #[test]
    fn test_native_terminal_title_uses_obvious_braille_spinner() {
        let mut bar = TitleBar::new();
        bar.set_running(true);
        let first = bar.terminal_title_text();
        assert!(first.starts_with(&format!("{} ", TERMINAL_SPINNER_FRAMES[0])));
        bar.advance_spinner();
        assert!(bar
            .terminal_title_text()
            .starts_with(&format!("{} ", TERMINAL_SPINNER_FRAMES[1])));
        assert_ne!(bar.terminal_title_text(), first);
    }

    #[test]
    fn test_spinner_interval_is_260ms() {
        assert!((SPINNER_INTERVAL - 0.26).abs() < 1e-12);
    }

    #[test]
    fn test_app_name_constant() {
        assert_eq!(APP_TITLE_NAME, "amplifier-app-newtui");
    }

    #[test]
    fn test_terminal_title_sequence_sanitizes_controls_and_bounds_length() {
        let sequence =
            terminal_title_sequence(&format!("✳ working\x1b]0;spoof\x07\n{}", "x".repeat(300)));
        assert!(sequence.starts_with("\x1b]0;✳ working ]0;spoof x"));
        assert!(sequence.ends_with('\x07'));
        let payload = sequence
            .strip_prefix("\x1b]0;")
            .unwrap()
            .strip_suffix('\x07')
            .unwrap();
        assert!(!payload.contains('\x1b'));
        assert!(!payload.contains('\x07'));
        assert!(!payload.contains('\n'));
        assert_eq!(payload.chars().count(), TERMINAL_TITLE_MAX_CHARS);
    }

    #[test]
    fn test_terminal_title_write_uses_osc_and_flushes() {
        #[derive(Default)]
        struct RecordingDriver {
            writes: Vec<String>,
            flushes: usize,
        }

        impl TerminalTitleDriver for RecordingDriver {
            fn is_headless(&self) -> bool {
                false
            }
            fn is_web(&self) -> bool {
                false
            }
            fn write(&mut self, data: &str) {
                self.writes.push(data.to_string());
            }
            fn flush(&mut self) {
                self.flushes += 1;
            }
        }

        let mut driver = RecordingDriver::default();
        assert!(write_terminal_title(Some(&mut driver), "✦ amplifier-app-newtui"));
        assert_eq!(driver.writes, ["\x1b]0;✦ amplifier-app-newtui\x07"]);
        assert_eq!(driver.flushes, 1);
        assert!(!write_terminal_title(None, "anything"));
    }

    // -- pure halves of the Pilot cases ---------------------------------------

    /// Pins the non-timer assertions of the Python Pilot test of the same
    /// name: the running toggle resets the frame, `advance_spinner` (the
    /// timer callback) rotates the glyph, and stopping strips the prefix.
    #[test]
    fn test_title_bar_spinner_runs_only_while_running() {
        let mut bar = TitleBar::new();
        bar.set_state_text("ready");
        bar.set_bundle("dev");
        bar.set_session_short("a1b2c3");
        assert_eq!(
            bar.title_text(),
            "amplifier-app-newtui — Amplifier — ready — dev — a1b2c3"
        );

        bar.set_running(true);
        let first = bar.spinner_glyph();
        assert_eq!(first, "✳");
        bar.advance_spinner(); // stands in for the ~260ms Textual timer tick
        assert_ne!(bar.spinner_glyph(), first);

        bar.set_running(false);
        let title = bar.title_text();
        assert!(!title.starts_with('✳') && !title.starts_with('✦') && !title.starts_with('✧'));
    }

    /// Pins the render-content assertion of the Python Pilot test of the
    /// same name (the reactive repaint plumbing itself does not port).
    #[test]
    fn test_title_state_text_updates_render() {
        let mut bar = TitleBar::new();
        bar.set_state_text("✳ coordinating 3 agents");
        assert!(bar.title_text().contains("coordinating 3 agents"));
    }

    // -- Rust-side additions ---------------------------------------------------

    /// `_repaint` only posts `TitleChanged` when the terminal title actually
    /// changed (`_last_emitted_title` dedupe).
    #[test]
    fn test_title_changed_dedupes_on_terminal_title() {
        let mut bar = TitleBar::new();
        let changed = bar.set_state_text("planning").expect("first change emits");
        assert_eq!(
            changed.title,
            "amplifier-app-newtui — Amplifier — planning"
        );
        assert_eq!(
            changed.terminal_title,
            "amplifier-app-newtui — Amplifier — planning"
        );
        assert!(bar.set_state_text("planning").is_none());
        let running = bar.set_running(true).expect("spinner prefix changes title");
        assert!(running
            .terminal_title
            .starts_with(&format!("{} ", TERMINAL_SPINNER_FRAMES[0])));
        assert!(running.title.starts_with("✳ "));
    }

    /// While running, the spinner glyph fragment carries the `orange` token
    /// (`[bold $orange]$glyph[/] $title`); idle titles are a single plain span.
    #[test]
    fn test_title_spans_orange_spinner_while_running() {
        let mut bar = TitleBar::new();
        assert_eq!(
            bar.title_spans(),
            vec![("amplifier-app-newtui — Amplifier — ready".to_string(), None)]
        );
        bar.set_running(true);
        assert_eq!(
            bar.title_spans(),
            vec![
                ("✳".to_string(), Some(StyleToken::Orange)),
                (" amplifier-app-newtui — Amplifier — ready".to_string(), None),
            ]
        );
    }
}
