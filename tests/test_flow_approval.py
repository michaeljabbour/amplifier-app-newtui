"""Flow tests — DESIGN-SPEC §7: approvals & the needs-you queue.

End-to-end over DemoRuntime + Pilot: the inline approval bar (arrows /
enter / esc semantics, ``› `` selection, composer swap), deny →
``⊘ blocked · … · denied by user · continuing without …`` with the turn
continuing to its (denied) close-out, and the auto-mode deferred
decision → footer badge → ctrl-y Needs-you block → chip action logging
``Applying decision: …`` and clearing the badge.
"""

from __future__ import annotations

import pytest

from amplifier_app_newtui.kernel.demo import (
    APPROVAL_OPTIONS,
    AUTO_BLOCK_CONTINUATION,
    AUTO_BLOCK_REASON,
    DEMO_DEFERRED_DECISION,
    DENY_BLOCKED_CMD,
    FORCE_PUSH_COMMAND,
    PYTEST_APPROVAL_PROMPT,
    build_denied_spec,
)
from amplifier_app_newtui.ui.app import NewTuiApp
from amplifier_app_newtui.ui.app_support import APPROVAL_NOTICE
from amplifier_app_newtui.ui.demo_wiring import DemoRuntimeAdapter
from amplifier_app_newtui.ui.footer import (
    footer_left_text,
    footer_right_text,
    footer_waiting_text,
)
from amplifier_app_newtui.ui.transcript import render_block

from .test_flow_helpers import (
    SIZE,
    blocks_of,
    rules,
    seed_done,
    set_mode,
    type_text,
    wait_for,
)


async def _reach_pytest_approval(pilot, app: NewTuiApp) -> None:
    """Seed, switch to chat (the app boots in auto — §4 amendment), then
    run the build turn up to its chat-mode pytest approval."""
    await seed_done(pilot, app)
    await set_mode(pilot, app, "chat")
    await type_text(pilot, "hi")
    await pilot.press("enter")
    assert await wait_for(pilot, lambda: app.approval_bar is not None)


@pytest.mark.asyncio
async def test_approval_bar_replaces_composer_arrows_and_confirm() -> None:
    app = NewTuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await _reach_pytest_approval(pilot, app)
        bar = app.approval_bar
        assert bar is not None
        assert bar.prompt == PYTEST_APPROVAL_PROMPT
        assert bar.options == APPROVAL_OPTIONS  # verbatim Allow once/always/Deny
        # The bar replaces the composer while open.
        assert app.composer.display is False
        assert app.notice_slot.current == APPROVAL_NOTICE
        assert app.footer_bar.state.context == "approval"
        assert (
            footer_right_text(app.footer_bar.state)
            == "arrows select · enter confirm · esc deny"
        )

        # Selected option prefixed "› "; arrows/tab cycle the selection.
        assert bar.option_texts() == ("› Allow once", "Allow always", "Deny")
        await pilot.press("right")
        assert bar.option_texts() == ("Allow once", "› Allow always", "Deny")
        await pilot.press("tab")
        assert bar.option_texts() == ("Allow once", "Allow always", "› Deny")
        # Shift+tab also cycles the selection (mockup: e.key === "Tab"
        # matches with or without shift) — it must NOT cycle the mode.
        mode_before = app.mode_id
        await pilot.press("shift+tab")
        assert bar.option_texts() == ("› Allow once", "Allow always", "Deny")
        assert app.mode_id == mode_before
        await pilot.press("right")
        assert bar.option_texts() == ("Allow once", "› Allow always", "Deny")
        await pilot.press("left")

        # Enter confirms → the composer comes back and the turn ships.
        await pilot.press("enter")
        assert await wait_for(pilot, lambda: rules(app) >= 2 and not app.turn_active)
        assert app.approval_bar is None
        assert app.composer.display is True
        rule = blocks_of(app, "turn_rule")[-1]
        assert rule.label.endswith("3 files · +142/−38 · tests ✔")
        # Footer ▲ yield glyph after a shipped turn (spec §10).
        state = app.footer_bar.state
        assert state.shipped
        assert " ▲" in footer_left_text(state)


@pytest.mark.asyncio
async def test_ctrl_y_parks_live_ticket_into_needs_you_answerable_later() -> None:
    """Issue #41: ctrl-y on the live approval bar parks the ticket into
    the needs-you queue WITHOUT resolving it (deny-and-continue), hands
    the composer back, and the parked decision is answerable later."""
    adapter = DemoRuntimeAdapter(instant=True)
    app = NewTuiApp(adapter)
    async with app.run_test(size=SIZE) as pilot:
        await _reach_pytest_approval(pilot, app)
        assert adapter.needs_you.pending_count == 0

        # ctrl-y parks the head ticket rather than answering it. The bar
        # owns the keyboard, so the global show_needs_you chord is
        # suppressed and the key reaches the bar's park handler.
        await pilot.press("ctrl+y")
        assert await wait_for(pilot, lambda: app.approval_bar is None)

        # Parked, not resolved: the composer is back, one decision is
        # waiting, and the underlying approval future is still pending
        # (no choice was sent to the runtime).
        assert app.composer.display is True
        assert adapter.needs_you.pending_count == 1
        assert app.footer_bar.state.waiting == 1
        assert footer_waiting_text(app.footer_bar.state) == "1 decision waiting · ctrl-y"
        item = adapter.needs_you.pending[0]
        assert item.question == PYTEST_APPROVAL_PROMPT
        # The live options travel through as the answerable chips.
        assert item.choices == APPROVAL_OPTIONS
        # The turn never shipped a close-out: the deny path did not run
        # and no second turn rule was cut (the ticket future is still
        # open, deny-and-continue timing out later).
        assert not any(b.cmd == DENY_BLOCKED_CMD for b in blocks_of(app, "blocked"))

        # Answerable later: ctrl-y now opens the needs-you listing (the
        # bar is gone, so the global chord is live again); acting on the
        # decision answers it and clears the badge.
        await pilot.press("ctrl+y")
        await pilot.pause()
        needs_you = blocks_of(app, "needs_you")[-1]
        entry = needs_you.items[0]
        assert entry.decision_id == item.decision_id
        await pilot.click(f"#needs-you-row-{entry.decision_id}")
        await pilot.pause()
        assert adapter.needs_you.pending_count == 0
        assert app.footer_bar.state.waiting == 0
        applied = adapter.needs_you.items[0]
        assert applied.status == "answered"


@pytest.mark.asyncio
async def test_approval_keeps_keyboard_when_lanes_toggle_while_open() -> None:
    app = NewTuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await _reach_pytest_approval(pilot, app)

        # ctrl+t may open the lanes panel (mockup fires it during an
        # approval) but the approval bar keeps the keyboard (spec §7)…
        await pilot.press("ctrl+t")
        await pilot.pause()
        assert app.lanes_panel.display
        assert app.approval_bar is not None

        # …so Esc still resolves Deny (mockup: the approval branch runs
        # before the esc chain), not close-lanes.
        await pilot.press("escape")
        assert await wait_for(pilot, lambda: rules(app) >= 2 and not app.turn_active)
        assert app.approval_bar is None
        blocked = blocks_of(app, "blocked")[-1]
        assert blocked.cmd == DENY_BLOCKED_CMD


@pytest.mark.asyncio
async def test_esc_denies_blocked_line_and_turn_continues() -> None:
    app = NewTuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await _reach_pytest_approval(pilot, app)

        # Esc = Deny.
        await pilot.press("escape")
        assert await wait_for(pilot, lambda: rules(app) >= 2 and not app.turn_active)

        # ⊘ blocked · <thing> · denied by user · continuing without <thing>.
        blocked = blocks_of(app, "blocked")[-1]
        assert blocked.cmd == DENY_BLOCKED_CMD
        assert blocked.reason == "denied by user"
        assert blocked.continuation == "continuing without test run"
        line = "".join(s.text for s in render_block(blocked, 200)[0])
        assert line == (
            "  ⊘ blocked · uv run pytest · denied by user · continuing without test run"
        )

        # The deny never halted the turn: the answer landed and the rule
        # closed out on the mockup's denied telemetry (no "tests ✔").
        assert any(
            "tests skipped by your denial" in "".join(s.text for s in b.spans)
            for b in blocks_of(app, "answer")
        )
        rule = blocks_of(app, "turn_rule")[-1]
        assert rule.label == build_denied_spec().rule_label
        assert "tests ✔" not in rule.label


@pytest.mark.asyncio
async def test_auto_mode_deferred_decision_ctrl_y_needs_you_flow() -> None:
    adapter = DemoRuntimeAdapter(instant=True)
    app = NewTuiApp(adapter)
    async with app.run_test(size=SIZE) as pilot:
        # Build turn first (approve), then the auto turn.
        await _reach_pytest_approval(pilot, app)
        await pilot.press("enter")
        assert await wait_for(pilot, lambda: rules(app) >= 2 and not app.turn_active)
        await type_text(pilot, "hi")
        await pilot.press("enter")
        assert await wait_for(pilot, lambda: rules(app) >= 3 and not app.turn_active)
        assert app.mode_id == "auto"

        # Trust-boundary block rendered ⊘ but the run continued to a
        # shipped-locally outcome.
        blocked = blocks_of(app, "blocked")[-1]
        assert blocked.cmd == FORCE_PUSH_COMMAND
        assert blocked.reason == AUTO_BLOCK_REASON
        assert blocked.continuation == AUTO_BLOCK_CONTINUATION
        assert blocks_of(app, "turn_rule")[-1].shipped

        # Deferred decision → footer badge "1 decision waiting · ctrl-y".
        assert adapter.needs_you.pending_count == 1
        state = app.footer_bar.state
        assert state.waiting == 1
        assert footer_waiting_text(state) == "1 decision waiting · ctrl-y"

        # ctrl-y prints the orange Needs-you block with the chip.
        await pilot.press("ctrl+y")
        await pilot.pause()
        needs_you = blocks_of(app, "needs_you")[-1]
        assert len(needs_you.items) == 1
        entry = needs_you.items[0]
        assert entry.question == DEMO_DEFERRED_DECISION.text
        assert entry.choices[0].label == DEMO_DEFERRED_DECISION.chip_label
        header = "".join(s.text for s in render_block(needs_you, 200)[0])
        assert header == "· Needs you  1 deferred decision"

        # Acting on the decision logs "Applying decision: …" and clears
        # the badge; scrollback is append-only (mockup §7), so the
        # Needs-you listing stays in the transcript. The click handler is
        # per decision row (mockup html:286-292), never the header.
        await pilot.click(f"#needs-you-row-{entry.decision_id}")
        await pilot.pause()
        assert adapter.needs_you.pending_count == 0
        assert app.footer_bar.state.waiting == 0
        assert blocks_of(app, "needs_you") == [needs_you]
        # Spec §12: transcript clicks never strand the keyboard — the
        # composer keeps keyboard focus through the row/chip click.
        await pilot.press("z")
        assert app.composer.text == "z"
        # The applied decision is a narration line: bright "● " marker +
        # the verbatim mockup text (design-v3-cohesive.html:289).
        applied = [
            b for b in blocks_of(app, "narration")
            if b.text == DEMO_DEFERRED_DECISION.applied_narration
        ]
        assert len(applied) == 1
        line = "".join(s.text for s in render_block(applied[0], 200)[0])
        assert line == f"● {DEMO_DEFERRED_DECISION.applied_narration}"


@pytest.mark.asyncio
async def test_deferred_decision_rings_the_attention_bell(monkeypatch) -> None:
    """The TUI-native hooks-notify replacement: a decision deferred to the
    needs-you queue rings Textual's driver-safe bell exactly once; quick
    turn close-outs (< ATTENTION_MIN_TURN_SECONDS) stay silent."""
    monkeypatch.delenv("AMPLIFIER_NOTIFY", raising=False)
    adapter = DemoRuntimeAdapter(instant=True)
    app = NewTuiApp(adapter)
    rings: list[str] = []
    monkeypatch.setattr(app, "bell", lambda: rings.append("bell"))
    async with app.run_test(size=SIZE) as pilot:
        await _reach_pytest_approval(pilot, app)
        await pilot.press("enter")
        assert await wait_for(pilot, lambda: rules(app) >= 2 and not app.turn_active)
        # Instant demo turns finish in well under the threshold — no bell.
        assert rings == []
        await type_text(pilot, "hi")
        await pilot.press("enter")
        assert await wait_for(pilot, lambda: rules(app) >= 3 and not app.turn_active)
        assert adapter.needs_you.pending_count == 1
        assert rings == ["bell"]


@pytest.mark.asyncio
@pytest.mark.parametrize("size", [(120, 50), (160, 50)])
async def test_needs_you_chip_stays_visible_and_clickable_after_late_wrap(size) -> None:
    """Regression (s7/s12): the tail anchor must hold through LATE height growth.

    ``ctrl-y`` appends the needs-you block, but :class:`NeedsYouList`
    mounts its rows asynchronously and ``_DecisionRow._update_wrap``
    grows the row 1→2 lines on its first resize — both AFTER any
    per-append scroll ran. A one-shot ``scroll_end`` per append left the
    chip row clipped below the viewport at 120x50 (the click then hit
    the widget underneath and the decision was never applied). The
    standing tail anchor keeps the view bottom-scrolled through that
    growth, so the chip is visible and clicking IT applies the decision
    at wrapped (120) and unwrapped (160) widths alike.
    """
    adapter = DemoRuntimeAdapter(instant=True)
    app = NewTuiApp(adapter)
    async with app.run_test(size=size) as pilot:
        await _reach_pytest_approval(pilot, app)
        await pilot.press("enter")
        assert await wait_for(pilot, lambda: rules(app) >= 2 and not app.turn_active)
        await type_text(pilot, "hi")
        await pilot.press("enter")
        assert await wait_for(pilot, lambda: rules(app) >= 3 and not app.turn_active)
        assert adapter.needs_you.pending_count == 1

        await pilot.press("ctrl+y")
        await pilot.pause()
        await pilot.pause()
        needs_you = blocks_of(app, "needs_you")[-1]
        entry = needs_you.items[0]
        view = app.transcript

        # The anchor re-asserted bottom scroll after the async row mount
        # + wrap growth: the view is at its end and the chip row is fully
        # inside the transcript viewport (not occluded by the live tail).
        assert view.follow is True
        assert view.is_vertical_scroll_end
        chip = app.query_one(f"#chip-{entry.decision_id}-0")
        assert chip.region.size.area > 0
        assert view.region.contains_region(chip.region)

        # Clicking the CHIP itself (the smallest target — off-screen
        # before the fix) applies the decision, logs the narration and
        # clears the footer badge.
        await pilot.click(f"#chip-{entry.decision_id}-0")
        await pilot.pause()
        assert adapter.needs_you.pending_count == 0
        assert app.footer_bar.state.waiting == 0
        assert any(
            b.text == DEMO_DEFERRED_DECISION.applied_narration
            for b in blocks_of(app, "narration")
        )


@pytest.mark.asyncio
async def test_tail_anchor_holds_through_wrapped_answer_growth() -> None:
    """The standing anchor also covers generic late wrap growth: a long
    answer line that wraps to many rows at a narrow width must not leave
    the tail stranded above the bottom."""
    from amplifier_app_newtui.model.blocks import Answer, Narration, Segment

    adapter = DemoRuntimeAdapter(instant=True)
    app = NewTuiApp(adapter)
    async with app.run_test(size=(60, 20)) as pilot:
        await seed_done(pilot, app)
        view = app.transcript
        for index in range(20):
            view.append(Narration(id=f"pad-{index}", text=f"pad line {index}"))
        await pilot.pause()
        long_line = "wrap me " * 60  # ~480 cells → 8+ rows at width 60
        view.append(
            Answer(id="long-answer", spans=(Segment(text=long_line, style_token="fg"),))
        )
        await pilot.pause()
        await pilot.pause()
        assert view.follow is True
        assert view.is_vertical_scroll_end


@pytest.mark.asyncio
async def test_kernel_parked_deferral_flows_rich_through_needs_you(monkeypatch) -> None:
    """Real-runtime path: the kernel parks the deferral item (native
    approval data) and emits ONE decision Notification with its id — the
    app must NOT park a duplicate, ctrl-y must render the kernel item's
    choices/reason/highlight, and acting must narrate the action and
    record the /improve override under the denied-action key."""
    from amplifier_app_newtui.kernel.approval import STANDARD_OPTIONS
    from amplifier_app_newtui.kernel.events import Notification
    from amplifier_app_newtui.ui.runtime_adapter import (
        RealRuntimeAdapter,
        RuntimeAdapter,
    )

    push = "git push origin main"
    monkeypatch.delenv("AMPLIFIER_NOTIFY", raising=False)
    # Boot nothing: the base start() just reports ready — the adapter's
    # queue resolution and narration paths are what this flow exercises.
    monkeypatch.setattr(RealRuntimeAdapter, "start", RuntimeAdapter.start)
    adapter = RealRuntimeAdapter(bundle="x")
    app = NewTuiApp(adapter)
    rings: list[str] = []
    monkeypatch.setattr(app, "bell", lambda: rings.append("bell"))
    async with app.run_test(size=SIZE) as pilot:
        # Kernel-side deferral: the item is parked at the point of
        # deferral (broker/governance), THEN the decision event arrives.
        item = adapter.needs_you.defer(
            f"Allow {push}?",
            "not authorized",
            choices=STANDARD_OPTIONS,
            highlight=push,
            action=push,
        )
        adapter.queue.put_nowait(
            Notification(
                session_id="root",
                message=f"decision deferred to queue · {item.question}",
                level="decision",
                source="needs_you",
                decision_id=item.decision_id,
            )
        )
        assert await wait_for(pilot, lambda: app.footer_bar.state.waiting == 1)
        assert adapter.needs_you.pending_count == 1  # no duplicate park
        assert rings == ["bell"]

        await pilot.press("ctrl+y")
        await pilot.pause()
        needs_you = blocks_of(app, "needs_you")[-1]
        entry = needs_you.items[0]
        assert entry.question == f"Allow {push}?"
        assert entry.reason == "not authorized"
        assert tuple(choice.label for choice in entry.choices) == STANDARD_OPTIONS
        assert entry.highlight == push

        await pilot.click(f"#needs-you-row-{entry.decision_id}")
        await pilot.pause()
        assert adapter.needs_you.pending_count == 0
        assert app.footer_bar.state.waiting == 0
        narration = blocks_of(app, "narration")[-1]
        assert narration.text == f"Applying decision: Allow once · {push}"
        rows = app.journal.overrides(adapter.denial_log)
        assert [(row.action, row.overridden) for row in rows] == [(push, 1)]
