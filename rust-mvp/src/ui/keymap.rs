//! Keymap as data: one table feeding key dispatch AND footer hints.
//!
//! Port of `src/amplifier_app_newtui/ui/keymap.py` (itself after
//! amplifier-app-cli `ui/key_bindings_table.py`, after codex `keymap.rs`):
//! every binding knows its key chord(s), its on-screen hint label, and the
//! UI contexts it is active in. Because both the key handlers and the
//! footer read the same table, the keys that work and the keys the UI
//! advertises can never drift.
//!
//! Shift+Enter needs the kitty keyboard protocol; on legacy terminals the
//! `fallback = true` alt+enter chord is the working alternative and
//! [`hint_label`] swaps the advertised label via overrides after the
//! terminal probe (DESIGN-SPEC §12).
//!
//! Esc precedence is specified as a table, not emergent behavior (codex
//! lesson): [`ESC_CHAIN`] is the priority order from DESIGN-SPEC §5.

use std::collections::HashMap;
use std::fmt;

/// UI contexts a binding can be active in (spec §2/§5 surfaces).
///
/// Python `Context = Literal[...]` — exact strings via [`Context::as_str`].
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub enum Context {
    /// composer focused, no turn running
    Idle,
    /// a turn is executing
    Running,
    /// command palette strip open
    Palette,
    /// workspace-file autocomplete open
    Mention,
    /// agent lanes panel open
    Lanes,
    /// a subagent lane is focused (child transcript shown)
    LaneFocus,
    /// rewind picker strip open
    Rewind,
    /// approval bar replaces the composer
    Approval,
    /// needs-you block focused
    NeedsYou,
    /// evidence block open
    Evidence,
}

impl Context {
    /// Every context, in declaration order.
    pub const ALL: [Context; 10] = [
        Context::Idle,
        Context::Running,
        Context::Palette,
        Context::Mention,
        Context::Lanes,
        Context::LaneFocus,
        Context::Rewind,
        Context::Approval,
        Context::NeedsYou,
        Context::Evidence,
    ];

    /// The exact Python literal string value.
    pub fn as_str(self) -> &'static str {
        match self {
            Context::Idle => "idle",
            Context::Running => "running",
            Context::Palette => "palette",
            Context::Mention => "mention",
            Context::Lanes => "lanes",
            Context::LaneFocus => "lane_focus",
            Context::Rewind => "rewind",
            Context::Approval => "approval",
            Context::NeedsYou => "needs_you",
            Context::Evidence => "evidence",
        }
    }
}

impl fmt::Display for Context {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// An immutable set of [`Context`]s — the Rust spelling of Python's
/// `frozenset[Context]` (bitmask over the ten variants).
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub struct ContextSet(u16);

impl ContextSet {
    /// `frozenset()` — the empty set.
    pub const EMPTY: ContextSet = ContextSet(0);

    const fn bit(context: Context) -> u16 {
        1 << (context as u16)
    }

    /// Build a set from the listed contexts.
    pub const fn of(contexts: &[Context]) -> ContextSet {
        let mut mask = 0u16;
        let mut i = 0;
        while i < contexts.len() {
            mask |= Self::bit(contexts[i]);
            i += 1;
        }
        ContextSet(mask)
    }

    /// `context in set`.
    pub const fn contains(self, context: Context) -> bool {
        self.0 & Self::bit(context) != 0
    }

    /// Set difference by one member (`ALL_CONTEXTS - {"approval"}`).
    pub const fn without(self, context: Context) -> ContextSet {
        ContextSet(self.0 & !Self::bit(context))
    }

    /// `self <= other`.
    pub const fn is_subset(self, other: ContextSet) -> bool {
        self.0 & !other.0 == 0
    }

    /// Members in [`Context::ALL`] order.
    pub fn iter(self) -> ContextSetIter {
        ContextSetIter { set: self, index: 0 }
    }
}

/// Iterator over a [`ContextSet`]'s members in [`Context::ALL`] order.
#[derive(Clone, Copy, Debug)]
pub struct ContextSetIter {
    set: ContextSet,
    index: usize,
}

impl Iterator for ContextSetIter {
    type Item = Context;

    fn next(&mut self) -> Option<Context> {
        while self.index < Context::ALL.len() {
            let context = Context::ALL[self.index];
            self.index += 1;
            if self.set.contains(context) {
                return Some(context);
            }
        }
        None
    }
}

impl IntoIterator for ContextSet {
    type Item = Context;
    type IntoIter = ContextSetIter;

    fn into_iter(self) -> ContextSetIter {
        self.iter()
    }
}

/// The full set of contexts (Python `ALL_CONTEXTS`).
pub const ALL_CONTEXTS: ContextSet = ContextSet::of(&Context::ALL);

/// The approval bar owns the keyboard while visible; most global chords
/// are suppressed under it (Python `NO_APPROVAL = ALL_CONTEXTS - {"approval"}`).
pub const NO_APPROVAL: ContextSet = ALL_CONTEXTS.without(Context::Approval);

const MAX_LABEL_CHARS: usize = 32;

/// One key chord bound to a named action in a set of UI contexts.
///
/// - `action`: stable action id the app dispatches on.
/// - `keys`: key chord names (e.g. `"shift+tab"`, `"ctrl+t"`). Multiple
///   entries for one action are alternates.
/// - `label`: hint text advertised for this chord; the first labeled table
///   entry per action wins (see [`hint_label`]).
/// - `contexts`: UI states the binding is active in.
/// - `fallback`: true for legacy-terminal alternates (alt+enter for
///   shift+enter) — registered always, advertised only when the terminal
///   probe says the primary chord cannot arrive.
///
/// Frozen pydantic model in Python; immutable by convention here.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Binding {
    pub action: &'static str,
    pub keys: &'static [&'static str],
    pub label: &'static str,
    pub contexts: ContextSet,
    pub fallback: bool,
}

const fn b(
    action: &'static str,
    keys: &'static [&'static str],
    label: &'static str,
    contexts: ContextSet,
) -> Binding {
    Binding {
        action,
        keys,
        label,
        contexts,
        fallback: false,
    }
}

const fn b_fallback(
    action: &'static str,
    keys: &'static [&'static str],
    label: &'static str,
    contexts: ContextSet,
) -> Binding {
    Binding {
        action,
        keys,
        label,
        contexts,
        fallback: true,
    }
}

const PALETTE: ContextSet = ContextSet::of(&[Context::Palette]);
const MENTION: ContextSet = ContextSet::of(&[Context::Mention]);
const LANES: ContextSet = ContextSet::of(&[Context::Lanes]);
const LANE_FOCUS: ContextSet = ContextSet::of(&[Context::LaneFocus]);
const REWIND: ContextSet = ContextSet::of(&[Context::Rewind]);
const APPROVAL: ContextSet = ContextSet::of(&[Context::Approval]);
const EVIDENCE: ContextSet = ContextSet::of(&[Context::Evidence]);
const RUNNING: ContextSet = ContextSet::of(&[Context::Running]);
const IDLE: ContextSet = ContextSet::of(&[Context::Idle]);
const IDLE_RUNNING: ContextSet = ContextSet::of(&[Context::Idle, Context::Running]);

/// The one table (Python `KEYMAP`), in the exact source order.
pub const KEYMAP: [Binding; 43] = [
    // Submission / steering / queueing (spec §5).
    b("submit", &["enter"], "enter", IDLE),
    b("steer", &["enter"], "enter", RUNNING),
    b("insert_newline", &["ctrl+j", "ctrl+enter"], "ctrl+j", NO_APPROVAL),
    b("history_prev", &["up"], "↑", IDLE_RUNNING),
    b("history_next", &["down"], "↓", IDLE_RUNNING),
    b("queue_message", &["shift+enter"], "shift+enter", NO_APPROVAL),
    b_fallback("queue_message", &["alt+enter"], "alt+enter", NO_APPROVAL),
    // Mode & permission cycles (independent controls, ADR-0005 amendment).
    b("cycle_mode", &["shift+tab"], "shift+tab", NO_APPROVAL),
    b("cycle_permission", &["ctrl+p"], "ctrl-p", NO_APPROVAL),
    // Panels / pickers.
    b("toggle_lanes", &["ctrl+t"], "ctrl-t", NO_APPROVAL),
    b("cycle_tail", &["ctrl+o"], "ctrl-o", NO_APPROVAL),
    // Show/hide the root stream box (thinking/response peek). Advertised
    // only while a turn runs — that is the only time a live box exists.
    b("toggle_thinking", &["ctrl+g"], "ctrl-g think", RUNNING),
    b("show_ledger", &["ctrl+l"], "ctrl-l", NO_APPROVAL),
    b("show_needs_you", &["ctrl+y"], "ctrl-y", NO_APPROVAL),
    b("open_rewind", &["ctrl+r"], "ctrl-r", NO_APPROVAL),
    // In-panel navigation.
    b("palette_up", &["up"], "↑↓", PALETTE),
    b("palette_down", &["down"], "↑↓", PALETTE),
    b("palette_run", &["enter"], "enter", PALETTE),
    b("mention_up", &["up"], "↑↓", MENTION),
    b("mention_down", &["down"], "↑↓", MENTION),
    b("mention_accept", &["enter", "tab"], "enter/tab", MENTION),
    b("mention_close", &["escape"], "esc", MENTION),
    b("lane_up", &["up"], "↑↓", LANES),
    b("lane_down", &["down"], "↑↓", LANES),
    b("focus_lane", &["enter"], "enter", LANES),
    b("rewind_prev", &["left"], "‹ ›", REWIND),
    b("rewind_next", &["right"], "‹ ›", REWIND),
    b("rewind_fork", &["enter"], "enter fork", REWIND),
    b("evidence_prev", &["left"], "←/→", EVIDENCE),
    b("evidence_next", &["right"], "←/→", EVIDENCE),
    b("evidence_expand", &["enter"], "enter", EVIDENCE),
    // Approval bar (owns the keyboard while open, spec §7). Mockup
    // keydown: `e.key === "Tab"` matches with or without shift, so
    // shift+tab cycles the selection here — never the mode.
    b("approval_prev", &["left", "up"], "arrows", APPROVAL),
    b("approval_next", &["right", "down", "tab", "shift+tab"], "arrows", APPROVAL),
    b("approval_confirm", &["enter"], "enter", APPROVAL),
    // ctrl-y parks the live ticket into the needs-you queue without
    // answering it (ADR-0007 approvals: "ctrl-y defers head to
    // NeedsYouQueue"; the bar owns the keyboard, so the global ctrl-y
    // show_needs_you is suppressed while it is open). Handled by the
    // approval bar's key handler — documented here so the table stays the
    // single source of every approval-context chord (footer hint stays
    // spec-exact).
    b("approval_defer", &["ctrl+y"], "ctrl-y defer", APPROVAL),
    // Esc chain — one binding per context; the app resolves priority via
    // ESC_CHAIN, never ad-hoc if/else ladders (spec §5).
    b("lane_unfocus", &["escape"], "esc", LANE_FOCUS),
    b("close_palette", &["escape"], "esc", PALETTE),
    b("close_rewind", &["escape"], "esc", REWIND),
    b("close_lanes", &["escape"], "esc", LANES),
    b("close_evidence", &["escape"], "esc", EVIDENCE),
    b("approval_deny", &["escape"], "esc", APPROVAL),
    b("interrupt_running", &["escape"], "esc", RUNNING),
    // Display-only affordance: "/" is ordinary composer text that opens
    // the palette; the footer still advertises it.
    b("open_palette", &[], "/", ContextSet::EMPTY),
];

/// Esc priority order (DESIGN-SPEC §5): the first entry whose context is
/// active consumes the Esc press. (Approval and evidence esc handling are
/// context-exclusive — the approval bar owns the keyboard, and evidence esc
/// only fires while the evidence block has focus — so they sit outside the
/// global chain.)
pub const ESC_CHAIN: [(Context, &str); 5] = [
    (Context::LaneFocus, "lane_unfocus"),
    (Context::Palette, "close_palette"),
    (Context::Rewind, "close_rewind"),
    (Context::Lanes, "close_lanes"),
    (Context::Running, "interrupt_running"),
];

/// A second Esc after interrupt opens rewind through the existing picker.
pub const ESC_BACKTRACK_WINDOW_SECONDS: f64 = 0.75;

/// Footer hint strings — EXACT text per DESIGN-SPEC §2 (Python
/// `FOOTER_HINTS` dict, keyed by context string).
pub const FOOTER_HINTS: [(&str, &str); 6] = [
    ("approval", "arrows select · enter confirm · esc deny"),
    ("lane_focus", "esc back to parent · transcript is the subagent's own"),
    ("palette", "↑↓ select · enter run · esc close"),
    ("mention", "↑↓ select · enter/tab insert · esc close"),
    ("running", "esc interrupt · enter steer · shift+enter queue"),
    ("idle", "↑ history · ctrl+j newline · ctrl-r rewind · / commands"),
];

/// `FOOTER_HINTS[context]` — the dict lookup as a function.
pub fn footer_hint(context: &str) -> Option<&'static str> {
    FOOTER_HINTS
        .iter()
        .find(|(key, _)| *key == context)
        .map(|(_, hint)| *hint)
}

/// Composer placeholder — exact string per DESIGN-SPEC §2.
pub const COMPOSER_PLACEHOLDER: &str =
    "Message Amplifier…  ( ↑ history · ctrl+j newline · enter send · / commands )";

/// Reject malformed tables.
///
/// Fails on: empty actions, oversized or missing labels, and — the point
/// of the exercise — two different actions claiming the same key while
/// the same context is active. Alternate chords for the SAME action
/// (shift+enter / alt+enter) are allowed.
pub fn validate(keymap: &[Binding]) -> Result<(), String> {
    let mut claimed: HashMap<(&str, Context), &str> = HashMap::new();
    for binding in keymap {
        if binding.action.is_empty() {
            return Err("binding with empty action".to_string());
        }
        if binding.label.is_empty() {
            return Err(format!("binding '{}' needs a display label", binding.action));
        }
        if binding.label.chars().count() > MAX_LABEL_CHARS {
            return Err(format!("binding '{}' display label too long", binding.action));
        }
        for key in binding.keys {
            for context in binding.contexts.iter() {
                let slot = (*key, context);
                if let Some(other) = claimed.get(&slot) {
                    if *other != binding.action {
                        return Err(format!(
                            "key '{key}' in context '{context}' is claimed by both \
                             '{other}' and '{}'",
                            binding.action
                        ));
                    }
                }
                claimed.insert(slot, binding.action);
            }
        }
    }
    Ok(())
}

/// On-screen label for `action` (first labeled table entry wins; fallbacks
/// never win the advertised label by default).
///
/// `overrides` is the terminal-capability seam: after the probe, pass
/// `&[("queue_message", "alt+enter")]` on terminals where real shift+enter
/// never arrives. Errors for unknown actions so a typo fails loudly
/// instead of rendering a stale shortcut (Python raises `KeyError`).
pub fn hint_label(action: &str, overrides: Option<&[(&str, &str)]>) -> Result<String, String> {
    if let Some(overrides) = overrides {
        if let Some((_, over)) = overrides.iter().find(|(key, _)| *key == action) {
            if !over.is_empty() {
                return Ok(over.chars().take(MAX_LABEL_CHARS).collect());
            }
        }
    }
    // Action → first labeled non-fallback binding …
    for binding in KEYMAP.iter() {
        if !binding.label.is_empty() && !binding.fallback && binding.action == action {
            return Ok(binding.label.to_string());
        }
    }
    // … fallback-only actions still get a label.
    for binding in KEYMAP.iter() {
        if !binding.label.is_empty() && binding.action == action {
            return Ok(binding.label.to_string());
        }
    }
    Err(format!("no display label for action '{action}'"))
}

/// All bindings active in `context`, in table order.
pub fn bindings_for(context: Context) -> Vec<&'static Binding> {
    KEYMAP
        .iter()
        .filter(|binding| binding.contexts.contains(context))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashSet;

    #[test]
    fn test_keymap_validates_clean() {
        validate(&KEYMAP).unwrap();
    }

    #[test]
    fn test_required_actions_present_with_expected_keys() {
        let mut by_action: HashMap<&str, Vec<&Binding>> = HashMap::new();
        for binding in KEYMAP.iter() {
            by_action.entry(binding.action).or_default().push(binding);
        }
        assert_eq!(by_action["cycle_mode"][0].keys, ["shift+tab"]);
        assert_eq!(by_action["toggle_lanes"][0].keys, ["ctrl+t"]);
        assert_eq!(by_action["show_ledger"][0].keys, ["ctrl+l"]);
        assert_eq!(by_action["show_needs_you"][0].keys, ["ctrl+y"]);
        assert_eq!(by_action["open_rewind"][0].keys, ["ctrl+r"]);
        assert_eq!(by_action["submit"][0].keys, ["enter"]);
        assert_eq!(by_action["insert_newline"][0].keys, ["ctrl+j", "ctrl+enter"]);
        assert_eq!(by_action["history_prev"][0].keys, ["up"]);
        assert_eq!(by_action["history_next"][0].keys, ["down"]);
    }

    #[test]
    fn test_shift_enter_with_alt_enter_fallback() {
        let queue: Vec<&Binding> = KEYMAP
            .iter()
            .filter(|b| b.action == "queue_message")
            .collect();
        assert_eq!(queue.len(), 2);
        let primary = queue.iter().find(|b| !b.fallback).unwrap();
        let fallback = queue.iter().find(|b| b.fallback).unwrap();
        assert_eq!(primary.keys, ["shift+enter"]);
        assert_eq!(fallback.keys, ["alt+enter"]);
        // The advertised label defaults to the primary chord …
        assert_eq!(hint_label("queue_message", None).unwrap(), "shift+enter");
        // … and the terminal probe swaps it via overrides on legacy terminals.
        assert_eq!(
            hint_label("queue_message", Some(&[("queue_message", "alt+enter")])).unwrap(),
            "alt+enter"
        );
    }

    #[test]
    fn test_esc_chain_priority_order_per_spec() {
        // DESIGN-SPEC §5: lane-focus → palette → rewind → lanes → interrupt.
        let order: Vec<&str> = ESC_CHAIN.iter().map(|(c, _)| c.as_str()).collect();
        assert_eq!(order, ["lane_focus", "palette", "rewind", "lanes", "running"]);
        // Every chained action really is an escape binding in that context.
        for (context, action) in ESC_CHAIN.iter() {
            let bindings: Vec<&Binding> = bindings_for(*context)
                .into_iter()
                .filter(|b| b.action == *action)
                .collect();
            assert!(!bindings.is_empty(), "{context} {action}");
            assert!(bindings[0].keys.contains(&"escape"));
        }
        assert_eq!(ESC_BACKTRACK_WINDOW_SECONDS, 0.75);
    }

    #[test]
    fn test_footer_hints_exact_spec_strings() {
        assert_eq!(
            footer_hint("approval").unwrap(),
            "arrows select · enter confirm · esc deny"
        );
        assert_eq!(
            footer_hint("lane_focus").unwrap(),
            "esc back to parent · transcript is the subagent's own"
        );
        assert_eq!(footer_hint("palette").unwrap(), "↑↓ select · enter run · esc close");
        assert_eq!(
            footer_hint("mention").unwrap(),
            "↑↓ select · enter/tab insert · esc close"
        );
        assert_eq!(
            footer_hint("running").unwrap(),
            "esc interrupt · enter steer · shift+enter queue"
        );
        assert_eq!(
            footer_hint("idle").unwrap(),
            "↑ history · ctrl+j newline · ctrl-r rewind · / commands"
        );
    }

    #[test]
    fn test_composer_placeholder_exact() {
        assert_eq!(
            COMPOSER_PLACEHOLDER,
            "Message Amplifier…  ( ↑ history · ctrl+j newline · enter send · / commands )"
        );
    }

    #[test]
    fn test_validate_rejects_conflicts() {
        let mut conflicted: Vec<Binding> = KEYMAP.to_vec();
        conflicted.push(Binding {
            action: "something_else",
            keys: &["shift+tab"],
            label: "shift+tab",
            contexts: ContextSet::of(&[Context::Idle]),
            fallback: false,
        });
        let err = validate(&conflicted).unwrap_err();
        assert!(err.contains("claimed by both"), "{err}");
    }

    #[test]
    fn test_validate_rejects_missing_label() {
        let bad = [Binding {
            action: "x",
            keys: &["ctrl+q"],
            label: "",
            contexts: ContextSet::of(&[Context::Idle]),
            fallback: false,
        }];
        let err = validate(&bad).unwrap_err();
        assert!(err.contains("display label"), "{err}");
    }

    #[test]
    fn test_hint_label_unknown_action_fails_loudly() {
        let err = hint_label("no_such_action", None).unwrap_err();
        assert_eq!(err, "no display label for action 'no_such_action'");
    }

    #[test]
    fn test_open_palette_is_display_only() {
        let binding = KEYMAP.iter().find(|b| b.action == "open_palette").unwrap();
        assert!(binding.keys.is_empty());
        assert_eq!(binding.label, "/");
    }

    #[test]
    fn test_contexts_are_known() {
        for binding in KEYMAP.iter() {
            assert!(binding.contexts.is_subset(ALL_CONTEXTS));
        }
    }

    #[test]
    fn test_file_mention_keys_live_in_the_keymap_table() {
        let actions: HashSet<&str> = bindings_for(Context::Mention)
            .into_iter()
            .map(|b| b.action)
            .collect();
        for required in ["mention_up", "mention_down", "mention_accept", "mention_close"] {
            assert!(actions.contains(required), "{required}");
        }
    }

    #[test]
    fn test_approval_context_suppresses_global_chords() {
        let approval_actions: HashSet<&str> = bindings_for(Context::Approval)
            .into_iter()
            .map(|b| b.action)
            .collect();
        assert!(!approval_actions.contains("cycle_mode"));
        assert!(!approval_actions.contains("queue_message"));
        for required in [
            "approval_prev",
            "approval_next",
            "approval_confirm",
            "approval_deny",
        ] {
            assert!(approval_actions.contains(required), "{required}");
        }
    }

    #[test]
    fn test_cycle_tail_is_bound_to_ctrl_o_everywhere_but_approval() {
        let binding = KEYMAP.iter().find(|b| b.action == "cycle_tail").unwrap();
        assert_eq!(binding.keys, ["ctrl+o"]);
        assert_eq!(binding.contexts, NO_APPROVAL);
    }

    #[test]
    fn test_approval_defer_parks_on_ctrl_y_in_approval_context_only() {
        // Issue #41: ctrl-y parks the live ticket into the needs-you queue.
        //
        // The chord lives in the approval context only — globally ctrl-y is
        // show_needs_you (NO_APPROVAL), and the bar owns the keyboard while
        // open, so the same key means "defer THIS ticket" there. validate()
        // accepts the split because no single (key, context) is double-claimed.
        let defer = KEYMAP.iter().find(|b| b.action == "approval_defer").unwrap();
        assert_eq!(defer.keys, ["ctrl+y"]);
        assert_eq!(defer.contexts, ContextSet::of(&[Context::Approval]));
        let show = KEYMAP.iter().find(|b| b.action == "show_needs_you").unwrap();
        assert_eq!(show.keys, ["ctrl+y"]);
        assert!(!show.contexts.contains(Context::Approval));
        validate(&KEYMAP).unwrap(); // the ctrl-y split does not trip the conflict guard
    }
}
