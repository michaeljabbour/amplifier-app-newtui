"""Composition-root helpers kept out of ``ui/app.py`` (<500-line budget).

Pure-ish functions the app delegates to: keymap-sourced global bindings,
block builders for the needs-you list and the /permissions surface,
transcript trimming after a confirmed fork, esc-chain resolution and the
footer-state snapshot. Everything here operates on the app's public
surface — no hidden state.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from time import monotonic
from typing import TYPE_CHECKING

from textual.binding import Binding, BindingType

from ..commands.permissions import PermissionSurface
from ..model.blocks import (
    Answer,
    BlockIdAllocator,
    Narration,
    NeedsYouBlock,
    NeedsYouChoice,
    NeedsYouEntry,
    Segment,
    SessionBanner,
    SteerEcho,
    TodoItem,
    UserLine,
)
from ..model.formatting import command_digest
from ..model.queues import NeedsYouItem
from . import keymap, notifications
from .footer import FooterState
from .plan_panel import plan_counts, plan_panel_max_height, plan_panel_width
from .transcript import TranscriptView

if TYPE_CHECKING:
    from .app import TuiApp

STEER_NOTICE = "steer queued · shift+enter queues a full next-turn message"
STEER_NOTICE_LEGACY = "steer queued · alt+enter queues a full next-turn message"
STEER_DISCARDED_NOTICE = "steer not applied · discarded at turn end"
QUEUED_NOTICE = "message queued · runs as the next turn"
APPROVAL_NOTICE = "approval required · choose below the transcript"
APPROVAL_NOTICE_DURATION = 6.0
"""Approval notices linger 6s, not the 4s default (mockup requestApproval)."""
LANE_FOCUS_INTRO_NOTICE = "focused view · esc or click Back returns to parent"
"""First-ever lane focus (S6 AC4): announces the exit path once per app
session — a transient notice (today's ~4s default), never a permanent
tutorial overlay."""

_GLOBAL_ACTIONS = frozenset(
    {
        "cycle_mode",
        "cycle_permission",
        "cycle_effort",
        "cycle_tail",
        "recall_queued",
        "toggle_lanes",
        "toggle_thinking",
        "show_ledger",
        "show_needs_you",
        "open_rewind",
        "return_to_answer",
        "plan_drilldown",
        "toggle_plan_overflow",
        "stash_prompt",
    }
)

ATTENTION_MIN_TURN_SECONDS = notifications.ATTENTION_MIN_TURN_SECONDS
"""Turn-end attention threshold (re-exported from :mod:`ui.notifications`,
the single source of the ladder policy): a turn shorter than this is a
live exchange (the user is watching); a longer one plausibly lost their
attention, so its close-out notifies. Deferred decisions always notify —
they block on the human by definition."""


def attention_bell_needed(
    reason: notifications.AttentionReason,
    elapsed_s: float = 0.0,
    *,
    environ: Mapping[str, str] | None = None,
) -> bool:
    """Whether the attention ladder should fire at all for *reason*.

    hooks-notify wrote OSC-777 + BEL straight to the TTY on
    ``orchestrator:complete`` — raw escapes that corrupt the full-screen
    Textual TUI, so the kernel strips it at mount. The signal it carried
    ("the assistant needs you") is re-emitted through the notification
    ladder (:mod:`ui.notifications`): the driver-safe ``App.bell`` always,
    plus an OSC 777 desktop notification when the window is unfocused on a
    capable terminal (``ui/app.TuiApp._notify_attention``).

    This predicate is the ladder's floor — the ``bell`` rung — kept as a
    named seam because the app and tests read it directly. True when a
    decision was deferred (always) or a turn finished after
    :data:`ATTENTION_MIN_TURN_SECONDS`; ``AMPLIFIER_NOTIFY=false/0/no/off``
    disables it entirely.
    """
    return notifications.attention_needed(reason, elapsed_s, environ=environ)


@dataclass
class EscSequence:
    """The small state machine behind interrupt-then-backtrack.

    Only an Esc that actually targets a running turn arms the sequence.
    Panel-close and approval Esc presses therefore cannot accidentally open
    rewind.  The second press may land before or just after turn close-out.
    """

    interrupted_at: float | None = None
    idle_at: float | None = None

    def arm_interrupt(self, now: float) -> None:
        self.interrupted_at = now
        self.idle_at = None

    def consume_backtrack(self, now: float) -> bool:
        interrupted_at = self.interrupted_at
        self.interrupted_at = None
        return (
            interrupted_at is not None
            and 0 <= now - interrupted_at <= keymap.ESC_BACKTRACK_WINDOW_SECONDS
        )

    def arm_idle(self, now: float) -> None:
        self.idle_at = now

    def consume_idle(self, now: float) -> bool:
        idle_at = self.idle_at
        self.idle_at = None
        return idle_at is not None and 0 <= now - idle_at <= keymap.ESC_BACKTRACK_WINDOW_SECONDS

    def reset(self) -> None:
        self.interrupted_at = None
        self.idle_at = None


def global_bindings() -> list[BindingType]:
    """App bindings sourced from the keymap table (single source, NOTES #7)."""
    bindings: list[BindingType] = [
        Binding(key, binding.action, binding.label, show=False, priority=True)
        for binding in keymap.KEYMAP
        if binding.action in _GLOBAL_ACTIONS
        for key in binding.keys
    ]
    bindings.append(Binding("up", "palette_up", "↑", show=False, priority=True))
    bindings.append(Binding("down", "palette_down", "↓", show=False, priority=True))
    bindings.append(Binding("escape", "app_esc", "esc", show=False))
    # amplifier-app-cli parity: Ctrl-D exits (its banner advertises it).
    # Textual's stock ctrl+q quit binding stays too.
    bindings.append(Binding("ctrl+d", "quit", "quit", show=False, priority=True))
    # Copy whichever selection exists (composer text or transcript drag).
    # Priority: TextArea's own ctrl+c binding otherwise swallows the key
    # while the composer has focus — transcript copies silently no-oped.
    bindings.append(Binding("ctrl+c,super+c", "copy_selection", "copy", show=False, priority=True))
    return bindings


def needs_you_display_question(item: NeedsYouItem) -> str:
    """The question as the Needs-you row shows it: compact, never raw sprawl.

    Governance parks classifier denials as ``Allow <raw action>?`` — for a
    heredoc write that raw action is the ENTIRE command (unreadable in a
    row). Exactly that shape is displayed as ``Allow <verb-noun digest>?``;
    any other question (scripted demo, escalation review) is already prose
    and passes through verbatim. Display-only: the parked item, the wire
    payload, and the answer injection keep the full raw action.
    """
    if item.action and item.question == f"Allow {item.action}?":
        return f"Allow {command_digest(item.action)}?"
    return item.question


def _needs_you_choices(item: NeedsYouItem) -> tuple[NeedsYouChoice, ...]:
    """Choices for a needs-you entry, pairing each label with its aligned
    option description (question tool) -- ``answer`` stays the bare label so
    the answered decision matches the donor contract."""
    descriptions = item.descriptions
    return tuple(
        NeedsYouChoice(
            label=label,
            answer=label,
            description=descriptions[i] if i < len(descriptions) else "",
        )
        for i, label in enumerate(item.choices)
    )


def needs_you_block(
    pending: tuple[NeedsYouItem, ...], allocator: BlockIdAllocator
) -> NeedsYouBlock | None:
    """The ``Needs you`` transcript block for the pending decisions (§7)."""
    if not pending:
        return None
    entries = tuple(
        NeedsYouEntry(
            decision_id=item.decision_id,
            question=needs_you_display_question(item),
            reason=item.reason,
            choices=_needs_you_choices(item),
            multiple=item.multiple,
            custom=item.custom,
            highlight=item.highlight,
        )
        for item in pending
    )
    return NeedsYouBlock(id=allocator.next_id(), items=entries)


def permissions_block(
    surface: PermissionSurface, trust_str: str, allocator: BlockIdAllocator
) -> Answer:
    """The ``/permissions`` trust-slot print as an Answer block."""
    snapshot = surface.snapshot()
    spans: list[Segment] = [
        Segment(text="· ", style_token="blue"),
        Segment(text="Permissions", style_token="bright", bold=True),
        Segment(text=f"  {trust_str}\n", style_token="dim"),
        Segment(
            text="  path policy · allowed roots + protected paths enforced\n",
            style_token="dim",
        ),
    ]
    spans.extend(Segment(text=f"  {slot.label()}\n", style_token="fg") for slot in surface.slots())
    if snapshot.exceptions:
        spans.append(
            Segment(
                text="  always allowed: " + " · ".join(snapshot.exceptions) + "\n",
                style_token="dim",
            )
        )
    if snapshot.blocks:
        spans.append(
            Segment(
                text="  blocked: " + " · ".join(snapshot.blocks) + "\n",
                style_token="dim",
            )
        )
    spans.append(Segment(text=f"  boundary: {snapshot.boundary}", style_token="dim"))
    return Answer(id=allocator.next_id(), spans=tuple(spans))


def trim_after_checkpoint(view: TranscriptView, checkpoint_id: str) -> None:
    """Drop every block after the turn rule stamped *checkpoint_id*.

    Runs only AFTER the fork is confirmed (confirm-then-trim, ADR-0007).
    """
    ids = view.block_ids
    cut: int | None = None
    for index, block_id in enumerate(ids):
        block = view.get_block(block_id)
        if block is not None and block.kind == "turn_rule" and block.checkpoint_id == checkpoint_id:
            cut = index
    if cut is None:
        return
    for block_id in ids[cut + 1 :]:
        view.remove_block(block_id)


def trim_from_checkpoint(view: TranscriptView, checkpoint_id: str) -> None:
    """Drop the selected checkpoint's prompt turn and everything after it.

    Pre-prompt restore differs from the legacy post-turn fork: the selected
    prompt itself is placed back in the composer, so its user line, response,
    and stamped rule must all leave the transcript.
    """
    ids = view.block_ids
    rule_index: int | None = None
    for index, block_id in enumerate(ids):
        block = view.get_block(block_id)
        if block is not None and block.kind == "turn_rule" and block.checkpoint_id == checkpoint_id:
            rule_index = index
            break
    if rule_index is None:
        return
    cut = rule_index
    for index in range(rule_index, -1, -1):
        block = view.get_block(ids[index])
        if block is not None and block.kind == "user_line":
            cut = index
            break
    for block_id in ids[cut:]:
        view.remove_block(block_id)


def announce_ready(app: TuiApp) -> None:
    """Session banner + any degraded-start notices once identity is known."""
    app.clear_boot_progress()
    # Resume offset for checkpoint turn ids (spec §9): known only after
    # the adapter booted, before the first turn event can arrive.
    app.reducer.turn_base = app.adapter.turn_base
    # Resume cost baseline travels the same handoff: RealRuntimeAdapter
    # learns prior session spend inside start(), after the reducer was
    # constructed — re-seed so footer $ and checkpoint cost_at include it
    # (one session cost basis everywhere, spec §11). Safe to assign: the
    # adapter contract calls ready() before any turn event, so the
    # running total still equals its constructor seed here.
    app.reducer.session_cost = app.adapter.session_cost_start
    headline, detail = app.adapter.banner
    if headline or detail:
        app.append_block(
            SessionBanner(id=app.allocator.next_id(), headline=headline, detail=detail)
        )
    # Resume replay: an empty screen over a restored context reads as a
    # fresh session. Full-fidelity path first — the stored UIEvents run
    # back through the reducer, rebuilding the transcript exactly as it
    # rendered live: tool digests, delegate summaries, lane focus
    # transcripts, plan state, turn rules (DESIGN-SPEC §3/§11). Sessions
    # without a usable event log (e.g. created by another amplifier app)
    # degrade to the prose-only prompts + answers below.
    from .live_tail import answer_spans

    # Seed ↑ history from the per-directory persistent store — a FRESH
    # session recalls prompts from prior sessions in the same working dir
    # (the cross-session-history fix; the store is the superset that also
    # holds a resumed session's own prompts). Fall back to the resumed
    # transcript's user prompts when nothing was persisted (legacy sessions
    # or ones created by another amplifier app without a shared history file).
    persisted = app.adapter.prompt_history()
    if persisted:
        app.composer.seed_history(persisted)
    else:
        app.composer.seed_history(
            text for role, text in app.adapter.restored_history if role == "user"
        )
    if not app.reducer.replay(
        app.adapter.restored_events,
        turn_base=app.adapter.turn_base,
        session_cost=app.adapter.session_cost_start,
    ):
        for role, text in app.adapter.restored_history:
            if role == "user":
                app.append_block(UserLine(id=app.allocator.next_id(), text=text, mode=app.mode_id))
            else:
                app.append_block(
                    Answer(
                        id=app.allocator.next_id(),
                        spans=answer_spans(text),
                        clickable=False,
                    )
                )
    for notice in app.adapter.startup_notices:
        app.append_block(
            Answer(
                id=app.allocator.next_id(),
                spans=(Segment(text=notice, style_token="orange", bold=True),),
            )
        )
    app.refresh_status()


def announce_boot_failure(app: TuiApp, error: Exception) -> None:
    """Boot failed: replace the progress line with a readable diagnosis
    instead of an unhandled worker crash (which used to surface only as
    the masked ``Event loop is closed`` teardown traceback).

    The session never came up, so there is nothing to drive — but keeping
    the app alive lets the supervisor read the reason, copy it, and quit
    cleanly rather than staring at a stack trace in the scrollback.
    """
    app.clear_boot_progress(immediate=True)  # error text, not a melting wordmark
    detail = str(error).strip() or error.__class__.__name__
    app.append_block(
        Answer(
            id=app.allocator.next_id(),
            spans=(
                Segment(text="⊘ session failed to start · ", style_token="red"),
                Segment(text=detail, style_token="fg"),
            ),
            clickable=False,
        )
    )
    hint = (
        "Check provider setup with `amplifier-tui doctor`, or run "
        "`--demo` for a credential-free UI. Press ctrl+d to quit."
    )
    app.append_block(
        Answer(
            id=app.allocator.next_id(),
            spans=(Segment(text=hint, style_token="dim"),),
            clickable=False,
        )
    )
    app.show_notice("session failed to start")
    app.refresh_status()


async def mount_approval(
    app: TuiApp, ticket_id: str, prompt: str, options: tuple[str, ...]
) -> None:
    """Swap the composer for the approval bar (spec §7 presentation).

    Notice order follows the mockup ``requestApproval``: the approval
    notice first, then — when a lane was focused — the auto-return's
    ``back to parent · approval required`` overwrites it and stays.
    """
    from textual.containers import Container

    from .approval_bar import ApprovalBar

    lane_was_focused = app.transcript.focused_lane is not None
    if lane_was_focused:
        await app.transcript.restore_main()
    # The approval bar owns the keyboard (spec §7): an open palette strip
    # would otherwise sit above the bar and steal the arrow keys.
    app.palette.apply_filter(None)
    if app.approval_bar is not None:
        app.approval_bar.remove()
    bar = ApprovalBar(ticket_id, prompt, options or ("Allow once", "Allow always", "Deny"))
    app.composer.display = False
    await app.query_one("#composer-slot", Container).mount(bar)
    # Publish the bar only once it is fully mounted. Callers use non-None as
    # the ready signal; exposing it before this await raced the focus/notice
    # setup and made approval presentation observably half-initialized.
    app.approval_bar = bar
    bar.focus()
    app.show_notice(APPROVAL_NOTICE, duration=APPROVAL_NOTICE_DURATION)
    if lane_was_focused:
        app.show_notice("back to parent · approval required", duration=APPROVAL_NOTICE_DURATION)
    app.refresh_status()


def echo_steer(app: TuiApp, text: str) -> None:
    """Queue a mid-turn steer and stamp its ↳ echo + notice (spec §5)."""
    queued = app.adapter.steering.enqueue(text, kind="steer")
    echo = SteerEcho(id=app.allocator.next_id(), text=text)
    app.steer_echoes[queued.message_id] = echo.id
    app.append_block(echo)
    # Advertise the queue chord the terminal can actually deliver
    # (README/§12: alt+enter is the legacy fallback).
    app.show_notice(STEER_NOTICE if app.kitty_protocol else STEER_NOTICE_LEGACY)


def echo_lane_steer(app: TuiApp, session_id: str, text: str) -> None:
    """Queue a steer for a running delegate and acknowledge it (issue #39).

    The root :func:`echo_steer` steers the coordinator; this steers ONE
    lane. On send the design sketch calls for a "queued for lane" line in
    chat plus a ``▸ N queued`` badge on the lane row; the delivery echo
    ("Applying steer: …") lands in the lane's focus transcript later, when
    the runtime consumes the steer at that delegate's next step boundary.
    """
    record = app.lanes.get(session_id)
    name = record.lane.name if record is not None else session_id
    try:
        app.adapter.lane_steering.enqueue(session_id, text)
    except ValueError as error:
        app.show_notice(str(error))
        return
    # Chat acknowledgement line (append lands in the stashed parent chat
    # while a lane is focused — spec §8: the parent keeps accumulating).
    app.append_block(
        Answer(
            id=app.allocator.next_id(),
            spans=(
                Segment(text="  ↳ ", style_token="teal"),
                Segment(text=f'queued for lane {name}: "{text}" ', style_token="teal"),
                Segment(text="· applies at its next step boundary", style_token="dimmer"),
            ),
            clickable=False,
        )
    )
    app.show_notice(f"steer queued for lane · {name}")
    app.lanes_changed()  # paint the ▸ N queued badge on the lane row


def handle_lane_focus_change(app: TuiApp, lane_id: str | None) -> None:
    """Lane focus swap follow-ups (spec §7/§8).

    On return to the parent: an open approval bar keeps the keyboard
    (auto-return path, §7) and its own notice; otherwise show the
    ``back to parent session`` notice and refocus the composer.
    """
    if lane_id is None:
        if app.approval_bar is not None:
            app.approval_bar.focus()
        else:
            app.show_notice("back to parent session")
            app.composer.focus_input()
    app.refresh_status()


def sync_steer_echoes(app: TuiApp) -> None:
    """Drop the ↳ echo of any steer no longer pending (spec §5).

    Steering-queue listener: a steer leaves the queue either when the
    runtime consumes it at a step boundary (``Applying steer: …``) or
    when it is discarded at turn end — both remove the echo.
    """
    pending = {m.message_id for m in app.adapter.steering.pending_steers}
    for message_id in [m for m in app.steer_echoes if m not in pending]:
        app.remove_block(app.steer_echoes.pop(message_id))


def finish_turn_queues(app: TuiApp) -> None:
    """Turn-end queue duties (mockup ``runTurn`` close + ``drainQueue``).

    Leftover steers are silently DISCARDED (mockup: ``runTurn`` start
    resets ``this.steer = null`` and its end only removes the steer
    line) — a steer the runtime never consumed must not become a turn
    the user never sent. The queued next-turn message auto-runs with
    the ``queued message picked up`` notice; the app defers this call
    until the runtime's end-of-turn events (e.g. the ``agents 1 done``
    notice) are reduced, so — as in the mockup ``drainQueue`` — the
    pickup notice lands last and stays visible.
    """
    # Discard leftovers (ADR-0007: an unconsumed steer must not become a
    # turn the user never sent) — but say so; silent loss of typed input
    # reads as a bug. The listener drops the ↳ echoes.
    if app.adapter.steering.drain_steers():
        app.show_notice(STEER_DISCARDED_NOTICE)
    pending = app.adapter.steering.pending_next_turn
    if pending and app.composer.text:
        # A checkpoint restore returns the selected prompt to the composer.
        # Do not immediately run a previously queued message behind that
        # editable draft: keep it visible so the user can interject, recall,
        # or discard it deliberately.
        app.queued_strip.show_queued(pending[0].text)
        app.show_notice("composer has a draft · queued message kept")
        return
    queued = app.adapter.steering.consume_next_turn_message()
    if queued is not None:
        app.show_notice("queued message picked up")
        # submit_queued, not submit: a drained turn emits no mode notice
        # (mockup drainQueue has no setMode), so the pickup notice stays.
        # The app-owned worker restores this exact capsule when checkpoint or
        # rewind recovery rejects it before acceptance, and contains later
        # provider failures so a queue drain can never crash the TUI.
        app.submit_queued_message(queued)
    remaining = app.adapter.steering.pending_next_turn
    if remaining:
        app.queued_strip.show_queued(remaining[0].text)
    else:
        app.queued_strip.clear_queued()


def handle_fork(app: TuiApp, checkpoint_id: str) -> None:
    """Rewind fork: backend confirms FIRST, then trim (ADR-0007 §Rewind)."""
    if app.session_ops.context_operation_pending:
        app.show_notice(
            f"{app.session_ops.context_operation_label} in progress · rewind unavailable"
        )
        return
    checkpoint = app.ledger.checkpoint_by_id(checkpoint_id)
    if checkpoint is None:
        app.show_notice(f"unknown checkpoint · {checkpoint_id}")
        return
    if app.fork_pending:
        return  # one fork at a time — a second Enter must not double-fork
    app.fork_pending = True
    app.run_worker(confirm_fork(app, checkpoint.id, checkpoint.label), exclusive=False)


def handle_restore(app: TuiApp, checkpoint_id: str, scope: str) -> None:
    """Start one pre-prompt checkpoint restore (code, conversation, or both)."""
    if app.session_ops.context_operation_pending:
        app.show_notice(
            f"{app.session_ops.context_operation_label} in progress · rewind unavailable"
        )
        return
    if app.composer.capturing_decision or getattr(app, "_pending_custom_decision", None):
        app.show_notice("finish or cancel the custom decision answer before restoring")
        return
    checkpoint = app.ledger.checkpoint_by_id(checkpoint_id)
    if checkpoint is None:
        app.show_notice(f"unknown checkpoint · {checkpoint_id}")
        return
    if scope not in {"both", "conversation", "code"}:
        app.show_notice(f"unknown restore mode · {scope}")
        return
    if app.fork_pending:
        return
    app.fork_pending = True
    app.composer.submission_blocked = True
    app.run_worker(
        confirm_restore(app, checkpoint.id, checkpoint.label, scope),
        exclusive=False,
    )


async def confirm_fork(app: TuiApp, checkpoint_id: str, label: str) -> None:
    """Request the session fork from the runtime; trim only on success.

    Interrupt-then-fork: a fork confirmed while a turn is running first
    interrupts that turn (the existing Esc path — the runtime breaks at
    the next step boundary) and awaits its close-out, so the dead turn's
    rule + checkpoint exist BEFORE the trim and are removed BY the trim
    (ledger ``trim_to`` + transcript trim). Forking under a live turn
    would orphan its still-streaming blocks and, on a real session,
    corrupt turn numbering (``context.set_messages()`` during the
    provider loop).

    The adapter's ``fork`` performs the backend fork (foundation
    ``fork_session_in_memory`` + ``context.set_messages()`` for a live
    real session; immediate for the in-memory demo script) and trims
    the ledger once confirmed. Only then does the transcript trim —
    confirm-then-trim: a failed fork leaves everything untouched.

    While ``fork_pending`` is up, ``_consume_events`` defers the
    turn-end queue drain, so a shift+enter-queued next-turn message is
    NOT auto-run against the abandoned pre-fork context (where the fork
    would silently trim its whole turn away). The drain runs here after
    the fork settles — the queued prompt picks up against the post-fork
    state instead (spec §5: it auto-runs when the turn ends).
    """
    from ..kernel.rewind import RewindError

    try:
        if app.turn_active:
            app.show_notice("interrupting turn to fork …")
            await app.adapter.interrupt()
            while app.turn_active:  # close-out = reducer handled PromptComplete
                await asyncio.sleep(0.05)
        try:
            await app.adapter.fork(checkpoint_id, app.ledger)
        except RewindError as error:
            app.show_notice(f"fork failed · {error}")
            return
        trim_after_checkpoint(app.transcript, checkpoint_id)
        app.reconcile_checkpoint_drafts()
        app.show_notice(f"forked from {checkpoint_id} · {label}")
        app.composer.focus_input()
        app.refresh_status()
    finally:
        app.fork_pending = False
        # Deferred turn-end queue duties (see docstring): the queued
        # next-turn message now picks up against the post-fork context.
        app.drain_turn_queues()


async def confirm_restore(app: TuiApp, checkpoint_id: str, prompt: str, scope: str) -> None:
    """Interrupt if needed, restore safely, then update the visible timeline.

    Code restoration is kernel-owned and compare-and-swap guarded. Conversation
    restoration reuses Amplifier Foundation's native turn slicing, but targets
    the boundary *before* the selected prompt (including empty context before
    turn one). The transcript and ledger change only after the kernel confirms.
    """
    from ..kernel.rewind import RewindError

    safe_to_drain_queue = False
    try:
        if app.turn_active:
            app.show_notice("interrupting turn to restore checkpoint …")
            await app.adapter.interrupt()
            while app.turn_active:
                await asyncio.sleep(0.05)
        checkpoint = app.ledger.checkpoint_by_id(checkpoint_id)
        if checkpoint is None:
            app.show_notice(f"restore failed · unknown checkpoint {checkpoint_id}")
            return
        restore_turn_id = checkpoint.before_turn_id
        # Capture before ``trim_before`` removes the selected checkpoint from
        # the live ledger. This app-owned capsule is the only place the compact
        # paste stub exists; provider context intentionally stores expanded
        # text and binary image blocks instead.
        restored_draft = app.checkpoint_draft(checkpoint_id)
        try:
            outcome = await app.adapter.restore_checkpoint(checkpoint_id, app.ledger, scope)
        except (RewindError, OSError, ValueError) as error:
            app.show_notice(f"restore failed · {error}")
            return

        if outcome.conversation_restored:
            # Real adapters restore against an immutable ledger snapshot on
            # their runtime thread. Commit the actual mutable UI ledger here,
            # on its owning app loop, only after the kernel confirms.
            app.ledger.trim_before(checkpoint_id)
            trim_from_checkpoint(app.transcript, checkpoint_id)
            app.reducer.turn_base = restore_turn_id
            if app.composer.text:
                app.composer.remember_and_clear_draft()
            if restored_draft is not None:
                app.composer.restore_draft(restored_draft)
            else:
                app.composer.set_draft(
                    prompt,
                    outcome.prompt_attachments,
                    compact_long_paste=True,
                )
            app.reconcile_checkpoint_drafts()
        prefix = "partial restore" if outcome.partial else "restored"
        app.show_notice(f"{prefix} {scope} · {outcome.summary}")
        safe_to_drain_queue = not outcome.partial
        app.composer.focus_input()
        app.refresh_status()
    finally:
        app.composer.submission_blocked = False
        app.fork_pending = False
        if safe_to_drain_queue:
            app.drain_turn_queues()
        else:
            # The interrupted turn set a close-out drain token. A partial or
            # failed restore keeps the queued prompt user-controlled, so that
            # stale token must not fire on the next unrelated runtime event.
            app.cancel_turn_queue_drain()
            pending = app.adapter.steering.pending_next_turn
            if pending:
                app.queued_strip.show_queued(pending[0].text)


def run_pending_directive(app: TuiApp) -> None:
    """Auto-run a resumed fork child's primed directive as the first turn.

    A session created by ``/fork`` / ``session fork`` stores a starting
    directive; :attr:`RuntimeAdapter.pending_directive` surfaces it on resume
    (consume-once, cleared in the store by ``RealRuntime.start``). Submitting it
    here makes the child *run* that instruction first — the reachable stand-in
    for app-cli's background-directive fork (true detached execution is not
    reachable from the full-screen host, issue #45). No pending directive (fresh
    session, ordinary resume) is the common case and does nothing.
    """
    directive = getattr(app.adapter, "pending_directive", "")
    if not directive:
        return
    app.adapter.pending_directive = ""  # belt-and-suspenders: never re-run in-process
    app.show_notice(f"fork directive · running: {directive[:48]}")
    app.submit_prompt(directive)


def apply_decision(app: TuiApp, decision_id: str, answer: str) -> bool:
    """Act on a deferred decision: answer it + log ``Applying decision``.

    Scrollback is append-only (mockup §7): the Needs-you listing stays in
    the transcript; only the footer badge clears and the narration lands
    after it.
    """
    from .needs_you import applying_decision_line

    if app.session_ops.context_operation_pending:
        app.show_notice(f"{app.session_ops.context_operation_label} in progress · decision kept")
        return False

    try:
        item = app.adapter.needs_you.answer(decision_id, answer)
    except (KeyError, ValueError) as error:
        app.show_notice(str(error))
        return False
    narration = app.adapter.decision_narration(answer, item.action) or applying_decision_line(
        answer
    )
    # Mockup logs the applied decision as a narration line: bright "● "
    # marker + fg text (design-v3-cohesive.html:289).
    app.append_block(Narration(id=app.allocator.next_id(), text=narration))
    # The denied ACTION is the /improve join key (DenialLog counts by
    # action); the chip label is only the fallback for actionless items.
    app.journal.record_override(item.action or answer)
    # Acting on the decision IS acknowledging it (B7 AC5): clear the
    # attention record + its destination indicator where supported.
    app._acknowledge_attention()
    app.refresh_status()
    return True


def begin_custom_decision_capture(app: TuiApp, decision_id: str) -> None:
    """Give the composer one explicit purpose: answer *decision_id*.

    The visible bottom band is persistent (unlike a four-second notice), and
    the composer's existing draft is parked losslessly until submit/cancel.
    """
    item = next(
        (
            pending
            for pending in app.adapter.needs_you.pending
            if pending.decision_id == decision_id
        ),
        None,
    )
    if item is None:
        app.show_notice("decision is no longer waiting")
        return
    # A second custom chip switches targets without nesting draft snapshots.
    if app._pending_custom_decision and app._pending_custom_decision != decision_id:
        close_custom_decision_capture(app, notice=False)
    app._pending_custom_decision = decision_id
    app.palette.apply_filter(None)
    from .file_mentions import close_file_mentions

    close_file_mentions(app)
    app.history_recall.show(None)
    app.composer.begin_decision_capture()
    app.decision_capture.show_question(needs_you_display_question(item))
    app.composer.focus_input()
    app._refresh_footer()


def close_custom_decision_capture(
    app: TuiApp,
    *,
    decision_id: str | None = None,
    notice: bool = True,
) -> bool:
    """Close custom-answer mode and restore the parked composer draft."""
    pending = app._pending_custom_decision
    if not pending or (decision_id is not None and pending != decision_id):
        return False
    app._pending_custom_decision = None
    app.decision_capture.close()
    app.composer.end_decision_capture()
    app.composer.focus_input()
    app._refresh_footer()
    if notice:
        app.show_notice("custom answer cancelled · decision still waiting")
    return True


def apply_pending_custom_answer(app: TuiApp, text: str) -> bool:
    """Apply *text* to the captured decision before normal composer routing.

    ``True`` means custom-answer mode owned the input, even if queue resolution
    failed; callers must not leak that text into submit/steer/queue handling.
    """
    decision_id = app._pending_custom_decision
    if not decision_id:
        return False
    if app.session_ops.context_operation_pending:
        app.show_notice(f"{app.session_ops.context_operation_label} in progress · decision kept")
        return True
    from .file_mentions import close_file_mentions

    close_file_mentions(app)
    if apply_decision(app, decision_id, text):
        close_custom_decision_capture(app, decision_id=decision_id, notice=False)
    return True


def recall_queued_message(app: TuiApp) -> None:
    """Recall the visible next-turn message into the composer for steering.

    The queue pop is atomic.  Existing drafts and pending steers win, so this
    action never overwrites text or creates the misleading second-steer→queue
    loop the user was trying to escape.
    """
    if app._pending_custom_decision:
        app.show_notice("finish or cancel the decision answer first")
        return
    if app.adapter.steering.pending_steers:
        app.show_notice("current steer already waiting · queued message kept")
        return
    if app.composer.text:
        app.show_notice("composer has a draft · queued message kept")
        return
    queued = app.adapter.steering.consume_next_turn_message()
    if queued is None:
        app.queued_strip.clear_queued()
        app.show_notice("no queued message to recall")
        app._refresh_footer()
        return
    app.queued_strip.clear_queued()
    if queued.draft is not None:
        app.composer.restore_draft(queued.draft)
    else:
        app.composer.set_draft(queued.text, queued.attachments)
    app.composer.focus_input()
    app._refresh_footer()
    action = "steers now" if app.turn_active else "sends now"
    app.show_notice(
        f"queued message recalled · enter {action} · {app.composer.queue_hint} requeues"
    )


def _os_clipboard_commands() -> tuple[tuple[str, ...], ...]:
    """Platform clipboard commands in preference order."""

    import sys

    if sys.platform == "darwin":
        return (("pbcopy",),)
    return (("wl-copy",), ("xclip", "-selection", "clipboard"), ("xsel", "-ib"))


def os_clipboard_available() -> bool:
    """Whether a native clipboard writer is available without running it."""

    import shutil

    return any(shutil.which(command[0]) is not None for command in _os_clipboard_commands())


def os_clipboard_copy(text: str) -> bool:
    """Write *text* to the OS clipboard via the platform tool, if any.

    OSC 52 alone is not enough: iTerm2 ships with terminal clipboard
    writes disabled, so copies silently vanished (user report). A local
    TUI can just use pbcopy / wl-copy / xclip directly. Returns True when
    a tool accepted the text; never raises.
    """
    import shutil
    import subprocess

    for command in _os_clipboard_commands():
        if shutil.which(command[0]) is None:
            continue
        try:
            subprocess.run(
                command,
                input=text.encode("utf-8"),
                timeout=5,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
        except Exception:  # noqa: BLE001 — clipboard is best-effort
            continue
    return False


def native_modes_segments(
    catalog: object, term_width: int = 120, active: tuple[str, ...] = ()
) -> tuple[Segment, ...]:
    """Render the mode tool's catalog output grouped by source bundle.

    The mounted mode tool reports ``{"modes": [{name, description,
    source}, …]}`` — dynamically composed (superpowers, modes, llm-wiki,
    …), so this formats whatever arrives rather than any fixed list.
    Non-mapping payloads fall back to plain text. Names in *active* are
    marked with a ``◆`` so ``/modes`` shows the currently-active set.
    """
    from collections.abc import Mapping as _Mapping

    modes: list[_Mapping] = []
    if isinstance(catalog, _Mapping):
        raw = catalog.get("modes")
        if isinstance(raw, list):
            modes = [m for m in raw if isinstance(m, _Mapping)]
    if not modes:
        text = str(catalog).strip()
        return (Segment(text=f"  {text}\n", style_token="dim"),) if text else ()
    by_source: dict[str, list[_Mapping]] = {}
    for mode in modes:
        by_source.setdefault(str(mode.get("source", "")), []).append(mode)
    segments: list[Segment] = []
    name_w = max(len(str(m.get("name", ""))) for m in modes)
    # Fill the terminal width instead of a fixed 90-col cap: indent(4) + name
    # column + 2-space gap leaves this for the description on one line.
    desc_budget = max(24, term_width - 4 - name_w - 2)
    for source in sorted(by_source):
        segments.append(Segment(text=f"  {source or 'bundle'}\n", style_token="dimmer"))
        for mode in sorted(by_source[source], key=lambda m: str(m.get("name", ""))):
            name = str(mode.get("name", ""))
            desc = str(mode.get("description", "")).split("\n")[0]
            if len(desc) > desc_budget:
                desc = desc[: desc_budget - 1] + "…"
            marker = "◆ " if name in active else "  "
            segments.append(Segment(text=f"  {marker}{name.ljust(name_w)}  ", style_token="teal"))
            segments.append(Segment(text=f"{desc}\n", style_token="dim"))
    segments.append(
        Segment(text="  /mode <name> activates · /mode off clears", style_token="dimmer")
    )
    return tuple(segments)


def go_back_to_parent(app: TuiApp) -> None:
    """Leave a focused lane back to the parent transcript.

    The single seam both Escape's ``lane_unfocus`` action (keyboard) and
    the transcript's focus-header Back control (click/enter/space) route
    through (S6 AC2/AC5) — pure navigation: it never interrupts or ends
    the subagent's turn (DESIGN-SPEC §5/§8 — focus is reversible view
    state, not a session lifecycle edge).
    """
    app.run_worker(app.transcript.restore_main(), exclusive=False)


def handle_esc(app: TuiApp, *, now: float | None = None) -> None:
    """Resolve Esc priority plus interrupt-then-backtrack (spec §5)."""
    pressed_at = monotonic() if now is None else now
    checks = {
        "lane_focus": lambda: app.transcript.focused_lane is not None,
        # Mockup Escape: ``if (this.palFilter !== null)`` — ANY live slash
        # filter consumes the Esc, even a zero-match one whose strip is
        # hidden, so typed "/…" text never falls through to interrupt.
        "palette": lambda: app.palette.filter_text is not None,
        "rewind": lambda: bool(app.rewind.display),
        "sessions": lambda: bool(app.sessions_strip.display),
        "lanes": lambda: bool(app.lanes_panel.display),
        "running": lambda: app.turn_active,
    }
    actions = {
        "lane_unfocus": lambda: go_back_to_parent(app),
        "close_palette": app.close_palette,
        "close_rewind": app.rewind.close_strip,
        "close_sessions": app.sessions_strip.close_strip,
        "close_lanes": app.lanes_panel.action_close,
        "interrupt_running": app.interrupt_turn,
    }
    for context, action in keymap.ESC_CHAIN:
        if checks[context]():
            if action == "interrupt_running":
                if app.esc_sequence.consume_backtrack(pressed_at):
                    app.action_open_rewind()
                else:
                    app.esc_sequence.arm_interrupt(pressed_at)
                    actions[action]()
                return
            app.esc_sequence.reset()
            actions[action]()
            return
    if app.esc_sequence.consume_backtrack(pressed_at):
        app.action_open_rewind()
        return
    if app.esc_sequence.consume_idle(pressed_at):
        if app.composer.text:
            app.composer.remember_and_clear_draft()
            app.show_notice("draft moved to history · ↑ restores it")
        else:
            app.action_open_rewind()
        return
    app.esc_sequence.arm_idle(pressed_at)


PLAN_PANEL_MIN_WIDTH = 90
"""Below this width the plan stacks under lanes and uses the full row.

The earlier responsive ladder hid the only expand/collapse control and left
narrow users with a passive ``Plan N/M`` footer count, contradicting S7 AC5.
"""


def apply_plan_change(app: TuiApp, items: tuple[TodoItem, ...]) -> None:
    """Reducer pushed a new root todo list — repaint the ambient surfaces."""
    app.plan_items = tuple(items)
    sync_plan_surfaces(app)


def sync_plan_surfaces(app: TuiApp) -> None:
    """Fit the interactive plan at every supported terminal width.

    Wide layouts keep lanes and plan side by side. Narrow layouts stack the
    plan below lanes at full width, preserving its keyboard/click expansion
    control and bounded internal scrolling (S7 AC1/AC5). Called on every plan
    change and terminal resize.
    """
    app.plan_panel.update_plan(app.plan_items)
    narrow = app.size.width < PLAN_PANEL_MIN_WIDTH
    app.query_one("#bottom-strip").set_class(narrow, "plan-narrow")
    if app.plan_items:
        # Content-fitted width (37 floor, one-third cap) — real plans carry
        # longer items than the mockup and wrapped at the fixed width. Narrow
        # layouts stack the plan, so it owns the full row instead.
        app.plan_panel.styles.width = (
            "100%" if narrow else plan_panel_width(app.plan_items, app.size.width)
        )
        # S7 AC5: bound the (possibly expanded) panel's height to the
        # terminal's actual rows so a long expanded plan can never grow the
        # bottom strip enough to push the composer/footer off-screen —
        # recomputed here so a resize re-fits it like the width does.
        app.plan_panel.styles.max_height = plan_panel_max_height(app.size.height)
        app.plan_panel.show_panel()
    else:
        app.plan_panel.hide_panel()
    app.refresh_status()  # footer carries the fallback count (Task 5)


EVIDENCE_PANEL_MIN_WIDTH = 80
"""Below this terminal width the evidence detail side panel collapses
(compliance item D7, AC4) — a docked sidebar plus a still-usable
transcript needs more room than a bare 40-col minimum; 80 matches the
narrowest golden width the transcript renderer itself is pinned to
(tests/goldens), so the panel never claims space the transcript can't
spare."""


def sync_evidence_panel(app: TuiApp, width: int) -> None:
    """One decision point for the evidence panel's responsive collapse
    (D7 AC4) — mirrors :func:`sync_plan_surfaces` (D2). Called on every
    terminal resize; the open/close/refresh decision itself is made where
    ``OpenEvidenceDetail`` is handled, not here.

    *width* is the resize event's OWN carried size (``event.size.width``),
    not ``app.size.width`` — empirically, ``app.size`` has not always
    settled to the new value at the point ``on_resize`` runs, while the
    event's own field is authoritative immediately (the same reason
    ``on_resize`` already feeds ``event.size.width`` to
    ``adapter.terminal.set_cols`` rather than reading ``app.size``).
    """
    app.evidence_panel.sync_width(width, min_width=EVIDENCE_PANEL_MIN_WIDTH)


def plan_footer_counts(app: TuiApp) -> tuple[int, int]:
    """``(done, total)`` fallback when a plan exists but no panel is visible."""
    if not app.plan_items or app.plan_panel.display:
        return (0, 0)
    return plan_counts(app.plan_items)


def footer_state(app: TuiApp) -> FooterState:
    """One frozen footer snapshot from the app's current interaction state."""
    done, total = plan_footer_counts(app)
    # Live context readout (donor sidebar-context parity): context tokens
    # used + true % of the real window, sourced from the app's own
    # ContextUsage. Once native compaction fires this is the provider-derived
    # budget and root request-view occupancy; before then it is the configured
    # fallback estimate. Shown only once real usage exists.
    usage = app.context_usage()
    context_tokens = usage.used or None
    context_pct = usage.used_pct if usage.used > 0 else None
    return FooterState(
        mode_id=app.mode_id,  # type: ignore[arg-type]
        gated_auto=app.adapter.gated_auto,
        native_modes=app.native_modes,
        # No bundle here (item D4): the TitleBar (chrome.py) is the one
        # persistent place the active bundle renders — see footer.py's
        # module docstring for the consolidation this seam landed.
        # The adapter may carry a provider-qualified id ("anthropic/x");
        # the footer speaks human and shows the bare model name (story #4).
        model=app.adapter.model_name.rpartition("/")[2],
        effort=app.current_effort,
        session_short=app.adapter.session_short,
        cost=max(Decimal("0"), app.reducer.live_session_cost),
        cost_estimated=app.reducer.live_cost_estimated,
        context_pct=context_pct,
        context_tokens=context_tokens,
        shipped=app.ledger.last_shipped,
        queued=len(app.adapter.steering.pending_next_turn),
        waiting=app.adapter.needs_you.pending_count,
        context=app.footer_context(),
        kitty_protocol=app.kitty_protocol,
        plan_done=done,
        plan_total=total,
        # Finding 1 (post-merge audit): S1 AC1 x D4 AC2/AC3 reconciliation --
        # see footer.FooterState.rewind_available. Checkpoints existing is
        # the SAME test open_rewind_strip already uses to decide whether
        # ctrl-r has anything to do ("no rewind checkpoints yet"); the
        # picker already being open is excluded so the hint never doubles
        # up on the rewind strip's own header, which already advertises
        # the equivalent affordance while it owns the screen.
        rewind_available=bool(app.ledger.checkpoints) and not app.rewind.display,
    )


__all__ = [
    "APPROVAL_NOTICE",
    "EVIDENCE_PANEL_MIN_WIDTH",
    "EscSequence",
    "LANE_FOCUS_INTRO_NOTICE",
    "PLAN_PANEL_MIN_WIDTH",
    "QUEUED_NOTICE",
    "STEER_NOTICE",
    "announce_ready",
    "apply_decision",
    "apply_plan_change",
    "sync_evidence_panel",
    "confirm_fork",
    "confirm_restore",
    "echo_lane_steer",
    "echo_steer",
    "finish_turn_queues",
    "footer_state",
    "global_bindings",
    "go_back_to_parent",
    "handle_esc",
    "handle_fork",
    "handle_restore",
    "handle_lane_focus_change",
    "mount_approval",
    "needs_you_block",
    "needs_you_display_question",
    "permissions_block",
    "plan_footer_counts",
    "sync_plan_surfaces",
    "sync_steer_echoes",
    "trim_after_checkpoint",
    "trim_from_checkpoint",
]
