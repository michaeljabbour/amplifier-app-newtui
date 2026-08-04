"""Tests for the title bar (ui/chrome.py) and notice slot (ui/notices.py)."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from rich.cells import cell_len

from amplifier_app_tui.ui.chrome import (
    APP_TITLE_NAME,
    SPINNER_INTERVAL,
    TERMINAL_TITLE_MAX_CHARS,
    TERMINAL_SPINNER_FRAMES,
    TITLE_BUNDLE_MAX_CELLS,
    TitleBar,
    _bundle_fit_budget,
    _truncate_bundle_label,
    terminal_title_sequence,
    write_terminal_title,
)
from amplifier_app_tui.ui.notices import NoticeSlot
from amplifier_app_tui.ui.themes import DEFAULT_THEME, register_themes, theme_id


class ChromeApp(App[None]):
    def __init__(self) -> None:
        super().__init__()
        # Themes must be registered before widget DEFAULT_CSS referencing
        # spec tokens ($bg-chrome, …) is parsed — i.e. in __init__.
        register_themes(self)
        self.theme = theme_id(DEFAULT_THEME)

    def compose(self) -> ComposeResult:
        yield TitleBar(id="title")
        yield NoticeSlot(duration=0.05, id="notice")


# -- title text ---------------------------------------------------------------


def test_idle_title_exact_format() -> None:
    bar = TitleBar()
    bar.set_reactive(TitleBar.state_text, "ready")
    bar.set_reactive(TitleBar.bundle_uri, "dev-bundle")
    bar.set_reactive(TitleBar.session_short, "a1b2c3")
    assert bar.title_text() == "amplifier — ready — dev-bundle — a1b2c3"


def test_empty_identity_fragments_are_skipped() -> None:
    bar = TitleBar()
    bar.set_reactive(TitleBar.state_text, "planning")
    assert bar.title_text() == "amplifier — planning"


# -- D4 AC4/gap 2: long bundle paths truncate safely, never wrap the composer -
#
# Finding 2 (post-merge compliance audit): ``TitleBar`` is the sole persistent
# home for the active bundle (item D4 AC1), but a ``--bundle`` path/URI or a
# ``bundle.active`` settings value (``kernel/config.resolve_bundle_source``)
# is user-supplied and unbounded. The first fix bounded it to a FIXED cell
# count (``TITLE_BUNDLE_MAX_CELLS``) regardless of the actual terminal width
# -- safe, but not viewport-aware: a wide terminal wasted space it could have
# used to show more of the real value, and a narrow one could still overflow
# once state/session grew (the fixed cap only ever bounded the bundle
# fragment, not the whole title). ``_bundle_fit_budget`` (D4 gap 2) replaces
# the constant with a live computation from the title row's actual rendered
# width, mirroring the footer's fit-ladder idiom (``ui/footer.py:_fit_drops``):
# reserve what the rest of the title needs, hand the remainder to the bundle
# fragment, and drop the fragment entirely once even a truncated stub would
# be meaningless. The row itself never grows past its fixed ``height: 1``
# (so it can never wrap down onto the composer docked below it), and the FULL
# value stays inspectable via ``/status`` (test_ui_session_ops_view.py pins
# that ``status_spans`` never truncates its ``bundle`` row).

_LONG_REALISTIC_BUNDLE_URI = (
    "git+https://github.com/microsoft/amplifier-foundation@main#bundles/anchors"
)
"""74 cells — realistic enough to fit in full at width 120 (budget 91) but
need truncation at 97/80/40 (budgets 68/51/11), so tests exercise both ends
of the viewport-aware ladder with one representative value."""


def test_truncate_bundle_label_passes_short_values_through_unchanged() -> None:
    assert _truncate_bundle_label("dev-bundle") == "dev-bundle"
    # Exactly at the budget: still untouched, no ellipsis appended.
    exact = "b" * TITLE_BUNDLE_MAX_CELLS
    assert _truncate_bundle_label(exact) == exact


def test_truncate_bundle_label_truncates_long_values_with_one_ellipsis() -> None:
    long_path = "/Users/dev/projects/" + ("nested-dir/" * 10) + "bundle.md"
    assert len(long_path) > TITLE_BUNDLE_MAX_CELLS
    truncated = _truncate_bundle_label(long_path)
    assert truncated != long_path
    assert truncated.endswith("\u2026")
    assert truncated.count("\u2026") == 1
    assert truncated.startswith(long_path[:10])  # a real prefix, not a generic marker
    # Bounded to the budget (cell-width, matching the house _clip/_elide shape).
    assert len(truncated) == TITLE_BUNDLE_MAX_CELLS


def test_truncate_bundle_label_honors_an_explicit_dynamic_budget() -> None:
    """The truncation helper itself is budget-agnostic — callers (``_plain_title``)
    supply a LIVE, viewport-derived ``max_cells`` instead of always defaulting
    to the fixed constant (D4 gap 2)."""
    long_path = "/Users/dev/projects/" + ("nested-dir/" * 10) + "bundle.md"
    for budget in (4, 11, 51, 68):
        truncated = _truncate_bundle_label(long_path, max_cells=budget)
        assert cell_len(truncated) == budget
        assert truncated.endswith("\u2026")


# -- _bundle_fit_budget: the viewport-aware replacement for the fixed cap -----


def test_bundle_fit_budget_falls_back_to_fixed_cap_before_layout() -> None:
    """``width <= 0`` means no real layout pass yet (a bare, unmounted
    ``TitleBar``) — falls back to the historical fixed budget rather than
    an unbounded or zero value."""
    assert _bundle_fit_budget(0, "ready", "a1b2c3") == TITLE_BUNDLE_MAX_CELLS
    assert _bundle_fit_budget(-1, "brainstorming", "") == TITLE_BUNDLE_MAX_CELLS


def test_bundle_fit_budget_reserves_exactly_what_the_rest_of_the_title_needs() -> None:
    """Budget = width - (app name + separators + state + session), matching
    ``_plain_title``'s own assembly so the WHOLE title, not just the bundle
    fragment, is guaranteed to fit."""
    width = 120
    state, session = "ready", "a1b2c3"
    budget = _bundle_fit_budget(width, state, session)
    # Directly against _plain_title's own separator math instead of a
    # hand-derived constant, so this test breaks if the two ever disagree.
    reserved = cell_len(f"{APP_TITLE_NAME} — {state}") + cell_len(" — ") * 2 + cell_len(session)
    assert budget == width - reserved


def test_bundle_fit_budget_with_no_session_reserves_only_one_separator() -> None:
    """No session yet (fresh boot): only ONE separator is reserved (state ->
    bundle), not two — matching ``_plain_title`` skipping the empty fragment."""
    width = 80
    budget = _bundle_fit_budget(width, "ready", "")
    reserved = cell_len(f"{APP_TITLE_NAME} — ready") + cell_len(" — ")
    assert budget == width - reserved


def test_bundle_fit_budget_grows_with_a_wider_viewport() -> None:
    """The core D4 gap 2 fix: the budget tracks the ACTUAL width instead of
    being stuck at a constant — a wider terminal shows more."""
    budgets = [_bundle_fit_budget(w, "ready", "a1b2c3") for w in (40, 80, 97, 120)]
    assert budgets == sorted(budgets)  # strictly non-decreasing as width grows
    assert budgets[-1] > TITLE_BUNDLE_MAX_CELLS  # wide terminals now exceed the old fixed cap


def test_bundle_fit_budget_drops_the_fragment_when_hopelessly_narrow() -> None:
    """Below ``_MIN_BUNDLE_CELLS`` a truncated stub would read as noise, not
    identity — the budget collapses to 0 (drop the fragment) rather than
    show one or two characters plus an ellipsis."""
    assert _bundle_fit_budget(20, "brainstorming", "a1b2c3") == 0


def test_idle_title_truncates_long_bundle_with_ellipsis_before_layout() -> None:
    """AC4, via the real rendering path (title_text -> _plain_title), for a
    bare/unmounted bar — no live width exists yet, so this pins the fixed
    pre-layout fallback (see test_bundle_fit_budget_falls_back_to_fixed_cap_before_layout).
    The mounted, viewport-aware path is proven below with a real Pilot app."""
    long_bundle = "org/" + "x" * 100 + "/tui-bundle"
    bar = TitleBar()
    bar.set_reactive(TitleBar.state_text, "ready")
    bar.set_reactive(TitleBar.bundle_uri, long_bundle)
    bar.set_reactive(TitleBar.session_short, "a1b2c3")
    title = bar.title_text()
    assert long_bundle not in title  # the raw unbounded value never rides the title
    assert "\u2026" in title
    assert title.startswith("amplifier — ready — org/")
    assert title.endswith("— a1b2c3")  # session fragment survives after the truncated one


# -- viewport-aware fitting, mounted (D4 gap 2): mirrors the footer's own
# responsive-at-every-golden-width proof (test_ui_footer.py) rather than a
# second, invented mechanism.

_GOLDEN_WIDTHS = (40, 80, 97, 120)


@pytest.mark.asyncio
async def test_title_bundle_fits_the_viewport_at_every_golden_width() -> None:
    for width in _GOLDEN_WIDTHS:
        app = ChromeApp()
        async with app.run_test(size=(width, 24)) as pilot:
            bar = app.query_one("#title", TitleBar)
            bar.state_text = "ready"
            bar.bundle_uri = _LONG_REALISTIC_BUNDLE_URI
            bar.session_short = "a1b2c3"
            await pilot.pause()
            title = bar.title_text()

            # The core guarantee: never wider than the terminal (never
            # wraps the height:1 row down onto the composer).
            assert cell_len(title) <= width
            assert bar.size.height == 1

            if width == 120:
                # Wide enough for the full, real URI — no ellipsis needed,
                # and the resolved value is genuinely all there (AC1).
                assert _LONG_REALISTIC_BUNDLE_URI in title
                assert "\u2026" not in title
            else:
                # Too narrow for the full URI: a safe, cell-bounded prefix
                # plus exactly one ellipsis, never the raw unbounded value.
                assert _LONG_REALISTIC_BUNDLE_URI not in title
                if width > 40:
                    # 80/97: still room for a meaningful truncated stub.
                    assert "\u2026" in title
                    assert title.startswith("amplifier — ready — git+https")
            assert title.endswith("— a1b2c3")  # session always survives


@pytest.mark.asyncio
async def test_title_bundle_reflows_on_live_resize() -> None:
    """Resizing the terminal mid-session changes the fit live — the same
    "reflow on resize" contract the footer's fit ladder already has."""
    app = ChromeApp()
    async with app.run_test(size=(120, 24)) as pilot:
        bar = app.query_one("#title", TitleBar)
        bar.state_text = "ready"
        bar.bundle_uri = _LONG_REALISTIC_BUNDLE_URI
        bar.session_short = "a1b2c3"
        await pilot.pause()
        wide_title = bar.title_text()
        assert _LONG_REALISTIC_BUNDLE_URI in wide_title  # fits in full at 120

        await pilot.resize_terminal(40, 24)
        await pilot.pause()
        narrow_title = bar.title_text()
        assert cell_len(narrow_title) <= 40
        assert _LONG_REALISTIC_BUNDLE_URI not in narrow_title
        assert "\u2026" in narrow_title  # dropped to a truncated, ellipsized stub

        await pilot.resize_terminal(120, 24)
        await pilot.pause()
        assert bar.title_text() == wide_title  # widening back restores the full value


def test_running_title_prefixes_spinner_and_cycles_frames() -> None:
    bar = TitleBar()
    bar.set_reactive(TitleBar.running, True)
    bar.set_reactive(TitleBar.state_text, "ready")
    assert bar.title_text().startswith("✳ ")
    seen = [bar.spinner_glyph]
    for _ in range(3):
        bar._frame_index = (bar._frame_index + 1) % 4
        seen.append(bar.spinner_glyph)
    assert seen == ["✳", "✦", "✧", "✦"]


def test_native_terminal_title_uses_obvious_braille_spinner() -> None:
    bar = TitleBar()
    bar.set_reactive(TitleBar.running, True)
    first = bar.terminal_title_text()
    assert first.startswith(f"{TERMINAL_SPINNER_FRAMES[0]} ")
    bar.advance_spinner()
    assert bar.terminal_title_text().startswith(f"{TERMINAL_SPINNER_FRAMES[1]} ")
    assert bar.terminal_title_text() != first


def test_spinner_interval_is_260ms() -> None:
    assert SPINNER_INTERVAL == pytest.approx(0.26)


def test_app_name_constant() -> None:
    assert APP_TITLE_NAME == "amplifier"


def test_terminal_title_sequence_sanitizes_controls_and_bounds_length() -> None:
    sequence = terminal_title_sequence(f"✳ working\x1b]0;spoof\x07\n{'x' * 300}")
    assert sequence.startswith("\x1b]0;✳ working ]0;spoof x")
    assert sequence.endswith("\x07")
    payload = sequence.removeprefix("\x1b]0;").removesuffix("\x07")
    assert "\x1b" not in payload
    assert "\x07" not in payload
    assert "\n" not in payload
    assert len(payload) == TERMINAL_TITLE_MAX_CHARS


def test_terminal_title_write_uses_osc_and_flushes() -> None:
    class RecordingDriver:
        is_headless = False
        is_web = False

        def __init__(self) -> None:
            self.writes: list[str] = []
            self.flushes = 0

        def write(self, data: str) -> None:
            self.writes.append(data)

        def flush(self) -> None:
            self.flushes += 1

    driver = RecordingDriver()
    assert write_terminal_title(driver, "✦ amplifier-app-tui")  # type: ignore[arg-type]
    assert driver.writes == ["\x1b]0;✦ amplifier-app-tui\x07"]
    assert driver.flushes == 1


# -- Pilot: spinner timer + rendering ------------------------------------------


@pytest.mark.asyncio
async def test_title_bar_spinner_runs_only_while_running() -> None:
    app = ChromeApp()
    async with app.run_test() as pilot:
        bar = app.query_one("#title", TitleBar)
        bar.state_text = "ready"
        bar.bundle_uri = "dev"
        bar.session_short = "a1b2c3"
        await pilot.pause()
        assert bar._spinner_timer is None
        assert bar.title_text() == "amplifier — ready — dev — a1b2c3"

        bar.running = True
        await pilot.pause()
        assert bar._spinner_timer is not None
        first = bar.spinner_glyph
        assert first == "✳"
        # The timer advances the glyph in real time (~260ms per frame).
        await pilot.pause(SPINNER_INTERVAL + 0.15)
        assert bar.spinner_glyph != first

        bar.running = False
        await pilot.pause()
        assert bar._spinner_timer is None
        assert not bar.title_text().startswith(("✳", "✦", "✧"))


@pytest.mark.asyncio
async def test_title_state_text_updates_render() -> None:
    app = ChromeApp()
    async with app.run_test() as pilot:
        bar = app.query_one("#title", TitleBar)
        bar.state_text = "✳ coordinating 3 agents"
        await pilot.pause()
        assert "coordinating 3 agents" in bar.title_text()


# -- notice slot ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_notice_shows_and_auto_dismisses() -> None:
    app = ChromeApp()
    async with app.run_test() as pilot:
        slot = app.query_one("#notice", NoticeSlot)
        slot.show_notice("mode plan · read-only")
        await pilot.pause(0.01)  # stay well inside the test slot's 0.05s TTL
        assert slot.current == "mode plan · read-only"
        assert slot.has_class("-visible")
        await pilot.pause(0.3)  # duration is 0.05s in this test app
        assert slot.current is None
        assert not slot.has_class("-visible")


@pytest.mark.asyncio
async def test_notice_is_single_slot_and_replaces() -> None:
    app = ChromeApp()
    async with app.run_test() as pilot:
        slot = app.query_one("#notice", NoticeSlot)
        slot.show_notice("first")
        slot.show_notice("steer queued · shift+enter queues a full next-turn message")
        await pilot.pause(0.01)  # stay well inside the test slot's 0.05s TTL
        assert slot.current == ("steer queued · shift+enter queues a full next-turn message")


@pytest.mark.asyncio
async def test_notice_per_call_duration_overrides_default() -> None:
    """Mockup showNotice(text, ms): approval notices pass 6000 over the 4000 default."""
    app = ChromeApp()
    async with app.run_test() as pilot:
        slot = app.query_one("#notice", NoticeSlot)
        slot.show_notice("approval required · choose below the transcript", duration=0.4)
        await pilot.pause(0.2)  # past the 0.05s default, before the override
        assert slot.current == "approval required · choose below the transcript"
        await pilot.pause(0.4)
        assert slot.current is None


@pytest.mark.asyncio
async def test_notice_manual_dismiss() -> None:
    app = ChromeApp()
    async with app.run_test() as pilot:
        slot = app.query_one("#notice", NoticeSlot)
        slot.show_notice("approval required · choose below the transcript")
        await pilot.pause()
        slot.dismiss_notice()
        assert slot.current is None
        assert not slot.has_class("-visible")
