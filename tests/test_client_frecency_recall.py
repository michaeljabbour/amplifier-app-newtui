"""Client frecency-recall autosuggestion (ui/composer + ui/history_recall).

Pure ranking selection and the ghost render are unit/golden tested; the live
Textual harness pins the composer wiring: a plain typed prefix surfaces the
frecency-best prior prompt, Tab accepts it, and the chronological up-ring is
left exactly as it was (the ghost never intercepts an arrow).
"""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.message import Message

from amplifier_app_tui.kernel.frecency import suggest_completion
from amplifier_app_tui.ui.composer import Composer
from amplifier_app_tui.ui.history_recall import (
    RECALL_LABEL,
    HistoryRecallStrip,
    render_recall_line,
)
from amplifier_app_tui.ui.themes import DEFAULT_THEME, register_themes, theme_id


# --------------------------------------------------------------------------
# pure: suggest_completion (frecency selection over the ring)
# --------------------------------------------------------------------------


def test_suggest_completion_frequency_beats_recency() -> None:
    # "deploy" used twice (non-consecutive) outranks a once-used, more-recent
    # "delete" -- the whole point of frecency over the plain up-ring.
    ring = ["deploy the release", "delete the cache", "deploy the release"]
    assert suggest_completion(ring, "de") == "deploy the release"


def test_suggest_completion_recency_breaks_frequency_ties() -> None:
    ring = ["deploy alpha", "deploy beta"]  # equal frequency
    assert suggest_completion(ring, "deploy") == "deploy beta"  # most recent wins


def test_suggest_completion_is_prefix_filtered_and_case_sensitive() -> None:
    ring = ["build the docs", "deploy the app"]
    assert suggest_completion(ring, "de") == "deploy the app"
    assert suggest_completion(ring, "De") is None  # startswith is case-sensitive


def test_suggest_completion_never_echoes_the_exact_draft() -> None:
    # An exact match is not a completion -- there is nothing to ghost.
    assert suggest_completion(["status"], "status") is None


def test_suggest_completion_empty_prefix_and_no_match_are_none() -> None:
    assert suggest_completion(["anything"], "") is None
    assert suggest_completion(["anything"], "zzz") is None


# --------------------------------------------------------------------------
# golden: the ghost line render
# --------------------------------------------------------------------------


def test_render_recall_line_is_exact() -> None:
    assert render_recall_line("deploy the release") == (
        "history recall: deploy the release  \u00b7  tab accepts"
    )
    assert RECALL_LABEL == "history recall:"


# --------------------------------------------------------------------------
# live harness: composer + strip wiring
# --------------------------------------------------------------------------


class _RecallApp(App[None]):
    def __init__(self) -> None:
        super().__init__()
        register_themes(self)
        self.theme = theme_id(DEFAULT_THEME)
        self.suggested: list[str | None] = []

    def compose(self) -> ComposeResult:
        yield HistoryRecallStrip(id="history-recall")
        yield Composer(id="composer")

    def on_mount(self) -> None:
        self.query_one("#composer", Composer).focus_input()

    def on_composer_history_suggested(self, message: Composer.HistorySuggested) -> None:
        message.stop()
        self.suggested.append(message.suggestion)
        self.query_one(HistoryRecallStrip).show(message.suggestion)

    # Absorb the other composer messages so they don't warn as unhandled.
    def on_composer_submit(self, message: Message) -> None:
        message.stop()


def _seed(app: _RecallApp) -> Composer:
    composer = app.query_one(Composer)
    composer.seed_history(["deploy the release", "delete the cache", "deploy the release"])
    return composer


@pytest.mark.asyncio
async def test_typed_prefix_surfaces_frecency_best_ghost() -> None:
    app = _RecallApp()
    async with app.run_test() as pilot:
        composer = _seed(app)
        strip = app.query_one(HistoryRecallStrip)
        assert not strip.is_open  # nothing typed yet

        await pilot.press(*"de")
        await pilot.pause()

        assert composer.suggestion == "deploy the release"
        assert composer.suggestion_active
        assert strip.is_open
        assert strip.suggestion == "deploy the release"


@pytest.mark.asyncio
async def test_tab_accepts_the_ghost_and_dismisses_it() -> None:
    app = _RecallApp()
    async with app.run_test() as pilot:
        composer = _seed(app)
        await pilot.press(*"de")
        await pilot.pause()
        assert composer.suggestion == "deploy the release"

        await pilot.press("tab")
        await pilot.pause()

        assert composer.text == "deploy the release"  # full prompt landed
        assert not composer.suggestion_active  # ghost cleared
        assert not app.query_one(HistoryRecallStrip).is_open


@pytest.mark.asyncio
async def test_up_ring_is_untouched_and_ghost_never_intercepts_it() -> None:
    app = _RecallApp()
    async with app.run_test() as pilot:
        composer = _seed(app)

        # Empty composer + Up = chronological recall (newest first), NOT the ghost.
        await pilot.press("up")
        await pilot.pause()
        assert composer.text == "deploy the release"  # ring's most recent
        assert composer.history_browsing
        assert not composer.suggestion_active  # ghost stays silent while browsing

        await pilot.press("up")
        await pilot.pause()
        assert composer.text == "delete the cache"  # older entry (pure recency)


@pytest.mark.asyncio
async def test_slash_draft_never_ghosts() -> None:
    app = _RecallApp()
    async with app.run_test() as pilot:
        _seed(app)
        composer = app.query_one(Composer)

        await pilot.press("/")  # slash command draft, not a recall prefix
        await pilot.pause()
        assert not composer.suggestion_active
        assert not app.query_one(HistoryRecallStrip).is_open


@pytest.mark.asyncio
async def test_prefix_with_no_completion_hides_the_ghost() -> None:
    app = _RecallApp()
    async with app.run_test() as pilot:
        _seed(app)
        composer = app.query_one(Composer)

        await pilot.press(*"zzz")  # nothing in the ring completes this
        await pilot.pause()
        assert not composer.suggestion_active
        assert not app.query_one(HistoryRecallStrip).is_open
