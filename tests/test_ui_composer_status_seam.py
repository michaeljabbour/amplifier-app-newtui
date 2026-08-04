"""Visual regression coverage for the D2 composer/status structural seam.

Compliance 2026-08-02, item D2 (David Koleczek's UX review, July 31 2026):
the composer and the persistent status band below it used to share one
undivided ``$bg-chrome`` fill (``ui/composer.py`` and ``ui/footer.py`` were
byte-identical since the review). ``FooterBar`` now owns an unconditional
``border-top: solid $rule`` (see ``ui/footer.py``) and ``Composer`` lifts
onto ``$bg-tab`` while focus is anywhere inside it (see ``ui/composer.py``).

These whole-fragment SVG snapshots pin that seam across the states the
brief calls out for AC5: empty, multiline, autocomplete (the ``@file``
strip), and streaming (a running turn). A focused harness — composer +
file-mention strip + footer, no title bar/transcript/palette/lanes — keeps
each pin tight to the seam itself rather than the whole app's chrome (the
existing ``test_ui_snapshots.py`` already locks two full-screen states).
"""

from __future__ import annotations

import re
from pathlib import Path

from textual._doc import take_svg_screenshot
from textual.app import App, ComposeResult
from textual.containers import Container
from textual.widgets import Static

from amplifier_app_tui.ui.composer import Composer
from amplifier_app_tui.ui.file_mentions import FileMentionIntent, FileMentionStrip
from amplifier_app_tui.ui.footer import FooterBar, FooterState
from amplifier_app_tui.ui.themes import DEFAULT_THEME, register_themes, theme_id

_SNAPSHOT_DIR = Path(__file__).parent / "__snapshots__" / "test_ui_composer_status_seam"
_SIZE = (100, 16)
_DYNAMIC_TERMINAL_ID = re.compile(r"terminal-\d+")

_IDLE_FOOTER = FooterState(
    mode_id="chat", bundle="dev-bundle", model="claude-fable-5", session_short="a1b2c3"
)
_MENTION_FILES = (
    "src/amplifier_app_tui/ui/composer.py",
    "src/amplifier_app_tui/ui/footer.py",
    "docs/DESIGN-SPEC.md",
)


def _clean_svg(value: str) -> str:
    """Remove Textual's per-process namespace and trailing whitespace."""
    stable_ids = _DYNAMIC_TERMINAL_ID.sub("terminal-SNAPSHOT", value)
    return "\n".join(line.rstrip() for line in stable_ids.splitlines()) + "\n"


def _assert_matches_snapshot(actual: str, name: str) -> None:
    path = _SNAPSHOT_DIR / f"{name}.raw"
    expected = path.read_text(encoding="utf-8")
    assert expected == _clean_svg(expected), "snapshot must remain whitespace-clean"
    assert _clean_svg(actual) == expected, (
        f"{name} changed — if intentional, regenerate the .raw snapshot and review the diff"
    )


class ComposerBandHarness(App[None]):
    """Composer + ``@file`` strip + footer, isolated at the D2 seam.

    Mirrors the real screen's stacking immediately around the seam (an
    auto-height ``$bg-term`` fill standing in for the transcript, the
    file-mention autocomplete strip, the composer-slot, then the footer)
    without pulling in the rest of ``TuiApp``'s chrome (title bar, palette,
    lanes/plan strips) — those already have their own coverage.
    """

    CSS = """
    Screen { background: $bg-term; }
    #fill { height: 1fr; }
    #composer-slot { height: auto; }
    """

    def __init__(self) -> None:
        super().__init__()
        register_themes(self)
        self.theme = theme_id(DEFAULT_THEME)
        self.composer = Composer(id="composer")
        self.file_mentions = FileMentionStrip(id="file-mentions")
        self.footer_bar = FooterBar(id="footer-bar")

    def compose(self) -> ComposeResult:
        yield Static(id="fill")
        yield self.file_mentions
        with Container(id="composer-slot"):
            yield self.composer
        yield self.footer_bar

    def on_mount(self) -> None:
        self.footer_bar.update_state(_IDLE_FOOTER)
        self.composer.focus_input()

    def on_file_mention_intent(self, message: FileMentionIntent) -> None:
        """Minimal stand-in for ``app_support.handle_file_mention_intent``:
        this harness has no command palette, so it drives ``file_mentions``
        directly rather than importing the app-level wiring (out of scope
        for a D2 seam snapshot)."""
        message.stop()
        if message.action == "filter":
            self.file_mentions.apply_filter(message.query)
            self.composer.mention_open = self.file_mentions.is_open
        elif message.action == "move":
            self.file_mentions.move_selection(message.delta)
        else:
            self.file_mentions.apply_filter(None)
            self.composer.mention_open = False


def test_composer_band_empty_snapshot(monkeypatch) -> None:
    """AC5 — empty composer, idle footer: the seam at rest."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLORTERM", "truecolor")
    app = ComposerBandHarness()

    async def settle(pilot) -> None:
        await pilot.pause()

    actual = take_svg_screenshot(app=app, terminal_size=_SIZE, run_before=settle)
    _assert_matches_snapshot(actual, "test_composer_band_empty_snapshot")


def test_composer_band_multiline_snapshot(monkeypatch) -> None:
    """AC5 — a grown, multi-line draft: the seam does not resize/merge as
    the composer grows taller (AC3)."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLORTERM", "truecolor")
    app = ComposerBandHarness()

    async def type_multiline(pilot) -> None:
        app.composer.set_draft("first line\nsecond line\nthird line")
        await pilot.pause()

    actual = take_svg_screenshot(app=app, terminal_size=_SIZE, run_before=type_multiline)
    _assert_matches_snapshot(actual, "test_composer_band_multiline_snapshot")


def test_composer_band_autocomplete_snapshot(monkeypatch) -> None:
    """AC5 — the ``@file`` autocomplete strip open above the composer."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLORTERM", "truecolor")
    app = ComposerBandHarness()

    async def open_mentions(pilot) -> None:
        app.file_mentions.set_files(_MENTION_FILES)
        await pilot.press("@", "c", "o", "m", "p")
        await pilot.pause()

    actual = take_svg_screenshot(app=app, terminal_size=_SIZE, run_before=open_mentions)
    _assert_matches_snapshot(actual, "test_composer_band_autocomplete_snapshot")


def test_composer_band_streaming_snapshot(monkeypatch) -> None:
    """AC5 — a running/streaming turn: the footer's running hints change,
    the seam still holds between the mid-turn composer and the status band."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLORTERM", "truecolor")
    app = ComposerBandHarness()

    async def go_running(pilot) -> None:
        app.composer.running = True
        app.composer.set_draft("focus on the store tests first")
        app.footer_bar.update_state(_IDLE_FOOTER.model_copy(update={"context": "running"}))
        await pilot.pause()

    actual = take_svg_screenshot(app=app, terminal_size=_SIZE, run_before=go_running)
    _assert_matches_snapshot(actual, "test_composer_band_streaming_snapshot")
