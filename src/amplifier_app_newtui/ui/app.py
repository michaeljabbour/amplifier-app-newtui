"""The composition root: NewTuiApp (ADR-0007, <500 lines, no mixins).

Layout (DESIGN-SPEC §2, top → bottom): TitleBar / TranscriptView /
LiveTail / NoticeSlot / overlay strips (palette · lanes · rewind ·
queued) / composer-or-approval-bar / FooterBar. The app consumes the
runtime adapter's ``asyncio.Queue[UIEvent]`` through
:class:`~amplifier_app_newtui.ui.reducer.TranscriptReducer` and owns
only interaction state (running, mode, palette filter, open strips,
focused lane, queued message, approval head); widgets own their own
state and talk back via Textual messages.

Esc precedence (DESIGN-SPEC §5, resolved via ``keymap.ESC_CHAIN`` in
:func:`~amplifier_app_newtui.ui.app_support.handle_esc` — never ad-hoc
ladders). The approval bar owns the keyboard while open, so it sits
outside the chain:

    ============  =====================  ==============================
    priority      context (active when)  action
    ============  =====================  ==============================
    1             lane_focus             restore the parent transcript
    2             palette                close the command palette
    3             rewind                 close the rewind picker strip
    4             lanes                  close the agent-lanes panel
    5             running                interrupt the running turn
    ============  =====================  ==============================
"""

from __future__ import annotations

import threading
import time
from typing import Any

from textual import events
from textual.app import App, ComposeResult
from textual.containers import Container

from ..commands.builtin import build_registry
from ..commands.context import ContextUsage
from ..commands.improve import ApprovalJournal
from ..commands.permissions import PermissionSurface
from ..model.blocks import (
    Answer,
    BlockIdAllocator,
    EvidenceBlock,
    Segment,
    TranscriptBlock,
    UserLine,
)
from ..model.lanes import LaneRegistry
from ..model.modes import DEFAULT_MODE, ModeProfile, cycle_mode, get_mode
from ..model.turn import OutcomeLedger
from . import app_support, keymap
from .approval_bar import ApprovalBar
from .chrome import TitleBar
from .command_context import AppCommandContext
from .composer import Composer
from .footer import FooterBar
from .lanes_panel import LanesPanel
from .live_tail import LiveTail
from .needs_you import NeedsYouList
from .notices import NoticeSlot
from .palette import PaletteStrip
from .queued_strip import QueuedStrip
from .reducer import TranscriptReducer
from .rewind_strip import RewindStrip
from .runtime_adapter import RuntimeAdapter
from .themes import DEFAULT_THEME, THEME_NAME_PREFIX, THEME_TOKENS, register_themes, theme_id
from .transcript import (
    BlockWidget,
    CloseEvidence,
    ExpandEvidenceClaim,
    LaneFocusChanged,
    OpenRewind,
    ShowEvidence,
    TranscriptView,
)


class NewTuiApp(App[None]):
    """The Amplifier full-screen TUI (v3 Cohesive)."""

    CSS = """
    Screen { background: $bg-term; }
    /* The notice floats on its own layer over the bottom-right of the
       region's last row (mockup: absolute overlay in a height-0
       container, right: 18px) so showing or hiding it never resizes the
       transcript and blanks only its own box. `align` applies per layer;
       the base layer (transcript 1fr + live tail) always fills the
       region exactly, so only the auto-width notice moves. */
    #transcript-region { height: 1fr; layers: base notice; align: right bottom; }
    #transcript { height: 1fr; padding: 0 1; }
    /* Scrollbar colors from the §1 tokens only (never Textual-derived);
       set here (not widget DEFAULT_CSS) so the token variables are
       guaranteed registered before the stylesheet parses. */
    #transcript {
        scrollbar-color: $rule;
        scrollbar-color-hover: $dim;
        scrollbar-color-active: $dim;
        scrollbar-background: $bg-term;
        scrollbar-background-hover: $bg-term;
        scrollbar-background-active: $bg-term;
    }
    #live-tail { padding: 0 1; }
    #composer-slot { height: auto; }
    """

    BINDINGS = app_support.global_bindings()

    def __init__(self, adapter: RuntimeAdapter, *, kitty_protocol: bool = True) -> None:
        super().__init__()
        register_themes(self)  # before first stylesheet parse (NOTES: chrome)
        self.theme = theme_id(DEFAULT_THEME)
        keymap.validate()
        self.adapter = adapter
        self.kitty_protocol = kitty_protocol
        self.allocator = BlockIdAllocator()
        self.ledger = OutcomeLedger()
        self.lanes = LaneRegistry()
        self.journal = ApprovalJournal()
        self.permissions = PermissionSurface()
        self._mode: ModeProfile = get_mode(DEFAULT_MODE)
        self._commands = build_registry()
        self._ctx = AppCommandContext(self)
        self.reducer = TranscriptReducer(
            self,
            allocator=self.allocator,
            ledger=self.ledger,
            lanes=self.lanes,
            spec_lookup=adapter.turn_spec,
            lane_seed_lookup=adapter.lane_seed,
            evidence_lookup=adapter.evidence_links,
            session_cost_start=adapter.session_cost_start,
        )
        self.turn_active = False
        self.fork_pending = False  # a confirmed fork is in flight (interrupt-then-fork)
        self._working_timer: Any = None  # 1s working-line heartbeat (Timer)
        self._boot_block_id: str | None = None  # boot-progress transcript line
        self._auto_native_mode: str | None = None  # posture-bridged native mode
        self._os_clipboard_copied = False  # last copy reached an OS clipboard tool
        self._selection_timer: Any = None  # copy-on-select debounce
        self._last_selection_copied = ""  # suppress duplicate auto-copies
        self._turn_queues_pending = False  # drain queues once end-of-turn events settle
        self.approval_bar: ApprovalBar | None = None
        self.steer_echoes: dict[str, str] = {}  # steer message_id → ↳ echo block id
        self._lanes_fanout_open = False  # active-lane edge for the auto-open
        self.title_bar = TitleBar(id="title-bar")
        self.transcript = TranscriptView(id="transcript")
        self.live_tail = LiveTail(id="live-tail")
        self.notice_slot = NoticeSlot(id="notice-slot")
        self.palette = PaletteStrip(self._commands.specs, id="palette-strip")
        self.lanes_panel = LanesPanel(id="lanes-panel")
        self.rewind = RewindStrip(id="rewind-strip")
        self.queued_strip = QueuedStrip(id="queued-strip")
        self.composer = Composer(kitty_protocol=kitty_protocol, id="composer")
        self.footer_bar = FooterBar(id="footer-bar")

    def compose(self) -> ComposeResult:
        yield self.title_bar
        with Container(id="transcript-region"):
            yield self.transcript
            yield self.live_tail
            yield self.notice_slot
        yield self.palette
        yield self.lanes_panel
        yield self.rewind
        yield self.queued_strip
        with Container(id="composer-slot"):
            yield self.composer
        yield self.footer_bar

    def on_mount(self) -> None:
        # Safety net: any mounted module that print()s raw ANSI under the
        # full-screen TUI would corrupt the Textual screen (found live —
        # a streaming-ui hook blanked the whole turn). Stray prints are
        # captured into the app log instead.
        self.begin_capture_print(self)
        self.composer.focus_input()
        self._ui_thread_id = threading.get_ident()
        self.adapter.steering.add_listener(self._on_steering_changed)
        self.refresh_status()
        self.run_worker(self._consume_events(), exclusive=False)
        self.run_worker(self._boot_runtime(), exclusive=False)
        # Copy-on-select (tmux-style): the ⌘C reflex never reaches a
        # terminal app, so a settled drag-selection lands on the clipboard
        # by itself — select, then paste anywhere. ctrl+c stays as the
        # explicit path (composer selections, re-copy).
        self.watch(self.screen, "selections", self._selection_changed, init=False)

    def _selection_changed(self) -> None:
        if self._selection_timer is not None:
            self._selection_timer.stop()
        self._selection_timer = self.set_timer(0.4, self._copy_settled_selection)

    def _copy_settled_selection(self) -> None:
        self._selection_timer = None
        text = self.screen.get_selected_text()
        if not text or text == self._last_selection_copied:
            return
        self._last_selection_copied = text
        self.copy_to_clipboard(text)
        self.show_notice(f"copied on select · {len(text)} chars")

    def on_unmount(self) -> None:
        shutdown = getattr(self.adapter, "shutdown", None)
        if callable(shutdown):
            shutdown()  # stop the runtime thread (real sessions)

    def on_print(self, event: events.Print) -> None:
        if text := event.text.strip():
            self.log(f"captured print: {text[:200]}")

    def _on_steering_changed(self) -> None:
        # A real runtime consumes steers on ITS thread (step-boundary
        # bridge); widget work must hop back to the UI thread.
        if threading.get_ident() == self._ui_thread_id:
            app_support.sync_steer_echoes(self)
        else:
            self.call_from_thread(app_support.sync_steer_echoes, self)


    async def _boot_runtime(self) -> None:
        self.adapter.attach(self)
        await self.adapter.start(lambda: app_support.announce_ready(self))

    async def _consume_events(self) -> None:
        while True:
            event = await self.adapter.queue.get()
            try:
                self.reducer.handle(event)
            except Exception:  # noqa: BLE001 — the render loop must survive bad events
                self.log.error(f"reducer failed on {event.kind}")
            if self.adapter.queue.empty() and not self.fork_pending:
                # Queue duties run once the runtime's end-of-turn burst is
                # reduced, so the ``queued message picked up`` notice lands
                # AFTER the end notice (mockup drainQueue order) and stays.
                # During an interrupt-then-fork the drain is deferred to
                # ``confirm_fork`` — a queued next-turn prompt must not be
                # auto-run (and trimmed away) against the pre-fork context.
                self.drain_turn_queues()
            self._refresh_title()

    def drain_turn_queues(self) -> None:
        """Run the deferred turn-end queue duties once (idempotent)."""
        if not self._turn_queues_pending:
            return
        self._turn_queues_pending = False
        app_support.finish_turn_queues(self)

    def submit_prompt(self, text: str) -> None:
        if self._boot_block_id is not None:
            # Mid-boot submits used to vanish silently (the runtime isn't
            # up yet) — keep the supervisor's words instead of eating them.
            self.composer.insert_text(text)
            self.show_notice("session still starting · message kept in the composer")
            return
        self.run_worker(self.adapter.submit(text), exclusive=False)

    # -- ReducerHost ---------------------------------------------------------------

    @property
    def mode_id(self) -> str:
        return self._mode.id

    def append_block(self, block: TranscriptBlock) -> None:
        self.transcript.append(block)

    def replace_block(self, block: TranscriptBlock) -> None:
        try:
            self.transcript.replace(block)
        except KeyError:
            self.transcript.append(block)

    def remove_block(self, block_id: str) -> None:
        try:
            self.transcript.remove_block(block_id)
        except KeyError:
            pass

    def show_notice(self, text: str, duration: float | None = None) -> None:
        self.notice_slot.show_notice(text, duration)

    def set_mode_by_id(self, mode_id: str, *, notify: bool = True) -> None:
        self._mode = get_mode(mode_id)
        self.permissions.set_mode(self._mode.id)
        self.composer.set_mode(self._mode)
        if notify:
            self.show_notice(self._mode.notice())
        # Action through amplifier-foundation (user directive): a posture
        # with a same-named bundle-composed mode activates it natively —
        # kernel-side gating and per-turn context come from hooks-mode,
        # not this app. Postures without a native twin clear only what
        # this bridge itself activated (an explicitly chosen native mode
        # is never clobbered).
        self.run_worker(self._sync_native_mode(mode_id), exclusive=False)
        self.refresh_status()

    _NATIVE_POSTURES = frozenset({"plan", "brainstorm"})

    async def _sync_native_mode(self, mode_id: str) -> None:
        if mode_id in self._NATIVE_POSTURES:
            ok, _detail = await self.adapter.set_native_mode(mode_id)
            if ok:
                self._auto_native_mode = mode_id
        elif self._auto_native_mode is not None:
            await self.adapter.set_native_mode(None)
            self._auto_native_mode = None

    def show_native_modes(self) -> None:
        """``/modes``: the bundle-composed catalog + this app's postures."""
        self.run_worker(self._show_native_modes(), exclusive=False)

    async def _show_native_modes(self) -> None:
        if self._boot_block_id is not None:
            self.show_notice("session still starting · /modes once the banner lands")
            return
        catalog = await self.adapter.list_native_modes()
        spans = [
            Segment(text="· ", style_token="blue"),
            Segment(text="Modes", style_token="bright", bold=True),
            Segment(
                text="  postures: chat plan brainstorm build auto · shift+tab cycles"
                " · trust layer\n",
                style_token="dim",
            ),
        ]
        native = app_support.native_modes_segments(catalog) if catalog else ()
        if native:
            spans.extend(native)
        else:
            spans.append(
                Segment(
                    text="  no bundle-composed modes (demo or minimal session)",
                    style_token="dimmer",
                )
            )
        self.append_block(Answer(id=self.allocator.next_id(), spans=tuple(spans)))

    def activate_native_mode(self, name: str | None) -> None:
        """``/mode <bundle-mode>`` / ``/mode off``: native activation."""
        self.run_worker(self._activate_native_mode(name), exclusive=False)

    async def _activate_native_mode(self, name: str | None) -> None:
        ok, detail = await self.adapter.set_native_mode(name)
        if ok:
            self._auto_native_mode = None  # explicit choice — never auto-cleared
            label = name or "off"
            self.show_notice(f"mode {label} · native (bundle)")
        else:
            self.show_notice(detail or f"no such mode · {name}")

    def turn_started(self) -> None:
        self.turn_active = True
        self.composer.running = True
        self.title_bar.running = True
        # 1s heartbeat: pulse the working line's spinner and (real turns)
        # its seconds counter — usage events alone froze it during long
        # provider calls (supervisor feedback, spec §3/§11).
        if self._working_timer is None:
            self._working_timer = self.set_interval(
                1.0, lambda: self.reducer.tick(time.time())
            )
        self.refresh_status()

    def turn_finished(self) -> None:
        self.turn_active = False
        self.composer.running = False
        self.title_bar.running = False
        if self._working_timer is not None:
            self._working_timer.stop()
            self._working_timer = None
        self._turn_queues_pending = True  # drained in _consume_events (§5)
        # Mockup openRewind/rewindNext read the live this.checkpoints
        # array — a checkpoint cut while the picker is open is
        # immediately navigable with › (spec §9).
        self.rewind.sync_checkpoints(self.ledger.checkpoints)
        self.refresh_status()

    def lanes_changed(self) -> None:
        self.lanes_panel.update_lanes(self.lanes.lanes)
        active = self.lanes.active_count > 0
        if active and not self._lanes_fanout_open and not self.lanes_panel.display:
            # Mockup runAgentsTurn: the panel opens automatically at fan-out.
            # Display only — the composer keeps focus (type to steer).
            self.lanes_panel.display = True
            self._refresh_footer()
        self._lanes_fanout_open = active
        self._refresh_title()

    def approval_opened(self, prompt: str, options: tuple[str, ...]) -> None:
        del prompt, options  # presentation runs via present_approval
        self._refresh_footer()

    def decision_deferred(self, message: str) -> None:
        question, reason, choices, highlight, action = self.adapter.deferred_decision(
            message
        )
        self.adapter.needs_you.defer(
            question, reason, choices=choices, highlight=highlight, action=action
        )
        self._refresh_footer()

    def stream_opened(self, block_type: str) -> None:
        self.transcript.set_streaming(True)
        self.live_tail.open_stream(block_type)

    def stream_delta(self, text: str) -> None:
        self.live_tail.feed(text)

    def stream_closed(self) -> None:
        # Durable text arrives on Channel B; the tail's consolidation
        # artifact is discarded (never reconstruct one channel from the other).
        self.live_tail.consolidate(self.allocator.next_id())
        self.transcript.set_streaming(False)

    def on_live_tail_consolidated(self, message: LiveTail.Consolidated) -> None:
        message.stop()  # durable record path owns the transcript append

    # -- approvals -------------------------------------------------------------------

    def boot_progress(self, action: str, detail: str) -> None:
        """Live boot feedback: one self-updating dim line in the transcript.

        Module prepare can run for minutes on a cold cache; the
        supervisor sees each phase ('preparing · newtui', foundation's
        per-module install messages, 'creating · session') instead of a
        blank screen. Removed by ``announce_ready``.
        """
        action = action.replace("_", " ")  # foundation emits snake_case phases
        spans = (
            Segment(text="✳ ", style_token="orange"),
            Segment(text=f"{action} · {detail}" if detail else action, style_token="dim"),
        )
        block = Answer(
            id=self._boot_block_id or self.allocator.next_id(),
            spans=spans,
            clickable=False,
        )
        if self._boot_block_id is None:
            self._boot_block_id = block.id
            self.append_block(block)
        else:
            self.replace_block(block)

    def clear_boot_progress(self) -> None:
        if self._boot_block_id is not None:
            self.remove_block(self._boot_block_id)
            self._boot_block_id = None

    def present_approval(self, ticket_id: str, prompt: str, options: tuple[str, ...]) -> None:
        """Show the inline approval bar for one ticket (spec §7)."""
        self.call_later(app_support.mount_approval, self, ticket_id, prompt, tuple(options))

    def on_approval_bar_resolved(self, message: ApprovalBar.Resolved) -> None:
        message.stop()
        bar = self.approval_bar
        if bar is not None:
            self.journal.record_ask(bar.prompt, approved=message.choice != "Deny")
            bar.remove()
            self.approval_bar = None
        self.composer.display = True
        self.composer.focus_input()
        self.adapter.answer_approval(message.ticket_id, message.choice)
        self._refresh_footer()

    # -- composer semantics -----------------------------------------------------------

    def on_composer_submit(self, message: Composer.Submit) -> None:
        message.stop()
        text = message.text
        selected = self.palette.selected_command if self.palette.is_open else None
        self.palette.apply_filter(None)
        if text.startswith("/"):
            if self._commands.parse_and_run(self._ctx, text):
                self._refresh_footer()
                return
            if selected is not None:
                self._commands.run(selected.name, self._ctx)
                self._refresh_footer()
                return
            # Mockup onKeyDown Enter: with zero palette matches the slash
            # text falls through and is sent as a normal user turn (§5/§6).
        self.submit_prompt(text)

    def on_composer_steer(self, message: Composer.Steer) -> None:
        message.stop()
        # Mockup onKeyDown: an open palette match runs BEFORE the steer
        # branch — a slash command typed mid-turn runs, never steers (§6).
        selected = self.palette.selected_command if self.palette.is_open else None
        if selected is not None:
            self.palette.apply_filter(None)
            if not self._commands.parse_and_run(self._ctx, message.text):
                self._commands.run(selected.name, self._ctx)
            self._refresh_footer()
            return
        if self.adapter.steering.pending_steers:
            self._queue_message(message.text)  # second steer queues (spec §5)
            return
        app_support.echo_steer(self, message.text)

    def on_composer_queue_message(self, message: Composer.QueueMessage) -> None:
        message.stop()
        # Mockup onKeyDown: every Enter — shift held or not — runs an open
        # palette's top match BEFORE the queue/submit branch (§5/§6).
        selected = self.palette.selected_command if self.palette.is_open else None
        if selected is not None:
            self.palette.apply_filter(None)
            if not self._commands.parse_and_run(self._ctx, message.text):
                self._commands.run(selected.name, self._ctx)
            self._refresh_footer()
            return
        if not self.turn_active:
            self.submit_prompt(message.text)
            return
        self._queue_message(message.text)

    def _queue_message(self, text: str) -> None:
        try:
            self.adapter.steering.enqueue(text, kind="next_turn")
        except ValueError as error:
            self.show_notice(str(error))
            return
        self.queued_strip.show_queued(text)
        self.show_notice(app_support.QUEUED_NOTICE)
        self._refresh_footer()

    def on_composer_open_palette(self, message: Composer.OpenPalette) -> None:
        message.stop()
        self.palette.apply_filter(message.filter)
        self._refresh_footer()

    def on_composer_palette_filter_cleared(
        self, message: Composer.PaletteFilterCleared
    ) -> None:
        message.stop()
        self.palette.apply_filter(None)
        self._refresh_footer()

    def on_composer_nav_key(self, message: Composer.NavKey) -> None:
        message.stop()
        # Empty-composer arrows drive the auto-opened (unfocused) lanes
        # panel — spec §8 advertises "↑↓ select" while fan-out keeps the
        # keyboard on the composer for steering.
        if self.lanes_panel.display and not self.lanes_panel.has_focus:
            self.lanes_panel.move_selection(message.delta)

    def on_composer_enter_empty(self, message: Composer.EnterEmpty) -> None:
        message.stop()
        if self.lanes_panel.display and not self.lanes_panel.has_focus:
            self.lanes_panel.focus_selected()

    def copy_to_clipboard(self, text: str) -> None:
        """Clipboard writes go BOTH ways: OSC 52 (Textual's built-in, works
        over SSH) AND the OS clipboard tool when one exists (pbcopy /
        wl-copy / xclip). iTerm2 ships with OSC 52 writes DISABLED, so
        relying on the escape alone silently copied nothing (user report:
        "can't copy still"). One choke point — ctrl+c and any /copy-style
        command all route through here."""
        super().copy_to_clipboard(text)
        self._os_clipboard_copied = app_support.os_clipboard_copy(text)

    def action_copy_selection(self) -> None:
        """ctrl+c: copy the composer's own selection, else the transcript
        drag-selection. Always confirms — clipboard writes are invisible."""
        text = self.composer.selected_text or self.screen.get_selected_text()
        if not text:
            self.show_notice("nothing selected · drag to select transcript text")
            return
        self.copy_to_clipboard(text)
        if self._os_clipboard_copied:
            self.show_notice(f"copied · {len(text)} chars")
        else:
            self.show_notice(
                f"copied · {len(text)} chars · empty clipboard? allow terminal clipboard access"
            )

    def on_composer_esc_pressed(self, message: Composer.EscPressed) -> None:
        message.stop()
        app_support.handle_esc(self)

    def on_composer_cycle_mode_requested(self, message: Composer.CycleModeRequested) -> None:
        message.stop()
        self.action_cycle_mode()

    # -- palette / lanes / rewind / needs-you messages ------------------------------------

    def on_palette_strip_command_run(self, message: PaletteStrip.CommandRun) -> None:
        message.stop()
        self.composer.clear()
        self.palette.apply_filter(None)
        self._commands.run(message.command.name, self._ctx)
        self.composer.focus_input()
        self._refresh_footer()

    def on_palette_strip_closed(self, message: PaletteStrip.Closed) -> None:
        message.stop()
        self.close_palette()

    def on_lanes_panel_focus_lane(self, message: LanesPanel.FocusLane) -> None:
        message.stop()
        blocks = self.adapter.lane_blocks(message.name, message.session_id, self.allocator)
        if blocks is None:
            self.show_notice(f"no transcript for lane · {message.name}")
            return
        # The panel stays open while a lane is focused (mockup focusLane
        # never touches lanesOpen); its row snaps to the focused lane.
        self.lanes_panel.set_focused(message.name)
        # Esc must resolve via ESC_CHAIN (lane_focus first, lanes later),
        # so the keyboard returns to the composer, not the panel.
        self.composer.focus_input()
        self.run_worker(
            self.transcript.focus_lane(message.session_id or message.name, blocks),
            exclusive=False,
        )

    def on_lanes_panel_type_through(self, message: LanesPanel.TypeThrough) -> None:
        # Mockup: the composer input keeps focus while lanesOpen — a
        # printable key typed "at" the panel lands in the composer ("/"
        # opens the palette via the composer's normal edit path) and the
        # keyboard returns to the composer for the rest of the typing.
        message.stop()
        self.composer.focus_input()
        self.composer.insert_text(message.character)

    def on_lanes_panel_closed(self, message: LanesPanel.Closed) -> None:
        message.stop()
        self._restore_keyboard()
        self._refresh_footer()

    def on_lane_focus_changed(self, message: LaneFocusChanged) -> None:
        app_support.handle_lane_focus_change(self, message.lane_id)

    def on_rewind_strip_fork_requested(self, message: RewindStrip.ForkRequested) -> None:
        message.stop()
        # The strip hid itself on fork; hand the keyboard back NOW — the
        # approval bar while one is open (it owns the keyboard, spec §7,
        # so Esc still means Deny for a fork parked behind a pending
        # approval), the composer otherwise. A fork-chip click must not
        # strand focus on the hidden strip (spec §12).
        self._restore_keyboard()
        self._refresh_footer()
        app_support.handle_fork(self, message.checkpoint_id)

    def on_rewind_strip_type_through(self, message: RewindStrip.TypeThrough) -> None:
        # Mockup: the composer input keeps focus while rewindOpen — a
        # printable key typed "at" the strip lands in the composer ("/"
        # opens the palette live-filtered, §5) and the keyboard returns
        # to the composer for the rest of the typing.
        message.stop()
        self.composer.focus_input()
        self.composer.insert_text(message.character)

    def on_rewind_strip_closed(self, message: RewindStrip.Closed) -> None:
        message.stop()
        self._restore_keyboard()
        self._refresh_footer()

    def on_open_rewind(self, message: OpenRewind) -> None:
        index = next(
            (i for i, c in enumerate(self.ledger.checkpoints) if c.id == message.checkpoint_id),
            None,
        )
        self.open_rewind_strip(index)

    def on_show_evidence(self, message: ShowEvidence) -> None:
        # A click on the answer block must not strand focus on the
        # transcript scroll container.
        self._restore_keyboard()
        if not message.links:
            self.show_notice("no evidence recorded for this answer")
            return
        # Double-clicks (and repeat clicks) must not stack duplicate
        # blocks (found live: 4× Evidence for one answer) — refocus the
        # already-open block instead.
        ids = self.transcript.block_ids
        last = self.transcript.get_block(ids[-1]) if ids else None
        if (
            last is not None
            and last.kind == "evidence"
            and last.links == tuple(message.links)
        ):
            existing = self.transcript.get_widget(last.id)
            if existing is not None:
                existing.focus()
                return
        widget = self.transcript.append(
            EvidenceBlock(id=self.allocator.next_id(), links=tuple(message.links))
        )
        # The block owns the keyboard while open so its advertised keys
        # (←/→ select · enter expand · esc close, spec §10) work; esc
        # hands the keyboard back via CloseEvidence.
        if widget is not None:
            widget.focus()
        # Mockup revealEvidence ends with this exact notice.
        self.show_notice("evidence revealed · every claim traces to a tool call")

    def on_expand_evidence_claim(self, message: ExpandEvidenceClaim) -> None:
        """Enter on the evidence block: deep-link the selected claim to
        the tool line that grounds it (correlation key, spec §10)."""
        link = message.link
        if link.tool_call_id:
            for block in self.transcript.blocks:
                if block.kind == "tool_line" and link.tool_call_id in block.tool_call_ids:
                    if block.body and not block.expanded:
                        self.transcript.replace(block.model_copy(update={"expanded": True}))
                    self.query_one(f"#block-{block.id}").scroll_visible(animate=False)
                    return
        # No correlated tool line in the transcript: surface the grounding
        # reference itself instead of silently doing nothing.
        self.show_notice(f"grounded by {link.tool_ref}")

    def on_close_evidence(self, message: CloseEvidence) -> None:
        """Esc on the evidence block: close it and hand the keyboard back."""
        if self.transcript.get_block(message.block_id) is not None:
            self.transcript.remove_block(message.block_id)
        self._restore_keyboard()

    def on_needs_you_list_decision_taken(self, message: NeedsYouList.DecisionTaken) -> None:
        message.stop()
        # Decision rows/chips stop their Click events (a row click must not
        # double-fire through the app's generic transcript-click handler),
        # so restore the keyboard here: transcript clicks never strand it
        # (DESIGN-SPEC §12; the composer keeps focus through every click).
        self._restore_keyboard()
        app_support.apply_decision(self, message.item_id, message.choice)

    def on_click(self, event: events.Click) -> None:
        """Transcript clicks never strand the keyboard (DESIGN-SPEC §12).

        Mockup ground truth: the composer input keeps keyboard focus
        through every transcript click (document-level keydown handler;
        clicks on transcript divs never blur the input). A click may
        still *open* a strip that then takes the keyboard — e.g. turn
        rule → rewind picker — because that message is processed after
        this synchronous bubble.
        """
        widget = event.widget
        if widget is None:
            return
        if isinstance(widget, BlockWidget) and widget.block.kind == "evidence":
            # Exception: the evidence block keeps the keyboard it took on
            # click so its advertised ←/→/enter/esc keys work (spec §10).
            return
        if widget is self.transcript or self.transcript in widget.ancestors:
            self._restore_keyboard()

    def on_footer_bar_waiting_badge_clicked(
        self, message: FooterBar.WaitingBadgeClicked
    ) -> None:
        message.stop()
        self.action_show_needs_you()

    # -- key actions ------------------------------------------------------------------------

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in ("palette_up", "palette_down"):
            # Mockup onKeyDown: the approval branch consumes ArrowUp/Down
            # before any palette handling — arrows always cycle a pending
            # approval's selection (spec §7).
            return self.approval_bar is None and self.palette.is_open
        if self.approval_bar is not None and action in ("cycle_mode", "cycle_permission"):
            # Mockup keydown: while an approval is open, Tab (with or
            # without shift) cycles the approval selection and returns —
            # cycleMode is unreachable, and the trust posture must not
            # change under a pending approval (spec §7).
            return False
        return True

    def action_cycle_mode(self) -> None:
        self.set_mode_by_id(cycle_mode(self._mode.id).id)

    def action_cycle_permission(self) -> None:
        self.show_notice(f"trust · {self._mode.trust_str} · edit via /permissions")

    def action_toggle_lanes(self) -> None:
        if self.lanes_panel.display:
            self.lanes_panel.hide_panel()
            self._restore_keyboard()
        else:
            self.lanes_panel.update_lanes(self.lanes.lanes)
            self.lanes_panel.show_panel()
            if self.approval_bar is not None:
                self.approval_bar.focus()  # approval owns the keyboard (spec §7)
        self._refresh_footer()

    def action_show_ledger(self) -> None:
        spec = self._commands.get("/ledger")
        if spec is not None:
            spec.handler(self._ctx, "")  # keyboard path: print without echo

    def action_show_needs_you(self) -> None:
        block = app_support.needs_you_block(self.adapter.needs_you.pending, self.allocator)
        if block is None:
            self.show_notice("no decisions waiting")
            return
        self.append_block(block)

    def action_open_rewind(self) -> None:
        self.open_rewind_strip(None)

    def action_palette_up(self) -> None:
        self.palette.move_selection(-1)

    def action_palette_down(self) -> None:
        self.palette.move_selection(1)

    def action_app_esc(self) -> None:
        app_support.handle_esc(self)

    def open_rewind_strip(self, index: int | None) -> None:
        checkpoints = self.ledger.checkpoints
        if not checkpoints:
            self.show_notice("no rewind checkpoints yet")
            return
        self.rewind.show_checkpoints(checkpoints, index)
        if self.approval_bar is not None:
            self.approval_bar.focus()  # approval owns the keyboard (spec §7)
        self._refresh_footer()

    def close_palette(self) -> None:
        # Mockup Esc only clears the filter (palFilter = null); the typed
        # "/…" text stays in the input.
        self.palette.apply_filter(None)
        self.composer.focus_input()
        self._refresh_footer()

    def _restore_keyboard(self) -> None:
        """Refocus after a strip closes: the approval bar while one is
        open (it owns the keyboard, spec §7), the composer otherwise."""
        if self.approval_bar is not None:
            self.approval_bar.focus()
        else:
            self.composer.focus_input()

    def interrupt_turn(self) -> None:
        # Esc only requests the break (mockup ``this.interrupt = true``);
        # the ``turn interrupted · context saved`` notice is shown by the
        # reducer at the actual turn close-out (mockup end of runTurn).
        self.run_worker(self.adapter.interrupt(), exclusive=False)

    # -- command-context surface ------------------------------------------------------------

    def echo_user_line(self, text: str) -> None:
        self.append_block(
            UserLine(id=self.allocator.next_id(), text=text, mode=self._mode.id)
        )

    def context_usage(self) -> ContextUsage:
        window = 200_000
        memory = min(self.reducer.memory_tokens, window)
        tools = min(self.reducer.tool_tokens, window - memory)
        return ContextUsage(
            conversation=min(self.reducer.total_tokens, window - memory - tools),
            tools=tools,
            memory=memory,
            window=window,
        )

    def set_theme_by_name(self, name: str) -> None:
        """Switch the spec theme at runtime (``/theme``, DESIGN-SPEC §1).

        Empty *name* cycles slate → graphite → carbon; unknown names get
        a notice listing the valid themes.
        """
        names = tuple(THEME_TOKENS)
        if not name:
            current = self.theme.removeprefix(THEME_NAME_PREFIX)
            index = names.index(current) if current in names else -1
            name = names[(index + 1) % len(names)]
        if name not in THEME_TOKENS:
            self.show_notice(f"unknown theme · {name} · themes: {', '.join(names)}")
            return
        self.theme = theme_id(name)
        self.show_notice(f"theme {name}")

    def open_permissions(self) -> None:
        self.append_block(
            app_support.permissions_block(self.permissions, self._mode.trust_str, self.allocator)
        )

    # -- painting ---------------------------------------------------------------------------------

    def footer_context(self) -> keymap.Context:
        if self.approval_bar is not None:
            return "approval"
        if self.transcript.focused_lane is not None:
            return "lane_focus"
        if self.palette.is_open:
            return "palette"
        if self.turn_active:
            return "running"
        return "idle"

    def _refresh_footer(self) -> None:
        self.footer_bar.update_state(app_support.footer_state(self))

    def _refresh_title(self) -> None:
        self.title_bar.state_text = self.reducer.title_state()
        self.title_bar.bundle = self.adapter.bundle_name
        self.title_bar.session_short = self.adapter.session_short

    def refresh_status(self) -> None:
        self._refresh_title()
        self._refresh_footer()


__all__ = ["NewTuiApp"]
