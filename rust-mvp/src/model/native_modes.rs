//! Active native (bundle-composed) modes as an ordered set + precedence rules.
//!
//! Port of `src/amplifier_app_newtui/model/native_modes.py`.
//!
//! Two independent tool-policy layers coexist in newtui:
//!
//! 1. **Posture** — the single shift+tab trust baseline (chat/plan/brainstorm/
//!    build/auto, [`crate::model::modes`] + [`crate::model::trust`]). What
//!    capabilities auto-run/ask/deny. Always exactly one.
//! 2. **Native modes** — bundle-composed modes (team-pulse, audit,
//!    superpowers, …) activated through the mounted `mode` tool. Each may
//!    declare/imply its own tool needs (`safe_tools`).
//!
//! **Upstream boundary (verified against `amplifier_module_tool_mode` +
//! `amplifier_module_hooks_mode`):** the mode tool and hooks-mode are strictly
//! *single-slot* — `coordinator.session_state["active_mode"]` is one string;
//! `set` replaces it, `clear` nulls it, and hooks-mode enforces exactly that
//! one mode's tool policy + instructions. There is no upstream
//! multi-activation.
//!
//! So newtui models an ordered **stack** of active native modes client-side.
//! The **primary** (most-recently activated, the top of the stack) is what
//! newtui points the upstream single slot at — so its policy + instructions
//! are the ones hooks-mode actually enforces. The rest of the stack is
//! retained here for display and precedence; removing the primary promotes
//! the next one back into the enforced slot. A single active native mode
//! therefore behaves exactly as the old single-slot `_native_mode` did
//! (backward compatible).
//!
//! **Tool-policy precedence rule.** The posture is the trust baseline; an
//! active native mode's *declared* tools take precedence over a
//! tool-restrictive posture:
//!
//! - The kernel governance hook lets a tool the active native mode declares
//!   `safe` through (abstains — `continue`) regardless of posture, so a
//!   no-tools posture no longer *silently* nullifies a native mode's own
//!   tools. hooks-mode remains authoritative for those tools.
//! - Where a native mode's needs cannot be settled from `safe_tools` alone
//!   (a mode leaning on `default_action` rather than an explicit safe list),
//!   the app surfaces a clear conflict notice — [`posture_conflict_notice`]
//!   — instead of a silent denial: "team-pulse active but brainstorm blocks
//!   tools — /mode build to run them".

use crate::model::modes::get_mode;
use crate::model::trust::{resolve_capability, CapabilityClass, Decision};

/// Footer glyph prefixing the active native-mode badge.
pub const NATIVE_BADGE: &str = "◆";

/// Ordered, de-duplicated set of active native modes (last == primary).
///
/// Frozen value type: [`add`](Self::add) / [`remove`](Self::remove) /
/// [`clear`](Self::clear) return a new instance rather than mutating, so the
/// app can hold one and swap it. `names` is activation order — the LAST
/// element is the [`primary`](Self::primary), the one newtui points the
/// upstream single slot at.
///
/// Frozen pydantic model in Python (`frozen=True, extra="forbid"`); a plain
/// immutable-by-convention struct here.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct ActiveNativeModes {
    pub names: Vec<String>,
}

impl ActiveNativeModes {
    /// An empty stack (Python `ActiveNativeModes()` with the default `()`).
    pub fn new() -> Self {
        Self::default()
    }

    /// The enforced native mode (top of the stack), or `None` if empty.
    pub fn primary(&self) -> Option<&str> {
        self.names.last().map(String::as_str)
    }

    /// Activate *name*, moving it to primary if already present.
    ///
    /// A blank name is a no-op (Python returns `self`; here an equal clone).
    /// Re-adding an active mode promotes it to primary rather than
    /// duplicating it — the newest intent wins the single upstream slot.
    pub fn add(&self, name: &str) -> ActiveNativeModes {
        let clean = name.trim();
        if clean.is_empty() {
            return self.clone();
        }
        let mut names: Vec<String> = self
            .names
            .iter()
            .filter(|existing| existing.as_str() != clean)
            .cloned()
            .collect();
        names.push(clean.to_string());
        ActiveNativeModes { names }
    }

    /// Deactivate *name*; a no-op when it is not active.
    pub fn remove(&self, name: &str) -> ActiveNativeModes {
        let clean = name.trim();
        ActiveNativeModes {
            names: self
                .names
                .iter()
                .filter(|n| n.as_str() != clean)
                .cloned()
                .collect(),
        }
    }

    /// Deactivate every native mode.
    pub fn clear(&self) -> ActiveNativeModes {
        ActiveNativeModes::default()
    }

    /// Python `__contains__` — exact membership, no trimming.
    pub fn contains(&self, name: &str) -> bool {
        self.names.iter().any(|n| n == name)
    }

    /// Python `__bool__` inverted: true when no native mode is active.
    pub fn is_empty(&self) -> bool {
        self.names.is_empty()
    }

    /// Python `__len__`.
    pub fn len(&self) -> usize {
        self.names.len()
    }

    /// Python `__iter__` — activation order (oldest first, primary last).
    pub fn iter(&self) -> impl Iterator<Item = &str> {
        self.names.iter().map(String::as_str)
    }
}

/// Primary first, then the rest of the stack newest-to-oldest.
///
/// Python's `_ordered_for_display` accepts `ActiveNativeModes | tuple`; the
/// Rust spelling is generic over anything string-like, so both
/// `&modes.names` and a plain `&["a", "b"]` literal work.
fn ordered_for_display<S: AsRef<str>>(names: &[S]) -> Vec<&str> {
    names.iter().rev().map(AsRef::as_ref).collect()
}

/// The footer badge for the active native-mode set (`""` when empty).
///
/// A single mode renders `◆ team-pulse` (unchanged from the single-slot
/// era). A stacked set renders the primary first then the others as `+`
/// entries — `◆ audit +team-pulse` — so the one actually enforced upstream
/// (`◆`) is visually distinct from the ones stacked behind it (`+`).
pub fn native_badge_text<S: AsRef<str>>(names: &[S]) -> String {
    let ordered = ordered_for_display(names);
    let Some((primary, rest)) = ordered.split_first() else {
        return String::new();
    };
    let mut badge = format!("{NATIVE_BADGE} {primary}");
    for name in rest {
        badge.push_str(" +");
        badge.push_str(name);
    }
    badge
}

/// True when *posture_id* denies (not merely asks for) tool use.
///
/// Derived from [`crate::model::trust`] rather than a hardcoded list: a
/// posture restricts tools when it *denies* the write capability (plan =
/// read-only, brainstorm = no tools). chat/build ask and auto allows, so
/// none of those nullify a native mode's tools.
pub fn posture_restricts_tools(posture_id: &str) -> bool {
    resolve_capability(posture_id, CapabilityClass::Write).decision == Decision::Deny
}

/// Notice text when a tool-restrictive posture coexists with native modes.
///
/// Returns `""` when there is no conflict (no native modes, or a posture
/// that does not deny tools). The message names the active modes and the
/// posture that is blocking them, and points at the fix — never a silent
/// nullification.
pub fn posture_conflict_notice<S: AsRef<str>>(posture_id: &str, names: &[S]) -> String {
    let ordered = ordered_for_display(names);
    if ordered.is_empty() || !posture_restricts_tools(posture_id) {
        return String::new();
    }
    let profile = get_mode(Some(posture_id));
    let joined = ordered.join(", ");
    let reads_denied =
        resolve_capability(posture_id, CapabilityClass::Read).decision == Decision::Deny;
    let blocks = if reads_denied {
        "blocks all tools".to_string()
    } else {
        format!("is {}", profile.trust_str)
    };
    format!(
        "{joined} active · {} {blocks} — /mode build or /mode auto to run its tools",
        profile.id.as_str()
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    // -- ActiveNativeModes: ordered set semantics -----------------------------

    #[test]
    fn test_empty_set_is_falsy_with_no_primary() {
        let modes = ActiveNativeModes::new();
        assert!(modes.is_empty());
        assert_eq!(modes.len(), 0);
        assert_eq!(modes.primary(), None);
        assert!(!modes.contains("team-pulse"));
    }

    #[test]
    fn test_add_appends_and_last_is_primary() {
        let modes = ActiveNativeModes::new().add("team-pulse").add("audit");
        assert_eq!(modes.names, ["team-pulse", "audit"]);
        // most-recently added is the enforced slot
        assert_eq!(modes.primary(), Some("audit"));
        assert_eq!(modes.len(), 2);
        assert!(modes.contains("team-pulse") && modes.contains("audit"));
    }

    #[test]
    fn test_re_adding_promotes_to_primary_without_duplicating() {
        let modes = ActiveNativeModes::new()
            .add("team-pulse")
            .add("audit")
            .add("team-pulse");
        assert_eq!(modes.names, ["audit", "team-pulse"]); // promoted, not duplicated
        assert_eq!(modes.primary(), Some("team-pulse"));
    }

    #[test]
    fn test_add_blank_is_noop() {
        let modes = ActiveNativeModes::new().add("audit");
        // Python asserts `modes.add("   ") is modes` (object identity);
        // Rust returns an equal value — value equality is the closest pin.
        assert_eq!(modes.add("   "), modes);
        assert_eq!(modes.add("audit").names, ["audit"]); // idempotent re-add of only mode
    }

    #[test]
    fn test_remove_promotes_next_and_missing_is_noop() {
        let modes = ActiveNativeModes::new().add("team-pulse").add("audit");
        let after = modes.remove("audit");
        assert_eq!(after.names, ["team-pulse"]);
        // next-newest promoted into the slot
        assert_eq!(after.primary(), Some("team-pulse"));
        assert_eq!(modes.remove("nope").names, modes.names); // unknown → unchanged
    }

    #[test]
    fn test_clear_empties_the_stack() {
        assert!(ActiveNativeModes::new()
            .add("a")
            .add("b")
            .clear()
            .names
            .is_empty());
    }

    #[test]
    fn test_is_frozen_value_type() {
        let modes = ActiveNativeModes::new().add("a");
        let other = modes.add("b");
        assert_eq!(modes.names, ["a"]); // original untouched (immutable)
        assert_eq!(other.names, ["a", "b"]);
    }

    #[test]
    fn test_iterates_in_activation_order() {
        let modes = ActiveNativeModes::new().add("a").add("b");
        assert_eq!(modes.iter().collect::<Vec<_>>(), ["a", "b"]);
    }

    // -- footer badge rendering ------------------------------------------------

    #[test]
    fn test_badge_empty_when_no_modes() {
        assert_eq!(native_badge_text(&ActiveNativeModes::new().names), "");
        assert_eq!(native_badge_text::<&str>(&[]), "");
    }

    #[test]
    fn test_badge_single_mode_matches_legacy() {
        assert_eq!(native_badge_text(&["team-pulse"]), "◆ team-pulse");
        assert_eq!(
            native_badge_text(&ActiveNativeModes::new().add("team-pulse").names),
            "◆ team-pulse"
        );
    }

    #[test]
    fn test_badge_stacked_marks_primary_first() {
        let modes = ActiveNativeModes::new().add("team-pulse").add("audit");
        // audit is primary (◆), team-pulse stacked behind it (+)
        assert_eq!(native_badge_text(&modes.names), "◆ audit +team-pulse");
    }

    #[test]
    fn test_badge_accepts_plain_tuple() {
        assert_eq!(
            native_badge_text(&["team-pulse", "audit", "careful"]),
            "◆ careful +audit +team-pulse"
        );
    }

    // -- tool-policy precedence: restrictive postures + conflict notice --------

    #[test]
    fn test_only_deny_postures_restrict_tools() {
        assert!(posture_restricts_tools("brainstorm")); // no tools
        assert!(posture_restricts_tools("plan")); // read-only (denies write)
        assert!(!posture_restricts_tools("chat")); // asks, never denies
        assert!(!posture_restricts_tools("build")); // asks
        assert!(!posture_restricts_tools("auto")); // allows
    }

    #[test]
    fn test_no_conflict_when_no_native_modes() {
        assert_eq!(
            posture_conflict_notice("brainstorm", &ActiveNativeModes::new().names),
            ""
        );
    }

    #[test]
    fn test_no_conflict_under_permissive_posture() {
        let modes = ActiveNativeModes::new().add("team-pulse");
        assert_eq!(posture_conflict_notice("build", &modes.names), "");
        assert_eq!(posture_conflict_notice("auto", &modes.names), "");
    }

    #[test]
    fn test_brainstorm_conflict_names_modes_and_the_fix() {
        let modes = ActiveNativeModes::new().add("team-pulse");
        let notice = posture_conflict_notice("brainstorm", &modes.names);
        assert!(notice.contains("team-pulse"));
        assert!(notice.contains("brainstorm"));
        assert!(notice.contains("blocks all tools")); // brainstorm denies even reads
        assert!(notice.contains("/mode build") || notice.contains("/mode auto"));
    }

    #[test]
    fn test_plan_conflict_is_read_only_not_all_blocked() {
        let modes = ActiveNativeModes::new().add("audit").add("team-pulse");
        let notice = posture_conflict_notice("plan", &modes.names);
        assert!(notice.contains("team-pulse") && notice.contains("audit"));
        assert!(notice.contains("read-only")); // plan allows reads
        assert!(!notice.contains("blocks all tools"));
    }

    // -- Rust-side pins against the Python oracle (exact full strings) ---------

    /// Oracle-checked against the real Python module (2026-07-26): the full
    /// notice strings, verbatim.
    #[test]
    fn conflict_notice_full_strings_match_python_oracle() {
        let stacked = ActiveNativeModes::new().add("audit").add("team-pulse");
        assert_eq!(
            posture_conflict_notice("plan", &stacked.names),
            "team-pulse, audit active · plan is read-only — /mode build or /mode auto to run its tools"
        );
        assert_eq!(
            posture_conflict_notice("brainstorm", &["team-pulse"]),
            "team-pulse active · brainstorm blocks all tools — /mode build or /mode auto to run its tools"
        );
        // Unknown posture resolves with chat's posture (ask, not deny) → no conflict.
        assert_eq!(posture_conflict_notice("bogus", &stacked.names), "");
        assert!(!posture_restricts_tools("bogus"));
    }
}
