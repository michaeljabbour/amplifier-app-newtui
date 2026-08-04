"""Tests for the title bar (ui/chrome.py) and notice slot (ui/notices.py)."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from amplifier_app_tui.ui.chrome import (
    APP_TITLE_NAME,
    SPINNER_INTERVAL,
    TERMINAL_TITLE_MAX_CHARS,
    TERMINAL_SPINNER_FRAMES,
    TITLE_BUNDLE_MAX_CELLS,
    TitleBar,
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
    bar.set_reactive(TitleBar.bundle, "dev-bundle")
    bar.set_reactive(TitleBar.session_short, "a1b2c3")
    assert bar.title_text() == "amplifier — ready — dev-bundle — a1b2c3"


def test_empty_identity_fragments_are_skipped() -> None:
    bar = TitleBar()
    bar.set_reactive(TitleBar.state_text, "planning")
    assert bar.title_text() == "amplifier — planning"


# -- D4 AC4: long bundle paths truncate safely, never wrap the composer -------
#
# Finding 2 (post-merge compliance audit): ``TitleBar`` is the sole persistent
# home for the active bundle (item D4 AC1), but a ``--bundle`` path/URI or a
# ``bundle.active`` settings value (``kernel/config.resolve_bundle_source``)
# is user-supplied and unbounded. Before this fix ``_plain_title`` appended it
# verbatim -- no truncation, no ellipsis, no tooltip -- so Textual silently
# hard-clipped an over-long value with zero visual cue anything was cut. These
# tests pin the reconciled behavior: a bounded, cell-width-safe truncation
# that always ends a cut value in one visible ellipsis, while the row itself
# never grows past its fixed ``height: 1`` (so it can never wrap down onto the
# composer docked below it). The FULL value stays inspectable via ``/status``
# (test_ui_session_ops_view.py pins that ``status_spans`` never truncates its
# ``bundle`` row).


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


def test_idle_title_truncates_long_bundle_with_ellipsis() -> None:
    """AC4, via the real rendering path (title_text -> _plain_title)."""
    long_bundle = "org/" + "x" * 100 + "/tui-bundle"
    bar = TitleBar()
    bar.set_reactive(TitleBar.state_text, "ready")
    bar.set_reactive(TitleBar.bundle, long_bundle)
    bar.set_reactive(TitleBar.session_short, "a1b2c3")
    title = bar.title_text()
    assert long_bundle not in title  # the raw unbounded value never rides the title
    assert "\u2026" in title
    assert title.startswith("amplifier — ready — org/")
    assert title.endswith("— a1b2c3")  # session fragment survives after the truncated one


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
        bar.bundle = "dev"
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
