//! The five interaction modes and their cycle (DESIGN-SPEC §4, ADR-0005).
//!
//! Port of `src/amplifier_app_newtui/model/modes.py`.
//!
//! Mode → color/trust table (verbatim from the spec):
//!
//! | mode       | color  | trust string                              |
//! |------------|--------|-------------------------------------------|
//! | chat       | dim    | `ask all · auto read`                     |
//! | plan       | blue   | `read-only`                               |
//! | brainstorm | teal   | `no tools`                                |
//! | build      | green  | `auto read,test · ask write,net,spend`    |
//! | auto       | orange | `auto read,write · asks if risky`         |
//!
//! Mode tint appears in exactly three places: composer `[mode]` badge,
//! composer left edge, footer. The composer *edge* for chat uses the `rule`
//! token (spec §4) — that is the `accent` field; `color_token` is the
//! badge/footer color.
//!
//! shift+tab cycles modes; this is a fully independent control from the
//! ctrl-p permission cycle (ADR-0005 amendment — the two 5-state cycles
//! share four members but diverge at brainstorm vs bypass and must never be
//! one control).

use std::fmt;

use crate::model::blocks::StyleToken;

/// Python `ModeId = Literal["chat", "plan", "brainstorm", "build", "auto"]`.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum ModeId {
    Chat,
    Plan,
    Brainstorm,
    Build,
    Auto,
}

impl ModeId {
    /// The exact Python literal string value.
    pub fn as_str(self) -> &'static str {
        match self {
            ModeId::Chat => "chat",
            ModeId::Plan => "plan",
            ModeId::Brainstorm => "brainstorm",
            ModeId::Build => "build",
            ModeId::Auto => "auto",
        }
    }

    /// Parse a mode-id string; `None` for anything outside the five literals
    /// (the Rust spelling of Python's `mode_id in MODE_PROFILES` membership
    /// test, which is exact — no trimming or case folding).
    pub fn parse(value: &str) -> Option<ModeId> {
        Some(match value {
            "chat" => ModeId::Chat,
            "plan" => ModeId::Plan,
            "brainstorm" => ModeId::Brainstorm,
            "build" => ModeId::Build,
            "auto" => ModeId::Auto,
            _ => return None,
        })
    }
}

impl fmt::Display for ModeId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// One interaction mode's presentation + trust identity.
///
/// - `id`: mode name, also the trust-preset key in [`crate::model::trust`].
/// - `color_token`: theme token for the `[mode]` badge and footer
///   `mode <id>` text.
/// - `trust_str`: the exact trust summary shown in mode-change notices
///   (`mode <id> · <trust_str>`) and the footer.
/// - `accent`: theme token tinting the composer's 2px left edge (`rule` for
///   chat, else the mode color).
///
/// Frozen pydantic model in Python (`frozen=True, extra="forbid"`); a plain
/// immutable-by-convention struct here.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct ModeProfile {
    pub id: ModeId,
    pub color_token: StyleToken,
    pub trust_str: &'static str,
    pub accent: StyleToken,
}

impl ModeProfile {
    /// The transient notice text on mode change: `mode <id> · <trust>`.
    pub fn notice(&self) -> String {
        format!("mode {} · {}", self.id.as_str(), self.trust_str)
    }
}

/// All five mode profiles (DESIGN-SPEC §4 table, verbatim). Python keys a
/// dict by id; here the array is in [`MODE_CYCLE`] order and [`get_mode`] /
/// [`profile`] do the lookup.
pub const MODE_PROFILES: [ModeProfile; 5] = [
    ModeProfile {
        id: ModeId::Chat,
        color_token: StyleToken::Dim,
        trust_str: "ask all · auto read",
        accent: StyleToken::Rule,
    },
    ModeProfile {
        id: ModeId::Plan,
        color_token: StyleToken::Blue,
        trust_str: "read-only",
        accent: StyleToken::Blue,
    },
    ModeProfile {
        id: ModeId::Brainstorm,
        color_token: StyleToken::Teal,
        trust_str: "no tools",
        accent: StyleToken::Teal,
    },
    ModeProfile {
        id: ModeId::Build,
        color_token: StyleToken::Green,
        trust_str: "auto read,test · ask write,net,spend",
        accent: StyleToken::Green,
    },
    ModeProfile {
        id: ModeId::Auto,
        color_token: StyleToken::Orange,
        trust_str: "auto read,write · asks if risky",
        accent: StyleToken::Orange,
    },
];

/// shift+tab cycle order (mockup Component.MODES array order, DESIGN-SPEC §4).
pub const MODE_CYCLE: [ModeId; 5] = [
    ModeId::Chat,
    ModeId::Plan,
    ModeId::Brainstorm,
    ModeId::Build,
    ModeId::Auto,
];

/// Boot posture. The mockup demo *starts* its scripted history in chat, but
/// the app defaults to auto — amplifier's natural wide scope (user directive
/// 2026-07-16): auto read/write/test, the rest asks if risky
/// (classifier-gated).
pub const DEFAULT_MODE: ModeId = ModeId::Auto;

/// The profile for a known [`ModeId`] (Python `MODE_PROFILES[mode_id]` with a
/// key that is statically known to exist).
pub fn profile(id: ModeId) -> &'static ModeProfile {
    &MODE_PROFILES[id as usize]
}

/// Look up a mode profile, falling back to [`DEFAULT_MODE`] for unknown/None
/// ids.
pub fn get_mode(mode_id: Option<&str>) -> &'static ModeProfile {
    match mode_id.and_then(ModeId::parse) {
        Some(id) => profile(id),
        None => profile(DEFAULT_MODE),
    }
}

/// Return the next mode in the shift+tab cycle.
///
/// Unknown/None `current` lands on the first cycle entry (or last for a
/// negative offset) rather than panicking — cycling must always succeed.
pub fn cycle_mode(current: Option<&str>, offset: i64) -> &'static ModeProfile {
    let Some(index) = current
        .and_then(ModeId::parse)
        .and_then(|id| MODE_CYCLE.iter().position(|&m| m == id))
    else {
        let fallback = if offset >= 0 {
            MODE_CYCLE[0]
        } else {
            MODE_CYCLE[MODE_CYCLE.len() - 1]
        };
        return profile(fallback);
    };
    let len = MODE_CYCLE.len() as i64;
    let next = (index as i64 + offset).rem_euclid(len) as usize;
    profile(MODE_CYCLE[next])
}

#[cfg(test)]
mod tests {
    use super::*;

    // --- modes (DESIGN-SPEC §4 table, verbatim) -----------------------------

    #[test]
    fn test_mode_table_matches_spec_exactly() {
        let expected: [(&str, &str, &str); 5] = [
            ("chat", "dim", "ask all · auto read"),
            ("plan", "blue", "read-only"),
            ("brainstorm", "teal", "no tools"),
            ("build", "green", "auto read,test · ask write,net,spend"),
            ("auto", "orange", "auto read,write · asks if risky"),
        ];
        assert_eq!(MODE_PROFILES.len(), expected.len());
        for (mode_id, color, trust) in expected {
            let id = ModeId::parse(mode_id).expect("known mode id");
            let profile = profile(id);
            assert_eq!(profile.id.as_str(), mode_id);
            assert_eq!(profile.color_token.as_str(), color, "{mode_id}");
            assert_eq!(profile.trust_str, trust, "{mode_id}");
        }
    }

    #[test]
    fn test_chat_composer_edge_uses_rule_token() {
        assert_eq!(profile(ModeId::Chat).accent, StyleToken::Rule);
        for id in [ModeId::Plan, ModeId::Brainstorm, ModeId::Build, ModeId::Auto] {
            assert_eq!(profile(id).accent, profile(id).color_token);
        }
    }

    #[test]
    fn test_mode_change_notice_format() {
        assert_eq!(profile(ModeId::Plan).notice(), "mode plan · read-only");
    }

    #[test]
    fn test_cycle_visits_all_five_modes_and_wraps() {
        let mut seen: Vec<&str> = Vec::new();
        let mut current: &str = DEFAULT_MODE.as_str();
        for _ in 0..MODE_CYCLE.len() {
            seen.push(current);
            current = cycle_mode(Some(current), 1).id.as_str();
        }
        let mut sorted_seen = seen.clone();
        sorted_seen.sort_unstable();
        let mut sorted_cycle: Vec<&str> = MODE_CYCLE.iter().map(|m| m.as_str()).collect();
        sorted_cycle.sort_unstable();
        assert_eq!(sorted_seen, sorted_cycle);
        assert_eq!(current, DEFAULT_MODE.as_str()); // full wrap
    }

    #[test]
    fn test_cycle_backwards() {
        assert_eq!(
            cycle_mode(Some("chat"), -1).id,
            MODE_CYCLE[MODE_CYCLE.len() - 1]
        );
    }

    /// Boot posture is auto — amplifier's natural wide scope (§4 amendment,
    /// ADR-0007 resolution 0).
    #[test]
    fn test_default_mode_is_auto() {
        assert_eq!(DEFAULT_MODE, ModeId::Auto);
        assert_eq!(DEFAULT_MODE.as_str(), "auto");
    }

    /// get_mode falls back to the DEFAULT_MODE profile (now auto) for
    /// unknown/None ids; trust *resolution* for unknown modes still uses the
    /// chat posture (see trust::tests::test_unknown_mode_uses_chat_posture).
    #[test]
    fn test_unknown_mode_falls_back_to_default() {
        assert_eq!(get_mode(Some("bogus")).id, DEFAULT_MODE);
        assert_eq!(get_mode(None).id, DEFAULT_MODE);
    }

    // --- Rust-side seams (no Python counterpart; keep the port honest) ------

    #[test]
    fn cycle_mode_unknown_current_lands_on_cycle_edge() {
        // Python: unknown/None current → first entry (or last for negative
        // offset) rather than raising.
        assert_eq!(cycle_mode(None, 1).id, MODE_CYCLE[0]);
        assert_eq!(cycle_mode(Some("bogus"), 1).id, MODE_CYCLE[0]);
        assert_eq!(cycle_mode(None, -1).id, MODE_CYCLE[MODE_CYCLE.len() - 1]);
        assert_eq!(cycle_mode(Some("bogus"), 0).id, MODE_CYCLE[0]);
    }

    #[test]
    fn cycle_mode_large_offsets_wrap_like_python_modulo() {
        // Python `(index + offset) % len` on negatives wraps positively;
        // rem_euclid matches. chat(0) + -6 → index 4 → auto.
        assert_eq!(cycle_mode(Some("chat"), -6).id, ModeId::Auto);
        assert_eq!(cycle_mode(Some("plan"), 5).id, ModeId::Plan);
        assert_eq!(cycle_mode(Some("auto"), 2).id, ModeId::Plan);
    }
}
