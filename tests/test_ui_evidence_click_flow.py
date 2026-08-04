"""Evidence-row click == Enter parity, driven through the REAL app.

Closes compliance item D7's outstanding gap, AC1: "every evidence row that
advertises an action is focusable and opens with Enter and click."

Before this fix: ``action_evidence_expand()`` (bound to ``enter`` in the
``evidence`` keymap context) posted :class:`ExpandEvidenceClaim` correctly,
but ``BlockWidget._activate()`` -- the method ``on_click`` calls for every
other block kind (tool lines, delegate summaries, thinking, the answer's
reveal, turn rules) -- had no branch for ``block.kind == "evidence"`` at
all. A click on a live evidence row was therefore a silent dead control.
``HistoryArchive.action_archive_activate()``'s own evidence branch was
*not* the coarser ``ShowEvidence`` reveal (that stays on the answer block,
a different target entirely, and is untouched here) -- it only selected +
focused the archived row, arming it for a second, separate ``enter`` press,
rather than performing the expand itself.

This module drives genuine ``pilot.click(...)`` events (never the action
method directly, which would prove nothing about the click path) through
:class:`TuiApp` and asserts click produces the identical resulting
transcript state Enter already produced -- proving the fix and locking it
against regression. See ``tests/test_ui_transcript_view.py`` for the
lighter-weight, message-only click-parity tests against the bare widgets.
"""

from __future__ import annotations

import pytest

from amplifier_app_tui.model.blocks import EvidenceBlock, ToolLine
from amplifier_app_tui.model.evidence import EvidenceLink, ToolCallRecord
from amplifier_app_tui.ui.app import TuiApp
from amplifier_app_tui.ui.composer import ComposerInput
from amplifier_app_tui.ui.demo_wiring import DemoRuntimeAdapter
from amplifier_app_tui.ui.transcript import BlockWidget

SIZE = (100, 30)


class _FakeRecord(DemoRuntimeAdapter):
    """DemoRuntimeAdapter plus a fixed provenance store (mirrors the
    fixture in ``test_ui_evidence_detail_flow.py``)."""

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
async def test_click_on_evidence_row_expands_correlated_tool_line_like_enter() -> None:
    """A click opens the row's detail exactly like Enter: the correlated
    tool line's body expands in place and scrolls into view -- never a
    dead control."""
    app = TuiApp(_FakeRecord())
    async with app.run_test(size=SIZE) as pilot:
        tool = app.transcript.append(
            ToolLine(
                id="tool-1",
                summary="Ran pytest",
                body=("41 passed in 3.2s",),
                status="completed",
                tool_call_ids=("c1",),
            )
        )
        assert tool is not None
        link = EvidenceLink(claim_quote="tests pass", tool_ref="pytest run", tool_call_id="c1")
        widget = app.transcript.append(EvidenceBlock(id="ev1", links=(link,)))
        assert isinstance(widget, BlockWidget)
        await pilot.pause()

        await pilot.click(widget)
        await pilot.pause()

        expanded = app.transcript.get_block("tool-1")
        assert isinstance(expanded, ToolLine)
        assert expanded.expanded is True


@pytest.mark.asyncio
async def test_click_on_evidence_row_with_no_tool_call_id_shows_explicit_fallback() -> None:
    """A claim that was never linked to a tool call (no correlation id at
    all) must not be a dead control on click -- the same explicit
    "grounded by ..." fallback Enter already shows must fire."""
    app = TuiApp(_FakeRecord())
    async with app.run_test(size=SIZE) as pilot:
        link = EvidenceLink(claim_quote="a claim", tool_ref="some tool · ref")
        widget = app.transcript.append(EvidenceBlock(id="ev1", links=(link,)))
        assert isinstance(widget, BlockWidget)
        await pilot.pause()

        await pilot.click(widget)
        await pilot.pause()

        assert "grounded by some tool · ref" in app.notice_slot.current


@pytest.mark.asyncio
async def test_click_on_evidence_row_whose_tool_call_no_longer_resolves_shows_same_fallback() -> (
    None
):
    """A correlation id that no longer resolves to any tool line currently
    in the transcript must not be a dead control on click either -- same
    explicit fallback, not silence."""
    app = TuiApp(_FakeRecord())
    async with app.run_test(size=SIZE) as pilot:
        link = EvidenceLink(claim_quote="a claim", tool_ref="gone tool", tool_call_id="gone")
        widget = app.transcript.append(EvidenceBlock(id="ev1", links=(link,)))
        assert isinstance(widget, BlockWidget)
        await pilot.pause()

        await pilot.click(widget)
        await pilot.pause()

        assert "grounded by gone tool" in app.notice_slot.current


@pytest.mark.asyncio
async def test_click_does_not_strand_focus_and_esc_still_closes_the_clicked_row() -> None:
    """A transcript click must never strand keyboard focus (the house rule
    already applied to ``on_copy_code_fence``/``on_show_evidence``): after
    a click performs the row's action (expanding the correlated tool
    line -- proving this exercises the actual fix, not just Textual's
    built-in click-to-focus), the row itself still owns the keyboard, so
    esc still closes exactly the row that was clicked and hands focus
    back to the composer -- the click never leaves the transcript in a
    state Enter couldn't also reach."""
    app = TuiApp(_FakeRecord({"c1": _record("c1")}))
    async with app.run_test(size=SIZE) as pilot:
        tool = app.transcript.append(
            ToolLine(
                id="tool-1",
                summary="Ran pytest",
                body=("41 passed in 3.2s",),
                status="completed",
                tool_call_ids=("c1",),
            )
        )
        assert tool is not None
        link = EvidenceLink(claim_quote="tests pass", tool_ref="pytest run", tool_call_id="c1")
        widget = app.transcript.append(EvidenceBlock(id="ev1", links=(link,)))
        assert isinstance(widget, BlockWidget)
        await pilot.pause()

        await pilot.click(widget)
        await pilot.pause()

        # The click's actual effect fired (not just a focus side effect).
        expanded = app.transcript.get_block("tool-1")
        assert isinstance(expanded, ToolLine)
        assert expanded.expanded is True
        assert widget.has_focus  # click focused the row, same as Enter's own precondition

        await pilot.press("escape")
        await pilot.pause()

        assert app.transcript.get_block("ev1") is None  # closed exactly the row that was clicked
        assert app.composer.query_one(ComposerInput).has_focus  # keyboard restored, never stranded


@pytest.mark.asyncio
async def test_keyboard_and_click_on_equivalent_rows_produce_the_same_resulting_state() -> None:
    """Keyboard/mouse parity (so the two input paths cannot silently
    diverge again): exercise the SAME shape of row via Enter and via
    click, through the real app, and assert the resulting transcript
    state is equivalent."""
    app = TuiApp(_FakeRecord({"c1": _record("c1"), "c2": _record("c2")}))
    async with app.run_test(size=SIZE) as pilot:
        app.transcript.append(
            ToolLine(
                id="tool-enter",
                summary="via enter",
                body=("x",),
                status="completed",
                tool_call_ids=("c1",),
            )
        )
        app.transcript.append(
            ToolLine(
                id="tool-click",
                summary="via click",
                body=("x",),
                status="completed",
                tool_call_ids=("c2",),
            )
        )
        link_enter = EvidenceLink(claim_quote="a", tool_ref="ref a", tool_call_id="c1")
        link_click = EvidenceLink(claim_quote="b", tool_ref="ref b", tool_call_id="c2")
        enter_widget = app.transcript.append(EvidenceBlock(id="ev-enter", links=(link_enter,)))
        click_widget = app.transcript.append(EvidenceBlock(id="ev-click", links=(link_click,)))
        assert isinstance(enter_widget, BlockWidget)
        assert isinstance(click_widget, BlockWidget)
        await pilot.pause()

        enter_widget.focus()
        await pilot.press("enter")
        await pilot.pause()

        await pilot.click(click_widget)
        await pilot.pause()

        via_enter = app.transcript.get_block("tool-enter")
        via_click = app.transcript.get_block("tool-click")
        assert isinstance(via_enter, ToolLine)
        assert isinstance(via_click, ToolLine)
        assert via_enter.expanded is True
        assert via_click.expanded is True
        assert via_enter.expanded == via_click.expanded  # equivalent resulting state
