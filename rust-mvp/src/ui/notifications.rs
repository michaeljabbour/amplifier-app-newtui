//! The attention-notification ladder: bell -> OSC 777 desktop -> push.
//!
//! Port of `ui/notifications.py`. Everything here is a pure function of its
//! inputs (no terminal, no runtime): escape-sequence builders,
//! terminal-support detection, and the ladder policy. The app-assembly layer
//! supplies the live driver, focus state, and environment and performs the
//! single side effect (the write).
//!
//! Donor parity (amplifier-app-cli, read-only reference): the OSC 777
//! `\x1b]777;notify;<title>;<body>\x07` shape and 80/240-char bounds mirror
//! `ui/repl.terminal_notification_sequence`; the terminal allowlist and the
//! `AMPLIFIER_TERMINAL_NOTIFICATIONS` off/force override mirror
//! `ui/terminal_probe.osc9_notifications_supported`; the notify-only-when-
//! unfocused trigger mirrors `ui/layered_repl_terminal.notify_turn_complete`.

use std::collections::HashMap;

/// Why attention is being requested (mirrors `attention_bell_needed`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Reason {
    TurnFinished,
    DecisionDeferred,
}

impl Reason {
    pub fn as_str(&self) -> &'static str {
        match self {
            Reason::TurnFinished => "turn_finished",
            Reason::DecisionDeferred => "decision_deferred",
        }
    }
}

/// A step on the notification ladder the app knows how to fire itself.
///
/// `Bell` is the driver-safe audible bell; `Desktop` is the OSC 777 sequence
/// written to the terminal. Off-machine `push` is the mounted
/// `hooks-notify-push` module's job and never appears here.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Rung {
    Bell,
    Desktop,
}

impl Rung {
    pub fn as_str(&self) -> &'static str {
        match self {
            Rung::Bell => "bell",
            Rung::Desktop => "desktop",
        }
    }
}

/// How high `AMPLIFIER_NOTIFY` lets the ladder climb (parsed value).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NotifyCeiling {
    Off,
    Bell,
    Desktop,
}

impl NotifyCeiling {
    pub fn as_str(&self) -> &'static str {
        match self {
            NotifyCeiling::Off => "off",
            NotifyCeiling::Bell => "bell",
            NotifyCeiling::Desktop => "desktop",
        }
    }
}

/// Turn-end threshold: a turn shorter than this is a live exchange (the
/// user is watching); a longer one plausibly lost their attention, so its
/// close-out notifies. Deferred decisions always notify -- they block on the
/// human by definition.
pub const ATTENTION_MIN_TURN_SECONDS: f64 = 10.0;

/// `AMPLIFIER_NOTIFY` values that silence every rung -- the exact kill
/// switch the (suppressed) hooks-notify module honored, kept for parity.
const NOTIFY_DISABLED_VALUES: [&str; 4] = ["false", "0", "no", "off"];

/// `AMPLIFIER_NOTIFY` values that cap the ladder at the audible bell and
/// never climb to a desktop notification.
const NOTIFY_BELL_ONLY_VALUES: [&str; 1] = ["bell"];

// -- terminal-support allowlist (donor: osc9_notifications_supported) --------

/// Escape hatch for desktop notifications: `off` silences them on
/// allowlisted terminals; `force` enables them anywhere.
pub const NOTIFY_TERMINAL_ENV: &str = "AMPLIFIER_TERMINAL_NOTIFICATIONS";

const TERMINAL_OFF_VALUES: [&str; 5] = ["off", "0", "false", "never", "none"];
const TERMINAL_FORCE_VALUES: [&str; 5] = ["force", "on", "1", "true", "always"];

/// `TERM_PROGRAM` values (lowercased) of terminals known to render OSC
/// notifications. Other terminals may print the escape as garbage, so they
/// are excluded unless `AMPLIFIER_TERMINAL_NOTIFICATIONS=force` opts them in.
const OSC_NOTIFY_TERM_PROGRAMS: [&str; 4] = ["ghostty", "iterm.app", "wezterm", "warpterminal"];

// -- OSC 777 escape sequence (donor: terminal_notification_sequence) ---------

const MAX_TITLE_CHARS: usize = 80;
const MAX_BODY_CHARS: usize = 240;

/// Environment lookup: `Some(map)` for an explicit environ (as in tests),
/// `None` to read the live process environment (Python's `os.environ`).
pub type Environ<'a> = Option<&'a HashMap<String, String>>;

fn env_get(environ: Environ<'_>, key: &str) -> String {
    match environ {
        Some(map) => map.get(key).cloned().unwrap_or_default(),
        None => std::env::var(key).unwrap_or_default(),
    }
}

/// Parse `AMPLIFIER_NOTIFY` into the highest rung the ladder may use.
///
/// `false`/`0`/`no`/`off` -> `Off` (silence, the historical kill switch);
/// `bell` -> `Bell` (audible only, never desktop); anything else -- unset,
/// `true`/`1`/`on`, or an explicit `desktop` -- opens the full ladder.
/// Unknown values default to the full ladder so a typo never silences you.
pub fn notify_ceiling(environ: Environ<'_>) -> NotifyCeiling {
    let value = env_get(environ, "AMPLIFIER_NOTIFY").trim().to_lowercase();
    if NOTIFY_DISABLED_VALUES.contains(&value.as_str()) {
        return NotifyCeiling::Off;
    }
    if NOTIFY_BELL_ONLY_VALUES.contains(&value.as_str()) {
        return NotifyCeiling::Bell;
    }
    NotifyCeiling::Desktop
}

/// Whether any rung should fire for `reason`.
///
/// Deferred decisions always qualify; a finished turn qualifies only once
/// it has run past [`ATTENTION_MIN_TURN_SECONDS`]. `AMPLIFIER_NOTIFY` set to
/// a disabled value suppresses everything.
pub fn attention_needed(reason: Reason, elapsed_s: f64, environ: Environ<'_>) -> bool {
    if notify_ceiling(environ) == NotifyCeiling::Off {
        return false;
    }
    if reason == Reason::DecisionDeferred {
        return true;
    }
    elapsed_s >= ATTENTION_MIN_TURN_SECONDS
}

/// Allowlist OSC 777 desktop notifications by terminal identity.
///
/// ghostty, iTerm2, WezTerm and Warp (via `TERM_PROGRAM`) and kitty (via
/// `TERM`/`KITTY_WINDOW_ID`) render OSC notifications; other terminals may
/// print the raw escape, so they are excluded.
/// `AMPLIFIER_TERMINAL_NOTIFICATIONS=off` silences them anywhere and
/// `=force` enables them anywhere.
pub fn desktop_notifications_supported(environ: Environ<'_>) -> bool {
    let override_value = env_get(environ, NOTIFY_TERMINAL_ENV).trim().to_lowercase();
    if TERMINAL_OFF_VALUES.contains(&override_value.as_str()) {
        return false;
    }
    if TERMINAL_FORCE_VALUES.contains(&override_value.as_str()) {
        return true;
    }
    let term_program = env_get(environ, "TERM_PROGRAM").trim().to_lowercase();
    if OSC_NOTIFY_TERM_PROGRAMS.contains(&term_program.as_str()) {
        return true;
    }
    env_get(environ, "TERM").contains("kitty") || !env_get(environ, "KITTY_WINDOW_ID").is_empty()
}

/// The ordered rungs to fire for `reason` -- the ladder decision.
///
/// Nothing fires unless attention is actually needed ([`attention_needed`]).
/// The audible bell is always the first rung. The ladder climbs to the
/// OSC 777 desktop rung only when the escalation is warranted and permitted:
/// the terminal window is **unfocused** (the user looked away, exactly when
/// a desktop toast earns its keep), the terminal is on the render allowlist,
/// and `AMPLIFIER_NOTIFY` was not capped at `bell`.
pub fn notification_rungs(
    reason: Reason,
    elapsed_s: f64,
    focused: bool,
    environ: Environ<'_>,
) -> Vec<Rung> {
    if !attention_needed(reason, elapsed_s, environ) {
        return Vec::new();
    }
    let mut rungs = vec![Rung::Bell];
    if notify_ceiling(environ) == NotifyCeiling::Desktop
        && !focused
        && desktop_notifications_supported(environ)
    {
        rungs.push(Rung::Desktop);
    }
    rungs
}

/// Unicode `Cf` (format) codepoint ranges, inclusive, mirroring Python's
/// `unicodedata.category(ch) == "Cf"` (Unicode 15.0, CPython 3.12).
const CF_RANGES: [(u32, u32); 21] = [
    (0x00AD, 0x00AD),
    (0x0600, 0x0605),
    (0x061C, 0x061C),
    (0x06DD, 0x06DD),
    (0x070F, 0x070F),
    (0x0890, 0x0891),
    (0x08E2, 0x08E2),
    (0x180E, 0x180E),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x2064),
    (0x2066, 0x206F),
    (0xFEFF, 0xFEFF),
    (0xFFF9, 0xFFFB),
    (0x110BD, 0x110BD),
    (0x110CD, 0x110CD),
    (0x13430, 0x1343F),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0001, 0xE0001),
    (0xE0020, 0xE007F),
];

fn is_format_char(character: char) -> bool {
    let cp = character as u32;
    CF_RANGES.iter().any(|&(lo, hi)| cp >= lo && cp <= hi)
}

/// Collapse untrusted text into one safe, control-free display line.
///
/// Control characters (including a smuggled `ESC`/`BEL` that could end the
/// OSC early and inject a second sequence) become spaces; bidi and other
/// invisible formatting codepoints are dropped; whitespace runs collapse to
/// single spaces. The caller bounds the length per field.
pub fn sanitize_notification_text(text: &str) -> String {
    let mut kept = String::with_capacity(text.len());
    for character in text.chars() {
        if character.is_control() {
            // C0/C1 controls (ESC, BEL, \n, \t) -> space
            kept.push(' ');
        } else if is_format_char(character) {
            // bidi / zero-width / invisible formatters -> drop
            continue;
        } else {
            kept.push(character);
        }
    }
    kept.split_whitespace().collect::<Vec<_>>().join(" ")
}

/// Build a bounded OSC 777 notification with escape injection stripped.
///
/// Shape `\x1b]777;notify;<title>;<body>\x07` (BEL-terminated) -- the
/// kitty/wezterm/rxvt desktop-notification form, rendered as a native OS
/// toast. Title and body are sanitized and capped (80/240 chars) so a
/// verbose recap cannot flood the notification or break out of the OSC.
pub fn osc777_notification_sequence(title: &str, body: &str) -> String {
    let safe_title: String = sanitize_notification_text(title)
        .chars()
        .take(MAX_TITLE_CHARS)
        .collect::<String>()
        .trim_end()
        .to_string();
    let safe_body: String = sanitize_notification_text(body)
        .chars()
        .take(MAX_BODY_CHARS)
        .collect::<String>()
        .trim_end()
        .to_string();
    format!("\x1b]777;notify;{safe_title};{safe_body}\x07")
}

/// The output seam a desktop notification is written through -- the ratatui
/// analogue of the Textual `Driver` surface `write_desktop_notification`
/// depends on (`is_headless`/`is_web`/`write`/`flush`).
pub trait NotificationDriver {
    fn is_headless(&self) -> bool;
    fn is_web(&self) -> bool;
    fn write(&mut self, data: &str);
    fn flush(&mut self);
}

/// Emit an OSC 777 desktop notification through the driver.
///
/// Mirrors `chrome.write_terminal_title`: the escape is written on the
/// driver's own synchronized output stream (never raw stdout, which would
/// race the compositor), and skipped when there is no real terminal to
/// receive it. Returns whether the sequence was written; never raises.
pub fn write_desktop_notification(
    driver: Option<&mut dyn NotificationDriver>,
    title: &str,
    body: &str,
) -> bool {
    let Some(driver) = driver else {
        return false;
    };
    if driver.is_headless() || driver.is_web() {
        return false;
    }
    driver.write(&osc777_notification_sequence(title, body));
    driver.flush();
    true
}

#[cfg(test)]
mod tests {
    use super::*;

    fn env(pairs: &[(&str, &str)]) -> HashMap<String, String> {
        pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect()
    }

    fn kitty() -> HashMap<String, String> {
        env(&[("TERM", "xterm-kitty")])
    }

    /// A non-headless driver stand-in that captures OSC writes + flushes.
    struct RecordingDriver {
        is_headless: bool,
        is_web: bool,
        writes: Vec<String>,
        flushes: usize,
    }

    impl RecordingDriver {
        fn new() -> Self {
            Self {
                is_headless: false,
                is_web: false,
                writes: Vec::new(),
                flushes: 0,
            }
        }
    }

    impl NotificationDriver for RecordingDriver {
        fn is_headless(&self) -> bool {
            self.is_headless
        }
        fn is_web(&self) -> bool {
            self.is_web
        }
        fn write(&mut self, data: &str) {
            self.writes.push(data.to_string());
        }
        fn flush(&mut self) {
            self.flushes += 1;
        }
    }

    // -- AMPLIFIER_NOTIFY ceiling parsing -------------------------------------

    #[test]
    fn test_notify_ceiling_off_bell_and_desktop() {
        for value in ["false", "0", "no", "off", "OFF", "False"] {
            let e = env(&[("AMPLIFIER_NOTIFY", value)]);
            assert_eq!(notify_ceiling(Some(&e)), NotifyCeiling::Off);
        }
        let e = env(&[("AMPLIFIER_NOTIFY", "bell")]);
        assert_eq!(notify_ceiling(Some(&e)), NotifyCeiling::Bell);
        let e = env(&[("AMPLIFIER_NOTIFY", "BELL")]);
        assert_eq!(notify_ceiling(Some(&e)), NotifyCeiling::Bell);
        // Unset, truthy, and explicit desktop all open the full ladder; an
        // unknown value defaults up (a typo must not silence you).
        for value in ["", "true", "1", "on", "desktop", "osc777", "wat"] {
            let e = env(&[("AMPLIFIER_NOTIFY", value)]);
            assert_eq!(notify_ceiling(Some(&e)), NotifyCeiling::Desktop);
        }
        let empty = env(&[]);
        assert_eq!(notify_ceiling(Some(&empty)), NotifyCeiling::Desktop);
    }

    // -- attention predicate (bell-rung floor) --------------------------------

    #[test]
    fn test_attention_needed_defers_always_and_turns_after_threshold() {
        let empty = env(&[]);
        assert!(attention_needed(Reason::DecisionDeferred, 0.0, Some(&empty)));
        assert!(!attention_needed(Reason::TurnFinished, 0.0, Some(&empty)));
        assert!(!attention_needed(
            Reason::TurnFinished,
            ATTENTION_MIN_TURN_SECONDS - 0.1,
            Some(&empty)
        ));
        assert!(attention_needed(
            Reason::TurnFinished,
            ATTENTION_MIN_TURN_SECONDS,
            Some(&empty)
        ));
    }

    #[test]
    fn test_attention_needed_honours_disable_switch() {
        for value in ["false", "0", "no", "off"] {
            let e = env(&[("AMPLIFIER_NOTIFY", value)]);
            assert!(!attention_needed(Reason::DecisionDeferred, 0.0, Some(&e)));
            assert!(!attention_needed(Reason::TurnFinished, 999.0, Some(&e)));
        }
    }

    // -- terminal-support allowlist -------------------------------------------

    #[test]
    fn test_desktop_supported_allowlists_known_terminals() {
        for (key, value) in [
            ("TERM_PROGRAM", "iTerm.app"),
            ("TERM_PROGRAM", "ghostty"),
            ("TERM_PROGRAM", "WezTerm"),
            ("TERM_PROGRAM", "WarpTerminal"),
            ("TERM", "xterm-kitty"),
            ("KITTY_WINDOW_ID", "1"),
        ] {
            let e = env(&[(key, value)]);
            assert!(
                desktop_notifications_supported(Some(&e)),
                "expected supported for {key}={value}"
            );
        }
    }

    #[test]
    fn test_desktop_supported_excludes_unknown_and_honours_override() {
        let e = env(&[("TERM", "xterm-256color")]);
        assert!(!desktop_notifications_supported(Some(&e)));
        let e = env(&[("TERM_PROGRAM", "Apple_Terminal")]);
        assert!(!desktop_notifications_supported(Some(&e)));
        // Override wins both ways over the allowlist.
        let e = env(&[
            ("TERM", "xterm-256color"),
            ("AMPLIFIER_TERMINAL_NOTIFICATIONS", "force"),
        ]);
        assert!(desktop_notifications_supported(Some(&e)));
        let e = env(&[
            ("TERM", "xterm-kitty"),
            ("AMPLIFIER_TERMINAL_NOTIFICATIONS", "off"),
        ]);
        assert!(!desktop_notifications_supported(Some(&e)));
    }

    // -- the ladder ------------------------------------------------------------

    #[test]
    fn test_ladder_silent_when_no_attention_or_disabled() {
        let k = kitty();
        assert_eq!(
            notification_rungs(Reason::TurnFinished, 1.0, false, Some(&k)),
            Vec::<Rung>::new()
        );
        let e = env(&[("TERM", "xterm-kitty"), ("AMPLIFIER_NOTIFY", "off")]);
        assert_eq!(
            notification_rungs(Reason::DecisionDeferred, 0.0, false, Some(&e)),
            Vec::<Rung>::new()
        );
    }

    #[test]
    fn test_ladder_bell_only_when_focused() {
        // Focused: the user is watching, a soft bell is enough (no desktop toast).
        let k = kitty();
        assert_eq!(
            notification_rungs(Reason::DecisionDeferred, 0.0, true, Some(&k)),
            vec![Rung::Bell]
        );
    }

    #[test]
    fn test_ladder_climbs_to_desktop_when_unfocused_on_capable_terminal() {
        let k = kitty();
        assert_eq!(
            notification_rungs(Reason::DecisionDeferred, 0.0, false, Some(&k)),
            vec![Rung::Bell, Rung::Desktop]
        );
        assert_eq!(
            notification_rungs(
                Reason::TurnFinished,
                ATTENTION_MIN_TURN_SECONDS,
                false,
                Some(&k)
            ),
            vec![Rung::Bell, Rung::Desktop]
        );
    }

    #[test]
    fn test_ladder_bell_cap_never_climbs_to_desktop() {
        let e = env(&[("TERM", "xterm-kitty"), ("AMPLIFIER_NOTIFY", "bell")]);
        assert_eq!(
            notification_rungs(Reason::DecisionDeferred, 0.0, false, Some(&e)),
            vec![Rung::Bell]
        );
    }

    #[test]
    fn test_ladder_stays_on_bell_when_terminal_cannot_render() {
        let e = env(&[("TERM", "xterm-256color")]);
        assert_eq!(
            notification_rungs(Reason::DecisionDeferred, 0.0, false, Some(&e)),
            vec![Rung::Bell]
        );
    }

    // -- OSC 777 escape builder -------------------------------------------------

    #[test]
    fn test_osc777_sequence_exact_shape() {
        let seq = osc777_notification_sequence("Amplifier", "Turn complete");
        assert_eq!(seq, "\x1b]777;notify;Amplifier;Turn complete\x07");
    }

    #[test]
    fn test_osc777_sequence_strips_injection_and_bounds_fields() {
        // A smuggled BEL/ESC + a second OSC must not survive into the payload:
        // the whole sequence carries exactly one ESC (its own opener) and one
        // BEL (its own terminator), so nothing can break out mid-notification.
        let body = format!("{}\nline\x1b\\rest", "b".repeat(400));
        let seq = osc777_notification_sequence("Amp\x07\x1b work", &body);
        assert!(seq.starts_with("\x1b]777;notify;"));
        assert!(seq.ends_with('\x07'));
        assert_eq!(seq.matches('\x1b').count(), 1);
        assert_eq!(seq.matches('\x07').count(), 1);
        let inner = seq
            .strip_prefix("\x1b]777;notify;")
            .unwrap()
            .strip_suffix('\x07')
            .unwrap();
        let (title_field, body_field) = inner.split_once(';').unwrap();
        assert!(!body_field.contains('\n'));
        assert!(title_field.chars().count() <= 80);
        assert!(body_field.chars().count() <= 240);
    }

    #[test]
    fn test_sanitize_collapses_whitespace_and_drops_invisibles() {
        // \u{200b} (zero-width space, Cf) is dropped; runs collapse to one space.
        assert_eq!(
            sanitize_notification_text("a\t b\n\nc\u{200b}d"),
            "a b cd"
        );
        assert_eq!(sanitize_notification_text("  spaced  out  "), "spaced out");
    }

    // -- driver write path ------------------------------------------------------

    #[test]
    fn test_write_desktop_notification_uses_osc_and_flushes() {
        let mut driver = RecordingDriver::new();
        assert!(write_desktop_notification(
            Some(&mut driver),
            "Amplifier",
            "done"
        ));
        assert_eq!(driver.writes, vec!["\x1b]777;notify;Amplifier;done\x07"]);
        assert_eq!(driver.flushes, 1);
    }

    #[test]
    fn test_write_desktop_notification_skips_when_no_real_terminal() {
        let mut driver = RecordingDriver::new();
        driver.is_headless = true;
        assert!(!write_desktop_notification(
            Some(&mut driver),
            "Amplifier",
            "done"
        ));
        assert!(driver.writes.is_empty());
        assert!(!write_desktop_notification(None, "Amplifier", "done"));
    }
}
