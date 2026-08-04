"""ui/evidence_panel.py — the evidence detail side panel (compliance item D7).

Covers AC2 (detail identifies tool/input/timestamp/output/agent), AC4
(panel mechanics: open/hide/close, responsive width collapse), and AC5
(unavailable/expired/oversized render as explicit, legible states, never
a blank or dead panel).
"""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from amplifier_app_tui.model.evidence import EvidenceDetail
from amplifier_app_tui.ui.evidence_panel import EvidencePanel, _detail_text
from amplifier_app_tui.ui.themes import DEFAULT_THEME, register_themes, theme_id

_TOKENS = {
    "fg": "#ffffff",
    "dim": "#888888",
    "dimmer": "#666666",
    "teal": "#00aaaa",
    "orange": "#ffaa00",
    "bright": "#ffffff",
}


def _ready_detail(**overrides: object) -> EvidenceDetail:
    base: dict[str, object] = dict(
        status="ready",
        claim_quote="41 tests pass",
        tool_ref="$ uv run pytest -q",
        tool_call_id="c1",
        tool_name="bash",
        input_summary="uv run pytest -q",
        output="41 passed in 3.2s",
        timestamp=1_700_000_000.0,
        agent="main agent",
    )
    base.update(overrides)
    return EvidenceDetail(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _detail_text: pure rendering (no Textual app needed)
# ---------------------------------------------------------------------------


def test_ready_detail_text_shows_every_ac2_fact() -> None:
    text = _detail_text(_ready_detail(), _TOKENS).plain
    assert "41 tests pass" in text
    assert "bash" in text
    assert "uv run pytest -q" in text
    assert "main agent" in text
    assert "2023-11-14" in text  # timestamp formatted
    assert "41 passed in 3.2s" in text


def test_unavailable_detail_text_shows_claim_and_fallback_only() -> None:
    detail = EvidenceDetail(
        status="unavailable",
        claim_quote="a claim",
        tool_ref="some tool",
        fallback="Evidence unavailable — this claim carries no tool-call reference.",
    )
    text = _detail_text(detail, _TOKENS).plain
    assert "a claim" in text
    assert "unavailable" in text.lower()
    assert "tool" not in text.split("unavailable")[0].lower().replace("some tool", "")


def test_expired_detail_text_shows_fallback() -> None:
    detail = EvidenceDetail(
        status="expired",
        claim_quote="a claim",
        tool_ref="some tool",
        tool_call_id="gone",
        fallback="Evidence expired — the grounding tool call is no longer in this session.",
    )
    text = _detail_text(detail, _TOKENS).plain
    assert "expired" in text.lower()


def test_oversized_detail_text_shows_content_and_truncation_note() -> None:
    detail = _ready_detail(
        status="oversized",
        output="x" * 50 + "…",
        output_truncated=True,
        fallback="Output truncated to 2,000 chars — press enter … for the full body.",
    )
    text = _detail_text(detail, _TOKENS).plain
    assert "x" * 50 in text
    assert "truncated" in text.lower()


def test_ready_without_output_says_so_explicitly() -> None:
    text = _detail_text(_ready_detail(output=""), _TOKENS).plain
    assert "no output recorded" in text.lower()


# ---------------------------------------------------------------------------
# EvidencePanel widget mechanics (AC4)
# ---------------------------------------------------------------------------


class Harness(App[None]):
    def __init__(self) -> None:
        super().__init__()
        register_themes(self)
        # Theme must be assigned before the first compose/mount cycle (the
        # same order TuiApp.__init__ uses) -- EvidencePanel's DEFAULT_CSS
        # references theme variables ($rule), and it is mounted as part of
        # this Harness's INITIAL compose() tree, unlike e.g. FocusHeader in
        # test_ui_transcript_view.py which only ever mounts later (lane
        # focus), well after an on_mount-assigned theme would already be set.
        self.theme = theme_id(DEFAULT_THEME)

    def compose(self) -> ComposeResult:
        yield EvidencePanel(id="evidence-panel")


def _panel(app: Harness) -> EvidencePanel:
    return app.query_one("#evidence-panel", EvidencePanel)


@pytest.mark.asyncio
async def test_show_detail_opens_and_is_open_true() -> None:
    app = Harness()
    async with app.run_test() as pilot:
        panel = _panel(app)
        assert panel.is_open is False
        assert panel.display is False
        panel.show_detail(_ready_detail())
        await pilot.pause()
        assert panel.is_open is True
        assert panel.display is True
        assert panel.detail is not None and panel.detail.tool_name == "bash"


@pytest.mark.asyncio
async def test_close_discards_detail_fully() -> None:
    app = Harness()
    async with app.run_test() as pilot:
        panel = _panel(app)
        panel.show_detail(_ready_detail())
        await pilot.pause()
        panel.close()
        await pilot.pause()
        assert panel.is_open is False
        assert panel.display is False
        assert panel.detail is None


@pytest.mark.asyncio
async def test_hide_panel_collapses_without_discarding_detail() -> None:
    """AC4: a width-driven collapse must be reversible — unlike close()."""
    app = Harness()
    async with app.run_test() as pilot:
        panel = _panel(app)
        panel.show_detail(_ready_detail())
        await pilot.pause()
        panel.hide_panel()
        await pilot.pause()
        assert panel.display is False
        assert panel.is_open is True  # detail survives the collapse
        assert panel.detail is not None


@pytest.mark.asyncio
async def test_sync_width_collapses_below_threshold_and_restores_above_it() -> None:
    app = Harness()
    async with app.run_test() as pilot:
        panel = _panel(app)
        panel.show_detail(_ready_detail())
        await pilot.pause()
        assert panel.display is True

        panel.sync_width(60, min_width=80)
        await pilot.pause()
        assert panel.display is False
        assert panel.is_open is True  # still "open" logically

        panel.sync_width(100, min_width=80)
        await pilot.pause()
        assert panel.display is True


@pytest.mark.asyncio
async def test_sync_width_is_a_noop_while_nothing_is_open() -> None:
    app = Harness()
    async with app.run_test() as pilot:
        panel = _panel(app)
        panel.sync_width(60, min_width=80)
        await pilot.pause()
        assert panel.display is False
        assert panel.is_open is False


@pytest.mark.asyncio
async def test_panel_never_takes_keyboard_focus() -> None:
    """The panel is supporting detail, not a second interactive surface
    (brief design note) — it must never grab keyboard focus itself."""
    app = Harness()
    async with app.run_test() as pilot:
        panel = _panel(app)
        assert panel.can_focus is False
        panel.show_detail(_ready_detail())
        await pilot.pause()
        assert app.focused is not panel
