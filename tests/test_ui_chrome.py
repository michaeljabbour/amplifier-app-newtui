"""Tests for the title bar (ui/chrome.py) and notice slot (ui/notices.py)."""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from amplifier_app_newtui.ui.chrome import (
    APP_TITLE_NAME,
    SPINNER_INTERVAL,
    TERMINAL_TITLE_MAX_CHARS,
    TERMINAL_SPINNER_FRAMES,
    TitleBar,
    terminal_title_sequence,
    write_terminal_title,
)
from amplifier_app_newtui.ui.notices import NoticeSlot
from amplifier_app_newtui.ui.themes import DEFAULT_THEME, register_themes, theme_id


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
    assert bar.title_text() == ("amplifier-app-newtui — Amplifier — ready — dev-bundle — a1b2c3")


def test_empty_identity_fragments_are_skipped() -> None:
    bar = TitleBar()
    bar.set_reactive(TitleBar.state_text, "planning")
    assert bar.title_text() == "amplifier-app-newtui — Amplifier — planning"


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
    assert APP_TITLE_NAME == "amplifier-app-newtui"


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
    assert write_terminal_title(driver, "✦ amplifier-app-newtui")  # type: ignore[arg-type]
    assert driver.writes == ["\x1b]0;✦ amplifier-app-newtui\x07"]
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
        assert bar.title_text() == "amplifier-app-newtui — Amplifier — ready — dev — a1b2c3"

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
