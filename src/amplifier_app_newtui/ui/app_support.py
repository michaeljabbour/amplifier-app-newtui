"""Composition-root helpers kept out of ``ui/app.py`` (<500-line budget).

Pure-ish functions the app delegates to: keymap-sourced global bindings,
block builders for the needs-you list and the /permissions surface,
transcript trimming after a confirmed fork, esc-chain resolution and the
footer-state snapshot. Everything here operates on the app's public
surface — no hidden state.
"""

from __future__ import annotations

import asyncio
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
from ..model.queues import NeedsYouItem
from . import keymap
from .footer import FooterState
from .plan_panel import plan_counts
from .transcript import TranscriptView

if TYPE_CHECKING:
    from .app import NewTuiApp

STEER_NOTICE = "steer queued · shift+enter queues a full next-turn message"
STEER_NOTICE_LEGACY = "steer queued · alt+enter queues a full next-turn message"
STEER_DISCARDED_NOTICE = "steer not applied · discarded at turn end"
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


@dataclass
class EscSequence:
    """The small state machine behind interrupt-then-backtrack.

    Only an Esc that actually targets a running turn arms the sequence.
    Panel-close and approval Esc presses therefore cannot accidentally open
    rewind.  The second press may land before or just after turn close-out.
    """

    interrupted_at: float | None = None

    def arm_interrupt(self, now: float) -> None:
        self.interrupted_at = now

    def consume_backtrack(self, now: float) -> bool:
        interrupted_at = self.interrupted_at
        self.interrupted_at = None
        return (
            interrupted_at is not None
            and 0 <= now - interrupted_at <= keymap.ESC_BACKTRACK_WINDOW_SECONDS
        )

    def reset(self) -> None:
        self.interrupted_at = None


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
    bindings.append(
        Binding("ctrl+c,super+c", "copy_selection", "copy", show=False, priority=True)
    )
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
        Segment(
            text="  path policy · allowed roots + protected paths enforced\n",
            style_token="dim",
        ),
    ]
    spans.extend(
        Segment(text=f"  {slot.label()}\n", style_token="fg") for slot in surface.slots()
    )
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


def announce_ready(app: NewTuiApp) -> None:
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
    # fresh session — replay the stored conversation (prompts + prose;
    # tool traffic skipped) so scrollback matches what the model knows.
    from .live_tail import answer_spans

    app.composer.seed_history(
        text for role, text in app.adapter.restored_history if role == "user"
    )
    for role, text in app.adapter.restored_history:
        if role == "user":
            app.append_block(
                UserLine(id=app.allocator.next_id(), text=text, mode=app.mode_id)
            )
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


def announce_boot_failure(app: NewTuiApp, error: Exception) -> None:
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
        "Check provider setup with `amplifier-newtui doctor`, or run "
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
    app.composer.display = False
    await app.query_one("#composer-slot", Container).mount(bar)
    # Publish the bar only once it is fully mounted. Callers use non-None as
    # the ready signal; exposing it before this await raced the focus/notice
    # setup and made approval presentation observably half-initialized.
    app.approval_bar = bar
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
    # Advertise the queue chord the terminal can actually deliver
    # (README/§12: alt+enter is the legacy fallback).
    app.show_notice(STEER_NOTICE if app.kitty_protocol else STEER_NOTICE_LEGACY)


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
    # Discard leftovers (ADR-0007: an unconsumed steer must not become a
    # turn the user never sent) — but say so; silent loss of typed input
    # reads as a bug. The listener drops the ↳ echoes.
    if app.adapter.steering.drain_steers():
        app.show_notice(STEER_DISCARDED_NOTICE)
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
    if app.fork_pending:
        return  # one fork at a time — a second Enter must not double-fork
    app.fork_pending = True
    app.run_worker(confirm_fork(app, checkpoint.id, checkpoint.label), exclusive=False)


async def confirm_fork(app: NewTuiApp, checkpoint_id: str, label: str) -> None:
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
        app.show_notice(f"forked from {checkpoint_id} · {label}")
        app.composer.focus_input()
        app.refresh_status()
    finally:
        app.fork_pending = False
        # Deferred turn-end queue duties (see docstring): the queued
        # next-turn message now picks up against the post-fork context.
        app.drain_turn_queues()


def apply_decision(app: NewTuiApp, decision_id: str, answer: str) -> None:
    """Act on a deferred decision: answer it + log ``Applying decision``.

    Scrollback is append-only (mockup §7): the Needs-you listing stays in
    the transcript; only the footer badge clears and the narration lands
    after it.
    """
    from .needs_you import applying_decision_line

    try:
        item = app.adapter.needs_you.answer(decision_id, answer)
    except (KeyError, ValueError) as error:
        app.show_notice(str(error))
        return
    narration = app.adapter.decision_narration(answer) or applying_decision_line(answer)
    # Mockup logs the applied decision as a narration line: bright "● "
    # marker + fg text (design-v3-cohesive.html:289).
    app.append_block(Narration(id=app.allocator.next_id(), text=narration))
    # The denied ACTION is the /improve join key (DenialLog counts by
    # action); the chip label is only the fallback for actionless items.
    app.journal.record_override(item.action or answer)
    app.refresh_status()


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
                command, input=text.encode("utf-8"), timeout=5, check=True,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return True
        except Exception:  # noqa: BLE001 — clipboard is best-effort
            continue
    return False


def native_modes_segments(catalog: object) -> tuple[Segment, ...]:
    """Render the mode tool's catalog output grouped by source bundle.

    The mounted mode tool reports ``{"modes": [{name, description,
    source}, …]}`` — dynamically composed (superpowers, modes, llm-wiki,
    …), so this formats whatever arrives rather than any fixed list.
    Non-mapping payloads fall back to plain text.
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
    width = max(len(str(m.get("name", ""))) for m in modes)
    for source in sorted(by_source):
        segments.append(Segment(text=f"  {source or 'bundle'}\n", style_token="dimmer"))
        for mode in sorted(by_source[source], key=lambda m: str(m.get("name", ""))):
            name = str(mode.get("name", ""))
            desc = str(mode.get("description", "")).split("\n")[0][:90]
            segments.append(Segment(text=f"    {name.ljust(width)}  ", style_token="teal"))
            segments.append(Segment(text=f"{desc}\n", style_token="dim"))
    segments.append(
        Segment(text="  /mode <name> activates · /mode off clears", style_token="dimmer")
    )
    return tuple(segments)


def handle_esc(app: NewTuiApp, *, now: float | None = None) -> None:
    """Resolve Esc priority plus interrupt-then-backtrack (spec §5)."""
    pressed_at = monotonic() if now is None else now
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


PLAN_PANEL_MIN_WIDTH = 90
"""Below this terminal width the plan panel yields; a ``Plan N/M`` count
falls back to the footer (design D2 responsive ladder)."""


def apply_plan_change(app: NewTuiApp, items: tuple[TodoItem, ...]) -> None:
    """Reducer pushed a new root todo list — repaint the ambient surfaces."""
    app.plan_items = tuple(items)
    sync_plan_surfaces(app)


def sync_plan_surfaces(app: NewTuiApp) -> None:
    """One decision point for the plan's responsive ladder (D2).

    Wide (≥ 90 cols) with todos → the bottom-strip panel; otherwise the
    panel hides and the footer carries the count (Task 5). Called on
    every plan change and on terminal resize.
    """
    app.plan_panel.update_plan(app.plan_items)
    if app.plan_items and app.size.width >= PLAN_PANEL_MIN_WIDTH:
        app.plan_panel.show_panel()
    else:
        app.plan_panel.hide_panel()
    app.refresh_status()  # footer carries the fallback count (Task 5)


def plan_footer_counts(app: NewTuiApp) -> tuple[int, int]:
    """``(done, total)`` for the footer — (0, 0) unless the panel is hidden
    while todos exist (the count never shows twice; design D2)."""
    if not app.plan_items or app.plan_panel.display:
        return (0, 0)
    return plan_counts(app.plan_items)


def footer_state(app: NewTuiApp) -> FooterState:
    """One frozen footer snapshot from the app's current interaction state."""
    done, total = plan_footer_counts(app)
    return FooterState(
        mode_id=app.mode_id,  # type: ignore[arg-type]
        bundle=app.adapter.bundle_name,
        session_short=app.adapter.session_short,
        cost=max(Decimal("0"), app.reducer.live_session_cost),
        cost_estimated=app.reducer.live_cost_estimated,
        shipped=app.ledger.last_shipped,
        queued=len(app.adapter.steering.pending_next_turn),
        waiting=app.adapter.needs_you.pending_count,
        context=app.footer_context(),
        kitty_protocol=app.kitty_protocol,
        plan_done=done,
        plan_total=total,
    )


__all__ = [
    "APPROVAL_NOTICE",
    "EscSequence",
    "PLAN_PANEL_MIN_WIDTH",
    "QUEUED_NOTICE",
    "STEER_NOTICE",
    "announce_ready",
    "apply_decision",
    "apply_plan_change",
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
    "plan_footer_counts",
    "sync_plan_surfaces",
    "sync_steer_echoes",
    "trim_after_checkpoint",
]
