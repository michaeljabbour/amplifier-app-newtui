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

from textual.app import App, ComposeResult
from textual.containers import Container

from ..commands.builtin import build_registry
from ..commands.context import ContextUsage
from ..commands.improve import ApprovalJournal
from ..commands.permissions import PermissionSurface
from ..model.blocks import (
    BlockIdAllocator,
    EvidenceBlock,
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
from .notices import NoticeSlot
from .palette import PaletteStrip
from .queued_strip import QueuedStrip
from .reducer import TranscriptReducer
from .rewind_strip import RewindStrip
from .runtime_adapter import RuntimeAdapter
from .themes import DEFAULT_THEME, register_themes, theme_id
from .transcript import (
    LaneFocusChanged,
    NeedsYouDecision,
    OpenRewind,
    ShowEvidence,
    TranscriptView,
)


class NewTuiApp(App[None]):
    """The Amplifier full-screen TUI (v3 Cohesive)."""

    CSS = """
    Screen { background: $bg-term; }
    #transcript { height: 1fr; padding: 0 1; }
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
        self.approval_bar: ApprovalBar | None = None
        self.steer_echoes: dict[str, str] = {}  # steer message_id → ↳ echo block id
        self._needs_you_block_id: str | None = None
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
        self.composer.focus_input()
        self.adapter.steering.add_listener(lambda: app_support.sync_steer_echoes(self))
        self.refresh_status()
        self.run_worker(self._consume_events(), exclusive=False)
        self.run_worker(self._boot_runtime(), exclusive=False)


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
            self._refresh_title()

    def submit_prompt(self, text: str) -> None:
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

    def show_notice(self, text: str) -> None:
        self.notice_slot.show_notice(text)

    def set_mode_by_id(self, mode_id: str, *, notify: bool = True) -> None:
        previous, self._mode = self._mode, get_mode(mode_id)
        self.permissions.set_mode(self._mode.id)
        self.composer.set_mode(self._mode)
        if notify:
            self.show_notice(self._mode.notice())
        if previous.id == "plan" and self._mode.id == "build":
            self.show_notice("plan handed to build")
        self.refresh_status()

    def turn_started(self) -> None:
        self.turn_active = True
        self.composer.running = True
        self.title_bar.running = True
        self.refresh_status()

    def turn_finished(self) -> None:
        self.turn_active = False
        self.composer.running = False
        self.title_bar.running = False
        app_support.finish_turn_queues(self)
        self.refresh_status()

    def lanes_changed(self) -> None:
        self.lanes_panel.update_lanes(self.lanes.lanes)
        self._refresh_title()

    def approval_opened(self, prompt: str, options: tuple[str, ...]) -> None:
        del prompt, options  # presentation runs via present_approval
        self._refresh_footer()

    def decision_deferred(self, message: str) -> None:
        question, reason, choices = self.adapter.deferred_decision(message)
        self.adapter.needs_you.defer(question, reason, choices=choices)
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
            if not self._commands.parse_and_run(self._ctx, text):
                if selected is not None:
                    self._commands.run(selected.name, self._ctx)
                else:
                    self.show_notice(f"unknown command · {text.split()[0]}")
            self._refresh_footer()
            return
        self.submit_prompt(text)

    def on_composer_steer(self, message: Composer.Steer) -> None:
        message.stop()
        if self.adapter.steering.pending_steers:
            self._queue_message(message.text)  # second steer queues (spec §5)
            return
        app_support.echo_steer(self, message.text)

    def on_composer_queue_message(self, message: Composer.QueueMessage) -> None:
        message.stop()
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
        self.lanes_panel.hide_panel()
        blocks = self.adapter.lane_blocks(message.name, message.session_id, self.allocator)
        if blocks is None:
            self.show_notice(f"no transcript for lane · {message.name}")
            return
        self.run_worker(
            self.transcript.focus_lane(message.session_id or message.name, blocks),
            exclusive=False,
        )

    def on_lanes_panel_closed(self, message: LanesPanel.Closed) -> None:
        message.stop()
        self.composer.focus_input()
        self._refresh_footer()

    def on_lane_focus_changed(self, message: LaneFocusChanged) -> None:
        app_support.handle_lane_focus_change(self, message.lane_id)

    def on_rewind_strip_fork_requested(self, message: RewindStrip.ForkRequested) -> None:
        message.stop()
        app_support.handle_fork(self, message.checkpoint_id)

    def on_rewind_strip_closed(self, message: RewindStrip.Closed) -> None:
        message.stop()
        self.composer.focus_input()
        self._refresh_footer()

    def on_open_rewind(self, message: OpenRewind) -> None:
        index = next(
            (i for i, c in enumerate(self.ledger.checkpoints) if c.id == message.checkpoint_id),
            None,
        )
        self.open_rewind_strip(index)

    def on_show_evidence(self, message: ShowEvidence) -> None:
        if not message.links:
            self.show_notice("no evidence recorded for this answer")
            return
        self.append_block(
            EvidenceBlock(id=self.allocator.next_id(), links=tuple(message.links))
        )

    def on_needs_you_decision(self, message: NeedsYouDecision) -> None:
        app_support.apply_decision(self, message.decision_id, message.answer)

    def on_footer_bar_waiting_badge_clicked(
        self, message: FooterBar.WaitingBadgeClicked
    ) -> None:
        message.stop()
        self.action_show_needs_you()

    def clear_needs_you_block(self) -> None:
        if self._needs_you_block_id is not None:
            self.remove_block(self._needs_you_block_id)
            self._needs_you_block_id = None

    # -- key actions ------------------------------------------------------------------------

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in ("palette_up", "palette_down"):
            return self.palette.is_open
        return True

    def action_cycle_mode(self) -> None:
        self.set_mode_by_id(cycle_mode(self._mode.id).id)

    def action_cycle_permission(self) -> None:
        self.show_notice(f"trust · {self._mode.trust_str} · edit via /permissions")

    def action_toggle_lanes(self) -> None:
        if self.lanes_panel.display:
            self.lanes_panel.hide_panel()
            self.composer.focus_input()
        else:
            self.lanes_panel.update_lanes(self.lanes.lanes)
            self.lanes_panel.show_panel()
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
        self._needs_you_block_id = block.id
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
        self._refresh_footer()

    def close_palette(self) -> None:
        self.composer.clear()
        self.palette.apply_filter(None)
        self.composer.focus_input()
        self._refresh_footer()

    def interrupt_turn(self) -> None:
        self.show_notice("turn interrupted · context saved")
        self.run_worker(self.adapter.interrupt(), exclusive=False)

    # -- command-context surface ------------------------------------------------------------

    def echo_user_line(self, text: str) -> None:
        self.append_block(
            UserLine(id=self.allocator.next_id(), text=text, mode=self._mode.id)
        )

    def context_usage(self) -> ContextUsage:
        window = 200_000
        return ContextUsage(
            conversation=min(self.reducer.total_tokens, window), window=window
        )

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
