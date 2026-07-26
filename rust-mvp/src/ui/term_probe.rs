//! Startup terminal-capability probe: kitty keyboard protocol support
//! (DESIGN-SPEC §12; docs/tui-v3-cohesive.md §"Bottom status bar"/§9).
//!
//! Port of `ui/term_probe.py`. The functional bindings never change —
//! both `shift+enter` and the works-everywhere `alt+enter` stay bound
//! (see `composer.rs`). This probe only decides which chord the UI
//! *advertises*: `shift+enter` when the terminal is known to speak the
//! kitty keyboard protocol (or xterm modifyOtherKeys), `alt+enter queue`
//! otherwise.
//!
//! Pure environment sniff, deliberately conservative: an unknown
//! terminal gets the fallback label, because advertising `shift+enter`
//! on a legacy terminal points at a chord that is never delivered, while
//! `alt+enter` works everywhere.
//!
//! Ratatui adaptation: the Python module also carries
//! `patch_legacy_alt_named_keys`, a monkeypatch of Textual's
//! `XTermParser._sequence_to_key_events` that restores the `alt+` prefix
//! on legacy `ESC`-prefixed named keys (Textual 8.2.8 drops it, turning a
//! mid-turn alt+enter queue into a steer). That patch is
//! Textual-internals surgery and does not port; the app-assembly layer
//! must ensure its terminal input parser (e.g. crossterm) reports
//! `ESC CR` from a legacy terminal as Alt+Enter rather than plain Enter.

use crate::ui::notifications::Environ;

/// `TERM` prefixes owned by terminals that speak the kitty protocol.
const KITTY_TERM_PREFIXES: [&str; 5] = ["xterm-kitty", "foot", "wezterm", "ghostty", "rio"];

/// `TERM_PROGRAM` values (lowercased) with kitty-protocol support.
const KITTY_TERM_PROGRAMS: [&str; 4] = ["kitty", "wezterm", "ghostty", "rio"];

/// Env vars whose presence identifies a capable terminal (Windows
/// Terminal delivers shift+enter via win32-input-mode).
const KITTY_ENV_MARKERS: [&str; 4] = [
    "KITTY_WINDOW_ID",
    "WEZTERM_PANE",
    "GHOSTTY_RESOURCES_DIR",
    "WT_SESSION",
];

/// iTerm2 gained the kitty keyboard protocol in 3.5.
const ITERM_MIN_VERSION: (u64, u64) = (3, 5);

/// `env.get(key, "")` — value of `key`, empty string when absent.
fn env_get(environ: Environ<'_>, key: &str) -> String {
    match environ {
        Some(map) => map.get(key).cloned().unwrap_or_default(),
        None => std::env::var(key).unwrap_or_default(),
    }
}

/// `key in env` — presence check, regardless of value.
fn env_has(environ: Environ<'_>, key: &str) -> bool {
    match environ {
        Some(map) => map.contains_key(key),
        None => std::env::var_os(key).is_some(),
    }
}

/// True when the hosting terminal is known to deliver shift+enter.
///
/// Reads the live process environment unless an explicit mapping is
/// passed (tests) — Python's `os.environ if environ is None else environ`.
pub fn probe_kitty_protocol(environ: Environ<'_>) -> bool {
    if !env_get(environ, "TEXTUAL_DISABLE_KITTY_KEY").is_empty() {
        return false; // Textual won't request the protocol at all
    }
    let term = env_get(environ, "TERM");
    if env_has(environ, "TMUX") || term.starts_with("screen") || term.starts_with("tmux") {
        return false; // multiplexer passthrough is not dependable
    }
    if KITTY_ENV_MARKERS
        .iter()
        .any(|marker| env_has(environ, marker))
    {
        return true;
    }
    if KITTY_TERM_PREFIXES
        .iter()
        .any(|prefix| term.starts_with(prefix))
    {
        return true;
    }
    if env_has(environ, "XTERM_VERSION") {
        return true; // genuine xterm: modifyOtherKeys delivers shift+enter
    }
    let program = env_get(environ, "TERM_PROGRAM").to_lowercase();
    if KITTY_TERM_PROGRAMS.contains(&program.as_str()) {
        return true;
    }
    if program == "iterm.app" {
        return parse_version(&env_get(environ, "TERM_PROGRAM_VERSION")) >= ITERM_MIN_VERSION;
    }
    false
}

/// Leading `major.minor` of a version string; (0, 0) when unparsable
/// (Python: `re.match(r"(\d+)\.(\d+)", raw)`).
fn parse_version(raw: &str) -> (u64, u64) {
    let major_end = raw.char_indices().find(|(_, c)| !c.is_ascii_digit());
    let Some((dot_at, '.')) = major_end else {
        return (0, 0);
    };
    if dot_at == 0 {
        return (0, 0); // no leading digits
    }
    let rest = &raw[dot_at + 1..];
    let minor_len = rest
        .chars()
        .take_while(|c| c.is_ascii_digit())
        .count();
    if minor_len == 0 {
        return (0, 0);
    }
    let major = raw[..dot_at].parse().unwrap_or(0);
    let minor = rest[..minor_len].parse().unwrap_or(0);
    (major, minor)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashMap;

    fn env(pairs: &[(&str, &str)]) -> HashMap<String, String> {
        pairs
            .iter()
            .map(|(k, v)| (k.to_string(), v.to_string()))
            .collect()
    }

    #[test]
    fn test_kitty_protocol_terminals_detected() {
        assert!(probe_kitty_protocol(Some(&env(&[
            ("TERM", "xterm-kitty"),
            ("KITTY_WINDOW_ID", "1"),
        ]))));
        assert!(probe_kitty_protocol(Some(&env(&[
            ("TERM", "xterm-256color"),
            ("WEZTERM_PANE", "0"),
        ]))));
        assert!(probe_kitty_protocol(Some(&env(&[
            ("TERM", "xterm-ghostty"),
            ("GHOSTTY_RESOURCES_DIR", "/x"),
        ]))));
        assert!(probe_kitty_protocol(Some(&env(&[("TERM", "foot")]))));
        assert!(probe_kitty_protocol(Some(&env(&[
            ("TERM", "xterm-256color"),
            ("TERM_PROGRAM", "WezTerm"),
        ]))));
        assert!(probe_kitty_protocol(Some(&env(&[
            ("TERM", "xterm-256color"),
            ("WT_SESSION", "guid"),
        ]))));
        assert!(probe_kitty_protocol(Some(&env(&[
            ("TERM", "xterm"),
            ("XTERM_VERSION", "XTerm(390)"),
        ]))));
    }

    #[test]
    fn test_iterm_needs_3_5() {
        let base = [("TERM", "xterm-256color"), ("TERM_PROGRAM", "iTerm.app")];
        let with_version = |v: &str| {
            let mut pairs = base.to_vec();
            pairs.push(("TERM_PROGRAM_VERSION", v));
            env(&pairs)
        };
        assert!(probe_kitty_protocol(Some(&with_version("3.5.13"))));
        assert!(probe_kitty_protocol(Some(&with_version("4.0"))));
        assert!(!probe_kitty_protocol(Some(&with_version("3.4.19"))));
        assert!(!probe_kitty_protocol(Some(&env(&base))));
    }

    #[test]
    fn test_legacy_terminals_fall_back() {
        assert!(!probe_kitty_protocol(Some(&env(&[]))));
        assert!(!probe_kitty_protocol(Some(&env(&[(
            "TERM",
            "xterm-256color"
        )]))));
        assert!(!probe_kitty_protocol(Some(&env(&[
            ("TERM", "xterm-256color"),
            ("TERM_PROGRAM", "Apple_Terminal"),
        ]))));
        assert!(!probe_kitty_protocol(Some(&env(&[
            ("TERM", "xterm-256color"),
            ("TERM_PROGRAM", "vscode"),
        ]))));
    }

    #[test]
    fn test_multiplexers_and_explicit_disable_fall_back() {
        // tmux/screen passthrough is not dependable even under a capable outer terminal
        assert!(!probe_kitty_protocol(Some(&env(&[
            ("TERM", "tmux-256color"),
            ("TMUX", "/tmp/t,1,0"),
            ("KITTY_WINDOW_ID", "1"),
        ]))));
        assert!(!probe_kitty_protocol(Some(&env(&[(
            "TERM",
            "screen-256color"
        )]))));
        // honoring Textual's own kill switch: it won't request the protocol at all
        assert!(!probe_kitty_protocol(Some(&env(&[
            ("TERM", "xterm-kitty"),
            ("TEXTUAL_DISABLE_KITTY_KEY", "1"),
        ]))));
    }
}
