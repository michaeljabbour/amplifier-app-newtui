"""UI wiring for external-editor compose (ctrl+e) — composer + app seams.

The pure temp-file/spawn logic is pinned in test_kernel_external_editor.py;
here we pin the client wiring: the composer intercepts ctrl+e into a message,
seeds the editor with the visible draft, replaces the draft with the editor's
result, and the app maps a compose outcome onto the composer (success replaces
text; every other outcome leaves it and shows a notice).
"""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.message import Message

from amplifier_app_tui.ui.composer import Composer
from amplifier_app_tui.ui.keymap import KEYMAP, NO_APPROVAL, validate
from amplifier_app_tui.ui.themes import DEFAULT_THEME, register_themes, theme_id


class _ComposerApp(App[None]):
    def __init__(self) -> None:
        super().__init__()
        register_themes(self)  # Composer CSS references $green/$rule/... tokens
        self.theme = theme_id(DEFAULT_THEME)
        self.messages: list[Message] = []

    def compose(self) -> ComposeResult:
        yield Composer(id="composer")

    def on_mount(self) -> None:
        self.query_one("#composer", Composer).focus_input()

    def on_composer_open_external_editor(self, message: Composer.OpenExternalEditor) -> None:
        self.messages.append(message)


# -- keymap binding ------------------------------------------------------------


def test_open_external_editor_binding_is_ctrl_e_and_valid() -> None:
    binding = next(b for b in KEYMAP if b.action == "open_external_editor")
    assert binding.keys == ("ctrl+e",)
    assert binding.label == "ctrl-e edit"
    # active everywhere the composer lives, never while the approval bar owns
    # the keyboard (spec §7).
    assert binding.contexts == NO_APPROVAL
    assert "approval" not in binding.contexts
    validate()  # ctrl+e is free -> the table still validates clean


# -- composer intercept + seed/apply ------------------------------------------


@pytest.mark.asyncio
async def test_ctrl_e_posts_open_external_editor() -> None:
    app = _ComposerApp()
    async with app.run_test() as pilot:
        await pilot.press(*"draft")
        await pilot.press("ctrl+e")
        await pilot.pause()
        posted = [m for m in app.messages if isinstance(m, Composer.OpenExternalEditor)]
        assert len(posted) == 1
        # the draft is untouched by the keypress itself (the app does the edit)
        assert app.query_one("#composer", Composer).text == "draft"


@pytest.mark.asyncio
async def test_editor_seed_returns_visible_text() -> None:
    app = _ComposerApp()
    async with app.run_test() as pilot:
        composer = app.query_one("#composer", Composer)
        await pilot.press(*"hello world")
        assert composer.editor_seed() == "hello world"


@pytest.mark.asyncio
async def test_apply_editor_result_replaces_text_and_puts_cursor_at_end() -> None:
    app = _ComposerApp()
    async with app.run_test() as pilot:
        composer = app.query_one("#composer", Composer)
        await pilot.press(*"old")
        composer.apply_editor_result("brand new draft")
        await pilot.pause()
        assert composer.text == "brand new draft"
        assert composer._input.cursor_location == (0, len("brand new draft"))

        # multi-line result: cursor lands on the last line's end
        composer.apply_editor_result("line one\nline two")
        await pilot.pause()
        assert composer.text == "line one\nline two"
        assert composer._input.cursor_location == (1, len("line two"))


# -- app handler maps the compose outcome -------------------------------------


@pytest.mark.asyncio
async def test_app_ctrl_e_applies_ok_outcome_to_composer(monkeypatch) -> None:
    from amplifier_app_tui.kernel import external_editor
    from amplifier_app_tui.ui.app import TuiApp
    from amplifier_app_tui.ui.demo_wiring import DemoRuntimeAdapter

    captured: dict[str, object] = {}

    def fake_compose(draft, *, runner, environ=None, cwd=None):
        # never touch the terminal/subprocess in a unit test
        captured["draft"] = draft
        return external_editor.EditorOutcome("ok", text="composed in $EDITOR")

    monkeypatch.setattr(external_editor, "compose_in_editor", fake_compose)

    app = TuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause(0.2)
        app.composer.focus_input()
        app.composer.insert_text("seed draft")
        await pilot.pause()
        await pilot.press("ctrl+e")
        await pilot.pause()
        assert captured["draft"] == "seed draft"
        assert app.composer.text == "composed in $EDITOR"


@pytest.mark.asyncio
async def test_app_ctrl_e_no_editor_keeps_draft_and_notices(monkeypatch) -> None:
    from amplifier_app_tui.kernel import external_editor
    from amplifier_app_tui.ui.app import TuiApp
    from amplifier_app_tui.ui.demo_wiring import DemoRuntimeAdapter

    def fake_compose(draft, *, runner, environ=None, cwd=None):
        return external_editor.EditorOutcome(
            "no_editor", detail="set $VISUAL or $EDITOR to compose externally"
        )

    monkeypatch.setattr(external_editor, "compose_in_editor", fake_compose)

    app = TuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.pause(0.2)
        app.composer.focus_input()
        app.composer.insert_text("keep me")
        await pilot.pause()
        await pilot.press("ctrl+e")
        await pilot.pause()
        assert app.composer.text == "keep me"  # draft untouched
        assert app.notice_slot.current == "set $VISUAL or $EDITOR to compose externally"
