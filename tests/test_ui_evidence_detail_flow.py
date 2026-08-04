"""Evidence detail side panel — full app wiring (compliance item D7).

Drives the REAL TuiApp (DemoRuntimeAdapter) end to end, covering the four
acceptance criteria this item adds on top of the already-working AC1
enter-expand/esc-close controls (verified unchanged in
tests/test_kernel_evidence.py / tests/test_ui_transcript_view.py):

- AC2: the detail view identifies the producing tool call, inputs,
  timestamp, source/output, and originating agent.
- AC3: closing detail restores focus and scroll position to the row.
- AC4: the panel toggles via a documented chord and collapses at narrow
  widths (never a silently-dead control).
- AC5: unavailable/expired evidence renders an explicit fallback.
"""

from __future__ import annotations

import pytest

from amplifier_app_tui.model.blocks import EvidenceBlock
from amplifier_app_tui.model.evidence import EvidenceLink, ToolCallRecord
from amplifier_app_tui.ui import app_support
from amplifier_app_tui.ui.app import TuiApp
from amplifier_app_tui.ui.demo_wiring import DemoRuntimeAdapter
from amplifier_app_tui.ui.transcript import BlockWidget


class _FakeRecord(DemoRuntimeAdapter):
    """DemoRuntimeAdapter plus a fixed provenance store, so D7's panel can
    be exercised without a full real-runtime EvidenceCollector."""

    def __init__(self, records: dict[str, ToolCallRecord] | None = None) -> None:
        super().__init__(instant=True)
        self._records = records or {}

    def evidence_tool_call(self, tool_call_id: str) -> ToolCallRecord | None:
        return self._records.get(tool_call_id)


def _record(tool_call_id: str = "c1") -> ToolCallRecord:
    return ToolCallRecord(
        tool_call_id=tool_call_id,
        tool_name="bash",
        tool_input={"command": "uv run pytest -q"},
        output="41 passed in 3.2s",
        ts=1_700_000_000.0,
        agent="main agent",
    )


@pytest.mark.asyncio
async def test_open_ready_detail_shows_ac2_facts() -> None:
    app = TuiApp(_FakeRecord({"c1": _record("c1")}))
    async with app.run_test(size=(120, 30)) as pilot:
        link = EvidenceLink(
            claim_quote="tests pass", tool_ref="$ uv run pytest -q", tool_call_id="c1"
        )
        widget = app.transcript.append(EvidenceBlock(id="ev1", links=(link,)))
        assert isinstance(widget, BlockWidget)
        widget.action_evidence_detail()
        await pilot.pause()

        assert app.evidence_panel.is_open
        detail = app.evidence_panel.detail
        assert detail is not None
        assert detail.status == "ready"
        assert detail.tool_name == "bash"
        assert detail.input_summary == "uv run pytest -q"
        assert detail.agent == "main agent"
        assert detail.output == "41 passed in 3.2s"
        assert detail.timestamp == 1_700_000_000.0


@pytest.mark.asyncio
async def test_second_d_on_same_claim_toggles_closed_and_restores_focus_and_scroll() -> None:
    """AC3."""
    app = TuiApp(_FakeRecord({"c1": _record("c1")}))
    async with app.run_test(size=(120, 30)) as pilot:
        link = EvidenceLink(
            claim_quote="tests pass", tool_ref="$ uv run pytest -q", tool_call_id="c1"
        )
        widget = app.transcript.append(EvidenceBlock(id="ev1", links=(link,)))
        assert isinstance(widget, BlockWidget)
        widget.focus()
        await pilot.pause()
        pre_open_scroll = app.transcript.scroll_y

        widget.action_evidence_detail()
        await pilot.pause()
        assert app.evidence_panel.is_open

        # Something moves scroll/focus away while the panel is open (a
        # docked panel's width claim reflows the transcript; the panel
        # itself never takes focus — see test_ui_evidence_panel.py).
        app.transcript.scroll_to(y=pre_open_scroll + 5, animate=False, immediate=True)
        app.set_focus(None)
        await pilot.pause()

        widget.action_evidence_detail()  # same (block_id, link) -> toggle-close
        await pilot.pause()

        assert not app.evidence_panel.is_open
        assert app.transcript.scroll_y == pre_open_scroll
        restored = app.transcript.get_widget("ev1")
        assert restored is not None
        assert restored.has_focus


@pytest.mark.asyncio
async def test_different_claim_while_open_refreshes_instead_of_toggling_closed() -> None:
    app = TuiApp(_FakeRecord({"c1": _record("c1"), "c2": _record("c2")}))
    async with app.run_test(size=(120, 30)) as pilot:
        link_a = EvidenceLink(claim_quote="a", tool_ref="ref a", tool_call_id="c1")
        link_b = EvidenceLink(claim_quote="b", tool_ref="ref b", tool_call_id="c2")
        widget = app.transcript.append(EvidenceBlock(id="ev1", links=(link_a, link_b)))
        assert isinstance(widget, BlockWidget)
        widget.action_evidence_detail()
        await pilot.pause()
        assert app.evidence_panel.detail is not None
        assert app.evidence_panel.detail.claim_quote == "a"

        widget.action_evidence_next()  # arrow-key claim nav (existing AC1 control)
        widget.action_evidence_detail()
        await pilot.pause()
        assert app.evidence_panel.is_open
        assert app.evidence_panel.detail is not None
        assert app.evidence_panel.detail.claim_quote == "b"


@pytest.mark.asyncio
async def test_narrow_terminal_shows_notice_instead_of_a_dead_control() -> None:
    """AC4: never a silently-dead control."""
    app = TuiApp(_FakeRecord({"c1": _record("c1")}))
    async with app.run_test(size=(60, 24)) as pilot:
        link = EvidenceLink(claim_quote="c", tool_ref="r", tool_call_id="c1")
        widget = app.transcript.append(EvidenceBlock(id="ev1", links=(link,)))
        assert isinstance(widget, BlockWidget)
        widget.action_evidence_detail()
        await pilot.pause()

        assert not app.evidence_panel.is_open
        assert "wider terminal" in app.notice_slot.current


@pytest.mark.asyncio
async def test_live_resize_collapses_and_restores_an_open_panel() -> None:
    """AC4 responsive collapse, mirroring the plan panel's ladder (D2)."""
    app = TuiApp(_FakeRecord({"c1": _record("c1")}))
    async with app.run_test(size=(120, 30)) as pilot:
        link = EvidenceLink(claim_quote="c", tool_ref="r", tool_call_id="c1")
        widget = app.transcript.append(EvidenceBlock(id="ev1", links=(link,)))
        assert isinstance(widget, BlockWidget)
        widget.action_evidence_detail()
        await pilot.pause()
        assert app.evidence_panel.display is True

        await pilot.resize_terminal(60, 24)
        await pilot.pause(0.1)
        assert app.evidence_panel.display is False
        assert app.evidence_panel.is_open is True  # detail preserved, not discarded

        await pilot.resize_terminal(120, 30)
        await pilot.pause(0.1)
        assert app.evidence_panel.display is True


@pytest.mark.asyncio
async def test_unavailable_and_expired_fallbacks_through_the_real_handler() -> None:
    """AC5, with no provenance wired for either claim."""
    app = TuiApp(_FakeRecord())
    async with app.run_test(size=(120, 30)) as pilot:
        no_id_link = EvidenceLink(claim_quote="a", tool_ref="ref")
        widget = app.transcript.append(EvidenceBlock(id="ev1", links=(no_id_link,)))
        assert isinstance(widget, BlockWidget)
        widget.action_evidence_detail()
        await pilot.pause()
        assert app.evidence_panel.detail is not None
        assert app.evidence_panel.detail.status == "unavailable"

        widget.action_evidence_detail()  # toggle-close before the next block
        await pilot.pause()

        expired_link = EvidenceLink(claim_quote="b", tool_ref="ref", tool_call_id="gone")
        widget2 = app.transcript.append(EvidenceBlock(id="ev2", links=(expired_link,)))
        assert isinstance(widget2, BlockWidget)
        widget2.action_evidence_detail()
        await pilot.pause()
        assert app.evidence_panel.detail is not None
        assert app.evidence_panel.detail.status == "expired"


@pytest.mark.asyncio
async def test_esc_closing_the_whole_block_also_closes_the_panel() -> None:
    app = TuiApp(_FakeRecord({"c1": _record("c1")}))
    async with app.run_test(size=(120, 30)) as pilot:
        link = EvidenceLink(claim_quote="c", tool_ref="r", tool_call_id="c1")
        widget = app.transcript.append(EvidenceBlock(id="ev1", links=(link,)))
        assert isinstance(widget, BlockWidget)
        widget.action_evidence_detail()
        await pilot.pause()
        assert app.evidence_panel.is_open

        widget.action_close_evidence()  # esc -- removes the whole block (AC1, unchanged)
        await pilot.pause()

        assert not app.evidence_panel.is_open
        assert app.transcript.get_block("ev1") is None


@pytest.mark.asyncio
async def test_evidence_panel_min_width_matches_narrowest_golden_width() -> None:
    """Sanity pin: the collapse threshold is a deliberate choice, not a
    magic number that could silently drift from the golden width matrix."""
    assert app_support.EVIDENCE_PANEL_MIN_WIDTH == 80
