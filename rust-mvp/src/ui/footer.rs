//! Footer status bar (DESIGN-SPEC §2 item 6).
//!
//! Port of `src/amplifier_app_newtui/ui/footer.py`.
//!
//! Left segment: `mode <mode>` (mode color) `· <trust> · bundle <bundle> ·
//! <model> · <session-short> · $<cost>` — segment text dim, the inline `·`
//! separators dimmer (mockup: each is its own `--dimmer` span) — plus the
//! green `▲` yield glyph when the last turn shipped and an orange `· q1`
//! when a next-turn message is queued; an optional orange, clickable
//! `N decisions waiting · ctrl-y` badge preceded by a dimmer `·` separator.
//!
//! Right segment: context-sensitive hints — the EXACT strings from
//! [`crate::ui::keymap::FOOTER_HINTS`], except the running hint which is
//! composed live from [`crate::ui::keymap::hint_label`] so the advertised
//! queue chord swaps to `alt+enter` on terminals without the kitty keyboard
//! protocol.
//!
//! Like the mockup's `flex-wrap: wrap` footer, when both segments do not fit
//! on one row the hints drop to their own full-width second row instead of
//! clipping; when the left segment plus the waiting badge still exceed the
//! width, the badge drops to its own row too (separator hidden) so the
//! `ctrl-y` affordance stays fully readable. [`footer_wrap`] is that
//! decision as a pure function.
//!
//! All rendering is a pure function of [`FooterState`] — the Textual widget
//! was a dumb painter, so the ratatui port keeps only the pure surface:
//! text builders, the narrow-width fit ladder, [`footer_left_segments`]
//! (the styled spans the widget painted), and the wrap decision. Widget
//! mechanics — mounting, resize events, CSS classes, the clickable badge's
//! message pump — are the app-assembly layer's job: it holds a
//! [`FooterState`], repaints from these functions on state/width change,
//! and maps a click on the badge's screen region to whatever
//! `WaitingBadgeClicked` handling it wants.

use rust_decimal::Decimal;

use crate::model::blocks::{Segment, StyleToken, GLYPH_YIELD};
use crate::model::modes::{profile, ModeId};
use crate::model::native_modes::native_badge_text;
use crate::ui::keymap::{footer_hint, hint_label, Context};

pub const SEPARATOR: &str = " · ";

/// Minimum cells between the left segment and the right hints before wrapping.
const SEGMENT_GAP: usize = 2;

/// Terminal cell width of `s` (Python: `rich.cells.cell_len`).
fn cell_len(s: &str) -> usize {
    ratatui::text::Span::raw(s).width()
}

/// Everything the footer needs to paint, as one frozen value.
///
/// Frozen pydantic model in Python (`frozen=True, extra="forbid"`,
/// `ge=0` on the counters and cost); immutable by convention here.
#[derive(Clone, Debug, PartialEq)]
pub struct FooterState {
    pub mode_id: ModeId,
    /// Active bundle-composed modes (`/mode <name>`), in activation order —
    /// the LAST is the primary (the one enforced upstream). Shown as a
    /// `◆ <primary> +<others>` badge next to the posture so activation is
    /// visible and sticky. A single active mode renders exactly as the old
    /// single-slot badge did (backward compatible).
    pub native_modes: Vec<String>,
    /// Bundle name — painted with a `bundle ` label (story #4: the footer
    /// speaks human; a bare `newtui` reads as noise).
    pub bundle: String,
    /// Primary model id, already bare (`claude-fable-5`, no provider
    /// prefix) — its own dim part between the bundle and the session.
    pub model: String,
    pub session_short: String,
    pub cost: Decimal,
    /// True when any usage this session was unpriceable → the total is a
    /// floor, rendered `~$1.23` (never lie in the footer).
    pub cost_estimated: bool,
    /// True when the last turn shipped → green `▲` yield glyph.
    pub shipped: bool,
    /// Queued next-turn messages → orange `· qN` marker.
    pub queued: u64,
    /// Deferred needs-you decisions → orange `N decisions waiting · ctrl-y`.
    pub waiting: u64,
    pub plan_done: u64,
    /// Plan fallback count — non-zero only while the plan panel is hidden
    /// (narrow terminal); the footer then carries `Plan N/M` (design D2).
    pub plan_total: u64,
    /// Which hint set the right segment shows.
    pub context: Context,
    /// Terminal probe result; false swaps shift+enter → alt+enter in hints.
    pub kitty_protocol: bool,
}

impl Default for FooterState {
    fn default() -> Self {
        FooterState {
            mode_id: ModeId::Chat,
            native_modes: Vec::new(),
            bundle: String::new(),
            model: String::new(),
            session_short: String::new(),
            cost: Decimal::ZERO,
            cost_estimated: false,
            shipped: false,
            queued: 0,
            waiting: 0,
            plan_done: 0,
            plan_total: 0,
            context: Context::Idle,
            kitty_protocol: true,
        }
    }
}

// -- pure text builders (exact strings; tests assert on these) ---------------

/// Which decorative left-segment parts are dropped (Python's `_fit_drops`
/// dict, inverted: a `true` field means the part is dropped). The default is
/// no drops — the full segment.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct FitDrops {
    pub trust: bool,
    pub session: bool,
    pub bundle: bool,
    pub model: bool,
}

/// Decorations in drop order: trust posture (the mode chip keeps the id),
/// then session id, then bundle, then the model — the model is the identity
/// users actually ask about, so it outlives the other decorations (story #4).
/// Mode, cost, queue and `Plan n/m` never drop — design D2's footer
/// fallback only works if the plan count survives.
const FIT_LADDER: [FitDrops; 4] = [
    FitDrops { trust: true, session: false, bundle: false, model: false },
    FitDrops { trust: true, session: true, bundle: false, model: false },
    FitDrops { trust: true, session: true, bundle: true, model: false },
    FitDrops { trust: true, session: true, bundle: true, model: true },
];

/// The `$<cost>` part (with `~` floor marker and `▲` yield glyph as asked).
///
/// `round_dp` uses banker's rounding (MidpointNearestEven), matching the
/// default decimal context Python's `f"{Decimal:.2f}"` formats with; `{:.2}`
/// then only pads.
fn cost_part(state: &FooterState, shipped_glyph: bool) -> String {
    let mut part = format!(
        "{}${:.2}",
        if state.cost_estimated { "~" } else { "" },
        state.cost.round_dp(2)
    );
    if shipped_glyph && state.shipped {
        part.push(' ');
        part.push_str(GLYPH_YIELD);
    }
    part
}

/// The left-segment parts, with decorative ones optionally dropped.
fn left_parts(state: &FooterState, drops: FitDrops) -> Vec<String> {
    let mode = profile(state.mode_id);
    let mut parts = vec![format!("mode {}", mode.id)];
    let badge = native_badge_text(&state.native_modes);
    if !badge.is_empty() {
        parts.push(badge);
    }
    if !drops.trust {
        parts.push(mode.trust_str.to_string());
    }
    if !drops.bundle && !state.bundle.is_empty() {
        parts.push(format!("bundle {}", state.bundle));
    }
    if !drops.model && !state.model.is_empty() {
        parts.push(state.model.clone());
    }
    if !drops.session && !state.session_short.is_empty() {
        parts.push(state.session_short.clone());
    }
    parts.push(cost_part(state, true));
    if state.queued > 0 {
        parts.push(format!("q{}", state.queued));
    }
    if state.plan_total > 0 {
        parts.push(format!("Plan {}/{}", state.plan_done, state.plan_total));
    }
    parts
}

/// The full left segment as plain text.
pub fn footer_left_text(state: &FooterState) -> String {
    left_parts(state, FitDrops::default()).join(SEPARATOR)
}

/// The mildest ladder step whose left text fits *width* cells.
///
/// Python's `width <= 0` pre-layout case is `width == 0` here (Textual
/// widths are never negative in practice).
fn fit_drops(state: &FooterState, width: usize) -> FitDrops {
    if width == 0 || cell_len(&footer_left_text(state)) <= width {
        return FitDrops::default();
    }
    for drops in FIT_LADDER {
        if cell_len(&left_parts(state, drops).join(SEPARATOR)) <= width {
            return drops;
        }
    }
    FIT_LADDER[FIT_LADDER.len() - 1]
}

/// The left segment, decorations dropped until it fits *width* cells.
///
/// Found live in forge at 80 cols: the full segment overflowed and the
/// terminal clipped `Plan n/m` — the one part the narrow-width ladder
/// exists to show. `width == 0` (pre-layout) returns the full string.
pub fn footer_left_text_fit(state: &FooterState, width: usize) -> String {
    left_parts(state, fit_drops(state, width)).join(SEPARATOR)
}

/// The waiting badge text; empty when nothing is deferred.
pub fn footer_waiting_text(state: &FooterState) -> String {
    if state.waiting == 0 {
        return String::new();
    }
    let plural = if state.waiting != 1 { "s" } else { "" };
    format!("{} decision{} waiting · ctrl-y", state.waiting, plural)
}

/// Context-sensitive hints (exact DESIGN-SPEC §2 strings).
pub fn footer_right_text(state: &FooterState) -> String {
    if state.context == Context::Running {
        const ALT_ENTER: [(&str, &str); 1] = [("queue_message", "alt+enter")];
        let overrides: Option<&[(&str, &str)]> = if state.kitty_protocol {
            None
        } else {
            Some(&ALT_ENTER)
        };
        let queue_chord =
            hint_label("queue_message", overrides).expect("queue_message is a known keymap action");
        return format!("esc interrupt · enter steer · {queue_chord} queue");
    }
    footer_hint(state.context.as_str())
        .unwrap_or_else(|| footer_hint("idle").expect("idle hint exists"))
        .to_string()
}

// -- painted form (what the Textual widget's _repaint produced) ---------------

fn seg(text: impl Into<String>, token: StyleToken) -> Segment {
    Segment {
        style_token: token,
        ..Segment::new(text)
    }
}

/// The painted left segment as styled spans (was `FooterBar._repaint`'s
/// left-Static markup): `mode <id>` in the mode color, segments dim with
/// dimmer `·` separators (mockup: each inline `·` is its own `--dimmer`
/// span), the native badge teal, `▲` green, `· qN` orange, `Plan n/m` dim.
/// Decorations are dropped per the fit ladder for *width* cells.
///
/// The Python parts carried no explicit style and inherited the widget's
/// `color: $dim` CSS; the segments spell that inherited dim out (styling by
/// token only — `to_ratatui_line` resolves colors from the theme).
pub fn footer_left_segments(state: &FooterState, width: usize) -> Vec<Segment> {
    let mode = profile(state.mode_id);
    let drops = fit_drops(state, width);

    let mut rest_parts: Vec<String> = Vec::new();
    if !drops.trust {
        rest_parts.push(mode.trust_str.to_string());
    }
    if !drops.bundle && !state.bundle.is_empty() {
        rest_parts.push(format!("bundle {}", state.bundle));
    }
    if !drops.model && !state.model.is_empty() {
        rest_parts.push(state.model.clone());
    }
    if !drops.session && !state.session_short.is_empty() {
        rest_parts.push(state.session_short.clone());
    }
    rest_parts.push(cost_part(state, false));

    let mut segments = vec![seg(format!("mode {}", mode.id), mode.color_token)];
    let native_badge = native_badge_text(&state.native_modes);
    if !native_badge.is_empty() {
        segments.push(seg(SEPARATOR, StyleToken::Dimmer));
        segments.push(seg(native_badge, StyleToken::Teal));
    }
    for part in rest_parts {
        segments.push(seg(SEPARATOR, StyleToken::Dimmer));
        segments.push(seg(part, StyleToken::Dim));
    }
    if state.shipped {
        segments.push(seg(" ", StyleToken::Dim));
        segments.push(seg(GLYPH_YIELD, StyleToken::Green));
    }
    if state.queued > 0 {
        segments.push(seg(format!("{SEPARATOR}q{}", state.queued), StyleToken::Orange));
    }
    if state.plan_total > 0 {
        segments.push(seg(SEPARATOR, StyleToken::Dimmer));
        segments.push(seg(
            format!("Plan {}/{}", state.plan_done, state.plan_total),
            StyleToken::Dim,
        ));
    }
    segments
}

// -- wrap decision (was FooterBar._update_wrap) --------------------------------

/// Which footer rows wrap at *width* cells (the Textual `-wrapped` /
/// `-badge-wrapped` CSS classes as data).
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct FooterWrap {
    /// The right hints drop to their own full-width second row.
    pub wrapped: bool,
    /// The waiting badge drops to its own row too (separator hidden).
    pub badge_wrapped: bool,
}

/// Drop the hints onto their own row when one row can't fit both.
///
/// Mirrors the mockup footer's `flex-wrap: wrap` — segments stay fully
/// readable instead of the right hints clipping off-screen. `width == 0`
/// (pre-layout) reports no wrapping, like the Python early return that left
/// the classes untouched.
pub fn footer_wrap(state: &FooterState, width: usize) -> FooterWrap {
    if width == 0 {
        return FooterWrap::default();
    }
    let mut group_needed = cell_len(&footer_left_text_fit(state, width));
    let badge_text = footer_waiting_text(state);
    if !badge_text.is_empty() {
        // dimmer "·" separator (padding 0 1) + badge (padding-right 1)
        group_needed += 3 + cell_len(&badge_text) + 1;
    }
    let needed = group_needed + SEGMENT_GAP + cell_len(&footer_right_text(state));
    FooterWrap {
        wrapped: needed > width,
        badge_wrapped: !badge_text.is_empty() && group_needed > width,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::ui::segments::line_plain;
    use std::str::FromStr;

    /// Python test module's FULL_STATE fixture.
    fn full_state() -> FooterState {
        FooterState {
            mode_id: ModeId::Build,
            bundle: "dev-bundle".to_string(),
            model: "claude-fable-5".to_string(),
            session_short: "a1b2c3".to_string(),
            cost: Decimal::from_str("0.87").unwrap(),
            shipped: true,
            queued: 1,
            waiting: 0,
            context: Context::Idle,
            ..FooterState::default()
        }
    }

    // -- pure text builders ---------------------------------------------------

    #[test]
    fn test_left_text_full_state_exact() {
        assert_eq!(
            footer_left_text(&full_state()),
            "mode build · auto read,test · ask write,net,spend \
             · bundle dev-bundle · claude-fable-5 · a1b2c3 · $0.87 ▲ · q1"
        );
    }

    #[test]
    fn test_left_text_shows_native_mode_badge() {
        let state = FooterState {
            native_modes: vec!["machete".to_string()],
            ..full_state()
        };
        let left = footer_left_text(&state);
        // badge right after the posture chip
        assert!(left.starts_with("mode build · ◆ machete · "), "{left}");
        // absent when no native mode active
        assert!(!footer_left_text(&full_state()).contains('◆'));
    }

    #[test]
    fn test_left_text_shows_stacked_native_modes() {
        // Activation order (last == primary): team-pulse then audit → audit enforced.
        let state = FooterState {
            native_modes: vec!["team-pulse".to_string(), "audit".to_string()],
            ..full_state()
        };
        let left = footer_left_text(&state);
        // Primary (◆) first, the stacked one as a +entry.
        assert!(left.starts_with("mode build · ◆ audit +team-pulse · "), "{left}");
    }

    /// Story #4 (status bar speaks human): the bundle is labeled as a
    /// bundle — never a bare name — and the primary model is its own part.
    #[test]
    fn test_left_text_labels_bundle_and_carries_model() {
        let left = footer_left_text(&full_state());
        assert!(left.contains(" · bundle dev-bundle · "));
        assert!(left.contains(" · claude-fable-5 · "));
        // Empty identity fields leave no orphaned label behind.
        let bare = FooterState::default();
        assert!(!footer_left_text(&bare).contains("bundle"));
    }

    #[test]
    fn test_left_text_minimal_state() {
        let state = FooterState::default();
        assert_eq!(footer_left_text(&state), "mode chat · ask all · auto read · $0.00");
    }

    #[test]
    fn test_left_text_no_yield_no_queue() {
        let state = FooterState {
            mode_id: ModeId::Plan,
            cost: Decimal::from_str("1.24").unwrap(),
            ..FooterState::default()
        };
        assert_eq!(footer_left_text(&state), "mode plan · read-only · $1.24");
    }

    /// Never lie in the footer: unpriceable usage → the total is a floor.
    #[test]
    fn test_left_text_unpriced_usage_marks_cost_with_tilde() {
        let state = FooterState {
            mode_id: ModeId::Plan,
            cost: Decimal::from_str("1.24").unwrap(),
            cost_estimated: true,
            ..FooterState::default()
        };
        assert_eq!(footer_left_text(&state), "mode plan · read-only · ~$1.24");
    }

    #[test]
    fn test_left_text_full_state_estimated_exact() {
        let state = FooterState {
            cost_estimated: true,
            ..full_state()
        };
        assert_eq!(
            footer_left_text(&state),
            "mode build · auto read,test · ask write,net,spend \
             · bundle dev-bundle · claude-fable-5 · a1b2c3 · ~$0.87 ▲ · q1"
        );
    }

    /// Design D2 ladder step 3: 'Plan N/M' rides the footer left segment.
    #[test]
    fn test_plan_count_segment_appears_only_when_total_positive() {
        let state = FooterState {
            plan_done: 2,
            plan_total: 4,
            ..full_state()
        };
        assert!(footer_left_text(&state).ends_with(" · Plan 2/4"));
        // default total=0 → absent
        assert!(!footer_left_text(&full_state()).contains("Plan"));
    }

    #[test]
    fn test_waiting_text_singular_plural_empty() {
        let waiting = |n: u64| FooterState {
            waiting: n,
            ..FooterState::default()
        };
        assert_eq!(footer_waiting_text(&waiting(1)), "1 decision waiting · ctrl-y");
        assert_eq!(footer_waiting_text(&waiting(3)), "3 decisions waiting · ctrl-y");
        assert_eq!(footer_waiting_text(&waiting(0)), "");
    }

    #[test]
    fn test_right_hints_exact_per_context() {
        let in_context = |context: Context| FooterState {
            context,
            ..FooterState::default()
        };
        assert_eq!(
            footer_right_text(&in_context(Context::Approval)),
            "arrows select · enter confirm · esc deny"
        );
        assert_eq!(
            footer_right_text(&in_context(Context::LaneFocus)),
            "esc back to parent · transcript is the subagent's own"
        );
        assert_eq!(
            footer_right_text(&in_context(Context::Palette)),
            "↑↓ select · enter run · esc close"
        );
        assert_eq!(
            footer_right_text(&in_context(Context::Running)),
            "esc interrupt · enter steer · shift+enter queue"
        );
        assert_eq!(
            footer_right_text(&in_context(Context::Idle)),
            "↑ history · ctrl+j newline · ctrl-r rewind · / commands"
        );
    }

    #[test]
    fn test_running_hint_swaps_queue_chord_without_kitty() {
        let state = FooterState {
            context: Context::Running,
            kitty_protocol: false,
            ..FooterState::default()
        };
        assert_eq!(
            footer_right_text(&state),
            "esc interrupt · enter steer · alt+enter queue"
        );
    }

    #[test]
    fn test_unknown_hint_context_falls_back_to_idle() {
        let state = FooterState {
            context: Context::Rewind,
            ..FooterState::default()
        };
        assert_eq!(
            footer_right_text(&state),
            "↑ history · ctrl+j newline · ctrl-r rewind · / commands"
        );
    }

    // -- painted form (Python's widget-rendering tests, minus the widget) -----
    //
    // The Textual tests mounted a FooterApp and asserted on the painted
    // Statics; the pure port asserts the same observable text/spans straight
    // from the segment producers. App mounting, pilot.pause(), theme
    // registration and the message pump are app-assembly concerns.

    // Python: test_footer_renders_left_and_right_segments (widget paint →
    // segment plain text; 120 cols is wide enough for FULL_STATE's full
    // left segment — narrow-width degradation has its own tests below).
    #[test]
    fn test_footer_renders_left_and_right_segments() {
        let state = full_state();
        assert_eq!(
            line_plain(&footer_left_segments(&state, 120)),
            footer_left_text(&state)
        );
        assert_eq!(
            footer_right_text(&state),
            "↑ history · ctrl+j newline · ctrl-r rewind · / commands"
        );
    }

    /// The _repaint plan branch: 'Plan N/M' lands in the painted segments.
    #[test]
    fn test_footer_paints_plan_count_in_left_segment() {
        let state = FooterState {
            plan_done: 2,
            plan_total: 4,
            ..full_state()
        };
        assert!(line_plain(&footer_left_segments(&state, 0)).contains("Plan 2/4"));
    }

    /// Mockup footer-left: every inline `·` between segments is its own
    /// `--dimmer` span while segment text stays dim (§2).
    #[test]
    fn test_footer_left_separators_use_dimmer_token() {
        let segments = footer_left_segments(&full_state(), 120);
        let dimmer_runs: Vec<&str> = segments
            .iter()
            .filter(|segment| segment.style_token == StyleToken::Dimmer)
            .map(|segment| segment.text.as_str())
            .collect();
        // mode·trust, trust·bundle, bundle·model, model·session, session·cost
        // = 5 separators (the orange "· q1" queue badge separator is NOT dimmer).
        assert_eq!(dimmer_runs, vec![" · "; 5]);
    }

    // Python: test_footer_badge_hidden_when_no_decisions_waiting — the
    // widget's `-visible` class is driven by footer_waiting_text emptiness.
    #[test]
    fn test_footer_badge_hidden_when_no_decisions_waiting() {
        let state = FooterState {
            waiting: 0,
            ..FooterState::default()
        };
        assert!(footer_waiting_text(&state).is_empty());
    }

    // Python: test_footer_badge_shows_and_click_posts_message — the badge
    // text half; the click → WaitingBadgeClicked message pump is widget
    // mechanics (see module docs).
    #[test]
    fn test_footer_badge_shows_and_click_posts_message() {
        let state = FooterState {
            waiting: 2,
            ..FooterState::default()
        };
        assert_eq!(footer_waiting_text(&state), "2 decisions waiting · ctrl-y");
    }

    /// Mockup footer has flex-wrap: wrap — when the left segment plus the
    /// waiting badge exceed the width, the badge drops to its own row (fully
    /// readable and clickable) instead of clipping the ctrl-y hint off-screen.
    #[test]
    fn test_footer_badge_wraps_onto_own_row_at_narrow_width() {
        let state = FooterState {
            mode_id: ModeId::Build,
            bundle: "dev-bundle".to_string(),
            session_short: "a1b2c3".to_string(),
            cost: Decimal::from_str("0.87").unwrap(),
            waiting: 1,
            context: Context::Idle,
            ..FooterState::default()
        };
        let wrap = footer_wrap(&state, 100);
        assert!(wrap.wrapped);
        assert!(wrap.badge_wrapped);
        // The wrapped badge stays fully readable (nothing clips at 100 cells).
        assert!(cell_len(&footer_waiting_text(&state)) <= 100);
    }

    #[test]
    fn test_footer_badge_stays_inline_at_wide_width() {
        let state = FooterState {
            waiting: 1,
            ..FooterState::default()
        };
        let wrap = footer_wrap(&state, 160);
        assert!(!wrap.badge_wrapped);
        assert!(!wrap.wrapped);
    }

    // Python: test_footer_hint_changes_with_context — repaint after a state
    // swap is just calling the pure builder with the new state.
    #[test]
    fn test_footer_hint_changes_with_context() {
        let running = FooterState {
            context: Context::Running,
            ..FooterState::default()
        };
        assert_eq!(
            footer_right_text(&running),
            "esc interrupt · enter steer · shift+enter queue"
        );
        let approval = FooterState {
            context: Context::Approval,
            ..FooterState::default()
        };
        assert_eq!(
            footer_right_text(&approval),
            "arrows select · enter confirm · esc deny"
        );
    }

    // -- narrow-width degradation (design D2: the plan fallback must survive) --

    /// Found live in forge at 80 cols: '… $0.70 ▲ · Pl' — the Plan n/m
    /// fallback (the whole point of the narrow-width ladder) clipped off the
    /// right edge. Decorative segments drop first; mode/cost/queue/plan never.
    #[test]
    fn test_footer_left_text_fit_drops_decorations_before_the_plan_count() {
        let state = FooterState {
            mode_id: ModeId::Auto,
            bundle: "anchors".to_string(),
            session_short: "e07d".to_string(),
            cost: Decimal::from_str("0.70").unwrap(),
            shipped: true,
            plan_done: 3,
            plan_total: 3,
            ..FooterState::default()
        };
        let full = footer_left_text(&state);
        // precondition: this state genuinely overflows
        assert!(cell_len(&full) > 80);
        let fitted = footer_left_text_fit(&state, 80);
        assert!(cell_len(&fitted) <= 80);
        assert!(fitted.starts_with("mode auto"));
        assert!(fitted.contains("$0.70") && fitted.contains("Plan 3/3"));
        // Wide terminals keep the untouched full string.
        assert_eq!(footer_left_text_fit(&state, 200), full);
    }

    /// Story #4 ladder: trust → session → bundle → model. The model is the
    /// identity users actually ask about, so it survives longer than the
    /// bundle/session decorations but still drops before cost and the plan.
    #[test]
    fn test_footer_left_text_fit_model_outlives_bundle_and_session() {
        let state = FooterState {
            mode_id: ModeId::Auto,
            bundle: "anchors".to_string(),
            model: "claude-fable-5".to_string(),
            session_short: "e07d".to_string(),
            cost: Decimal::from_str("0.70").unwrap(),
            shipped: true,
            plan_done: 3,
            plan_total: 3,
            ..FooterState::default()
        };
        // 60 cells: trust, session AND bundle have dropped — the model is still up.
        let fitted = footer_left_text_fit(&state, 60);
        assert!(cell_len(&fitted) <= 60);
        assert!(fitted.contains("claude-fable-5"));
        assert!(!fitted.contains("bundle") && !fitted.contains("e07d"));
        // 40 cells: the model finally drops; mode/cost/plan never do.
        let tight = footer_left_text_fit(&state, 40);
        assert!(cell_len(&tight) <= 40);
        assert!(!tight.contains("claude-fable-5"));
        assert!(tight.starts_with("mode auto"));
        assert!(tight.contains("$0.70") && tight.contains("Plan 3/3"));
    }

    // Python: test_footer_narrow_width_paints_plan_not_clipped — the widget
    // paints from the same fit ladder, so the pure segments carry the same
    // guarantee. (The Textual test ran at size 80 with `padding: 0 1`, i.e.
    // a 78-cell content width; both 78 and 80 land on the same ladder step.)
    #[test]
    fn test_footer_narrow_width_paints_plan_not_clipped() {
        let state = FooterState {
            mode_id: ModeId::Auto,
            bundle: "anchors".to_string(),
            model: "claude-fable-5".to_string(),
            session_short: "e07d".to_string(),
            cost: Decimal::from_str("0.70").unwrap(),
            shipped: true,
            plan_done: 3,
            plan_total: 3,
            ..FooterState::default()
        };
        let painted = line_plain(&footer_left_segments(&state, 78));
        assert!(painted.contains("Plan 3/3"));
        // the model outlives the trust drop
        assert!(painted.contains("claude-fable-5"));
        assert!(cell_len(&painted) <= 80);
    }
}
