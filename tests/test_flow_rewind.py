"""Flow tests — DESIGN-SPEC §9: pre-prompt checkpoints and restore.

Every prompt cuts a checkpoint before execution. Ctrl-r opens the newest
prompt with clamped checkpoint and restore-scope navigation. Restoring a
conversation removes the selected prompt turn and returns that full prompt
to the composer; code-only leaves conversation history untouched.
"""

from __future__ import annotations

import pytest

from amplifier_app_tui.kernel.demo import BRAINSTORM_PROMPT
from amplifier_app_tui.ui.app import TuiApp
from amplifier_app_tui.ui.demo_wiring import DemoRuntimeAdapter
from amplifier_app_tui.ui.footer import footer_right_text
from amplifier_app_tui.ui.rewind_strip import rewind_line

from .test_flow_helpers import (
    SIZE,
    GatedDemoAdapter,
    blocks_of,
    rules,
    seed_done,
    set_mode,
    type_text,
    wait_for,
)


async def _two_turns(pilot, app: TuiApp) -> None:
    """Seed (t1) + the build turn (t2, chat-mode pytest approval allowed).

    The app boots in auto (§4 amendment) — chat is set explicitly so the
    build turn stops at its approval."""
    await seed_done(pilot, app)
    await set_mode(pilot, app, "chat")
    await type_text(pilot, "hi")
    await pilot.press("enter")
    assert await wait_for(pilot, lambda: app.approval_bar is not None)
    await pilot.press("enter")  # Allow once
    assert await wait_for(pilot, lambda: rules(app) >= 2 and not app.turn_active)


@pytest.mark.asyncio
async def test_ctrl_r_opens_picker_on_newest_and_navigation_clamps() -> None:
    app = TuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await _two_turns(pilot, app)
        checkpoints = app.ledger.checkpoints
        assert [c.id for c in checkpoints] == ["t1", "t2"]
        # Checkpoints retain the full original prompt, not a result label.
        assert checkpoints[0].label == "explain what this repo is in simple terms"
        assert checkpoints[1].label == "hi"

        await pilot.press("ctrl+r")
        await pilot.pause()
        assert app.rewind.display
        # Finding 1 guard: checkpoints exist (rewind IS available), but the
        # picker itself is now open and already shows its own "rewind . pick
        # a turn . ..." header + enter/esc hints -- the idle footer must NOT
        # also advertise ctrl-r rewind underneath it (that would just
        # duplicate the strip's own affordance while it owns the screen).
        assert footer_right_text(app.footer_bar.state) == ""
        # Newest selected by default; exact strip text.
        assert app.rewind.label_text == rewind_line(checkpoints[1])
        assert app.rewind.label_text.startswith("checkpoint · pick a prompt · before turn 2 · $")
        assert app.footer_bar.state.context == "idle"  # footer_context has no rewind branch

        # ‹ / › navigate, clamped at both ends.
        await pilot.press("left")
        assert app.rewind.label_text == rewind_line(checkpoints[0])
        await pilot.press("left")
        assert app.rewind.label_text == rewind_line(checkpoints[0])  # clamped
        await pilot.press("right")
        assert app.rewind.label_text == rewind_line(checkpoints[1])
        await pilot.press("right")
        assert app.rewind.label_text == rewind_line(checkpoints[1])  # clamped

        # Esc closes the strip.
        await pilot.press("escape")
        await pilot.pause()
        assert not app.rewind.display
        assert app.footer_bar.state.context == "idle"
        # Post-merge audit Finding 1 (S1 AC1 x D4 AC2/AC3): checkpoints exist
        # and the picker is closed, so the idle hint restores exactly the
        # ctrl-r chord -- never the old always-on generic reminder row D4
        # removed (history/newline/commands stay off; see test_ui_footer.py
        # for the pure-function pin of both constraints together).
        assert footer_right_text(app.footer_bar.state) == "ctrl-r rewind"


@pytest.mark.asyncio
async def test_double_esc_interrupts_then_opens_existing_rewind_picker() -> None:
    adapter = GatedDemoAdapter()
    app = TuiApp(adapter)
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        app.submit_prompt(BRAINSTORM_PROMPT)
        assert await wait_for(pilot, lambda: app.turn_active)

        await pilot.press("escape")
        await pilot.pause()
        assert not app.rewind.display
        await pilot.press("escape")
        await pilot.pause()
        assert app.rewind.display
        assert app.rewind.current is not None
        # The running prompt already owns its pre-prompt checkpoint.
        assert app.rewind.current.id == "t2"

        adapter.release()
        assert await wait_for(pilot, lambda: not app.turn_active)


@pytest.mark.asyncio
async def test_second_esc_after_fast_close_out_still_opens_rewind() -> None:
    adapter = GatedDemoAdapter()
    app = TuiApp(adapter)
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        app.submit_prompt(BRAINSTORM_PROMPT)
        assert await wait_for(pilot, lambda: app.turn_active)

        await pilot.press("escape")
        adapter.release()
        assert await wait_for(pilot, lambda: not app.turn_active)
        await pilot.press("escape")
        await pilot.pause()
        assert app.rewind.display


@pytest.mark.asyncio
async def test_clicking_turn_rule_opens_picker_at_that_checkpoint() -> None:
    app = TuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await _two_turns(pilot, app)
        first_rule = blocks_of(app, "turn_rule")[0]
        assert first_rule.checkpoint_id == "t1"
        widget = app.query_one(f"#block-{first_rule.id}")
        widget.scroll_visible(animate=False)
        await pilot.pause()
        await pilot.click(f"#block-{first_rule.id}")
        await pilot.pause()
        assert app.rewind.display
        current = app.rewind.current
        assert current is not None and current.id == "t1"


@pytest.mark.asyncio
async def test_typing_passes_through_focused_rewind_strip_to_composer() -> None:
    """Mockup keydown (the composer input keeps focus while rewindOpen):
    printable keys typed while the strip holds focus are never swallowed —
    '/' opens the palette live-filtered and text lands in the composer (§5)."""
    app = TuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await _two_turns(pilot, app)
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert app.rewind.has_focus

        # '/led' reaches the composer and opens the palette live-filtered.
        await pilot.press("/", "l", "e", "d")
        assert await wait_for(pilot, lambda: app.palette.is_open)
        assert app.composer.text == "/led"
        assert app.composer.has_focus_within
        assert app.rewind.display  # the strip stays open

        # Esc closes the palette first (ESC_CHAIN); reset the input.
        await pilot.press("escape")
        assert await wait_for(pilot, lambda: not app.palette.is_open)
        assert app.rewind.display
        app.composer.clear()

        # Refocus the strip and type plain text: it lands in the composer.
        app.rewind.focus()
        await pilot.pause()
        await pilot.press("h", "i")
        assert await wait_for(pilot, lambda: app.composer.text == "hi")
        assert app.composer.has_focus_within

        # ←→/enter still belong to the strip when it holds focus.
        app.rewind.focus()
        await pilot.pause()
        await pilot.press("left")
        assert app.rewind.label_text == rewind_line(app.ledger.checkpoints[0])


@pytest.mark.asyncio
async def test_checkpoint_cut_while_picker_open_is_navigable() -> None:
    """A running prompt's pre-checkpoint is available before close-out."""
    adapter = GatedDemoAdapter()
    app = TuiApp(adapter)
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)  # completed t1
        app.submit_prompt(BRAINSTORM_PROMPT)  # no approvals: strip keeps focus
        assert await wait_for(pilot, lambda: app.turn_active and blocks_of(app, "narration"))

        # Open mid-turn: pending t2 is already the newest restore target.
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert app.rewind.display
        assert [c.id for c in app.ledger.checkpoints] == ["t1", "t2"]
        assert app.rewind.label_text == rewind_line(app.ledger.checkpoints[1])
        await pilot.press("right")
        assert app.rewind.label_text == rewind_line(app.ledger.checkpoints[1])
        await pilot.press("left")
        assert app.rewind.label_text == rewind_line(app.ledger.checkpoints[0])

        # Let the turn finish: the same t2 id is finalized, not duplicated.
        adapter.release()
        assert await wait_for(pilot, lambda: rules(app) >= 2 and not app.turn_active)
        checkpoints = app.ledger.checkpoints
        assert [c.id for c in checkpoints] == ["t1", "t2"]
        assert app.rewind.display
        # Cursor stays where it was and can still navigate to finalized t2.
        assert app.rewind.label_text == rewind_line(checkpoints[0])
        await pilot.press("right")
        assert app.rewind.label_text == rewind_line(checkpoints[1])


@pytest.mark.asyncio
async def test_restore_during_running_turn_interrupts_then_restores_before_prompt() -> None:
    """The pending checkpoint makes the current turn directly undoable."""
    adapter = GatedDemoAdapter()
    app = TuiApp(adapter)
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)  # t1 cut
        t1_block_ids = [b.id for b in app.transcript.blocks]

        # Park the brainstorm turn mid-turn (no approvals in its script).
        app.submit_prompt(BRAINSTORM_PROMPT)
        assert await wait_for(pilot, lambda: app.turn_active and blocks_of(app, "narration"))

        # The newest target is the running turn's own pre-prompt checkpoint.
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert app.rewind.display
        current = app.rewind.current
        assert current is not None and current.id == "t2"
        await pilot.press("enter")
        await pilot.pause()
        # The restore is parked awaiting the turn's close-out — nothing trimmed
        # yet (trim runs strictly AFTER close-out).
        assert app.fork_pending and app.turn_active
        assert app.notice_slot.current == "interrupting turn to restore checkpoint …"
        assert len(app.transcript.blocks) > len(t1_block_ids)

        # Release the gate: the turn breaks at its step boundary, closes out,
        # and only then does restore trim ledger + transcript.
        adapter.release()
        assert await wait_for(pilot, lambda: not app.fork_pending)
        assert not app.turn_active
        # No dead-turn checkpoint in the ledger; transcript is EXACTLY t1.
        assert [c.id for c in app.ledger.checkpoints] == ["t1"]
        assert [b.id for b in app.transcript.blocks] == t1_block_ids
        last = app.transcript.blocks[-1]
        assert last.kind == "turn_rule" and last.checkpoint_id == "t1"
        assert rules(app) == 1  # the interrupted turn's rule was trimmed away
        assert app.notice_slot.current == (
            "restored both · conversation restored · no tracked code edits in demo sessions"
        )
        assert app.composer.text == BRAINSTORM_PROMPT
        assert not app.rewind.display


@pytest.mark.asyncio
async def test_restore_mid_turn_keeps_queued_message_beside_restored_prompt() -> None:
    """Neither the restored prompt nor an already-queued next turn is lost."""
    adapter = GatedDemoAdapter()
    app = TuiApp(adapter)
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)  # t1 cut
        seen: list[str] = []
        orig_notice = app.show_notice
        app.show_notice = lambda text, duration=None: (  # type: ignore[method-assign]
            seen.append(text),
            orig_notice(text, duration),
        )

        # Park the brainstorm turn mid-turn, queue a next-turn message.
        app.submit_prompt(BRAINSTORM_PROMPT)
        assert await wait_for(pilot, lambda: app.turn_active and blocks_of(app, "narration"))
        await type_text(pilot, "hi")
        await pilot.press("shift+enter")
        await pilot.pause()
        assert app.adapter.steering.pending_next_turn

        # Restore the running prompt's t2 checkpoint, then release the gate.
        await pilot.press("ctrl+r")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.fork_pending and app.turn_active
        adapter.release()
        assert await wait_for(pilot, lambda: not app.fork_pending)

        restore_idx = next(i for i, text in enumerate(seen) if text.startswith("restored both"))
        assert await wait_for(pilot, lambda: "composer has a draft · queued message kept" in seen)
        assert restore_idx < seen.index("composer has a draft · queued message kept")

        # The original prompt is restored to the composer and the queued
        # next-turn message stays visible/interjectable rather than running
        # behind the user's back.
        assert not app.turn_active and rules(app) == 1
        assert [c.id for c in app.ledger.checkpoints] == ["t1"]
        assert app.composer.text == BRAINSTORM_PROMPT
        assert app.adapter.steering.pending_next_turn[0].text == "hi"
        assert app.queued_strip.display


@pytest.mark.asyncio
async def test_partial_restore_never_auto_drains_queued_message() -> None:
    """A failed code half must leave the queued next turn user-controlled."""
    adapter = GatedDemoAdapter()
    app = TuiApp(adapter)
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        app.submit_prompt(BRAINSTORM_PROMPT)
        assert await wait_for(pilot, lambda: app.turn_active and blocks_of(app, "narration"))
        await type_text(pilot, "run only after I inspect the restore")
        await pilot.press("shift+enter")
        assert app.adapter.steering.pending_next_turn

        await pilot.press("ctrl+r")
        await pilot.pause()
        await pilot.press("down", "down")  # code only: demo returns a partial outcome
        assert app.rewind.scope == "code"
        await pilot.press("enter")
        assert app.fork_pending and app.turn_active
        adapter.release()
        assert await wait_for(pilot, lambda: not app.fork_pending)
        await pilot.pause(0.2)  # prove no deferred auto-drain races in afterward

        assert not app.turn_active
        assert [message.text for message in app.adapter.steering.pending_next_turn] == [
            "run only after I inspect the restore"
        ]
        assert app.queued_strip.display
        assert app.notice_slot.current == (
            "partial restore code · code restore unavailable in demo sessions"
        )
        assert rules(app) == 2  # interrupted turn only; queued turn never started
        assert not app._turn_queues_pending  # no stale drain on the next runtime event


@pytest.mark.asyncio
async def test_restore_chip_click_during_pending_approval_keeps_keyboard() -> None:
    """A restore click cannot steal keyboard ownership from an approval.

    The running t2 checkpoint restores the conversation to immediately
    before that prompt, returns ``hi`` to the composer, and preserves the
    already-completed t1 timeline plus the interstitial mode narration.
    """
    app = TuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)  # t1 cut
        # The approval only asks in chat (the app boots in auto — §4
        # amendment). The ``/mode chat`` echo is before t2 and survives a
        # restore to t2's pre-prompt boundary.
        await set_mode(pilot, app, "chat")
        before_t2_block_ids = [b.id for b in app.transcript.blocks]

        # Park the build turn at the pytest approver.
        await type_text(pilot, "hi")
        await pilot.press("enter")
        assert await wait_for(pilot, lambda: app.approval_bar is not None)

        # Open the picker; the approval bar keeps the keyboard (§7).
        await pilot.press("ctrl+r")
        await pilot.pause()
        assert app.rewind.display
        assert app.focused is app.approval_bar

        # Click restore: interrupt-first restore parks behind the approval.
        await pilot.click("#rewind-fork")
        await pilot.pause()
        assert app.fork_pending and app.turn_active
        assert not app.rewind.display
        # The keyboard is NOT stranded — the approval bar still owns it.
        assert app.focused is app.approval_bar

        # Esc = Deny (§7): the turn closes out and the parked restore settles.
        await pilot.press("escape")
        assert await wait_for(pilot, lambda: app.approval_bar is None)
        assert await wait_for(pilot, lambda: not app.fork_pending)
        assert not app.turn_active
        assert [c.id for c in app.ledger.checkpoints] == ["t1"]
        assert [b.id for b in app.transcript.blocks] == before_t2_block_ids
        assert app.notice_slot.current == (
            "restored both · conversation restored · no tracked code edits in demo sessions"
        )
        assert app.composer.text == "hi"
        assert app.composer.has_focus_within


@pytest.mark.asyncio
async def test_completed_restore_removes_selected_prompt_turn_and_returns_prompt() -> None:
    app = TuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await _two_turns(pilot, app)
        assert blocks_of(app, "plan")  # the build turn left its plan block
        t1_rule = next(
            block for block in blocks_of(app, "turn_rule") if block.checkpoint_id == "t1"
        )
        t2_user = next(block for block in blocks_of(app, "user_line") if block.text == "hi")
        t2_rule = next(
            block for block in blocks_of(app, "turn_rule") if block.checkpoint_id == "t2"
        )

        await pilot.press("ctrl+r")
        await pilot.pause()
        # Newest t2 is selected. Restore targets the boundary BEFORE t2,
        # rather than retaining t2 as the old post-turn fork did.
        await pilot.press("enter")
        assert await wait_for(pilot, lambda: [c.id for c in app.ledger.checkpoints] == ["t1"])

        assert not app.rewind.display
        remaining_ids = {block.id for block in app.transcript.blocks}
        assert t1_rule.id in remaining_ids
        assert t2_user.id not in remaining_ids
        assert t2_rule.id not in remaining_ids
        assert not blocks_of(app, "plan")  # build-turn blocks are gone
        assert app.notice_slot.current == (
            "restored both · conversation restored · no tracked code edits in demo sessions"
        )
        assert app.composer.text == "hi"


@pytest.mark.asyncio
async def test_restore_parks_rich_live_draft_losslessly_for_up_recall() -> None:
    """Restore rehydrates its image and parks the current rich draft exactly."""
    from amplifier_app_tui.kernel.clipboard import ImageAttachment

    restored_image = ImageAttachment(b"\x89PNG\r\n\x1a\n" + b"\x01" * 32, "image/png")
    live_image = ImageAttachment(b"\x89PNG\r\n\x1a\n" + b"\x02" * 32, "image/png")

    class RichRestoreAdapter(DemoRuntimeAdapter):
        def __init__(self) -> None:
            super().__init__(instant=True)
            self.submissions: list[tuple[str, tuple[object, ...]]] = []

        async def submit(
            self,
            text: str,
            attachments: tuple[object, ...] = (),
            *,
            queued: bool = False,
        ) -> None:
            self.submissions.append((text, attachments))
            await super().submit(text, attachments, queued=queued)

        async def restore_checkpoint(
            self, checkpoint_id: str, ledger: object, scope: str
        ) -> object:
            outcome = await super().restore_checkpoint(checkpoint_id, ledger, scope)
            return outcome.model_copy(update={"prompt_attachments": (restored_image,)})

    adapter = RichRestoreAdapter()
    app = TuiApp(adapter)
    payload = "\n".join(f"unsent row {index}" for index in range(20))
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        app.submit_prompt("inspect [Image #1]", (restored_image,))
        assert await wait_for(pilot, lambda: rules(app) >= 2 and not app.turn_active)
        stub = app.composer.register_paste(payload)
        assert stub is not None
        app.composer.insert_text(f"keep {stub} ")
        app.composer.add_image(live_image)
        rich_draft = app.composer.text

        await pilot.press("ctrl+r")
        await pilot.pause()
        await pilot.press("enter")
        assert await wait_for(pilot, lambda: app.composer.text == "inspect [Image #1]")
        assert app.composer._staged_attachments(app.composer.text) == (restored_image,)

        await pilot.press("up")
        await pilot.pause()
        assert app.composer.text == rich_draft
        assert payload in app.composer._expand(app.composer.text)
        assert app.composer._staged_attachments(app.composer.text) == (live_image,)
        expanded_live_draft = app.composer._expand(rich_draft).strip()

        # The combined path matters: history recall must not merely look
        # correct. Sending the recalled draft has to expand the retained paste
        # and deliver the binary attachment through the app boundary.
        await pilot.press("enter")
        assert await wait_for(pilot, lambda: len(adapter.submissions) == 2)
        sent_text, sent_attachments = adapter.submissions[-1]
        assert sent_text == expanded_live_draft
        assert sent_attachments == (live_image,)


@pytest.mark.asyncio
async def test_restore_returns_submitted_long_paste_as_exact_compact_draft() -> None:
    """The selected prompt keeps its compact paste/image representation."""
    from amplifier_app_tui.kernel.clipboard import ImageAttachment

    app = TuiApp(DemoRuntimeAdapter(instant=True))
    payload = "\n".join(f"submitted row {index}" for index in range(20))
    image = ImageAttachment(b"\x89PNG\r\n\x1a\n" + b"\x03" * 32, "image/png")
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        stub = app.composer.register_paste(payload)
        assert stub is not None
        app.composer.insert_text(f"review {stub} ")
        app.composer.add_image(image)
        compact_prompt = app.composer.text
        expanded_prompt = app.composer._expand(compact_prompt).strip()

        await pilot.press("enter")
        assert await wait_for(pilot, lambda: rules(app) >= 2 and not app.turn_active)
        assert app.ledger.checkpoints[-1].label == expanded_prompt

        await pilot.press("ctrl+r")
        await pilot.pause()
        await pilot.press("enter")
        assert await wait_for(pilot, lambda: app.composer.text == compact_prompt)

        assert app.composer.text == compact_prompt
        assert app.composer._expand(app.composer.text).strip() == expanded_prompt
        assert app.composer._staged_attachments(app.composer.text) == (image,)


@pytest.mark.asyncio
async def test_conversation_only_restore_leaves_code_scope_unrequested() -> None:
    app = TuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await _two_turns(pilot, app)

        await pilot.press("ctrl+r")
        await pilot.pause()
        await pilot.press("down")  # conversation only
        assert app.rewind.scope == "conversation"
        await pilot.press("enter")
        assert await wait_for(pilot, lambda: [c.id for c in app.ledger.checkpoints] == ["t1"])

        assert not blocks_of(app, "plan")
        assert app.composer.text == "hi"
        assert app.notice_slot.current == "restored conversation · conversation restored"


@pytest.mark.asyncio
async def test_code_only_restore_keeps_conversation_and_composer_unchanged() -> None:
    app = TuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await _two_turns(pilot, app)
        before_ids = [block.id for block in app.transcript.blocks]
        before_checkpoints = [checkpoint.id for checkpoint in app.ledger.checkpoints]
        assert app.composer.text == ""

        await pilot.press("ctrl+r")
        await pilot.pause()
        await pilot.press("down", "down")  # code only
        assert app.rewind.scope == "code"
        await pilot.press("enter")
        assert await wait_for(
            pilot,
            lambda: (
                app.notice_slot.current
                == "partial restore code · code restore unavailable in demo sessions"
            ),
        )

        assert [checkpoint.id for checkpoint in app.ledger.checkpoints] == before_checkpoints
        assert [block.id for block in app.transcript.blocks] == before_ids
        assert app.composer.text == ""


@pytest.mark.asyncio
async def test_idle_double_esc_with_empty_composer_opens_checkpoint_picker() -> None:
    app = TuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        assert app.composer.text == ""

        await pilot.press("escape", "escape")
        await pilot.pause()

        assert app.rewind.display
        assert app.rewind.current is not None
        assert app.rewind.current.id == "t1"


@pytest.mark.asyncio
async def test_composer_activity_disarms_idle_double_esc_chord() -> None:
    app = TuiApp(DemoRuntimeAdapter(instant=True))
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)

        await pilot.press("escape")
        await pilot.press("x")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

        assert app.composer.text == "x"
        assert not app.rewind.display
        assert app.esc_sequence.idle_at is not None

        # Only the next consecutive Esc completes the newly armed chord.
        await pilot.press("escape")
        await pilot.pause()
        assert app.composer.text == ""
        assert app.notice_slot.current == "draft moved to history · ↑ restores it"


@pytest.mark.asyncio
async def test_idle_double_esc_parks_nonempty_draft_and_up_restores_it() -> None:
    app = TuiApp(DemoRuntimeAdapter(instant=True))
    draft = "keep this exact draft"
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        await type_text(pilot, draft)

        await pilot.press("escape", "escape")
        await pilot.pause()

        assert not app.rewind.display
        assert app.composer.text == ""
        assert app.notice_slot.current == "draft moved to history · ↑ restores it"

        await pilot.press("up")
        await pilot.pause()
        assert app.composer.text == draft
