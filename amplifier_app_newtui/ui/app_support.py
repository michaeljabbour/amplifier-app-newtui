"""Composition-root helpers kept out of ``ui/app.py`` (<500-line budget).

Pure-ish functions the app delegates to: keymap-sourced global bindings,
block builders for the needs-you list and the /permissions surface,
transcript trimming after a confirmed fork, esc-chain resolution and the
footer-state snapshot. Everything here operates on the app's public
surface — no hidden state.
"""

from __future__ import annotations

from decimal import Decimal
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
)
from ..model.queues import NeedsYouItem
from . import keymap
from .footer import FooterState
from .transcript import TranscriptView

if TYPE_CHECKING:
    from .app import NewTuiApp

STEER_NOTICE = "steer queued · shift+enter queues a full next-turn message"
QUEUED_NOTICE = "message queued · runs as the next turn"
APPROVAL_NOTICE = "approval required · choose below the transcript"
APPROVAL_NOTICE_DURATION = 6.0
"""Approval notices linger 6s, not the 4s default (mockup requestApproval)."""

_GLOBAL_ACTIONS = frozenset(
    {
        "cycle_mode",
        "cycle_permission",
        "toggle_lanes",
        "show_ledger",
        "show_needs_you",
        "open_rewind",
    }
)


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
    return bindings


def needs_you_block(
    pending: tuple[NeedsYouItem, ...], allocator: BlockIdAllocator
) -> NeedsYouBlock | None:
    """The ``Needs you`` transcript block for the pending decisions (§7)."""
    if not pending:
        return None
    entries = tuple(
        NeedsYouEntry(
            decision_id=item.decision_id,
            question=item.question,
            reason=item.reason,
            choices=tuple(NeedsYouChoice(label=c, answer=c) for c in item.choices),
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
    ]
    spans.extend(
        Segment(text=f"  {slot.label}\n", style_token="fg") for slot in surface.slots()
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


def announce_ready(app: NewTuiApp) -> None:
    """Session banner + any degraded-start notices once identity is known."""
    # Resume offset for checkpoint turn ids (spec §9): known only after
    # the adapter booted, before the first turn event can arrive.
    app.reducer.turn_base = app.adapter.turn_base
    headline, detail = app.adapter.banner
    if headline or detail:
        app.append_block(
            SessionBanner(id=app.allocator.next_id(), headline=headline, detail=detail)
        )
    for notice in app.adapter.startup_notices:
        app.append_block(
            Answer(
                id=app.allocator.next_id(),
                spans=(Segment(text=notice, style_token="orange", bold=True),),
            )
        )
    app.refresh_status()


async def mount_approval(
    app: NewTuiApp, ticket_id: str, prompt: str, options: tuple[str, ...]
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
    app.approval_bar = bar
    app.composer.display = False
    await app.query_one("#composer-slot", Container).mount(bar)
    bar.focus()
    app.show_notice(APPROVAL_NOTICE, duration=APPROVAL_NOTICE_DURATION)
    if lane_was_focused:
        app.show_notice(
            "back to parent · approval required", duration=APPROVAL_NOTICE_DURATION
        )
    app.refresh_status()


def echo_steer(app: NewTuiApp, text: str) -> None:
    """Queue a mid-turn steer and stamp its ↳ echo + notice (spec §5)."""
    queued = app.adapter.steering.enqueue(text, kind="steer")
    echo = SteerEcho(id=app.allocator.next_id(), text=text)
    app.steer_echoes[queued.message_id] = echo.id
    app.append_block(echo)
    app.show_notice(STEER_NOTICE)


def handle_lane_focus_change(app: NewTuiApp, lane_id: str | None) -> None:
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


def sync_steer_echoes(app: NewTuiApp) -> None:
    """Drop the ↳ echo of any steer no longer pending (spec §5).

    Steering-queue listener: a steer leaves the queue either when the
    runtime consumes it at a step boundary (``Applying steer: …``) or
    when it is discarded at turn end — both remove the echo.
    """
    pending = {m.message_id for m in app.adapter.steering.pending_steers}
    for message_id in [m for m in app.steer_echoes if m not in pending]:
        app.remove_block(app.steer_echoes.pop(message_id))


def finish_turn_queues(app: NewTuiApp) -> None:
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
    app.adapter.steering.drain_steers()  # discard; the listener drops the ↳ echoes
    queued = app.adapter.steering.consume_next_turn_message()
    if queued is not None:
        app.show_notice("queued message picked up")
        # submit_queued, not submit: a drained turn emits no mode notice
        # (mockup drainQueue has no setMode), so the pickup notice stays.
        app.run_worker(app.adapter.submit_queued(queued.text), exclusive=False)
    remaining = app.adapter.steering.pending_next_turn
    if remaining:
        app.queued_strip.show_queued(remaining[0].text)
    else:
        app.queued_strip.clear_queued()


def handle_fork(app: NewTuiApp, checkpoint_id: str) -> None:
    """Rewind fork: backend confirms FIRST, then trim (ADR-0007 §Rewind)."""
    checkpoint = app.ledger.checkpoint_by_id(checkpoint_id)
    if checkpoint is None:
        app.show_notice(f"unknown checkpoint · {checkpoint_id}")
        return
    app.run_worker(confirm_fork(app, checkpoint.id, checkpoint.label), exclusive=False)


async def confirm_fork(app: NewTuiApp, checkpoint_id: str, label: str) -> None:
    """Request the session fork from the runtime; trim only on success.

    The adapter's ``fork`` performs the backend fork (foundation
    ``fork_session_in_memory`` + ``context.set_messages()`` for a live
    real session; immediate for the in-memory demo script) and trims
    the ledger once confirmed. Only then does the transcript trim —
    confirm-then-trim: a failed fork leaves everything untouched.
    """
    from ..kernel.rewind import RewindError

    try:
        await app.adapter.fork(checkpoint_id, app.ledger)
    except RewindError as error:
        app.show_notice(f"fork failed · {error}")
        return
    trim_after_checkpoint(app.transcript, checkpoint_id)
    app.show_notice(f"forked from {checkpoint_id} · {label}")
    app.composer.focus_input()
    app.refresh_status()


def apply_decision(app: NewTuiApp, decision_id: str, answer: str) -> None:
    """Act on a deferred decision: answer it + log ``Applying decision``.

    Scrollback is append-only (mockup §7): the Needs-you listing stays in
    the transcript; only the footer badge clears and the narration lands
    after it.
    """
    from .needs_you import applying_decision_line

    try:
        app.adapter.needs_you.answer(decision_id, answer)
    except (KeyError, ValueError) as error:
        app.show_notice(str(error))
        return
    narration = app.adapter.decision_narration(answer) or applying_decision_line(answer)
    # Mockup logs the applied decision as a narration line: bright "● "
    # marker + fg text (design-v3-cohesive.html:289).
    app.append_block(Narration(id=app.allocator.next_id(), text=narration))
    app.journal.record_override(answer)
    app.refresh_status()


def handle_esc(app: NewTuiApp) -> None:
    """Resolve one Esc press via ``keymap.ESC_CHAIN`` (spec §5 table)."""
    checks = {
        "lane_focus": lambda: app.transcript.focused_lane is not None,
        # Mockup Escape: ``if (this.palFilter !== null)`` — ANY live slash
        # filter consumes the Esc, even a zero-match one whose strip is
        # hidden, so typed "/…" text never falls through to interrupt.
        "palette": lambda: app.palette.filter_text is not None,
        "rewind": lambda: bool(app.rewind.display),
        "lanes": lambda: bool(app.lanes_panel.display),
        "running": lambda: app.turn_active,
    }
    actions = {
        "lane_unfocus": lambda: app.run_worker(app.transcript.restore_main(), exclusive=False),
        "close_palette": app.close_palette,
        "close_rewind": app.rewind.close_strip,
        "close_lanes": app.lanes_panel.action_close,
        "interrupt_running": app.interrupt_turn,
    }
    for context, action in keymap.ESC_CHAIN:
        if checks[context]():
            actions[action]()
            return


def footer_state(app: NewTuiApp) -> FooterState:
    """One frozen footer snapshot from the app's current interaction state."""
    return FooterState(
        mode_id=app.mode_id,  # type: ignore[arg-type]
        bundle=app.adapter.bundle_name,
        session_short=app.adapter.session_short,
        cost=max(Decimal("0"), app.reducer.session_cost),
        shipped=app.ledger.last_shipped,
        queued=len(app.adapter.steering.pending_next_turn),
        waiting=app.adapter.needs_you.pending_count,
        context=app.footer_context(),
        kitty_protocol=app.kitty_protocol,
    )


__all__ = [
    "APPROVAL_NOTICE",
    "QUEUED_NOTICE",
    "STEER_NOTICE",
    "announce_ready",
    "apply_decision",
    "confirm_fork",
    "echo_steer",
    "finish_turn_queues",
    "footer_state",
    "global_bindings",
    "handle_esc",
    "handle_fork",
    "handle_lane_focus_change",
    "mount_approval",
    "needs_you_block",
    "permissions_block",
    "sync_steer_echoes",
    "trim_after_checkpoint",
]
