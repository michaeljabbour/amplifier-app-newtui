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

import asyncio
import threading
import time
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
from ..model.lanes import LaneRegistry
from ..model.modes import DEFAULT_MODE, ModeProfile, cycle_mode, get_mode
from ..model.turn import OutcomeLedger
from . import app_support, keymap
from .approval_bar import ApprovalBar
from .chrome import APP_TITLE_NAME, TitleBar, write_terminal_title
from .command_context import AppCommandContext
from .composer import Composer
from .footer import FooterBar
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
from .plan_panel import PlanPanel
from .queued_strip import QueuedStrip
from .reducer import TranscriptReducer
from .rewind_strip import RewindStrip
from .runtime_adapter import RuntimeAdapter
from .session_ops_view import (
    diff_spans,
    mcp_spans,
    model_listing_spans,
    names_spans,
    skill_loaded_spans,
    skills_spans,
    status_spans,
)
from .splash import BootSplash
from .themes import DEFAULT_THEME, THEME_NAME_PREFIX, THEME_TOKENS, register_themes, theme_id
from .transcript import (
    BlockWidget,
    CloseEvidence,
    DelegateSummaryToggled,
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
        self._splash: BootSplash | None = None  # boot splash overlay (wordmark)
        self._auto_native_mode: str | None = None  # posture-bridged native mode
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
        self.plan_items: tuple[TodoItem, ...] = ()  # latest root todo list
        self.title_bar = TitleBar(id="title-bar")
        self.transcript = TranscriptView(id="transcript")
        self.live_tail = LiveTail(id="live-tail")
        self.notice_slot = NoticeSlot(id="notice-slot")
        self.palette = PaletteStrip(self._commands.specs, id="palette-strip")
        # Open registry (story #2): any runtime registration — skills at
        # boot, recipe/pipeline verbs later — re-feeds the palette rows.
        self._commands.subscribe(self._sync_palette_commands)
        self.lanes_panel = LanesPanel(id="lanes-panel")
        self.plan_panel = PlanPanel(id="plan-panel")
        self.rewind = RewindStrip(id="rewind-strip")
        self.queued_strip = QueuedStrip(id="queued-strip")
        self.file_mentions = FileMentionStrip(id="file-mentions")
        self.composer = Composer(kitty_protocol=kitty_protocol, id="composer")
        self.footer_bar = FooterBar(id="footer-bar")

    def compose(self) -> ComposeResult:
        yield self.title_bar
        with Container(id="transcript-region"):
            yield self.transcript
            yield self.live_tail
            yield self.notice_slot
        yield self.palette
        with Horizontal(id="bottom-strip"):
            yield self.lanes_panel
            yield self.plan_panel
        yield self.rewind
        yield self.queued_strip
        yield self.file_mentions
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
        if not self.screen_stack:
            return  # debounce timer outlived the app (shutdown race)
        text = self.screen.get_selected_text()
        if not text or text == self._last_selection_copied:
            return
        self._last_selection_copied = text
        self.copy_to_clipboard(text)
        self.show_notice(f"copied on select · {len(text)} chars")

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


    async def _boot_runtime(self) -> None:
        self.adapter.attach(self)
        try:
            await self.adapter.start(lambda: app_support.announce_ready(self))
            self.file_mentions.set_files(await self.adapter.workspace_files())
            self._register_skill_commands(await self.adapter.list_skills())
        except Exception as error:  # boot failed — show why, don't crash out
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
        self.run_worker(self.adapter.submit(text, attachments), exclusive=False)

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
        if self._splash is not None:
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

    # -- in-session ops (/model /effort /compact /clear /status /tools …) ----
    # Each is a sync trigger the command handler calls; the async body runs
    # on a worker so the coordinator call marshals through the adapter to the
    # runtime loop without blocking the UI (mirrors _show_native_modes).

    def _ops_starting(self) -> bool:
        """True (and notices) when the session banner has not landed yet."""
        if self._splash is not None:
            self.show_notice("session still starting · try again once the banner lands")
            return True
        return False

    def show_status(self) -> None:
        self.run_worker(self._show_status(), exclusive=False)

    async def _show_status(self) -> None:
        info = await self.adapter.status()
        self.append_block(
            Answer(
                id=self.allocator.next_id(),
                spans=status_spans(
                    info,
                    mode=self.mode_id,
                    bundle=self.adapter.bundle_name,
                    session_short=self.adapter.session_short,
                    cost=self.reducer.session_cost,
                    compaction=self.adapter.compaction,
                ),
            )
        )

    def show_model(self, arg: str) -> None:
        if arg and self._ops_starting():
            return
        self.run_worker(self._show_model(arg), exclusive=False)

    async def _show_model(self, arg: str) -> None:
        if arg:
            ok, detail = await self.adapter.set_model(arg)
            if ok:
                self.refresh_status()  # footer model field is adapter-derived
            self.show_notice(f"model · {detail}" if ok else detail)
            return
        listing = await self.adapter.list_models()
        self.append_block(
            Answer(id=self.allocator.next_id(), spans=model_listing_spans(listing))
        )

    def apply_effort(self, arg: str) -> None:
        if arg and self._ops_starting():
            return
        self.run_worker(self._apply_effort(arg), exclusive=False)

    async def _apply_effort(self, arg: str) -> None:
        if arg:
            ok, detail = await self.adapter.set_effort(arg)
            self.show_notice(f"effort · {detail}" if ok else detail)
            return
        current = await self.adapter.get_effort()
        self.show_notice(f"effort · {current or '(default)'} · /effort <level> to set")

    def compact_context(self, focus: str) -> None:
        if self._ops_starting():
            return
        self.run_worker(self._compact_context(focus), exclusive=False)

    async def _compact_context(self, focus: str) -> None:
        ok, detail = await self.adapter.compact(focus)
        self.show_notice(f"compacted · {detail}" if ok else detail)

    def clear_context(self) -> None:
        if self._ops_starting():
            return
        self.run_worker(self._clear_context(), exclusive=False)

    async def _clear_context(self) -> None:
        ok, count = await self.adapter.clear_context()
        self.show_notice(
            f"context cleared · {count} messages dropped"
            if ok
            else "clear unavailable in this session"
        )

    def show_tools(self) -> None:
        self.run_worker(self._show_tools(), exclusive=False)

    async def _show_tools(self) -> None:
        names = await self.adapter.list_tools()
        self.append_block(
            Answer(
                id=self.allocator.next_id(),
                spans=names_spans("Tools", names, "no tools mounted"),
            )
        )

    def show_agents(self) -> None:
        self.run_worker(self._show_agents(), exclusive=False)

    async def _show_agents(self) -> None:
        names = await self.adapter.list_agents()
        self.append_block(
            Answer(
                id=self.allocator.next_id(),
                spans=names_spans(
                    "Agents", names, "no agents · bundle has no agents: include: block"
                ),
            )
        )

    _DIFF_STAGED_ARGS = frozenset({"staged", "cached", "--staged", "--cached"})

    def show_diff(self, arg: str) -> None:
        self.run_worker(self._show_diff(arg), exclusive=False)

    async def _show_diff(self, arg: str) -> None:
        staged = arg.strip().lower() in self._DIFF_STAGED_ARGS
        patch = await self.adapter.diff(staged)
        self.append_block(
            Answer(id=self.allocator.next_id(), spans=diff_spans(patch, staged=staged))
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
        palette follows via the registry subscription."""
        from ..commands.skills import register_skill_commands

        register_skill_commands(self._commands, skills)

    def show_skills(self) -> None:
        self.run_worker(self._show_skills(), exclusive=False)

    async def _show_skills(self) -> None:
        skills = await self.adapter.list_skills()
        self.append_block(Answer(id=self.allocator.next_id(), spans=skills_spans(skills)))

    def load_skill(self, name: str) -> None:
        if not name:
            self.show_notice("usage: /skill <name> · /skills lists them")
            return
        if self._ops_starting():
            return
        self.run_worker(self._load_skill(name), exclusive=False)

    async def _load_skill(self, name: str) -> None:
        ok, payload = await self.adapter.load_skill(name)
        if ok:
            self.append_block(
                Answer(id=self.allocator.next_id(), spans=skill_loaded_spans(name, payload))
            )
            self.show_notice(f"skill loaded · {name}")
        else:
            self.show_notice(payload or f"no such skill · {name}")

    def manage_mcp(self, args: str) -> None:
        self.run_worker(self._manage_mcp(args), exclusive=False)

    def manage_directories(self, kind: str, args: str) -> None:
        from .directory_admin import manage

        self.run_worker(manage(self, kind, args), exclusive=False)

    async def _manage_mcp(self, args: str) -> None:
        from ..kernel import mcp_config

        parts = args.split()
        sub = parts[0].lower() if parts else "list"
        path = mcp_config.mcp_config_path()
        if sub in ("", "list"):
            servers = {
                name: mcp_config.describe_server(spec)
                for name, spec in mcp_config.read_servers(path).items()
            }
            live = await self.adapter.mcp_tools()
            self.append_block(
                Answer(id=self.allocator.next_id(), spans=mcp_spans(servers, live))
            )
        elif sub == "add":
            if len(parts) < 3:
                self.show_notice("usage: /mcp add <name> <command> [args…]")
                return
            mcp_config.add_stdio_server(path, parts[1], parts[2], tuple(parts[3:]))
            self.show_notice(f"mcp server added · {parts[1]} · restart the session to connect")
        elif sub == "remove":
            if len(parts) < 2:
                self.show_notice("usage: /mcp remove <name>")
                return
            removed = mcp_config.remove_server(path, parts[1])
            self.show_notice(
                f"mcp server removed · {parts[1]} · restart to apply"
                if removed
                else f"no such server · {parts[1]}"
            )
        else:
            self.show_notice(f"unknown /mcp subcommand · {sub} (list | add | remove)")

    def turn_started(self) -> None:
        self.turn_active = True
        self._turn_started_at = time.monotonic()
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
        # Attention signal for the suppressed hooks-notify (raw OSC/BEL would
        # corrupt Textual): ring the driver-safe bell after long turns only —
        # policy + rationale in app_support.attention_bell_needed.
        elapsed = (
            0.0 if self._turn_started_at is None else time.monotonic() - self._turn_started_at
        )
        self._turn_started_at = None
        if app_support.attention_bell_needed("turn_finished", elapsed):
            self.bell()
        self.refresh_status()

    def lanes_changed(self) -> None:
        tailed = self.lanes.tail_lane
        self.lanes_panel.update_lanes(
            self.lanes.lanes,
            tailed_session_id=None if tailed is None else tailed.session_id,
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
        app_support.sync_plan_surfaces(self)  # responsive ladder (D2)

    def approval_opened(self, prompt: str, options: tuple[str, ...]) -> None:
        del prompt, options  # presentation runs via present_approval
        self._refresh_footer()

    def decision_deferred(self, message: str, decision_id: str = "") -> None:
        # A kernel-side deferral (real runtime) already parked its item in
        # the shared queue — parking again would double the badge count.
        # Message-only deferrals (demo script, mounted-hook notices) still
        # derive the item through the adapter and park it here.
        parked = decision_id and any(
            item.decision_id == decision_id for item in self.adapter.needs_you.items
        )
        if not parked:
            question, reason, choices, highlight, action = self.adapter.deferred_decision(
                message, decision_id
            )
            self.adapter.needs_you.defer(
                question, reason, choices=choices, highlight=highlight, action=action
            )
        # A deferred decision blocks on the human: always worth the bell.
        if app_support.attention_bell_needed("decision_deferred"):
            self.bell()
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

    def lane_tail_updated(self, text: str) -> None:
        # Throttle + focus policy live in the reducer (design doc D4);
        # this just paints. LiveTail itself refuses while a root stream
        # is open, so preemption is belt-and-braces.
        self.live_tail.show_lane_tail(text)

    def lane_tail_cleared(self) -> None:
        self.live_tail.clear_lane_tail()

    # -- approvals -------------------------------------------------------------------

    def boot_progress(self, action: str, detail: str) -> None:
        """Live boot feedback: the AMPLIFIER splash with the phase beneath.

        Module prepare can run for minutes on a cold cache; the
        supervisor sees the wordmark plus each phase ('preparing ·
        newtui', foundation's per-module install messages, 'creating ·
        session') instead of a blank screen. Dissolved by
        ``announce_ready`` via :meth:`clear_boot_progress`.
        """
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
        self._splash.set_status(f"{action} · {detail}" if detail else action)

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

    # -- composer semantics -----------------------------------------------------------

    def on_composer_submit(self, message: Composer.Submit) -> None:
        message.stop()
        text = message.text
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
            self.show_notice(f"unknown command: {name} · / lists commands")
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
        if self.adapter.steering.pending_steers:
            self._queue_message(message.text)  # second steer queues (spec §5)
            return
        app_support.echo_steer(self, message.text)

    def on_composer_queue_message(self, message: Composer.QueueMessage) -> None:
        message.stop()
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

    def on_composer_palette_filter_cleared(
        self, message: Composer.PaletteFilterCleared
    ) -> None:
        message.stop()
        self.palette.apply_filter(None)
        self._refresh_footer()

    def on_file_mention_intent(self, message: FileMentionIntent) -> None:
        handle_file_mention_intent(self, message)

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
                    self.transcript.scroll_block_visible(block.id)
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

    def action_cycle_tail(self) -> None:
        """ctrl+o: pin the live tail to the next running lane (spec §8)."""
        record = self.lanes.cycle_tail_focus()
        if record is None:
            self.show_notice("no running lanes to tail")
            return
        self.lanes_changed()  # repaints the ▸ marker with the new pin
        self.reducer.repaint_lane_tail()  # tail switches with the pin, not on next delta
        self.show_notice(f"tail · {record.lane.name}")

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
