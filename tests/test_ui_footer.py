"""Tests for the footer status bar (ui/footer.py) — exact spec strings."""

from __future__ import annotations

from decimal import Decimal

import pytest
from textual.app import App, ComposeResult
from textual.content import Content
from textual.message import Message
from textual.widgets import Static

from rich.cells import cell_len

from amplifier_app_tui.ui.footer import (
    FooterBar,
    FooterState,
    footer_left_text,
    footer_left_text_fit,
    footer_right_text,
    footer_waiting_text,
)
from amplifier_app_tui.ui.themes import DEFAULT_THEME, THEME_TOKENS, register_themes, theme_id

FULL_STATE = FooterState(
    mode_id="build",
    model="claude-fable-5",
    session_short="a1b2c3",
    cost=Decimal("0.87"),
    shipped=True,
    queued=1,
    waiting=0,
    context="idle",
)


# -- pure text builders ---------------------------------------------------------


def test_left_text_full_state_exact() -> None:
    assert footer_left_text(FULL_STATE) == (
        "mode build · auto read,test · ask write,net,spend · claude-fable-5 · a1b2c3 · $0.87 ▲ · q1"
    )


def test_left_text_shows_native_mode_badge() -> None:
    state = FULL_STATE.model_copy(update={"native_modes": ("machete",)})
    left = footer_left_text(state)
    assert left.startswith("mode build · ◆ machete · ")  # badge right after the posture chip
    assert "◆" not in footer_left_text(FULL_STATE)  # absent when no native mode active


def test_left_text_shows_stacked_native_modes() -> None:
    # Activation order (last == primary): team-pulse then audit → audit enforced.
    state = FULL_STATE.model_copy(update={"native_modes": ("team-pulse", "audit")})
    left = footer_left_text(state)
    # Primary (◆) first, the stacked one as a +entry.
    assert left.startswith("mode build · ◆ audit +team-pulse · ")


def test_left_text_never_shows_bundle() -> None:
    """AC1 (item D4): the active bundle path renders in exactly ONE
    persistent location — :class:`~amplifier_app_tui.ui.chrome.TitleBar`,
    docked at the top. The footer must never paint a second, always-
    identical copy — ``FooterState`` doesn't even carry a ``bundle``
    field any more, so there's nothing here left to accidentally show."""
    assert "bundle" not in footer_left_text(FULL_STATE)
    assert "bundle" not in footer_left_text(FooterState())
    assert not hasattr(FooterState(), "bundle")


def test_left_text_carries_model() -> None:
    """Story #4 (status bar speaks human): the primary model is its own
    part of the left segment."""
    left = footer_left_text(FULL_STATE)
    assert " · claude-fable-5 · " in left


def test_left_text_minimal_state() -> None:
    state = FooterState()
    assert footer_left_text(state) == "mode chat · ask all · auto read · $0.00"


def test_left_text_no_yield_no_queue() -> None:
    state = FooterState(mode_id="plan", cost=Decimal("1.24"))
    assert footer_left_text(state) == "mode plan · read-only · $1.24"


def test_left_text_unpriced_usage_marks_cost_with_tilde() -> None:
    """Never lie in the footer: unpriceable usage → the total is a floor."""
    state = FooterState(mode_id="plan", cost=Decimal("1.24"), cost_estimated=True)
    assert footer_left_text(state) == "mode plan · read-only · ~$1.24"


def test_left_text_full_state_estimated_exact() -> None:
    state = FULL_STATE.model_copy(update={"cost_estimated": True})
    assert footer_left_text(state) == (
        "mode build · auto read,test · ask write,net,spend"
        " · claude-fable-5 · a1b2c3 · ~$0.87 ▲ · q1"
    )


def test_plan_count_segment_appears_only_when_total_positive() -> None:
    """Design D2 ladder step 3: 'Plan N/M' rides the footer left segment."""
    state = FULL_STATE.model_copy(update={"plan_done": 2, "plan_total": 4})
    assert footer_left_text(state).endswith(" · Plan 2/4")
    assert "Plan" not in footer_left_text(FULL_STATE)  # default total=0 → absent


def test_effort_segment_appears_only_when_set() -> None:
    """HGT effort indicator: the tier rides the left segment just before the
    cost, and only when set (None keeps the lean footer)."""
    state = FULL_STATE.model_copy(update={"effort": "high"})
    assert " · effort high · $0.87" in footer_left_text(state)
    # default (None) → absent; an explicit "none" still shows (null vs "none").
    assert "effort" not in footer_left_text(FULL_STATE)
    shown = FULL_STATE.model_copy(update={"effort": "none"})
    assert " · effort none · " in footer_left_text(shown)


# -- context readout (live context tokens + true % of the real window) --------


def test_left_text_context_pct_segment_sits_before_cost() -> None:
    """Donor order (tokens/% then $ spent): the live ``NN% ctx`` readout sits
    right before the cost part."""
    state = FooterState(mode_id="plan", cost=Decimal("1.24"), context_pct=41)
    assert footer_left_text(state) == "mode plan · read-only · 41% ctx · $1.24"


def test_left_text_context_tokens_only_when_window_unknown() -> None:
    """Honest fallback (donor ``model.limit.context ? … : null``): an unknown
    window omits the % and shows the compact token count alone."""
    state = FooterState(mode_id="plan", cost=Decimal("1.24"), context_tokens=12_400)
    assert footer_left_text(state) == "mode plan · read-only · 12k ctx · $1.24"


def test_left_text_context_pct_preferred_over_tokens() -> None:
    """A known window wins: the % form, never the bare token count."""
    state = FooterState(context_pct=41, context_tokens=12_400)
    left = footer_left_text(state)
    assert "41% ctx" in left
    assert "12k ctx" not in left


def test_left_text_no_context_readout_before_usage() -> None:
    """Both None (no usage yet) → no ctx segment at all (donor renders from the
    last response, not an empty session); existing golden states are unaffected."""
    assert "ctx" not in footer_left_text(FooterState())
    assert "ctx" not in footer_left_text(FULL_STATE)


def test_waiting_text_singular_plural_empty() -> None:
    assert footer_waiting_text(FooterState(waiting=1)) == "1 decision waiting · ctrl-y"
    assert footer_waiting_text(FooterState(waiting=3)) == "3 decisions waiting · ctrl-y"
    assert footer_waiting_text(FooterState(waiting=0)) == ""


def test_right_hints_exact_per_context() -> None:
    assert footer_right_text(FooterState(context="approval")) == (
        "arrows select · enter confirm · esc deny"
    )
    assert footer_right_text(FooterState(context="lane_focus")) == (
        "esc back to parent · transcript is the subagent's own"
    )
    assert footer_right_text(FooterState(context="palette")) == (
        "↑↓ select · enter run · esc close"
    )
    assert footer_right_text(FooterState(context="running")) == (
        "esc interrupt · enter steer · shift+enter queue"
    )
    # Item D4 (AC2/AC3): idle is deliberately empty — the generic reminder
    # moved to COMPOSER_PLACEHOLDER + /keys instead of riding every frame.
    assert footer_right_text(FooterState(context="idle")) == ""


def test_running_hint_swaps_queue_chord_without_kitty() -> None:
    state = FooterState(context="running", kitty_protocol=False)
    assert footer_right_text(state) == "esc interrupt · enter steer · alt+enter queue"


def test_unknown_hint_context_falls_back_to_idle() -> None:
    state = FooterState(context="rewind")
    assert footer_right_text(state) == ""


# -- widget rendering ---------------------------------------------------------------


class FooterApp(App[None]):
    def __init__(self) -> None:
        super().__init__()
        register_themes(self)
        self.theme = theme_id(DEFAULT_THEME)
        self.messages: list[Message] = []

    def compose(self) -> ComposeResult:
        yield FooterBar(id="footer")

    def on_footer_bar_waiting_badge_clicked(self, message: FooterBar.WaitingBadgeClicked) -> None:
        self.messages.append(message)


def _plain(widget: Static) -> str:
    content = widget.content
    return getattr(content, "plain", str(content))


@pytest.mark.asyncio
async def test_footer_renders_left_and_right_segments() -> None:
    # Wide enough for FULL_STATE's full left segment — narrow-width
    # degradation has its own tests below.
    app = FooterApp()
    async with app.run_test(size=(120, 24)) as pilot:
        bar = app.query_one("#footer", FooterBar)
        bar.update_state(FULL_STATE)
        await pilot.pause()
        assert _plain(app.query_one("#footer-left", Static)) == footer_left_text(FULL_STATE)
        assert _plain(app.query_one("#footer-right", Static)) == footer_right_text(FULL_STATE)


@pytest.mark.asyncio
async def test_footer_paints_plan_count_in_left_segment() -> None:
    """The _repaint plan branch: 'Plan N/M' lands in the painted widget."""
    app = FooterApp()
    async with app.run_test() as pilot:
        bar = app.query_one("#footer", FooterBar)
        state = FULL_STATE.model_copy(update={"plan_done": 2, "plan_total": 4})
        bar.update_state(state)
        await pilot.pause()
        assert "Plan 2/4" in _plain(app.query_one("#footer-left", Static))


@pytest.mark.asyncio
async def test_footer_paints_context_readout_in_left_segment() -> None:
    """The _repaint context branch: ``NN% ctx`` lands in the painted widget,
    placed identically to the pure text builder (before the cost part)."""
    app = FooterApp()
    async with app.run_test(size=(120, 24)) as pilot:
        bar = app.query_one("#footer", FooterBar)
        state = FULL_STATE.model_copy(update={"context_pct": 41})
        bar.update_state(state)
        await pilot.pause()
        painted = _plain(app.query_one("#footer-left", Static))
        # Placement, not full equality: the widget’s own fit ladder may drop
        # decorations at this width — what matters is the ctx readout paints
        # immediately before the cost part (donor order), which the cost never
        # drops so this adjacency is stable.
        assert "41% ctx · $0.87" in painted


@pytest.mark.asyncio
async def test_footer_left_separators_use_dimmer_token() -> None:
    """Mockup footer-left: every inline ``·`` between segments is its own
    ``--dimmer`` span while segment text stays dim (§2)."""
    app = FooterApp()
    async with app.run_test(size=(120, 24)) as pilot:
        bar = app.query_one("#footer", FooterBar)
        bar.update_state(FULL_STATE)
        await pilot.pause()
        content = app.query_one("#footer-left", Static).content
        assert isinstance(content, Content)
        dimmer_runs = [
            content.plain[span.start : span.end]
            for span in content.spans
            if span.style == "$dimmer"
        ]
        # mode·trust, trust·model, model·session, session·cost = 4 separators
        # (item D4 dropped the bundle part, so one fewer than before; the
        # orange "· q1" queue badge separator is NOT dimmer).
        assert dimmer_runs == [" · "] * 4


@pytest.mark.asyncio
async def test_footer_badge_hidden_when_no_decisions_waiting() -> None:
    app = FooterApp()
    async with app.run_test() as pilot:
        bar = app.query_one("#footer", FooterBar)
        bar.update_state(FooterState(waiting=0))
        await pilot.pause()
        badge = bar._badge
        assert not badge.has_class("-visible")


@pytest.mark.asyncio
async def test_footer_badge_shows_and_click_posts_message() -> None:
    app = FooterApp()
    async with app.run_test() as pilot:
        bar = app.query_one("#footer", FooterBar)
        bar.update_state(FooterState(waiting=2))
        await pilot.pause()
        badge = bar._badge
        assert badge.has_class("-visible")
        assert _plain(badge) == "2 decisions waiting · ctrl-y"
        await pilot.click(badge)
        await pilot.pause()
        assert len(app.messages) == 1
        assert isinstance(app.messages[0], FooterBar.WaitingBadgeClicked)


@pytest.mark.asyncio
async def test_footer_badge_wraps_onto_own_row_at_narrow_width() -> None:
    """Mockup footer has flex-wrap: wrap — when the left segment plus the
    waiting badge exceed the width, the badge drops to its own row (fully
    readable and clickable) instead of clipping the ctrl-y hint off-screen."""
    app = FooterApp()
    async with app.run_test(size=(100, 24)) as pilot:
        bar = app.query_one("#footer", FooterBar)
        bar.update_state(
            FooterState(
                mode_id="build",
                model="claude-fable-5",
                session_short="a1b2c3",
                cost=Decimal("0.87"),
                waiting=1,
                context="idle",
            )
        )
        await pilot.pause()
        assert bar.has_class("-wrapped")
        assert bar.has_class("-badge-wrapped")
        badge = bar._badge
        assert badge.region.right <= 100
        assert badge.region.width >= len(footer_waiting_text(bar.state))
        await pilot.click(badge)
        await pilot.pause()
        assert len(app.messages) == 1


@pytest.mark.asyncio
async def test_footer_badge_stays_inline_at_wide_width() -> None:
    app = FooterApp()
    async with app.run_test(size=(160, 24)) as pilot:
        bar = app.query_one("#footer", FooterBar)
        bar.update_state(FooterState(waiting=1))
        await pilot.pause()
        assert not bar.has_class("-badge-wrapped")
        assert bar._badge.region.y == bar._left.region.y


@pytest.mark.asyncio
async def test_footer_hint_changes_with_context() -> None:
    app = FooterApp()
    async with app.run_test() as pilot:
        bar = app.query_one("#footer", FooterBar)
        bar.update_state(FooterState(context="running"))
        await pilot.pause()
        assert _plain(app.query_one("#footer-right", Static)) == (
            "esc interrupt · enter steer · shift+enter queue"
        )
        bar.update_state(FooterState(context="approval"))
        await pilot.pause()
        assert _plain(app.query_one("#footer-right", Static)) == (
            "arrows select · enter confirm · esc deny"
        )


# -- narrow-width degradation (design D2: the plan fallback must survive) ------


def test_footer_left_text_fit_drops_decorations_before_the_plan_count() -> None:
    """Found live in forge at 80 cols: '… $0.70 ▲ · Pl' — the Plan n/m
    fallback (the whole point of the narrow-width ladder) clipped off the
    right edge. Decorative segments drop first; mode/cost/queue/plan never."""
    state = FooterState(
        mode_id="auto",
        model="claude-fable-5",
        session_short="e07d",
        cost=Decimal("0.70"),
        shipped=True,
        effort="high",
        context_pct=41,
        plan_done=3,
        plan_total=3,
    )
    full = footer_left_text(state)
    assert cell_len(full) > 80  # precondition: this state genuinely overflows
    fitted = footer_left_text_fit(state, 80)
    assert cell_len(fitted) <= 80
    assert fitted.startswith("mode auto")
    assert "$0.70" in fitted and "Plan 3/3" in fitted
    # Wide terminals keep the untouched full string.
    assert footer_left_text_fit(state, 200) == full


def test_footer_left_text_fit_model_outlives_trust_and_session() -> None:
    """Story #4 ladder (item D4: bundle is gone from this ladder entirely —
    it no longer rides the footer at all): trust → session → model. The
    model is the identity users actually ask about, so it survives longer
    than the other decorations but still drops before cost and the plan."""
    state = FooterState(
        mode_id="auto",
        model="claude-fable-5",
        session_short="e07d",
        cost=Decimal("0.70"),
        shipped=True,
        plan_done=3,
        plan_total=3,
    )
    # 50 cells: trust AND session have dropped — the model is still up.
    fitted = footer_left_text_fit(state, 50)
    assert cell_len(fitted) <= 50
    assert "claude-fable-5" in fitted
    assert "e07d" not in fitted
    # 40 cells: the model finally drops too; mode/cost/plan never do.
    tight = footer_left_text_fit(state, 40)
    assert cell_len(tight) <= 40
    assert "claude-fable-5" not in tight
    assert tight.startswith("mode auto")
    assert "$0.70" in tight and "Plan 3/3" in tight


@pytest.mark.asyncio
async def test_footer_narrow_width_paints_plan_not_clipped() -> None:
    app = FooterApp()
    async with app.run_test(size=(80, 24)) as pilot:
        bar = app.query_one("#footer", FooterBar)
        state = FooterState(
            mode_id="auto",
            model="claude-fable-5",
            session_short="e07d",
            cost=Decimal("0.70"),
            shipped=True,
            plan_done=3,
            plan_total=3,
        )
        bar.update_state(state)
        await pilot.pause()
        painted = _plain(app.query_one("#footer-left", Static))
        assert "Plan 3/3" in painted
        assert "claude-fable-5" in painted  # the model outlives the trust drop
        assert cell_len(painted) <= 80


# -- D2 structural seam: composer/status boundary (compliance 2026-08-02) -----
#
# David Koleczek's UX review (2026-07-31): the composer and this footer used
# to share one undivided ``$bg-chrome`` band. ``FooterBar`` now carries an
# unconditional ``border-top: solid $rule`` in DEFAULT_CSS -- these tests pin
# that it is a REAL structural border (not padding/spacing simulating one),
# that it renders in every theme's own ``$rule`` color (AC1/AC2's "every
# theme" bar), and that it survives every footer content state -- idle,
# narrow-width fit-dropping, ``-wrapped``, ``-badge-wrapped`` -- so status
# updates never dissolve the boundary while the user is typing (AC3), even
# at narrow widths / short heights (AC4). Item D4 (footer hint + bundle-
# metadata consolidation) builds new footer content inside this same
# bordered box; these tests protect the seam it will build on.


@pytest.mark.asyncio
async def test_footer_border_top_is_a_real_border_in_every_theme() -> None:
    """The seam is a Textual border (structural), colored from ``$rule`` --
    never a color-only cue, and never simulated with blank padding rows."""
    for theme_name, tokens in THEME_TOKENS.items():
        app = FooterApp()
        async with app.run_test() as pilot:
            app.theme = theme_id(theme_name)
            await pilot.pause()  # theme swap re-resolves CSS on the next refresh
            bar = app.query_one("#footer", FooterBar)
            edge, color = bar.styles.border_top
            assert edge == "solid", f"{theme_name}: seam must be a real border edge"
            assert color.hex.lower() == tokens["rule"].lower(), (
                f"{theme_name}: seam color must track the theme's own $rule token"
            )


@pytest.mark.asyncio
async def test_footer_border_top_persists_across_every_footer_state() -> None:
    """AC3: a resize/rewrap/badge never dissolves the boundary.

    Sweeps a representative state per footer layout mode -- plain idle,
    a fully loaded left segment, the exact state proven elsewhere
    (``test_footer_badge_wraps_onto_own_row_at_narrow_width``) to force
    BOTH ``-wrapped`` and ``-badge-wrapped``, and the running/"streaming"
    context -- and asserts the seam outlives every one of them.
    """
    app = FooterApp()
    async with app.run_test(size=(100, 24)) as pilot:
        bar = app.query_one("#footer", FooterBar)
        wrap_forcing_state = FooterState(
            mode_id="build",
            model="claude-fable-5",
            session_short="a1b2c3",
            cost=Decimal("0.87"),
            waiting=1,
            context="idle",
        )
        states = (FooterState(), FULL_STATE, wrap_forcing_state, FooterState(context="running"))
        for state in states:
            bar.update_state(state)
            await pilot.pause()
            assert bar.styles.border_top[0] == "solid"
            if state is wrap_forcing_state:
                # Proven elsewhere (test_footer_badge_wraps_onto_own_row_at_narrow_width)
                # to force BOTH classes at this exact width -- confirm the fixture is
                # still doing its job precisely where the seam is most at risk.
                assert bar.has_class("-wrapped")
                assert bar.has_class("-badge-wrapped")
                assert bar.styles.border_top[0] == "solid"


@pytest.mark.asyncio
async def test_footer_border_top_survives_narrow_width_and_short_height() -> None:
    """AC4: readable at narrow widths / short heights -- the seam doesn't
    vanish, and the footer never grows so tall it eats the composer's row
    above it (a 1-row border, not a multi-row redesign)."""
    app = FooterApp()
    async with app.run_test(size=(40, 10)) as pilot:
        bar = app.query_one("#footer", FooterBar)
        bar.update_state(
            FooterState(
                mode_id="auto",
                model="claude-fable-5",
                session_short="e07d",
                cost=Decimal("0.70"),
                shipped=True,
                plan_done=3,
                plan_total=3,
            )
        )
        await pilot.pause()
        assert bar.styles.border_top[0] == "solid"
        # One border row + at most two wrapped content rows: never balloons.
        assert bar.size.height <= 3


# -- D4 AC5: responsive proof — no duplicate metadata, no collisions ----------
#
# A fully-loaded footer state, swept across the same width matrix the
# transcript-renderer goldens treat as "supported" (40/80/97/120 —
# tests/test_golden_widths.py), proving AC1 (the bundle never rides the
# footer, at any width) and AC4 (long content truncates safely: it always
# fits or wraps onto its own row, never overlapping or spilling past the
# terminal edge) at every size the app is expected to run at — not just
# the one or two widths the tests above happen to exercise. Also re-proves
# the D2 seam (border-top) holds throughout, since D4 built inside it.

_SUPPORTED_WIDTHS = (40, 80, 97, 120)


@pytest.mark.asyncio
async def test_footer_responsive_no_bundle_and_no_collision_at_every_width() -> None:
    loaded_idle = FooterState(
        mode_id="auto",
        model="claude-fable-5",
        session_short="e07d",
        cost=Decimal("0.70"),
        shipped=True,
        effort="high",
        context_pct=41,
        queued=1,
        waiting=2,
        plan_done=3,
        plan_total=3,
        context="idle",
    )
    loaded_running = loaded_idle.model_copy(update={"context": "running", "waiting": 0})
    for width in _SUPPORTED_WIDTHS:
        for state in (loaded_idle, loaded_running):
            app = FooterApp()
            async with app.run_test(size=(width, 24)) as pilot:
                bar = app.query_one("#footer", FooterBar)
                bar.update_state(state)
                await pilot.pause()

                # AC1: single-sourced to the TitleBar — never here, at any width.
                left_text = _plain(app.query_one("#footer-left", Static))
                right_text = _plain(app.query_one("#footer-right", Static))
                assert "bundle" not in left_text
                assert "bundle" not in right_text

                # AC4/AC5: never wider than the terminal, never clipped —
                # the widget's own auto layout must stay inside `width`.
                assert bar.size.width <= width
                left_group = app.query_one("#footer-left-group")
                right = app.query_one("#footer-right", Static)
                if bar.has_class("-wrapped"):
                    # Hints dropped to their own full-width row below the
                    # left segment — never overlapping it (AC4: truncates
                    # safely instead of colliding).
                    assert right.region.y > left_group.region.y
                elif right_text:
                    # Same row: the hints sit strictly to the right of the
                    # left segment, never underneath/overlapping it.
                    assert right.region.x >= left_group.region.right

                # D2 seam (do not regress the boundary D4 builds inside).
                assert bar.styles.border_top[0] == "solid"
