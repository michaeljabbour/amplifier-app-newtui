"""The composition root: TuiApp (ADR-0007, <500 lines, no mixins).

Layout (DESIGN-SPEC §2, top → bottom): TitleBar / TranscriptView /
LiveTail / NoticeSlot / overlay strips (palette · lanes · rewind ·
queued) / composer-or-approval-bar / FooterBar. The app consumes the
runtime adapter's ``asyncio.Queue[UIEvent]`` through
:class:`~amplifier_app_tui.ui.reducer.TranscriptReducer` and owns
only interaction state (running, mode, palette filter, open strips,
focused lane, queued message, approval head); widgets own their own
state and talk back via Textual messages.

Esc precedence (DESIGN-SPEC §5, resolved via ``keymap.ESC_CHAIN`` in
:func:`~amplifier_app_tui.ui.app_support.handle_esc` — never ad-hoc
ladders). The approval bar owns the keyboard while open, so it sits
outside the chain:

    ============  =====================  ==============================
    priority      context (active when)  action
    ============  =====================  ==============================
    1             lane_focus             restore the parent transcript
    2             palette                close the command palette
    3             rewind                 close the rewind picker strip
    4             sessions               close the sessions picker strip
    5             lanes                  close the agent-lanes panel
    6             running                interrupt the running turn
    ============  =====================  ==============================
"""

from __future__ import annotations

import asyncio
import threading
import time
from decimal import Decimal
from typing import Any

from textual import events
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal

from ..commands.builtin import build_registry
from ..commands.context import ContextUsage
from ..commands.improve import ApprovalJournal
from ..commands.permissions import PermissionSurface
from ..model.blocks import (
    Answer,
    BlockIdAllocator,
    EvidenceBlock,
    Segment,
    TodoItem,
    TranscriptBlock,
    UserLine,
)
from ..model.evidence import EvidenceLink, build_evidence_detail
from ..model.lanes import TERMINAL_LANE_STATES, LaneRegistry
from ..model.modes import ModeProfile, cycle_mode, get_mode
from ..model.native_modes import ActiveNativeModes, posture_conflict_notice
from ..model.prompt_stash import PromptStash, stash_list_spans
from ..model.turn import OutcomeLedger
from . import app_support, keymap, notifications, transcript_render
from .approval_bar import ApprovalBar
from .chrome import APP_TITLE_NAME, TitleBar, write_terminal_title
from .sessions_strip import SessionsStrip
from .command_context import AppCommandContext
from .composer import Composer
from .evidence_panel import EvidencePanel
from .footer import FooterBar
from .history_recall import HistoryRecallStrip
from .file_mentions import (
    FileMentionIntent,
    FileMentionStrip,
    close_file_mentions,
    handle_file_mention_intent,
)
from .lanes_panel import LanesPanel
from .live_tail import LiveTail
from .needs_you import NeedsYouList
from .notices import NoticeSlot
from .palette import PaletteStrip
from .plan_panel import PlanPanel, plan_drill_notice, plan_overflow_notice
from .queued_strip import QueuedStrip
from .reducer import TranscriptReducer
from .rewind_strip import RewindStrip
from .runtime_adapter import RuntimeAdapter
from .session_ops_controller import SessionOpsController
from .session_ops_view import session_detail_spans, session_resume_spans, sessions_spans
from .splash import BootSplash
from .themes import DEFAULT_THEME, THEME_NAME_PREFIX, THEME_TOKENS, register_themes, theme_id
from .transcript import (
    BackToParent,
    BlockWidget,
    CloseEvidence,
    CopyCodeFence,
    DelegateSummaryToggled,
    ExpandEvidenceClaim,
    LaneFocusChanged,
    OpenEvidenceDetail,
    OpenRewind,
    ShowEvidence,
    TranscriptView,
)


_NOTIFY_TITLE = "Amplifier"
"""Notification title for every desktop rung (kept short: OSC 777 title
field is bounded to 80 chars in ``ui/notifications``)."""

_NOTIFY_BODY: dict[notifications.AttentionReason, str] = {
    "completion": "Turn complete",
    "awaiting_approval": "A decision needs your approval",
    "awaiting_clarification": "A decision needs your input",
    "error": "The session hit an error",
}
"""Default OSC 777 body per attention reason; a deferral passes its own
message through instead (see :meth:`TuiApp._notify_attention`)."""


class TuiApp(App[None]):
    """The Amplifier full-screen TUI (v3 Cohesive)."""

    CSS = """
    Screen { background: $bg-term; }
    /* The notice floats on its own layer over the bottom-right of the
       region's last row (mockup: absolute overlay in a height-0
       container, right: 18px) so showing or hiding it never resizes the
       transcript and blanks only its own box. `align` applies per layer;
       the base layer (transcript 1fr + live tail) always fills the
       region exactly, so only the auto-width notice moves. */
    #transcript-region { height: 1fr; layers: base splash notice; align: right bottom; }
    /* Boot splash: full-region overlay between base and notice — opaque so
       the wordmark sits on a clean field, gone entirely once dismissed.
       Styled here (not widget DEFAULT_CSS) for the same token-registration
       reason as the scrollbar rules above. */
    #boot-splash {
        layer: splash;
        width: 100%;
        height: 100%;
        background: $bg-term;
        content-align: center middle;
    }
    /* D7: the transcript shares its row with the evidence detail side
       panel, which defaults display:none (an empty split occupies zero
       extra columns) and claims a fixed width only once opened. */
    #transcript-split { width: 100%; height: 1fr; }
    #transcript-split > #transcript { width: 1fr; height: 1fr; padding: 0 1; }
    #transcript-split > #evidence-panel { width: 44; height: 1fr; }
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
    /* Bottom strip (design 2026-07-21 §1): lanes flexible left, plan
       fixed right. Both children default display:none, height:auto —
       an empty strip occupies zero rows. */
    #bottom-strip { width: 100%; height: auto; }
    #bottom-strip > #lanes-panel { width: 1fr; }
    #bottom-strip > #plan-panel { width: 37; }  /* = plan_panel.PLAN_PANEL_WIDTH */
    """

    BINDINGS = app_support.global_bindings()

    def __init__(
        self,
        adapter: RuntimeAdapter,
        *,
        kitty_protocol: bool = True,
        initial_mode: str | None = None,
    ) -> None:
        super().__init__()
        register_themes(self)  # before first stylesheet parse (NOTES: chrome)
        self.theme = theme_id(DEFAULT_THEME)
        keymap.validate()
        self.adapter = adapter
        self.kitty_protocol = kitty_protocol
        self.allocator = BlockIdAllocator()
        self.ledger = OutcomeLedger()
        self._stash = PromptStash()
        self.lanes = LaneRegistry()
        self.journal = ApprovalJournal()
        self.permissions = PermissionSurface()
        # ``initial_mode`` seeds the opening interaction posture (the launcher's
        # --mode override); None / unknown ids fall back to DEFAULT_MODE.
        self._mode: ModeProfile = get_mode(initial_mode)
        self._commands = build_registry()
        self._ctx = AppCommandContext(self)
        self.session_ops = SessionOpsController(self)
        self.reducer = TranscriptReducer(
            self,
            allocator=self.allocator,
            ledger=self.ledger,
            lanes=self.lanes,
            spec_lookup=adapter.turn_spec,
            lane_seed_lookup=adapter.lane_seed,
            evidence_lookup=adapter.evidence_links,
            session_cost_start=adapter.session_cost_start,
            # D5 AC5: guarantees a coalesced lane-rows repaint is never
            # stranded — see LaneReducer._schedule_trailing_flush.
            schedule_flush=self.set_timer,
        )
        self.turn_active = False
        # Current reasoning-effort tier for the footer indicator (HGT effort
        # cycle). None = unset/default -> the footer omits the segment; a
        # value appears once ctrl+b cycles or /effort <level> sets it.
        self._effort: str | None = None
        # Terminal-window focus (Textual AppFocus/AppBlur, the mode-1004
        # focus report): assumed focused until a blur says otherwise, so
        # the desktop rung of the notification ladder only escalates when
        # the user has demonstrably looked away (issue #47).
        self._terminal_focused = True
        # Dedup + acknowledgement bookkeeping for the attention-notification
        # ladder (B7, issue #47): mints one AttentionRecord per transition
        # into an attention state, keyed for idempotent re-fire protection
        # -- see _notify_attention / _acknowledge_attention.
        self._attention = notifications.AttentionCenter()
        self.fork_pending = False  # a confirmed fork is in flight (interrupt-then-fork)
        self._working_timer: Any = None  # 1s working-line heartbeat (Timer)
        self._splash: BootSplash | None = None  # boot splash overlay (wordmark)
        self._auto_native_mode: str | None = None  # posture-bridged native mode
        # Explicitly-activated native modes (/mode <name>) as an ordered stack;
        # the last is the primary (the one pointed at the single upstream slot).
        # Backward compatible: a single active mode behaves as the old single
        # ``_native_mode`` string did.
        self._native_modes = ActiveNativeModes()
        self._os_clipboard_copied = False  # last copy reached an OS clipboard tool
        self._clipboard_write_seq = 0  # latest native write wins
        self._clipboard_write_lock = asyncio.Lock()
        self._selection_timer: Any = None  # copy-on-select debounce
        self._last_selection_copied = ""  # suppress duplicate auto-copies
        self._turn_queues_pending = False  # drain queues once end-of-turn events settle
        self._turn_started_at: float | None = None  # attention-bell elapsed basis
        self.esc_sequence = app_support.EscSequence()
        self.approval_bar: ApprovalBar | None = None
        self.steer_echoes: dict[str, str] = {}  # steer message_id → ↳ echo block id
        self._lanes_fanout_open = False  # active-lane edge for the auto-open
        self._lane_focus_intro_shown = (
            False  # first-ever focus shows the exit-path notice once (S6)
        )
        self.plan_items: tuple[TodoItem, ...] = ()  # latest root todo list
        self._evidence_panel_target: tuple[str, EvidenceLink] | None = None
        """(block_id, link) currently shown in the evidence side panel —
        None while it is closed (D7). Lets a second ``d`` press on the
        SAME claim toggle-close instead of re-opening."""
        self.title_bar = TitleBar(id="title-bar")
        self.transcript = TranscriptView(id="transcript")
        self.evidence_panel = EvidencePanel(id="evidence-panel")
        self.live_tail = LiveTail(id="live-tail")
        self.notice_slot = NoticeSlot(id="notice-slot")
        self.palette = PaletteStrip(self._commands.specs, id="palette-strip")
        # Open registry (story #2): any runtime registration — skills at
        # boot, recipe/pipeline verbs later — re-feeds the palette rows.
        self._commands.subscribe(self._sync_palette_commands)
        self.lanes_panel = LanesPanel(id="lanes-panel")
        self.plan_panel = PlanPanel(id="plan-panel")
        self.rewind = RewindStrip(id="rewind-strip")
        self.sessions_strip = SessionsStrip(id="sessions-strip")
        self.queued_strip = QueuedStrip(id="queued-strip")
        self.file_mentions = FileMentionStrip(id="file-mentions")
        self.history_recall = HistoryRecallStrip(id="history-recall")
        self.composer = Composer(kitty_protocol=kitty_protocol, id="composer")
        self.footer_bar = FooterBar(id="footer-bar")

    def compose(self) -> ComposeResult:
        yield self.title_bar
        with Container(id="transcript-region"):
            with Horizontal(id="transcript-split"):
                yield self.transcript
                yield self.evidence_panel
            yield self.live_tail
            yield self.notice_slot
        yield self.palette
        with Horizontal(id="bottom-strip"):
            yield self.lanes_panel
            yield self.plan_panel
        yield self.rewind
        yield self.sessions_strip
        yield self.queued_strip
        yield self.file_mentions
        yield self.history_recall
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
        self.adapter.lane_steering.add_listener(self._on_lane_steering_changed)
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
        if not self.screen_stack:
            return  # debounce timer outlived the app (shutdown race)
        text = self.screen.get_selected_text()
        if not text or text == self._last_selection_copied:
            return
        self._last_selection_copied = text
        self.copy_to_clipboard(text)
        self.show_notice(f"copied on select · {len(text)} chars")

    def on_app_focus(self, event: events.AppFocus) -> None:
        # The terminal window regained focus: the user is watching, so the
        # ladder drops back to the audible bell alone (no desktop toast).
        # Refocusing also counts as "resuming" (B7 AC5): clear whatever
        # attention indicator is currently open.
        self._terminal_focused = True
        self._acknowledge_attention()

    def on_app_blur(self, event: events.AppBlur) -> None:
        # The terminal window lost focus: a finished turn or deferred
        # decision now earns the OSC 777 desktop rung (if the terminal
        # renders it and AMPLIFIER_NOTIFY permits).
        self._terminal_focused = False

    def _notify_attention(
        self,
        reason: notifications.AttentionReason,
        elapsed_s: float = 0.0,
        *,
        occasion: str,
        detail: str = "",
    ) -> None:
        """Emit ONE normalized ``AttentionRecord`` for a transition into
        *reason* and fire its ladder (B7, issue #47 -- AC1/AC3).

        The suppressed hooks-notify wrote OSC-777 + BEL straight to the TTY
        (which corrupts the full-screen TUI); its signal is re-expressed
        here as a ladder driven by the record rather than an ad-hoc call:
        rung 1 is Textual's driver-safe ``App.bell``; rung 2 is an OSC 777
        desktop notification written through the same sanctioned driver
        path the terminal title uses, only when the window is unfocused on
        a capable terminal. Rung 3 (off-machine push) is the mounted
        ``hooks-notify-push`` module's job and is not routed through this
        record (see ``ui/notifications.py`` module docstring).

        ``occasion`` is *reason*'s stable idempotency handle -- the
        finishing turn's id, or the parked decision's id -- so a re-render
        or a repeated kernel-side ping for the SAME occasion dedupes (AC3)
        instead of re-notifying. ``AMPLIFIER_NOTIFY`` gates the whole
        ladder, including whether a record is minted at all;
        ``notification_rungs`` owns the rung policy.
        """
        if not notifications.attention_needed(reason, elapsed_s):
            return
        record, is_new = self._attention.note(
            self.adapter.session_id, reason, occasion, detail=detail
        )
        if not is_new:
            return  # AC3: same transition already notified (re-render/reconnect/re-ping)
        rungs = notifications.notification_rungs(reason, elapsed_s, focused=self._terminal_focused)
        body = detail.strip() or _NOTIFY_BODY[reason]
        notifications.fire_attention_ladder(
            rungs, bell=self.bell, driver=self._driver, title=_NOTIFY_TITLE, body=body
        )
        # B7 gap 2: route the SAME record-derived payload (carrying the
        # attention event_id) onto the adapter's push seam, additive to the
        # ladder above -- never blocking, never raising (see
        # RuntimeAdapter.publish_attention / RealRuntimeAdapter's override).
        self.adapter.publish_attention(
            notifications.attention_push_payload(record, title=_NOTIFY_TITLE, body=body)
        )

    def _acknowledge_attention(self) -> None:
        """Clear the open attention record + its destination indicator
        where the destination supports it (B7, issue #47 -- AC5).

        OSC 777/desktop is ours to rewrite, so acknowledging best-effort
        clears it; the bell already rang and has nothing to retract;
        off-machine ntfy push has no acknowledgement channel here at all
        (a different device's notification tray, fired by the mounted
        ``hooks-notify-push`` module -- see ``ui/notifications.py`` module
        docstring). A destination failure is logged, never raised, and
        never blocks the session.
        """
        if self._attention.acknowledge(self.adapter.session_id) is None:
            return  # nothing was open -- an idle resume/ack is a no-op
        notifications.clear_desktop_notification(self._driver)

    def on_unmount(self) -> None:
        # A quit during a running turn must not leave a frozen spinner in the
        # terminal tab after Textual restores the shell screen.
        write_terminal_title(self._driver, APP_TITLE_NAME)
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

    def _on_lane_steering_changed(self) -> None:
        # Per-lane steers are consumed on the runtime thread (the step-
        # boundary bridge); the ▸ N queued badge repaint must hop back to
        # the UI thread (issue #39).
        if threading.get_ident() == self._ui_thread_id:
            self._repaint_lane_badges()
        else:
            self.call_from_thread(self._repaint_lane_badges)

    def _repaint_lane_badges(self) -> None:
        """Repaint the lanes panel's per-lane steer badges in place."""
        tailed = self.lanes.tail_lane
        self.lanes_panel.update_lanes(
            self.lanes.lanes,
            tailed_session_id=None if tailed is None else tailed.session_id,
            queued_counts=self.adapter.lane_steering.counts(),
        )

    async def _boot_runtime(self) -> None:
        self.adapter.attach(self)
        try:
            await self.adapter.start(lambda: app_support.announce_ready(self))
            # Both bindings below fire ONCE, right here, because this is the
            # one boundary that owns session identity: RuntimeAdapter.start()
            # has just resolved adapter.session_id / adapter.session_dir as
            # plain attributes (set synchronously before start() returns), so
            # neither call below can observe a half-initialized adapter, and
            # their relative order is inconsequential -- each binds a
            # DIFFERENT, independent piece of module/instance state and
            # neither reads the other's output. A resume/second window is a
            # fresh process (``amplifier-tui resume SESSION_ID``), so there is
            # no in-place session switch that could leave either bound to a
            # stale value.
            #
            # Session identity is resolved by now (RuntimeAdapter.start()'s own
            # contract); bind it for transcript_render's render-failure log
            # lines (S5 AC4) here, at the ONE boundary that owns session
            # identity, rather than threading a session_id through every pure
            # renderer. Empty for demo sessions, matching adapter.session_id.
            transcript_render.bind_session_context(self.adapter.session_id)
            # B7 gap 1: the session directory is only known once boot
            # completes -- bind durability now so a restart/second-process
            # observes prior attention state (no-op for the demo adapter,
            # whose session_dir is always None).
            self._attention.bind(self.adapter.session_dir)
            self.file_mentions.set_files(await self.adapter.workspace_files())
            self._register_skill_commands(await self.adapter.list_skills())
            # A resumed fork child carries a primed directive; run it as the
            # first turn (the reachable stand-in for app-cli's background fork).
            app_support.run_pending_directive(self)
        except Exception as error:  # noqa: BLE001 — boot failure is shown to the user, not crashed out
            # (CancelledError/KeyboardInterrupt stay uncaught: a real
            # shutdown mid-boot must not read as "session failed to start".)
            app_support.announce_boot_failure(self, error)

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
            if event.kind == "provider_response_usage":
                # Provider usage is sparse (one record per response), so
                # repaint the footer immediately without tying it to the
                # high-frequency streaming-delta path.
                self._refresh_footer()

    def drain_turn_queues(self) -> None:
        """Run the deferred turn-end queue duties once (idempotent)."""
        if not self._turn_queues_pending:
            return
        self._turn_queues_pending = False
        app_support.finish_turn_queues(self)

    def submit_prompt(self, text: str, attachments: tuple[Any, ...] = ()) -> None:
        if self._splash is not None:
            # Mid-boot submits used to vanish silently (the runtime isn't
            # up yet) — keep the supervisor's words instead of eating them.
            self.composer.insert_text(text)
            self.show_notice("session still starting · message kept in the composer")
            return
        self.run_worker(self._submit_prompt(text, attachments), exclusive=False)

    async def _submit_prompt(self, text: str, attachments: tuple[Any, ...]) -> None:
        try:
            await self.adapter.submit(text, attachments)
        except Exception as error:  # noqa: BLE001 — a turn error must not tear down the app
            # ``run_worker`` defaults to ``exit_on_error=True``: an exception
            # raised by ``submit`` (provider auth expiry, network drop mid-turn)
            # used to crash the whole TUI. Surface it and keep the session live.
            # (CancelledError/KeyboardInterrupt are BaseException — they stay
            # uncaught so a real shutdown isn't misreported as a turn failure.)
            self.log.error(f"turn failed: {error}")
            self.show_notice(f"turn failed · {error}")
            # B7 gap 3 (production error transition #1 -- a failed turn):
            # the ``finally`` inside ``RealRuntime.submit`` always emits its
            # close-out first (turn_finished() already fired above this
            # except), so this exception is genuinely on top of that --
            # error is the more specific, more urgent signal. Same occasion
            # derivation as turn_finished() (the just-recorded turn's id, so
            # a re-render of the SAME failure dedupes -- AC3) with its own
            # prefix so it never collides with the "completion" reason's key
            # (the composite event id already namespaces by reason too).
            turn_id = self.ledger.turns[-1].turn_id if self.ledger.turns else None
            occasion = (
                f"submit-error-{turn_id}"
                if turn_id is not None
                else f"submit-error-{time.monotonic()}"
            )
            self._notify_attention("error", occasion=occasion, detail=str(error))

    # -- ReducerHost ---------------------------------------------------------------

    @property
    def mode_id(self) -> str:
        return self._mode.id

    @property
    def native_modes(self) -> tuple[str, ...]:
        """Active bundle modes in activation order (last == primary) for the footer."""
        return self._native_modes.names

    @property
    def current_effort(self) -> str | None:
        """Current reasoning-effort tier for the footer indicator (None = unset)."""
        return self._effort

    @property
    def splash_active(self) -> bool:
        """True while the boot splash is up (SessionOpsController host surface)."""
        return self._splash is not None

    @property
    def session_cost(self) -> Decimal:
        """Cumulative session cost (SessionOpsController host surface)."""
        return self.reducer.session_cost

    def append_block(self, block: TranscriptBlock) -> None:
        self.transcript.append(block)

    def set_effort_indicator(self, level: str | None) -> None:
        """Cache the reasoning-effort tier and repaint the footer indicator.

        The SessionOpsController calls this after a successful effort change
        (ctrl+b cycle or ``/effort <level>``) so the footer's ``effort <tier>``
        segment stays honest without an async ``get_effort`` on every repaint.
        """
        self._effort = level
        self._refresh_footer()

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

    def clear_transcript_view(self) -> None:
        """View-only reset for ``/clear`` (D3): SessionOpsHost surface.

        Unmounts every rendered row and starts a new clear-generation so
        the reducer fences any already-queued event from the pre-clear
        generation (a delayed delta/tool-result/notice can no longer
        append, replace or remove a row here). The composer keeps focus
        either way, but a removed widget can steal it first, so re-assert
        explicitly rather than rely on nothing else having grabbed it.

        D7: the evidence detail panel is keyed to a specific block that
        ``clear_view()`` is about to unmount unconditionally, along with
        everything else. Direct close here -- the same "whole block gone"
        shape as ``on_close_evidence`` -- rather than ``close_evidence_panel()``:
        there is no row left to restore focus/scroll to, and replaying a
        remembered scroll position would fight the fresh bottom-follow
        anchor ``clear_view()`` itself asserts below.
        """
        if self.evidence_panel.is_open:
            self.evidence_panel.close()
            self._evidence_panel_target = None
        self.transcript.clear_view()
        self.reducer.bump_generation()
        self.composer.focus_input()

    def show_notice(self, text: str, duration: float | None = None) -> None:
        # The approval bar owns both input and its explanatory notice. A late
        # notification from the preceding turn (notably an agents-done event)
        # must not overwrite the instruction while the modal decision is live.
        if self.approval_bar is not None and "approval required" not in text:
            return
        self.notice_slot.show_notice(text, duration)

    def set_mode_by_id(self, mode_id: str, *, notify: bool = True) -> None:
        self._mode = get_mode(mode_id)
        self.permissions.set_mode(self._mode.id)
        self.composer.set_mode(self._mode)
        if notify:
            self.show_notice(self._mode.notice())
        # Precedence: a tool-restrictive posture must never SILENTLY nullify an
        # active native mode. Surface the conflict so the user knows the modes
        # coexist and how to let the native tools run (governance already lets
        # the mode's declared safe tools survive; this covers the rest).
        conflict = posture_conflict_notice(self._mode.id, self._native_modes)
        if conflict:
            self.show_notice(conflict)
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
        # Explicitly-activated native modes own the single upstream slot; the
        # posture bridge must never clobber them (the posture-conflict notice
        # covers that case instead). Only auto-activate a posture twin when no
        # explicit native mode is active — preserving single-mode behavior.
        if self._native_modes:
            return
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
        if self._splash is not None:
            self.show_notice("session still starting · /modes once the banner lands")
            return
        catalog = await self.adapter.list_native_modes()
        active = self._native_modes.names
        active_line = f" · active: {', '.join(active)}" if active else ""
        spans = [
            Segment(text="· ", style_token="blue"),
            Segment(text="Modes", style_token="bright", bold=True),
            Segment(
                text="  postures: chat plan brainstorm build auto · shift+tab cycles"
                f" · trust layer{active_line}\n",
                style_token="dim",
            ),
        ]
        native = (
            app_support.native_modes_segments(catalog, self.size.width, active=active)
            if catalog
            else ()
        )
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

    def show_keys(self) -> None:
        """``/keys``: the keyboard-shortcut reference (item D4).

        Renders straight from :func:`keymap.help_rows` — the same table
        that drives key bindings and the footer's context-sensitive
        hints — so this listing can never drift from what's actually
        bound. The footer's old generic ``idle`` hint moved here; overlay
        chords (palette/mentions/lanes/rewind/approval/evidence) are
        deliberately left off since those already teach themselves live
        in the footer the moment that overlay is open.
        """
        rows = keymap.help_rows()
        label_width = max(len(label) for label, _ in rows)
        spans: list[Segment] = [
            Segment(text="· ", style_token="blue"),
            Segment(text="Keys", style_token="bright", bold=True),
            Segment(
                text="  shortcuts that work any time \u2014 overlays (palette, "
                "mentions, lanes, rewind, approval) teach their own keys "
                "live in the footer while they're open\n",
                style_token="dim",
            ),
        ]
        for label, description in rows:
            spans.append(Segment(text=f"  {label.ljust(label_width)}  ", style_token="teal"))
            spans.append(Segment(text=f"{description}\n", style_token="dim"))
        self.append_block(Answer(id=self.allocator.next_id(), spans=tuple(spans)))

    def activate_native_mode(self, name: str | None) -> None:
        """``/mode <bundle-mode>`` ADDs to the active set; ``/mode off`` clears all."""
        self.run_worker(self._activate_native_mode(name), exclusive=False)

    async def _activate_native_mode(self, name: str | None) -> None:
        if name is None:
            # /mode off — clear the whole stack (single upstream slot → clear).
            await self.adapter.set_native_mode(None)
            self._auto_native_mode = None
            self._native_modes = self._native_modes.clear()
            self._refresh_footer()
            self.show_notice("mode off · native (bundle)")
            return
        # ADD: point the single upstream slot at the new primary; on success it
        # joins (or is promoted within) the client-side stack.
        ok, detail = await self.adapter.set_native_mode(name)
        if ok:
            self._auto_native_mode = None  # explicit choice — never auto-cleared
            self._native_modes = self._native_modes.add(name)
            self._refresh_footer()
            self.show_notice(f"mode {name} · native (bundle)")
            conflict = posture_conflict_notice(self._mode.id, self._native_modes)
            if conflict:
                self.show_notice(conflict)
        else:
            self.show_notice(detail or f"no such mode · {name}")

    def deactivate_native_mode(self, name: str) -> None:
        """``/mode -<name>`` / ``/mode off <name>``: remove ONE native mode."""
        self.run_worker(self._deactivate_native_mode(name), exclusive=False)

    async def _deactivate_native_mode(self, name: str) -> None:
        if name not in self._native_modes:
            self.show_notice(f"mode not active · {name}")
            return
        remaining = self._native_modes.remove(name)
        # Re-point the single upstream slot at the new primary (or clear it when
        # the stack empties): only ever one native mode is enforced at a time.
        ok, detail = await self.adapter.set_native_mode(remaining.primary)
        if ok:
            self._native_modes = remaining
            self._refresh_footer()
            promoted = remaining.primary
            tail = f" · now {promoted}" if promoted else ""
            self.show_notice(f"mode -{name} · native (bundle){tail}")
        else:
            self.show_notice(detail or f"could not deactivate · {name}")

    # -- registry wiring + directory admin -----------------------------------
    # In-session coordinator ops (/status /model /effort /compact /clear /tools
    # /agents /diff /skills /skill /mcp) live in SessionOpsController; the
    # command context drives them via ``self.session_ops`` (ADR-0007 seam).

    # -- stored-session lifecycle (/rename /sessions /branch) ---------------

    def rename_session(self, name: str) -> None:
        if not name.strip():
            self.show_notice("usage: /rename <new name>")
            return
        if self.session_ops._ops_starting():
            return
        self.run_worker(self._rename_session(name.strip()), exclusive=False)

    async def _rename_session(self, name: str) -> None:
        ok, detail = await self.adapter.rename_session(name)
        self.show_notice(f"session renamed · {detail}" if ok else detail)

    def show_sessions(self) -> None:
        self.run_worker(self._show_sessions(), exclusive=False)

    async def _show_sessions(self) -> None:
        """``/sessions``: open the interactive picker (S2 compliance gap 2).

        An empty roster shows a notice instead of an empty strip (mirrors
        ``open_rewind_strip`` on zero checkpoints). Selecting a row does
        NOT resume in-place (the stored-session roster has always been
        read-only here) -- it opens that session's full-id detail via
        :meth:`on_sessions_strip_session_activated`.
        """
        summaries = await self.adapter.session_summaries()
        if not summaries:
            self.show_notice("no stored sessions \u00b7 this project has no history yet")
            return
        self.sessions_strip.show_sessions(summaries, current=self.adapter.session_short)
        self._refresh_footer()

    # -- prompt-stash (HGT from opencode) -----------------------------------

    def action_stash_prompt(self) -> None:
        """``ctrl+s``: stash the in-progress draft and clear the composer.

        A no-op with a notice when the composer is empty (donor: the stash
        command is enabled only for a non-empty draft).
        """
        text = self.composer.text
        if not text.strip():
            self.show_notice("nothing to stash")
            return
        self._stash.push(text, now=time.time())
        self.composer.clear()
        self.composer.focus_input()
        self.show_notice(f"draft stashed · {self._stash.count} in stash")

    def recall_stash(self, index: int | None) -> None:
        """``/unstash [n]``: restore a stashed draft into the composer.

        ``index`` is ``None`` for the most-recent (LIFO ``pop``) or a 1-based
        newest-first position as listed by ``/stashes``. The entry is removed.
        """
        if self._stash.is_empty:
            self.show_notice("no stashed drafts")
            return
        entry = self._stash.pop() if index is None else self._stash.recall(index)
        if entry is None:
            self.show_notice(f"no stashed draft #{index} · /stashes lists them")
            return
        self.composer.set_draft(entry.text)
        self.composer.focus_input()
        remaining = self._stash.count
        self.show_notice(f"draft restored · {remaining} left" if remaining else "draft restored")

    def list_stashes(self) -> None:
        """``/stashes``: post the stashed-draft roster (newest first)."""
        if self._stash.is_empty:
            self.show_notice("no stashed drafts")
            return
        self.append_block(
            Answer(
                id=self.allocator.next_id(),
                spans=stash_list_spans(self._stash.entries, now=time.time()),
            )
        )

    def branch_session(self, name: str) -> None:
        if self.session_ops._ops_starting():
            return
        self.run_worker(self._branch_session(name.strip()), exclusive=False)

    async def _branch_session(self, name: str) -> None:
        ok, detail = await self.adapter.branch_session(name)
        if ok:
            self.show_notice(
                f"branch created · {detail[:12]} · resume: amplifier-tui resume {detail[:8]}"
            )
        else:
            self.show_notice(detail)

    def fork_session(self, directive: str) -> None:
        if not directive.strip():
            self.show_notice("usage: /fork <directive>")
            return
        if self.session_ops._ops_starting():
            return
        self.run_worker(self._fork_session(directive.strip()), exclusive=False)

    async def _fork_session(self, directive: str) -> None:
        ok, detail = await self.adapter.fork_with_directive(directive)
        if ok:
            self.show_notice(
                f"fork primed · {detail[:12]} · resume runs the directive: "
                f"amplifier-tui resume {detail[:8]}"
            )
        else:
            self.show_notice(detail)

    # -- session tags (/tag add|rm|list|sessions) ---------------------------

    def manage_tags(self, args: str) -> None:
        """``/tag`` sub-verb dispatch (add / rm / list / sessions).

        One protocol method (like ``/config``) keeps the CommandContext surface
        minimal; the verb selects a worker so metadata IO never blocks the UI.
        """
        parts = args.split()
        verb = parts[0].lower() if parts else "list"
        rest = tuple(parts[1:])
        if verb in ("list", "ls", "show") and not rest:
            self.run_worker(self._show_tags(), exclusive=False)
        elif verb == "add":
            if not rest:
                self.show_notice("usage: /tag add <name> [name ...]")
                return
            if self.session_ops._ops_starting():
                return
            self.run_worker(self._add_tags(rest), exclusive=False)
        elif verb in ("rm", "remove", "del", "delete"):
            if not rest:
                self.show_notice("usage: /tag rm <name> [name ...]")
                return
            if self.session_ops._ops_starting():
                return
            self.run_worker(self._remove_tags(rest), exclusive=False)
        elif verb == "sessions":
            if not rest:
                self.show_notice("usage: /tag sessions <tag>")
                return
            self.run_worker(self._sessions_by_tag(rest[0]), exclusive=False)
        else:
            self.show_notice("usage: /tag [list | add <name> | rm <name> | sessions <tag>]")

    async def _show_tags(self) -> None:
        tags = await self.adapter.session_tags()
        if tags:
            self.show_notice(f"tags: {', '.join(tags)}")
        else:
            self.show_notice("no session tags \u00b7 attach with /tag add <name>")

    async def _add_tags(self, tags: tuple[str, ...]) -> None:
        _ok, detail = await self.adapter.add_session_tags(tags)
        self.show_notice(detail)

    async def _remove_tags(self, tags: tuple[str, ...]) -> None:
        _ok, detail = await self.adapter.remove_session_tags(tags)
        self.show_notice(detail)

    async def _sessions_by_tag(self, tag: str) -> None:
        summaries = await self.adapter.sessions_by_tag(tag)
        self.append_block(
            Answer(
                id=self.allocator.next_id(),
                spans=sessions_spans(summaries, current=self.adapter.session_short),
            )
        )

    def _sync_palette_commands(self) -> None:
        """Registry subscriber: every successful register/unregister
        re-feeds the palette rows — palette and help stay a live
        reflection of the ONE registry (story #2)."""
        self.palette.set_commands(self._commands.specs)

    def _register_skill_commands(self, skills: tuple[Any, ...]) -> None:
        """Discovered skills (+ ``shortcut:`` aliases) become
        ``skill``-sourced registry contributions, so ``/cosam`` resolves
        in dispatch before the unknown-command notice (story #1); the
        palette follows via the registry subscription.

        Alias collisions — a skill name or shortcut a built-in or an
        earlier skill already holds — are surfaced here rather than
        skipped silently (compliance B2 AC4: collision detection is
        deterministic; "configuration load" IS this boot-time discovery
        pass). A rich listing goes to the transcript, a short dim notice
        points at it (the same split ``/ledger`` and ``/doctor`` use:
        block for the listing, notice for the pointer).
        """
        from ..commands.skills import alias_collision_spans, register_skill_commands_reporting

        plan = register_skill_commands_reporting(self._commands, skills)
        if plan.collisions:
            self.append_block(
                Answer(id=self.allocator.next_id(), spans=alias_collision_spans(plan.collisions))
            )
            count = len(plan.collisions)
            noun = "collision" if count == 1 else "collisions"
            self.show_notice(f"{count} skill alias {noun} · printed to scrollback")

    def manage_directories(self, kind: str, args: str) -> None:
        from .directory_admin import manage

        self.run_worker(manage(self, kind, args), exclusive=False)

    def manage_config(self, args: str) -> None:
        from .config_admin import manage

        self.run_worker(manage(self, args), exclusive=False)

    def turn_started(self) -> None:
        self.turn_active = True
        self._turn_started_at = time.monotonic()
        self.composer.running = True
        self.title_bar.running = True
        # 1s heartbeat: pulse the working line's spinner and (real turns)
        # its seconds counter — usage events alone froze it during long
        # provider calls (supervisor feedback, spec §3/§11).
        if self._working_timer is None:
            self._working_timer = self.set_interval(1.0, lambda: self.reducer.tick(time.time()))
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
        # Attention signal for the suppressed hooks-notify (raw OSC/BEL would
        # corrupt Textual): ring the driver-safe bell after long turns only —
        # policy + rationale in app_support.attention_bell_needed. Occasion
        # is the just-recorded turn's durable id (always present here — the
        # reducer records the ledger turn before calling turn_finished), so
        # a re-render for the SAME turn dedupes instead of re-notifying
        # (B7 AC3).
        elapsed = 0.0 if self._turn_started_at is None else time.monotonic() - self._turn_started_at
        self._turn_started_at = None
        turn_id = self.ledger.turns[-1].turn_id if self.ledger.turns else None
        occasion = f"turn-{turn_id}" if turn_id is not None else f"turn-clock-{time.monotonic()}"
        self._notify_attention("completion", elapsed, occasion=occasion)
        self.refresh_status()

    def lanes_changed(self) -> None:
        # A finished delegate never reaches another step boundary, so drop
        # any undelivered steers queued for it — otherwise a stale ▸ queued
        # badge would pin to a done lane (issue #39).
        for record in self.lanes.lanes:
            if (
                record.lane.state in TERMINAL_LANE_STATES
                and self.adapter.lane_steering.queued_count(record.session_id)
            ):
                self.adapter.lane_steering.drain(record.session_id)
        tailed = self.lanes.tail_lane
        self.lanes_panel.update_lanes(
            self.lanes.lanes,
            tailed_session_id=None if tailed is None else tailed.session_id,
            queued_counts=self.adapter.lane_steering.counts(),
        )
        active = self.lanes.active_count > 0
        if active and not self._lanes_fanout_open and not self.lanes_panel.display:
            # Mockup runAgentsTurn: the panel opens automatically at fan-out.
            # Display only — the composer keeps focus (type to steer). The
            # panel then STAYS visible showing the completed lanes (DESIGN-SPEC
            # §8 tri-state ends on ✔ done); it retracts on ctrl-t / esc, not
            # the instant every agent finishes.
            self.lanes_panel.show_panel(focus=False)
            self._refresh_footer()
        self._lanes_fanout_open = active
        self._refresh_title()

    def plan_changed(self, items: tuple[TodoItem, ...]) -> None:
        app_support.apply_plan_change(self, items)

    def on_resize(self, event: events.Resize) -> None:
        # Feed the live terminal width to the kernel's width-aware surface
        # hint (#35); a resize lands on the next turn's provider:request.
        self.adapter.terminal.set_cols(event.size.width)
        app_support.sync_plan_surfaces(self)  # responsive ladder (D2)
        app_support.sync_evidence_panel(self, event.size.width)  # responsive collapse (D7 AC4)

    def approval_opened(self, prompt: str, options: tuple[str, ...]) -> None:
        del prompt, options  # presentation runs via present_approval
        self._refresh_footer()

    def decision_deferred(self, message: str, decision_id: str = "") -> None:
        # A kernel-side deferral (real runtime) already parked its item in
        # the shared queue — parking again would double the badge count.
        # Message-only deferrals (demo script, mounted-hook notices) still
        # derive the item through the adapter and park it here.
        parked_item = (
            next((i for i in self.adapter.needs_you.items if i.decision_id == decision_id), None)
            if decision_id
            else None
        )
        if parked_item is not None:
            item = parked_item
        else:
            question, reason, choices, highlight, action = self.adapter.deferred_decision(
                message, decision_id
            )
            item = self.adapter.needs_you.defer(
                question, reason, choices=choices, highlight=highlight, action=action
            )
        # A deferred decision blocks on the human: always worth notifying,
        # but only ONCE per decision -- a repeated kernel-side ping for an
        # ALREADY-parked item (e.g. a second dependent tool call blocked on
        # the same decision) dedupes by the decision's own stable id
        # (B7 AC3) instead of re-ringing the bell. A governance/tool-action
        # deferral carries a denied ``action`` key; a question-tool ask
        # does not -- that distinguishes approval from clarification.
        occasion = item.decision_id
        reason_kind: notifications.AttentionReason = (
            "awaiting_approval" if item.action else "awaiting_clarification"
        )
        self._notify_attention(reason_kind, occasion=occasion, detail=message)
        self._refresh_footer()

    def attention_error(self, detail: str, *, occasion: str) -> None:
        """``ReducerHost`` hook for a session-level error transition detected
        by the reducer (B7 gap 3): a provider/runtime notice, or a delegate
        settling into the terminal ``error`` lane state. Just forwards to
        the SAME normalized ladder as every other reason -- no parallel
        notion of "attention-worthy error" lives here.
        """
        self._notify_attention("error", occasion=occasion, detail=detail)

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

    def lane_tail_updated(self, text: str) -> None:
        # Throttle + focus policy live in the reducer (design doc D4); this
        # just paints. The tail renders under its lane's row in the lanes
        # panel (issue #90) — co-located with the agent it streams for, not a
        # detached strip. Child streams (thinking/narration) render in the
        # lanes panel ONLY: the old main-chat delegate tail mirrored the
        # same text under the working line, duplicating lane content into
        # the chat transcript — the chat now carries compact lifecycle
        # markers instead (reducer._agent_spawned/_agent_completed).
        #
        # D5 AC5: resync the panel's tailed-row pointer from the
        # authoritative registry FIRST. This callback's own D4 throttle is
        # independent of (and can now run ahead of) the coalesced
        # lanes_changed() repaint, so ``show_lane_tail`` must not depend on
        # that repaint having already landed to find its row.
        tailed = self.lanes.tail_lane
        if tailed is not None:
            self.lanes_panel.sync_tailed(tailed.session_id)
        self.lanes_panel.show_lane_tail(text)

    def lane_tail_cleared(self) -> None:
        self.lanes_panel.clear_lane_tail()

    # -- approvals -------------------------------------------------------------------

    def boot_progress(self, action: str, detail: str) -> None:
        """Live boot feedback: the AMPLIFIER splash with the phase beneath.

        Module prepare can run for minutes on a cold cache; the
        supervisor sees the wordmark plus each phase ('preparing ·
        tui', foundation's per-module install messages, 'creating ·
        session') instead of a blank screen. Dissolved by
        ``announce_ready`` via :meth:`clear_boot_progress`.
        """
        if not self.is_running:
            # Late callbacks after the app exited (quit during boot) land in
            # a context with no active app; painting would raise
            # NoActiveAppError and spam the terminal post-exit.
            return
        action = action.replace("_", " ")  # foundation emits snake_case phases
        if self._splash is None:
            self._splash = BootSplash(id="boot-splash")
            # This runs as a raw call_soon_threadsafe callback — no Textual
            # context (active_app unset). Mounting here would create the
            # widget's pump and timer tasks in that empty context, and the
            # splash timer would die on its first tick (Timer._tick reads
            # active_app with no fallback). call_later hops into the app's
            # message pump, same as present_approval.
            self.call_later(self._mount_splash, self._splash)
        try:
            self._splash.set_status(f"{action} · {detail}" if detail else action)
        except (RuntimeError, LookupError):
            # is_running can flip between the guard and the paint during
            # teardown; a lost status frame beats a traceback.
            pass

    async def _mount_splash(self, splash: BootSplash) -> None:
        await self.query_one("#transcript-region").mount(splash)

    def clear_boot_progress(self, *, immediate: bool = False) -> None:
        """Dismiss the splash — dissolving normally, instantly on failure.

        The dismissal hops through call_later so it queues FIFO behind a
        still-pending ``_mount_splash`` (ready can land while the mount
        callback is queued) and runs with proper Textual context.
        """
        if self._splash is not None:
            splash = self._splash
            self._splash = None
            self.call_later(splash.dismiss_splash, immediate=immediate)

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

    def on_approval_bar_deferred(self, message: ApprovalBar.Deferred) -> None:
        """ctrl-y on the approval bar: park the live ticket into the
        needs-you queue WITHOUT answering it (deny-and-continue), then
        hand the composer back. The decision stays retro-answerable via
        ctrl-y — answering it later becomes a next-turn instruction
        (ADR-0007 resolution 5). No journal ask is recorded: the ticket
        was deferred, not decided.
        """
        message.stop()
        bar = self.approval_bar
        if bar is None:
            return
        prompt, options = bar.prompt, bar.options
        bar.remove()
        self.approval_bar = None
        self.composer.display = True
        self.composer.focus_input()
        # Real runtime routes through the broker (which parks the shared
        # needs-you item and fires the decision Notification the app
        # already handles); the demo runtime parks directly in the base
        # adapter. Either way the footer badge reflects the deferral.
        self.adapter.defer_approval(message.ticket_id, prompt, options)
        self.show_notice("decision deferred to queue · answer later with ctrl-y")
        self._refresh_footer()

    # -- composer semantics -----------------------------------------------------------

    def on_composer_submit(self, message: Composer.Submit) -> None:
        message.stop()
        text = message.text
        pending_custom = getattr(self, "_pending_custom_decision", "")
        if pending_custom and text and not text.startswith("/"):
            # A free-text answer to a needs-you question (donor custom option):
            # answer the parked decision instead of starting a turn.
            self._pending_custom_decision = ""
            close_file_mentions(self)
            app_support.apply_decision(self, pending_custom, text)
            self._refresh_footer()
            return
        # Persist for cross-session ↑ recall (mirrors the composer's own
        # in-memory ring; the adapter scrubs + dedups + caps). Base/demo
        # adapters no-op, so only real sessions write to disk.
        self.adapter.record_prompt(text)
        close_file_mentions(self)
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
            # Story #1 amendment to the mockup: zero matches no longer
            # falls through as chat — an unrecognized /command costs a
            # notice, never a silent provider turn. Skills + shortcuts
            # registered at boot resolve above via parse_and_run.
            name = text.split(maxsplit=1)[0]
            # AC3: a typo'd command/alias gets nearby suggestions instead
            # of a bare rejection — never a silent fall-through to chat.
            suggestions = self._commands.suggest(name, limit=1)
            hint = f" · did you mean {' or '.join(suggestions)}?" if suggestions else ""
            self.show_notice(f"unknown command: {name}{hint} · / lists commands")
            self._refresh_footer()
            return
        self.submit_prompt(text, message.attachments)

    def on_composer_paste_image(self, message: Composer.PasteImage) -> None:
        message.stop()
        self.run_worker(self._paste_clipboard_image(), exclusive=False)

    async def _paste_clipboard_image(self) -> None:
        """Read the system clipboard image off-thread, stage it on the
        composer as an ``[Image #N]`` placeholder (amplifier-app-cli parity)."""
        import asyncio

        from ..kernel.clipboard import read_clipboard_image

        try:
            attachment = await asyncio.to_thread(read_clipboard_image)
        except Exception:  # noqa: BLE001 — clipboard read is best-effort
            attachment = None
        if attachment is None:
            self.show_notice("no image in clipboard")
            return
        self.composer.add_image(attachment)
        kb = len(attachment.data) // 1024
        self.show_notice(f"image attached · {attachment.media_type.split('/')[-1]} · {kb} KB")

    def on_composer_steer(self, message: Composer.Steer) -> None:
        message.stop()
        self.adapter.record_prompt(message.text)
        close_file_mentions(self)
        # Mockup onKeyDown: an open palette match runs BEFORE the steer
        # branch — a slash command typed mid-turn runs, never steers (§6).
        selected = self.palette.selected_command if self.palette.is_open else None
        if selected is not None:
            self.palette.apply_filter(None)
            if not self._commands.parse_and_run(self._ctx, message.text):
                self._commands.run(selected.name, self._ctx)
            self._refresh_footer()
            return
        # A focused lane targets THAT delegate: mid-turn Enter steers the
        # running child at its next step boundary (issue #39), not the root.
        focused = self.transcript.focused_lane
        if focused is not None:
            record = self.lanes.get(focused)
            if record is not None and record.lane.state not in TERMINAL_LANE_STATES:
                app_support.echo_lane_steer(self, record.session_id, message.text)
                return
        if self.adapter.steering.pending_steers:
            self._queue_message(message.text)  # second steer queues (spec §5)
            return
        app_support.echo_steer(self, message.text)

    def on_composer_queue_message(self, message: Composer.QueueMessage) -> None:
        message.stop()
        self.adapter.record_prompt(message.text)
        close_file_mentions(self)
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
        close_file_mentions(self)
        self.palette.apply_filter(message.filter)
        self._refresh_footer()

    def on_composer_palette_filter_cleared(self, message: Composer.PaletteFilterCleared) -> None:
        message.stop()
        self.palette.apply_filter(None)
        self._refresh_footer()

    def on_file_mention_intent(self, message: FileMentionIntent) -> None:
        handle_file_mention_intent(self, message)

    def on_composer_history_suggested(self, message: Composer.HistorySuggested) -> None:
        # The frecency-recall ghost changed; reflect it on the strip above
        # the composer (None hides it). The up-ring is untouched.
        message.stop()
        self.history_recall.show(message.suggestion)

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

    def on_title_bar_title_changed(self, message: TitleBar.TitleChanged) -> None:
        """Mirror the in-app title into the native terminal window/tab title."""
        message.stop()
        self.title = message.terminal_title
        write_terminal_title(self._driver, message.terminal_title)

    def copy_to_clipboard(self, text: str) -> None:
        """Clipboard writes go BOTH ways: OSC 52 (Textual's built-in, works
        over SSH) AND the OS clipboard tool when one exists (pbcopy /
        wl-copy / xclip). iTerm2 ships with OSC 52 writes DISABLED, so
        relying on the escape alone silently copied nothing (user report:
        "can't copy still"). One choke point — ctrl+c and any /copy-style
        command all route through here."""
        super().copy_to_clipboard(text)
        self._clipboard_write_seq += 1
        sequence = self._clipboard_write_seq
        self._os_clipboard_copied = app_support.os_clipboard_available()
        if self._os_clipboard_copied:
            self.run_worker(
                self._copy_to_os_clipboard(text, sequence),
                exclusive=False,
            )

    async def _copy_to_os_clipboard(self, text: str, sequence: int) -> None:
        """Run the potentially blocking native writer outside the UI loop.

        Writes are serialized so an older slow ``pbcopy`` can never finish
        after a newer selection and overwrite it. Pending stale writes are
        skipped before they reach the OS tool.
        """

        async with self._clipboard_write_lock:
            if sequence != self._clipboard_write_seq:
                return
            copied = await asyncio.to_thread(app_support.os_clipboard_copy, text)
            if sequence == self._clipboard_write_seq:
                self._os_clipboard_copied = copied

    def action_copy_selection(self) -> None:
        """ctrl+c: copy the composer's own selection, else the transcript
        drag-selection (always confirms — clipboard writes are invisible).

        With NOTHING selected, honor the terminal/Mac convention that ctrl+c
        interrupts/kills rather than being a dead no-op: a running turn is
        interrupted (like esc), and an idle app quits (like ctrl+d). Copy always
        wins whenever text is actually selected, so selecting-then-ctrl+c still
        copies."""
        text = self.composer.selected_text or self.screen.get_selected_text()
        if not text:
            if self.turn_active:
                self.interrupt_turn()
                self.show_notice("interrupting… (ctrl+c)")
            else:
                self.exit()
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

    def on_composer_open_external_editor(self, message: Composer.OpenExternalEditor) -> None:
        """ctrl+e: suspend the TUI, compose the draft in $VISUAL/$EDITOR, and
        read it back into the composer (normalized).

        The kernel owns the temp-file round-trip (pure, no Textual); the app
        owns the terminal suspension, so no pure-logic module ever imports
        Textual (ADR-0007). Every outcome restores the TUI cleanly -- the
        draft is only replaced on a successful, non-empty edit; every other
        path leaves it untouched and explains itself with a notice.
        """
        message.stop()
        import os
        import subprocess

        from textual.app import SuspendNotSupported

        from ..kernel import external_editor

        seed = self.composer.editor_seed()

        def runner(argv: list[str], cwd: str | None) -> int:
            # App.suspend hands the real terminal to the editor (donor
            # renderer.suspend()/resume()); blocking here is fine -- the app
            # is not painting while suspended.
            with self.suspend():
                completed = subprocess.run(argv, cwd=cwd)
            return completed.returncode

        try:
            outcome = external_editor.compose_in_editor(
                seed, runner=runner, environ=os.environ, cwd=None
            )
        except SuspendNotSupported:
            self.show_notice("external editor needs a real terminal (suspend unsupported)")
            return
        if outcome.ok:
            self.composer.apply_editor_result(outcome.text)
            self.composer.focus_input()
        elif outcome.status == "no_editor":
            self.show_notice(outcome.detail)  # setup hint, not an editor error
        elif outcome.status == "empty":
            self.show_notice("editor left the draft empty · unchanged")
        else:
            self.show_notice(f"editor · {outcome.detail}")

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
            # Real sessions have no scripted lane logs — the reducer
            # accumulates each child's diverted events into a focus
            # transcript instead (DESIGN-SPEC §8).
            blocks = self.reducer.lane_transcript(message.session_id or message.name)
        if blocks is None:
            self.show_notice(f"no transcript for lane · {message.name}")
            return
        # The panel stays open while a lane is focused (mockup focusLane
        # never touches lanesOpen); its row snaps to the focused lane.
        self.lanes_panel.set_focused(message.name)
        # Esc must resolve via ESC_CHAIN (lane_focus first, lanes later),
        # so the keyboard returns to the composer, not the panel.
        self.composer.focus_input()
        if not self._lane_focus_intro_shown:
            # First-ever focus transition (S6 AC4): a transient notice
            # announcing the exit path, not a permanent tutorial overlay —
            # never repeats once the user has seen it.
            self._lane_focus_intro_shown = True
            self.show_notice(app_support.LANE_FOCUS_INTRO_NOTICE)
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

    def on_back_to_parent(self, message: BackToParent) -> None:
        """Focus-header Back control (click or enter/space): the exact
        same navigation seam as Escape's ``lane_unfocus`` action — never
        a turn interrupt/cancel (S6 AC2/AC5)."""
        message.stop()
        app_support.go_back_to_parent(self)

    def on_delegate_summary_toggled(self, message: DelegateSummaryToggled) -> None:
        """Drill-down v1 (ambient-progress D5): an expanded summary opens the
        lanes panel — the full lane transcript stays one Enter away there."""
        if message.expanded:
            self.lanes_panel.show_panel(focus=False)

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

    def on_sessions_strip_session_activated(self, message: SessionsStrip.SessionActivated) -> None:
        """A session row was activated (Enter or click) -- S2 gap 1 + 2:
        show its full-id detail rather than an in-place resume (the
        stored-session roster has always been read-only here), and
        best-effort copy the full id via the app's existing clipboard
        helper (OSC 52 + OS tool where available; the detail block below
        is the reliable fallback -- terminal clipboard access is
        environment-dependent)."""
        message.stop()
        summary = next(
            (s for s in self.sessions_strip.summaries if s.session_id == message.session_id),
            None,
        )
        self.sessions_strip.close_strip()
        self._restore_keyboard()
        self._refresh_footer()
        if summary is None:
            return
        self.copy_to_clipboard(summary.session_id)
        self.append_block(Answer(id=self.allocator.next_id(), spans=session_detail_spans(summary)))

    def on_sessions_strip_resume_requested(self, message: SessionsStrip.ResumeRequested) -> None:
        """``r``, or a click on a row's resume glyph -- S2 gap 2: post the
        session's exact ready-to-run ``amplifier-tui resume SESSION_ID``
        command rather than an in-place teardown (the stored-session
        roster stays read-only, same as :meth:`on_sessions_strip_session_
        activated` above) -- and best-effort copy that command (not the
        bare id) to the clipboard, so pasting it into a new terminal just
        works."""
        message.stop()
        summary = next(
            (s for s in self.sessions_strip.summaries if s.session_id == message.session_id),
            None,
        )
        self.sessions_strip.close_strip()
        self._restore_keyboard()
        self._refresh_footer()
        if summary is None:
            return
        from .session_ops_view import resume_command_for

        self.copy_to_clipboard(resume_command_for(summary))
        self.append_block(Answer(id=self.allocator.next_id(), spans=session_resume_spans(summary)))

    def on_sessions_strip_closed(self, message: SessionsStrip.Closed) -> None:
        message.stop()
        self._restore_keyboard()
        self._refresh_footer()

    def on_open_rewind(self, message: OpenRewind) -> None:
        index = next(
            (i for i, c in enumerate(self.ledger.checkpoints) if c.id == message.checkpoint_id),
            None,
        )
        self.open_rewind_strip(index)

    def on_copy_code_fence(self, message: CopyCodeFence) -> None:
        # Clicking a fenced code block copies just that fence (/copy still
        # grabs the whole answer). A transcript click must not strand focus
        # on the scroll container.
        self._restore_keyboard()
        self.copy_to_clipboard(message.text)
        self.show_notice(f"copied code · {len(message.text)} chars")

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
        if last is not None and last.kind == "evidence" and last.links == tuple(message.links):
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
                    self.transcript.scroll_block_visible(block.id)
                    return
        # No correlated tool line in the transcript: surface the grounding
        # reference itself instead of silently doing nothing.
        self.show_notice(f"grounded by {link.tool_ref}")

    def on_open_evidence_detail(self, message: OpenEvidenceDetail) -> None:
        """``d`` on the evidence block (D7 AC4): open the side panel's
        detail for the selected claim, refresh it for a different claim,
        or toggle-close it for the same one (a second ``d``)."""
        target = (message.block_id, message.link)
        if self._evidence_panel_target == target and self.evidence_panel.is_open:
            self.close_evidence_panel()
            return
        if self.size.width < app_support.EVIDENCE_PANEL_MIN_WIDTH:
            # A dead control would silently do nothing; say why instead.
            self.show_notice(
                "evidence detail needs a wider terminal "
                f"(≥{app_support.EVIDENCE_PANEL_MIN_WIDTH} cols)"
            )
            return
        if not self.evidence_panel.is_open:
            # First open in this view: remember the row + scroll to
            # restore on close (AC3). Refreshing to a different claim
            # while already open must NOT re-capture — that would
            # overwrite the anchor with the panel already-open state.
            self.transcript.capture_evidence_focus(message.block_id)
        record = self.adapter.evidence_tool_call(message.link.tool_call_id)
        detail = build_evidence_detail(message.link, record)
        self.evidence_panel.show_detail(detail)
        self._evidence_panel_target = target
        # The docked panel's width claim reflows the transcript; keep the
        # evidence row itself in view through that reflow.
        self.transcript.scroll_block_visible(message.block_id)

    def close_evidence_panel(self) -> None:
        """Dismiss the evidence detail panel (D7 AC3): restores focus and
        scroll position to the evidence row exactly as before the panel
        opened. A no-op while already closed."""
        if not self.evidence_panel.is_open:
            return
        self.evidence_panel.close()
        self._evidence_panel_target = None
        self.transcript.restore_evidence_focus()

    def on_close_evidence(self, message: CloseEvidence) -> None:
        """Esc on the evidence block: close it and hand the keyboard back."""
        if self.transcript.get_block(message.block_id) is not None:
            self.transcript.remove_block(message.block_id)
        if (
            self._evidence_panel_target is not None
            and self._evidence_panel_target[0] == message.block_id
        ):
            # The whole block is gone — no row left to restore focus to;
            # _restore_keyboard() below hands focus to the composer instead.
            self.evidence_panel.close()
            self._evidence_panel_target = None
        self._restore_keyboard()

    def on_needs_you_list_decision_taken(self, message: NeedsYouList.DecisionTaken) -> None:
        message.stop()
        # Decision rows/chips stop their Click events (a row click must not
        # double-fire through the app's generic transcript-click handler),
        # so restore the keyboard here: transcript clicks never strand it
        # (DESIGN-SPEC §12; the composer keeps focus through every click).
        self._restore_keyboard()
        app_support.apply_decision(self, message.item_id, message.choice)

    def on_needs_you_list_custom_answer_requested(
        self, message: NeedsYouList.CustomAnswerRequested
    ) -> None:
        message.stop()
        # Donor "Type your own answer": route the NEXT composer submit into
        # this decision's answer instead of a turn (see on_composer_submit).
        self._pending_custom_decision = message.item_id
        self._restore_keyboard()
        self.show_notice("type your answer, then enter to submit · esc to cancel")

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

    def on_footer_bar_waiting_badge_clicked(self, message: FooterBar.WaitingBadgeClicked) -> None:
        message.stop()
        self.action_show_needs_you()

    # -- key actions ------------------------------------------------------------------------

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in ("palette_up", "palette_down"):
            # Mockup onKeyDown: the approval branch consumes ArrowUp/Down
            # before any palette handling — arrows always cycle a pending
            # approval's selection (spec §7).
            return self.approval_bar is None and self.palette.is_open
        if self.approval_bar is not None and action in (
            "cycle_mode",
            "cycle_permission",
            "cycle_effort",
        ):
            # Mockup keydown: while an approval is open, Tab (with or
            # without shift) cycles the approval selection and returns —
            # cycleMode is unreachable, and the trust posture must not
            # change under a pending approval (spec §7).
            return False
        if self.approval_bar is not None and action == "show_needs_you":
            # The bar owns the keyboard (spec §7): ctrl-y parks THIS
            # ticket (ApprovalBar.Deferred) instead of opening the
            # needs-you listing. Disable the global chord so the key
            # reaches the bar (same seam as arrows above).
            return False
        return True

    def action_cycle_mode(self) -> None:
        self.set_mode_by_id(cycle_mode(self._mode.id).id)

    def action_cycle_permission(self) -> None:
        self.show_notice(f"trust · {self._effective_trust_str()} · edit via /permissions")

    def action_cycle_effort(self) -> None:
        """ctrl+e: advance the reasoning-effort tier one step in the ring."""
        self.session_ops.cycle_effort()

    def action_toggle_lanes(self) -> None:
        if self.lanes_panel.display:
            self.lanes_panel.hide_panel()
            self._restore_keyboard()
        else:
            self.lanes_panel.update_lanes(
                self.lanes.lanes,
                queued_counts=self.adapter.lane_steering.counts(),
            )
            self.lanes_panel.show_panel()
            if self.approval_bar is not None:
                self.approval_bar.focus()  # approval owns the keyboard (spec §7)
        self._refresh_footer()

    def action_cycle_tail(self) -> None:
        """ctrl+o: pin the live tail to the next running lane (spec §8)."""
        record = self.lanes.cycle_tail_focus()
        if record is None:
            self.show_notice("no running lanes to tail")
            return
        self.lanes_changed()  # repaints the ▸ marker with the new pin
        self.reducer.repaint_lane_tail()  # tail switches with the pin, not on next delta
        self.show_notice(f"tail · {record.lane.name}")

    def action_toggle_thinking(self) -> None:
        """ctrl+g: expand/collapse thinking (issue #129).

        The durable home is the transcript's collapsible Thinking block, so
        ctrl-g toggles the newest one and scrolls it into view. When no
        durable block exists yet (a still-streaming demo turn), it falls
        back to PR #128's ephemeral live-tail reveal (peek ⇄ content)."""
        for block in reversed(self.transcript.blocks):
            if block.kind == "thinking" and block.text:
                toggled = block.model_copy(update={"expanded": not block.expanded})
                self.transcript.replace(toggled)
                self.transcript.scroll_block_visible(block.id)
                self.show_notice(
                    "thinking · expanded" if toggled.expanded else "thinking · collapsed"
                )
                return
        revealed = self.live_tail.toggle_reveal()
        self.show_notice("thinking · shown" if revealed else "thinking · hidden")

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
        # Take the keyboard so number keys answer the decision (the donor
        # number-key + enter flow); the block hands focus back to the composer
        # on answer via _restore_keyboard. A deliberate ctrl-y "review" grab,
        # like the evidence block -- not a stray transcript click.
        self.call_after_refresh(self._focus_needs_you)

    def _focus_needs_you(self) -> None:
        widgets = self.query(NeedsYouList)
        if widgets:
            try:
                self.set_focus(widgets.last())
            except Exception:  # noqa: BLE001 -- focus is best-effort
                pass

    def action_open_rewind(self) -> None:
        self.open_rewind_strip(None)

    def action_return_to_answer(self) -> None:
        """ctrl+f: jump back to the current/most-recent turn's final-answer
        start anchor (AC2, compliance 2026-08-02 B1).

        Scans the visible transcript (main, or a focused lane's own list --
        whichever ``self.transcript.blocks`` currently holds) for the most
        recent ``Answer`` the reducer stamped ``final`` and scrolls its
        START into view, same block-id targeting as toggle_thinking. A
        long answer's start survives scrolling away and back, resume
        replay, and history navigation because it rides the block's own
        persisted state, not a transient scroll position."""
        for block in reversed(self.transcript.blocks):
            if block.kind == "answer" and block.final:
                self.transcript.scroll_block_visible(block.id, top=True)
                self.show_notice("back to the final answer")
                return
        self.show_notice("no final answer yet")

    def action_plan_drilldown(self) -> None:
        """ctrl+n: cycle the plan panel's row window (default → +2 → +3).

        The todo data model is flat (no sub-items), so drilling honestly
        shows MORE rows of the same list; a hidden ``⋮ +N more`` shrinks
        accordingly. No-op notice when the panel is not on screen."""
        if not self.plan_panel.display:
            self.show_notice("no plan panel to drill")
            return
        extra = self.plan_panel.cycle_drill()
        self.show_notice(plan_drill_notice(extra))

    def action_toggle_plan_overflow(self) -> None:
        """ctrl+h: reach + toggle the plan panel's hidden-rows control.

        S7 gap 1: Enter/Space already activated ``_PlanOverflowControl``
        once it had focus, but nothing gave a keyboard-only user a way to
        REACH it -- Tab is not a general focus chain here (mention/approval
        claim it; shift+tab is cycle_mode), and the composer intentionally
        keeps focus so typing always steers (S6's FocusHeader made the
        same call, documented in its own docstring). This follows the
        app's real idiom instead of Tab order: a dedicated global chord
        that acts on the panel directly, exactly like ctrl+n's
        action_plan_drilldown right above -- so the composer never loses
        focus mid-typing. No-op notices mirror that action's own shape.
        """
        if not self.plan_panel.display:
            self.show_notice("no plan panel to expand")
            return
        if not self.plan_panel.overflow_control.display:
            self.show_notice("plan · nothing hidden to expand")
            return
        expanded = self.plan_panel.toggle_expand()
        self.show_notice(plan_overflow_notice(expanded))

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
        self.append_block(UserLine(id=self.allocator.next_id(), text=text, mode=self._mode.id))

    def context_usage(self) -> ContextUsage:
        window = self.adapter.compaction.max_tokens
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
            app_support.permissions_block(
                self.permissions, self._effective_trust_str(), self.allocator
            )
        )

    # -- painting ---------------------------------------------------------------------------------

    def _effective_trust_str(self) -> str:
        """Auto's posture string, truthful about live governance (not the
        profile's static string — the gate is a settings toggle)."""
        from ..model.modes import effective_trust_str

        return effective_trust_str(self._mode, gated_auto=self.adapter.gated_auto)

    def footer_context(self) -> keymap.Context:
        if self.approval_bar is not None:
            return "approval"
        if self.transcript.focused_lane is not None:
            return "lane_focus"
        if self.palette.is_open:
            return "palette"
        if self.sessions_strip.is_open:
            return "sessions"
        if self.turn_active:
            return "running"
        return "idle"

    def _refresh_footer(self) -> None:
        self.footer_bar.update_state(app_support.footer_state(self))

    def _refresh_title(self) -> None:
        self.title_bar.state_text = self.reducer.title_state()
        # The resolved bundle URI/path, not just its short name (D4 gap 1) --
        # TitleBar fits it to the live terminal width itself (D4 gap 2).
        self.title_bar.bundle_uri = self.adapter.bundle_uri
        self.title_bar.session_short = self.adapter.session_short

    def refresh_status(self) -> None:
        self._refresh_title()
        self._refresh_footer()


__all__ = ["TuiApp"]
