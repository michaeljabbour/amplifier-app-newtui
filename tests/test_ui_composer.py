"""Tests for the composer (ui/composer.py) — input semantics as messages."""

from __future__ import annotations

from typing import TypeVar

import pytest
from textual.app import App, ComposeResult
from textual.message import Message

from amplifier_app_tui.model.modes import get_mode
from amplifier_app_tui.ui.composer import (
    Composer,
    ComposerInput,
    ModeBadge,
    active_file_mention,
)
from amplifier_app_tui.ui.file_mentions import FileMentionIntent
from amplifier_app_tui.ui.keymap import COMPOSER_PLACEHOLDER
from amplifier_app_tui.ui.themes import DEFAULT_THEME, register_themes, theme_id


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

    def on_composer_palette_filter_cleared(self, message: Composer.PaletteFilterCleared) -> None:
        self.messages.append(message)

    def on_file_mention_intent(self, message: FileMentionIntent) -> None:
        self.messages.append(message)

    def on_composer_esc_pressed(self, message: Composer.EscPressed) -> None:
        self.messages.append(message)

    def on_composer_cycle_mode_requested(self, message: Composer.CycleModeRequested) -> None:
        self.messages.append(message)


MessageT = TypeVar("MessageT", bound=Message)


def _of(app: ComposerApp, kind: type[MessageT]) -> list[MessageT]:
    return [m for m in app.messages if isinstance(m, kind)]


def test_placeholder_is_exact_spec_string() -> None:
    composer_input = ComposerInput()
    assert composer_input.placeholder == COMPOSER_PLACEHOLDER
    assert COMPOSER_PLACEHOLDER == (
        "Message Amplifier…  ( ↑ history · ctrl+j newline · enter send · / commands )"
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
async def test_ctrl_j_and_ctrl_enter_insert_newlines_before_submit() -> None:
    app = ComposerApp()
    async with app.run_test() as pilot:
        await pilot.press(*"first", "ctrl+j", *"second", "ctrl+enter", *"third")
        assert app.query_one(Composer).text == "first\nsecond\nthird"
        await pilot.press("enter")
        await pilot.pause()
        assert _of(app, Composer.Submit)[0].text == "first\nsecond\nthird"


@pytest.mark.asyncio
async def test_up_down_recall_prompts_and_restore_current_draft() -> None:
    app = ComposerApp()
    async with app.run_test() as pilot:
        await pilot.press(*"first", "enter", *"second", "enter", *"draft")
        composer = app.query_one(Composer)

        await pilot.press("up")
        assert composer.text == "second"
        await pilot.press("up")
        assert composer.text == "first"
        await pilot.press("down")
        assert composer.text == "second"
        await pilot.press("down")
        assert composer.text == "draft"


@pytest.mark.asyncio
async def test_resumed_prompt_history_is_seeded_and_deduplicated() -> None:
    app = ComposerApp()
    async with app.run_test() as pilot:
        composer = app.query_one(Composer)
        composer.seed_history(("older prompt", "latest prompt", "latest prompt"))
        await pilot.press("up")
        assert composer.text == "latest prompt"
        await pilot.press("up")
        assert composer.text == "older prompt"


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


def test_active_file_mention_only_matches_token_at_cursor() -> None:
    text = "review @src/ap then later"
    assert active_file_mention(text, (0, 14)) == ("src/ap", 7, 14)
    assert active_file_mention(text, (0, len(text))) is None
    assert active_file_mention("mail@example.com", (0, 16)) is None


@pytest.mark.asyncio
async def test_file_mention_posts_filter_and_intercepts_navigation() -> None:
    app = ComposerApp()
    async with app.run_test() as pilot:
        await pilot.press("@", "s", "r", "c")
        await pilot.pause()
        filters = [message for message in _of(app, FileMentionIntent) if message.action == "filter"]
        assert [message.query for message in filters] == ["", "s", "sr", "src"]

        composer = app.query_one(Composer)
        composer.mention_open = True
        await pilot.press("down", "enter")
        await pilot.pause()
        intents = _of(app, FileMentionIntent)
        assert [message.delta for message in intents if message.action == "move"] == [1]
        assert sum(message.action == "accept" for message in intents) == 1
        assert not _of(app, Composer.Submit)


@pytest.mark.asyncio
async def test_apply_file_mention_replaces_query_and_quotes_spaces() -> None:
    app = ComposerApp()
    async with app.run_test() as pilot:
        composer = app.query_one(Composer)
        await pilot.press(*"open @rea")
        await pilot.pause()
        assert composer.apply_file_mention("docs/read me.md") is True
        await pilot.pause()
        assert composer.text == 'open @"docs/read me.md" '


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
    from amplifier_app_tui.kernel.clipboard import ImageAttachment

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
async def test_immediate_identical_paste_replay_is_suppressed() -> None:
    """A terminal replay may emit two identical bracketed-paste events.

    The second event must not duplicate the prompt, while an intentional
    repeat after the narrow replay window remains possible.
    """
    from textual import events

    from amplifier_app_tui.ui.composer import PASTE_DUPLICATE_WINDOW_SECONDS

    app = ComposerApp()
    async with app.run_test() as pilot:
        composer = app.query_one("#composer", Composer)
        payload = "investigate the ~/dev/amplifier-runpodsetup setup"
        composer._input.post_message(events.Paste(payload))
        composer._input.post_message(events.Paste(payload))
        await pilot.pause()
        assert composer.text == payload

        await pilot.pause(PASTE_DUPLICATE_WINDOW_SECONDS + 0.05)
        composer._input.post_message(events.Paste(payload))
        await pilot.pause()
        assert composer.text == payload * 2


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

    from amplifier_app_tui.ui.app import TuiApp
    from amplifier_app_tui.ui.demo_wiring import DemoRuntimeAdapter

    app = TuiApp(DemoRuntimeAdapter(instant=True))
    copied: list[str] = []

    def _fake_copy(text: str) -> None:
        copied.append(text)
        app._os_clipboard_copied = True  # OS tool accepted (pbcopy path)

    app.copy_to_clipboard = _fake_copy  # type: ignore[method-assign]
    async with app.run_test(size=(120, 36)) as pilot:
        # Wait for the demo transcript to actually paint rows before the drag.
        # A fixed pause races the paint under coverage instrumentation, leaving
        # the drag over empty rows → an empty selection → a flaky ctrl+c copy.
        for _ in range(60):
            await pilot.pause(0.05)
            if any(b.kind == "answer" for b in app.transcript.blocks):
                break
        await pilot.pause(0.1)

        def ev(cls, x: int, y: int):
            return cls(
                widget=None,
                x=x,
                y=y,
                delta_x=0,
                delta_y=0,
                button=1,
                shift=False,
                meta=False,
                ctrl=False,
                screen_x=x,
                screen_y=y,
                style="",
            )

        # +1 row: the AC2 final-answer start marker ("● Final answer") now
        # opens a final answer, so row 0 of the block is the marker, not
        # prose -- drag one row into the body, which is correct whether or
        # not a marker is present (region-relative, never a magic number).
        answer_block = next(b for b in app.transcript.blocks if b.kind == "answer")
        answer_widget = app.transcript.get_widget(answer_block.id)
        assert answer_widget is not None
        row = answer_widget.region.y + 1

        app.screen._forward_event(ev(MouseDown, 10, row))
        await pilot.pause()
        app.screen._forward_event(ev(MouseMove, 60, row))
        await pilot.pause()
        app.screen._forward_event(ev(MouseUp, 60, row))
        await pilot.pause()
        app.composer.focus_input()
        await pilot.pause()

        await pilot.press("ctrl+c")
        await pilot.pause()
        assert copied and len(copied[0]) > 10
        # Copy-on-select may also fire on the settled drag, so the notice can
        # land as either "copied" (explicit ctrl+c) or "copied on select"
        # (the settle reflex) depending on which resolves last. Both are a
        # correct "copied N chars" outcome; asserting one exact string raced
        # the settle under coverage instrumentation (flaky). The copy actually
        # happening despite composer focus (above) is the real contract.
        assert app.notice_slot.current in (
            f"copied · {len(copied[0])} chars",
            f"copied on select · {len(copied[0])} chars",
        )

        # The composer's own selection wins over the transcript's.
        await pilot.press("h", "i")
        app.composer._input.select_all()
        await pilot.pause()
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert copied[-1] == "hi"

        # Nothing selected: ctrl+c honors the terminal/Mac convention.
        app.composer._input.clear()
        app.screen.clear_selection()
        await pilot.pause()

        # ...idle → quit (like ctrl+d). Spy so the test app doesn't actually exit.
        quit_calls: list[bool] = []
        app.exit = lambda *a, **k: quit_calls.append(True)  # type: ignore[method-assign]
        app.action_copy_selection()
        assert quit_calls == [True]

        # ...running turn → interrupt (not quit).
        interrupts: list[bool] = []
        app.turn_active = True
        app.interrupt_turn = lambda: interrupts.append(True)  # type: ignore[method-assign]
        app.action_copy_selection()
        assert interrupts == [True]


@pytest.mark.asyncio
async def test_settled_drag_selection_copies_automatically() -> None:
    """Copy-on-select: the ⌘C reflex never reaches a terminal app (user
    report: 'copy and paste still not working'), so a settled transcript
    drag-selection must land on the clipboard by itself."""
    from textual.events import MouseDown, MouseMove, MouseUp

    from amplifier_app_tui.ui.app import TuiApp
    from amplifier_app_tui.ui.demo_wiring import DemoRuntimeAdapter

    app = TuiApp(DemoRuntimeAdapter(instant=True))
    copied: list[str] = []
    app.copy_to_clipboard = lambda text: copied.append(text)  # type: ignore[method-assign]
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause(0.4)

        def ev(cls, x: int, y: int):
            return cls(
                widget=None,
                x=x,
                y=y,
                delta_x=0,
                delta_y=0,
                button=1,
                shift=False,
                meta=False,
                ctrl=False,
                screen_x=x,
                screen_y=y,
                style="",
            )

        # +1 row: see test_ctrl_c_copies_transcript_selection_despite_composer_focus
        # -- drag one row into the answer body, past the AC2 start marker.
        answer_block = next(b for b in app.transcript.blocks if b.kind == "answer")
        answer_widget = app.transcript.get_widget(answer_block.id)
        assert answer_widget is not None
        row = answer_widget.region.y + 1

        app.screen._forward_event(ev(MouseDown, 10, row))
        await pilot.pause()
        app.screen._forward_event(ev(MouseMove, 60, row))
        await pilot.pause()
        app.screen._forward_event(ev(MouseUp, 60, row))
        await pilot.pause(0.7)  # let the 0.4s settle timer fire
        assert copied and len(copied[0]) > 10
        assert app.notice_slot.current.startswith("copied on select · ")
        # No duplicate copy for the same settled selection.
        assert len(copied) == 1


@pytest.mark.asyncio
async def test_transcript_selection_is_character_ranged() -> None:
    """Regression pin (user report: 'I can only select lines'): the
    transcript delegates to Textual's native character-ranged selection —
    a drag anchors at a (line, column) cell, so what lands on the clipboard
    is the partial first line from the anchor column, full middle lines,
    and the partial last line to the head column, never whole rows. Pinned
    so a Textual upgrade or an app-side selection model can never quietly
    degrade the granularity back to lines."""
    from textual.events import MouseDown, MouseMove, MouseUp

    from amplifier_app_tui.model.blocks import Answer, Segment
    from amplifier_app_tui.ui.app import TuiApp
    from amplifier_app_tui.ui.demo_wiring import DemoRuntimeAdapter

    app = TuiApp(DemoRuntimeAdapter(instant=True))
    # Keep the copy-on-settle reflex away from the real OS clipboard.
    app.copy_to_clipboard = lambda text: None  # type: ignore[method-assign]
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause(0.3)
        w1 = app.transcript.append(Answer(id="sel-a", spans=(Segment(text="alpha bravo charlie"),)))
        w2 = app.transcript.append(Answer(id="sel-b", spans=(Segment(text="delta echo foxtrot"),)))
        w3 = app.transcript.append(Answer(id="sel-c", spans=(Segment(text="你好 world"),)))
        await pilot.pause(0.3)
        assert w1 is not None and w2 is not None and w3 is not None

        def ev(cls, x: int, y: int):
            return cls(
                widget=None,
                x=x,
                y=y,
                delta_x=0,
                delta_y=0,
                button=1,
                shift=False,
                meta=False,
                ctrl=False,
                screen_x=x,
                screen_y=y,
                style="",
            )

        async def drag(x0: int, y0: int, x1: int, y1: int) -> str | None:
            app.screen._forward_event(ev(MouseDown, x0, y0))
            await pilot.pause()
            app.screen._forward_event(ev(MouseMove, x1, y1))
            await pilot.pause()
            app.screen._forward_event(ev(MouseUp, x1, y1))
            await pilot.pause()
            return app.screen.get_selected_text()

        r1, r2, r3 = w1.region, w2.region, w3.region

        # Single line, mid-word to mid-word: exactly the dragged cells —
        # not the whole rendered row.
        assert await drag(r1.x + 6, r1.y, r1.x + 10, r1.y) == "bravo"

        # Across two blocks: partial first line from the anchor column,
        # partial last line to the head column.
        assert await drag(r1.x + 6, r1.y, r2.x + 4, r2.y) == "bravo charlie\ndelt"

        # Wide glyphs: selection columns are terminal cells (你好 spans
        # four cells), and the extraction still lands on the right chars.
        assert await drag(r3.x + 5, r3.y, r3.x + 9, r3.y) == "world"


@pytest.mark.asyncio
async def test_native_clipboard_write_does_not_block_the_ui(monkeypatch) -> None:
    """A slow platform clipboard process runs off the Textual event loop."""
    import threading
    import time

    from amplifier_app_tui.ui import app_support
    from amplifier_app_tui.ui.app import TuiApp
    from amplifier_app_tui.ui.demo_wiring import DemoRuntimeAdapter

    finished = threading.Event()

    def slow_copy(_text: str) -> bool:
        time.sleep(0.2)
        finished.set()
        return True

    monkeypatch.setattr(app_support, "os_clipboard_available", lambda: True)
    monkeypatch.setattr(app_support, "os_clipboard_copy", slow_copy)
    app = TuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        # Non-timing proof of non-blocking (replaces a wall-clock
        # ``elapsed < 0.05`` flake — audit 2026-07): the patched native
        # writer sleeps 0.2s before it sets ``finished``. Had
        # ``copy_to_clipboard`` run it on the UI loop, ``finished`` would
        # already be set the instant the call returns — that it is NOT
        # proves the write was off-loaded to a worker thread.
        app.copy_to_clipboard("non-blocking")
        assert not finished.is_set()
        await pilot.pause(0.3)
        assert finished.is_set()


@pytest.mark.asyncio
async def test_set_draft_replaces_buffer_and_ends_history_nav() -> None:
    """set_draft (the /unstash recall seam) loads the whole draft, cursor at
    the end, and ends any history browsing."""
    app = ComposerApp()
    async with app.run_test() as pilot:
        composer = app.query_one(Composer)
        await pilot.press(*"older", "enter")  # seeds prompt history
        await pilot.press("up")  # now browsing history: text == "older"
        assert composer.history_browsing
        composer.set_draft("recalled stash text")
        await pilot.pause()
        assert composer.text == "recalled stash text"
        assert not composer.history_browsing
        assert composer._input.cursor_location == (0, len("recalled stash text"))
