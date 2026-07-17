"""Tests for the composer (ui/composer.py) — input semantics as messages."""

from __future__ import annotations

from typing import TypeVar

import pytest
from textual.app import App, ComposeResult
from textual.message import Message

from amplifier_app_newtui.model.modes import get_mode
from amplifier_app_newtui.ui.composer import Composer, ComposerInput, ModeBadge
from amplifier_app_newtui.ui.keymap import COMPOSER_PLACEHOLDER
from amplifier_app_newtui.ui.themes import DEFAULT_THEME, register_themes, theme_id


class ComposerApp(App[None]):
    def __init__(self, *, kitty_protocol: bool = True) -> None:
        super().__init__()
        register_themes(self)
        self.theme = theme_id(DEFAULT_THEME)
        self._kitty = kitty_protocol
        self.messages: list[Message] = []

    def compose(self) -> ComposeResult:
        yield Composer(kitty_protocol=self._kitty, id="composer")

    def on_mount(self) -> None:
        self.query_one("#composer", Composer).focus_input()

    def on_composer_submit(self, message: Composer.Submit) -> None:
        self.messages.append(message)

    def on_composer_steer(self, message: Composer.Steer) -> None:
        self.messages.append(message)

    def on_composer_queue_message(self, message: Composer.QueueMessage) -> None:
        self.messages.append(message)

    def on_composer_open_palette(self, message: Composer.OpenPalette) -> None:
        self.messages.append(message)

    def on_composer_palette_filter_cleared(
        self, message: Composer.PaletteFilterCleared
    ) -> None:
        self.messages.append(message)

    def on_composer_esc_pressed(self, message: Composer.EscPressed) -> None:
        self.messages.append(message)

    def on_composer_cycle_mode_requested(
        self, message: Composer.CycleModeRequested
    ) -> None:
        self.messages.append(message)


MessageT = TypeVar("MessageT", bound=Message)


def _of(app: ComposerApp, kind: type[MessageT]) -> list[MessageT]:
    return [m for m in app.messages if isinstance(m, kind)]


def test_placeholder_is_exact_spec_string() -> None:
    composer_input = ComposerInput()
    assert composer_input.placeholder == COMPOSER_PLACEHOLDER
    assert COMPOSER_PLACEHOLDER == (
        "Message Amplifier…  "
        "( / commands · shift+tab mode · enter send · type mid-turn to steer )"
    )


@pytest.mark.asyncio
async def test_idle_enter_posts_submit_and_clears() -> None:
    app = ComposerApp()
    async with app.run_test() as pilot:
        await pilot.press("h", "i", "enter")
        await pilot.pause()
        submits = _of(app, Composer.Submit)
        assert len(submits) == 1
        assert submits[0].text == "hi"
        assert not _of(app, Composer.Steer)
        assert app.query_one("#composer", Composer).text == ""


@pytest.mark.asyncio
async def test_running_enter_posts_steer_not_submit() -> None:
    app = ComposerApp()
    async with app.run_test() as pilot:
        app.query_one("#composer", Composer).running = True
        await pilot.press("g", "o", "enter")
        await pilot.pause()
        steers = _of(app, Composer.Steer)
        assert len(steers) == 1
        assert steers[0].text == "go"
        assert not _of(app, Composer.Submit)


@pytest.mark.asyncio
async def test_empty_enter_posts_nothing() -> None:
    app = ComposerApp()
    async with app.run_test() as pilot:
        await pilot.press("enter")
        await pilot.pause()
        assert not _of(app, Composer.Submit)
        assert not _of(app, Composer.Steer)


@pytest.mark.asyncio
async def test_shift_enter_posts_queue_message() -> None:
    app = ComposerApp()
    async with app.run_test() as pilot:
        await pilot.press("l", "a", "t", "e", "r", "shift+enter")
        await pilot.pause()
        queued = _of(app, Composer.QueueMessage)
        assert len(queued) == 1
        assert queued[0].text == "later"


@pytest.mark.asyncio
async def test_alt_enter_fallback_posts_queue_message() -> None:
    app = ComposerApp(kitty_protocol=False)
    async with app.run_test() as pilot:
        await pilot.press("x", "alt+enter")
        await pilot.pause()
        queued = _of(app, Composer.QueueMessage)
        assert len(queued) == 1
        assert queued[0].text == "x"


def test_queue_hint_swaps_on_missing_kitty_protocol() -> None:
    assert Composer(kitty_protocol=True).queue_hint == "shift+enter"
    assert Composer(kitty_protocol=False).queue_hint == "alt+enter"


def test_short_paste_stays_inline() -> None:
    c = Composer()
    assert c.register_paste("a short paste\nwith two lines") is None


def test_long_paste_collapses_to_stub_and_expands() -> None:
    c = Composer()
    payload = "\n".join(f"line {i}" for i in range(30))  # > 10 lines
    stub = c.register_paste(payload)
    assert stub is not None and stub.startswith("[Pasted #1")
    assert "30 lines" in stub
    # composer shows only the stub, but it expands to the full text
    typed = f"here is the code: {stub} — please review"
    assert c._expand(typed) == f"here is the code: {payload} — please review"
    # a big single-line paste (> char threshold) also collapses
    big = "x" * 900
    stub2 = c.register_paste(big)
    assert stub2 is not None and "900 chars" in stub2


@pytest.mark.asyncio
async def test_staged_image_rides_submit_and_drops_when_placeholder_deleted() -> None:
    from amplifier_app_newtui.kernel.clipboard import ImageAttachment

    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
    app = ComposerApp()
    async with app.run_test() as pilot:
        composer = app.query_one("#composer", Composer)
        composer.add_image(ImageAttachment(png, "image/png"))
        await pilot.pause()
        assert "[Image #1]" in composer.text
        await pilot.press("h", "i", "enter")
        await pilot.pause()
        submits = _of(app, Composer.Submit)
        assert len(submits) == 1
        assert len(submits[0].attachments) == 1  # carried with the surviving placeholder
        assert "[Image #1]" in submits[0].text

    # Deleting the placeholder drops the attachment.
    app2 = ComposerApp()
    async with app2.run_test() as pilot:
        composer = app2.query_one("#composer", Composer)
        composer.add_image(ImageAttachment(png, "image/png"))
        await pilot.pause()
        composer._input.clear()  # placeholder gone
        composer._input.insert("just text")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        submits = _of(app2, Composer.Submit)
        assert len(submits) == 1 and submits[0].attachments == ()


@pytest.mark.asyncio
async def test_pasting_an_image_file_path_attaches_it(tmp_path) -> None:
    # Cmd+V of an image file / drag-and-drop arrives as a bracketed paste of
    # the path — it must attach as an image, not insert the path as text.
    from textual import events

    png = tmp_path / "shot.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40)
    app = ComposerApp()
    async with app.run_test() as pilot:
        composer = app.query_one("#composer", Composer)
        composer._input.post_message(events.Paste(str(png)))
        await pilot.pause()
        assert "[Image #1]" in composer.text
        assert str(png) not in composer.text  # path not left as literal text
        await pilot.press("enter")
        await pilot.pause()
        submits = _of(app, Composer.Submit)
        assert len(submits) == 1 and len(submits[0].attachments) == 1


@pytest.mark.asyncio
async def test_paste_event_collapses_long_block_and_submits_full_text() -> None:
    from textual import events

    app = ComposerApp()
    async with app.run_test() as pilot:
        composer = app.query_one("#composer", Composer)
        payload = "\n".join(f"row {i}" for i in range(20))
        composer._input.post_message(events.Paste(payload))
        await pilot.pause()
        shown = composer.text
        assert "[Pasted #1" in shown and "row 19" not in shown  # collapsed, not flooded
        await pilot.press("enter")
        await pilot.pause()
        submits = _of(app, Composer.Submit)
        assert len(submits) == 1
        assert submits[0].text == payload  # full text restored on submit
        assert composer.text == ""  # cleared, stubs forgotten


@pytest.mark.asyncio
async def test_slash_prefix_posts_live_palette_filters() -> None:
    app = ComposerApp()
    async with app.run_test() as pilot:
        await pilot.press("slash", "m", "o")
        await pilot.pause()
        opens = _of(app, Composer.OpenPalette)
        assert [m.filter for m in opens] == ["/", "/m", "/mo"]


@pytest.mark.asyncio
async def test_deleting_slash_prefix_clears_palette_filter() -> None:
    app = ComposerApp()
    async with app.run_test() as pilot:
        await pilot.press("slash", "m", "backspace", "backspace")
        await pilot.pause()
        assert len(_of(app, Composer.PaletteFilterCleared)) == 1


@pytest.mark.asyncio
async def test_escape_posts_esc_pressed() -> None:
    app = ComposerApp()
    async with app.run_test() as pilot:
        await pilot.press("escape")
        await pilot.pause()
        assert len(_of(app, Composer.EscPressed)) == 1


@pytest.mark.asyncio
async def test_mode_badge_click_requests_cycle() -> None:
    app = ComposerApp()
    async with app.run_test() as pilot:
        await pilot.click(ModeBadge)
        await pilot.pause()
        assert len(_of(app, Composer.CycleModeRequested)) == 1


@pytest.mark.asyncio
async def test_set_mode_updates_badge_and_accent_classes() -> None:
    app = ComposerApp()
    async with app.run_test() as pilot:
        composer = app.query_one("#composer", Composer)
        badge = app.query_one(ModeBadge)
        # Default: auto — the boot posture (§4 amendment), orange accent.
        assert composer.has_class("mode-auto")
        assert badge.has_class("mode-auto")
        assert str(badge.content) == "[auto]"
        # chat's accent uses the rule token via the mode-chat class.
        composer.set_mode(get_mode("chat"))
        await pilot.pause()
        assert composer.has_class("mode-chat")
        assert not composer.has_class("mode-auto")
        assert badge.has_class("mode-chat")
        assert str(badge.content) == "[chat]"
        composer.set_mode(get_mode("build"))
        await pilot.pause()
        assert composer.has_class("mode-build")
        assert not composer.has_class("mode-chat")
        assert badge.has_class("mode-build")
        assert str(badge.content) == "[build]"


@pytest.mark.asyncio
async def test_placeholder_uses_dimmer_token() -> None:
    """Mockup CSS: input::placeholder { color: var(--dimmer); } (§1/§2)."""
    app = ComposerApp()
    async with app.run_test() as pilot:
        del pilot
        composer_input = app.query_one(ComposerInput)
        style = composer_input.get_visual_style("text-area--placeholder")
        assert style.foreground is not None
        assert style.foreground.hex.lower() == app.theme_variables["dimmer"].lower()


@pytest.mark.asyncio
async def test_palette_filter_is_trimmed_of_trailing_whitespace() -> None:
    """Mockup onInput: palFilter = value.trim() — '/m ' still filters '/m'."""
    app = ComposerApp()
    async with app.run_test() as pilot:
        await pilot.press("slash", "m", "space")
        await pilot.pause()
        opens = _of(app, Composer.OpenPalette)
        assert [m.filter for m in opens] == ["/", "/m", "/m"]


@pytest.mark.asyncio
async def test_ctrl_c_copies_transcript_selection_despite_composer_focus() -> None:
    """TextArea's own ctrl+c binding swallowed the key while the composer
    had focus, so transcript drag-selections could never be copied (user
    report: "can't copy from the terminal"). The app-level priority
    binding copies whichever selection exists and confirms with a notice."""
    from textual.events import MouseDown, MouseMove, MouseUp

    from amplifier_app_newtui.ui.app import NewTuiApp
    from amplifier_app_newtui.ui.demo_wiring import DemoRuntimeAdapter

    app = NewTuiApp(DemoRuntimeAdapter(instant=True))
    copied: list[str] = []

    def _fake_copy(text: str) -> None:
        copied.append(text)
        app._os_clipboard_copied = True  # OS tool accepted (pbcopy path)

    app.copy_to_clipboard = _fake_copy  # type: ignore[method-assign]
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause(0.4)

        def ev(cls, x: int, y: int):
            return cls(widget=None, x=x, y=y, delta_x=0, delta_y=0, button=1,
                       shift=False, meta=False, ctrl=False, screen_x=x, screen_y=y, style="")

        app.screen._forward_event(ev(MouseDown, 10, 8))
        await pilot.pause()
        app.screen._forward_event(ev(MouseMove, 60, 8))
        await pilot.pause()
        app.screen._forward_event(ev(MouseUp, 60, 8))
        await pilot.pause()
        app.composer.focus_input()
        await pilot.pause()

        await pilot.press("ctrl+c")
        await pilot.pause()
        assert copied and len(copied[0]) > 10
        assert app.notice_slot.current == f"copied · {len(copied[0])} chars"

        # The composer's own selection wins over the transcript's.
        await pilot.press("h", "i")
        app.composer._input.select_all()
        await pilot.pause()
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert copied[-1] == "hi"

        # Nothing selected → guidance, not a silent no-op.
        app.composer._input.clear()
        app.screen.clear_selection()
        await pilot.pause()
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert app.notice_slot.current.startswith("nothing selected")


@pytest.mark.asyncio
async def test_settled_drag_selection_copies_automatically() -> None:
    """Copy-on-select: the ⌘C reflex never reaches a terminal app (user
    report: 'copy and paste still not working'), so a settled transcript
    drag-selection must land on the clipboard by itself."""
    from textual.events import MouseDown, MouseMove, MouseUp

    from amplifier_app_newtui.ui.app import NewTuiApp
    from amplifier_app_newtui.ui.demo_wiring import DemoRuntimeAdapter

    app = NewTuiApp(DemoRuntimeAdapter(instant=True))
    copied: list[str] = []
    app.copy_to_clipboard = lambda text: copied.append(text)  # type: ignore[method-assign]
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause(0.4)

        def ev(cls, x: int, y: int):
            return cls(widget=None, x=x, y=y, delta_x=0, delta_y=0, button=1,
                       shift=False, meta=False, ctrl=False, screen_x=x, screen_y=y, style="")

        app.screen._forward_event(ev(MouseDown, 10, 8))
        await pilot.pause()
        app.screen._forward_event(ev(MouseMove, 60, 8))
        await pilot.pause()
        app.screen._forward_event(ev(MouseUp, 60, 8))
        await pilot.pause(0.7)  # let the 0.4s settle timer fire
        assert copied and len(copied[0]) > 10
        assert app.notice_slot.current.startswith("copied on select · ")
        # No duplicate copy for the same settled selection.
        assert len(copied) == 1
