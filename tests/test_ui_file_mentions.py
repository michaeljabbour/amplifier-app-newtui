from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from amplifier_app_newtui.ui.file_mentions import FileMentionStrip
from amplifier_app_newtui.ui.themes import DEFAULT_THEME, register_themes, theme_id


class MentionApp(App[None]):
    def __init__(self) -> None:
        super().__init__()
        register_themes(self)
        self.theme = theme_id(DEFAULT_THEME)

    def compose(self) -> ComposeResult:
        yield FileMentionStrip(id="mentions")


@pytest.mark.asyncio
async def test_strip_filters_and_clamps_selection() -> None:
    app = MentionApp()
    async with app.run_test() as pilot:
        strip = app.query_one(FileMentionStrip)
        strip.set_files(("README.md", "docs/README-dev.md", "src/app.py"))
        strip.apply_filter("read")
        await pilot.pause()
        assert strip.is_open
        assert strip.matches == ("README.md", "docs/README-dev.md")
        assert strip.selected_path == "README.md"

        strip.move_selection(20)
        assert strip.selected_path == "docs/README-dev.md"
        strip.apply_filter(None)
        await pilot.pause()
        assert not strip.is_open


@pytest.mark.asyncio
async def test_real_app_routes_composer_keys_through_the_strip() -> None:
    from amplifier_app_newtui.ui.app import NewTuiApp
    from amplifier_app_newtui.ui.demo_wiring import DemoRuntimeAdapter

    from .test_flow_helpers import seed_done, type_text

    class MentionDemo(DemoRuntimeAdapter):
        async def workspace_files(self) -> tuple[str, ...]:
            return ("README.md", "docs/USER-GUIDE.md")

    app = NewTuiApp(MentionDemo(instant=True))
    async with app.run_test(size=(120, 40)) as pilot:
        await seed_done(pilot, app)
        await type_text(pilot, "review @read")
        await pilot.pause()
        assert app.file_mentions.is_open
        assert app.file_mentions.selected_path == "README.md"
        await pilot.press("enter")
        await pilot.pause()
        assert app.composer.text == "review @README.md "
        assert not app.file_mentions.is_open
