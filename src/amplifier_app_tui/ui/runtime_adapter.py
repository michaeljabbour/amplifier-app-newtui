"""Runtime adapters: the seam between the Textual app and a runtime.

ADR-0007 §Runtimes: the app consumes one ``asyncio.Queue[UIEvent]`` and
cannot tell a :class:`~amplifier_app_tui.kernel.demo.DemoRuntime`
from a real session. The adapter owns that queue plus the shared
interaction-state queues (steering / needs-you / denial log) so the
kernel wiring and the app act on the SAME objects.

:class:`RuntimeAdapter` is the base contract (all hooks optional);
:class:`RealRuntimeAdapter` wires ``kernel/runtime.RealRuntime`` (lazy
import — ``--demo`` boot never touches amplifier-foundation);
``ui/demo_wiring.DemoRuntimeAdapter`` is the scripted counterpart.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from ..kernel.events import ParsedEvent, UIEvent

from ..kernel.compaction import CompactionConfig
from ..kernel.directory_permissions import DirectoryEntry, DirectoryKind
from ..kernel.prompt_history import PromptHistoryStore
from ..kernel.session_ops import ModelListing, StatusInfo
from ..kernel.session_manager import SessionSummary
from ..model.blocks import BlockIdAllocator, TranscriptBlock
from ..model.config import (
    ConfigChange,
    ConfigSnapshotView,
    SessionConfigState,
    default_config_state,
)
from ..model.evidence import EvidenceLink, ToolCallRecord
from ..model.queues import LaneSteeringQueue, NeedsYouQueue, SteeringQueue
from ..model.terminal import TerminalSurface
from ..model.trust import (
    CapabilityClass,
    DenialLog,
    TrustDecision,
    resolve,
    resolve_capability,
)

if TYPE_CHECKING:
    from .reducer import LaneSeed, TurnSpecLike

logger = logging.getLogger(__name__)

_OpT = TypeVar("_OpT")


@dataclass(frozen=True)
class SessionOp(Generic[_OpT]):
    """One passthrough session op, declared ONCE (ADR-0007 §Runtimes).

    The ~14 in-session ops (``/model`` ``/effort`` ``/status`` ``/tools``
    …) used to be re-declared at every rung of the adapter ladder: a
    neutral stub on the base adapter and a ``_runtime is None`` guard plus
    a thread-marshalling twin on :class:`RealRuntimeAdapter`. This
    descriptor collapses the two adapter rungs onto one seam:

    - ``name`` is the method the adapter and ``kernel/runtime.RealRuntime``
      share — the marshalling target looked up by name;
    - ``demo`` is what the base/demo adapter answers (no session at all);
    - ``starting`` is what the real adapter answers before its runtime
      thread has finished booting.

    The single marshalling seam is :meth:`RuntimeAdapter._run_op`. Adding
    op #15 is one entry in :data:`SESSION_OPS` plus a two-line typed shim
    on the base adapter — never a fifth hand-written twin.
    """

    name: str
    demo: _OpT
    starting: _OpT


# The real adapter's shared "runtime thread still booting" reply for the
# fallible (ok, detail) ops.
_STILL_STARTING = "session still starting"

_INTERRUPT: SessionOp[bool] = SessionOp("interrupt", False, False)
_LIST_NATIVE_MODES: SessionOp[Any] = SessionOp("list_native_modes", "", "")
_SET_NATIVE_MODE: SessionOp[tuple[bool, str]] = SessionOp(
    "set_native_mode",
    (False, "native modes need a real session"),
    (False, _STILL_STARTING),
)
_LIST_MODELS: SessionOp[ModelListing] = SessionOp(
    "list_models",
    ModelListing(provider="", current=""),
    ModelListing(provider="", current=""),
)
_SET_MODEL: SessionOp[tuple[bool, str]] = SessionOp(
    "set_model",
    (False, "switching models needs a real session"),
    (False, _STILL_STARTING),
)
_GET_EFFORT: SessionOp[str | None] = SessionOp("get_effort", None, None)
_SET_EFFORT: SessionOp[tuple[bool, str]] = SessionOp(
    "set_effort",
    (False, "reasoning effort needs a real session"),
    (False, _STILL_STARTING),
)
_COMPACT: SessionOp[tuple[bool, str]] = SessionOp(
    "compact",
    (False, "compaction needs a real session"),
    (False, _STILL_STARTING),
)
_CLEAR_CONTEXT: SessionOp[tuple[bool, int]] = SessionOp("clear_context", (False, 0), (False, 0))
_STATUS: SessionOp[StatusInfo] = SessionOp("status", StatusInfo(), StatusInfo())
_LIST_TOOLS: SessionOp[tuple[str, ...]] = SessionOp("list_tools", (), ())
_LIST_AGENTS: SessionOp[tuple[str, ...]] = SessionOp("list_agents", (), ())
_DIFF: SessionOp[str | None] = SessionOp("diff", None, None)
_WORKSPACE_FILES: SessionOp[tuple[str, ...]] = SessionOp("workspace_files", (), ())
_LIST_SKILLS: SessionOp[tuple[Any, ...]] = SessionOp("list_skills", (), ())
_LOAD_SKILL: SessionOp[tuple[bool, str]] = SessionOp(
    "load_skill",
    (False, "skills need a real session"),
    (False, _STILL_STARTING),
)
_MCP_TOOLS: SessionOp[tuple[str, ...]] = SessionOp("mcp_tools", (), ())
_LOAD_DEFERRED_BUNDLE: SessionOp[tuple[bool, str]] = SessionOp(
    "load_deferred_bundle",
    (False, "loading a bundle needs a real session"),
    (False, _STILL_STARTING),
)

SESSION_OPS: tuple[SessionOp[Any], ...] = (
    _INTERRUPT,
    _LIST_NATIVE_MODES,
    _SET_NATIVE_MODE,
    _LIST_MODELS,
    _SET_MODEL,
    _GET_EFFORT,
    _SET_EFFORT,
    _COMPACT,
    _CLEAR_CONTEXT,
    _STATUS,
    _LIST_TOOLS,
    _LIST_AGENTS,
    _DIFF,
    _WORKSPACE_FILES,
    _LIST_SKILLS,
    _LOAD_SKILL,
    _MCP_TOOLS,
    _LOAD_DEFERRED_BUNDLE,
)
"""The one declaration site for the passthrough session-op ladder."""


class RuntimeAdapter:
    """Base adapter: owns the event queue and shared interaction queues.

    The app calls :meth:`attach` before :meth:`start`; ``start`` must
    call ``ready()`` once session identity (banner/bundle/session) is
    known and BEFORE producing turn events.
    """

    def __init__(self) -> None:
        self.queue: asyncio.Queue[UIEvent] = asyncio.Queue()
        self.steering = SteeringQueue()
        self.lane_steering = LaneSteeringQueue()
        """Per-lane steer FIFOs (issue #39): a message aimed at a running
        delegate, delivered at that child's next step boundary. Shared with
        the kernel wiring so the app and runtime act on the SAME queues."""
        self.needs_you = NeedsYouQueue()
        self.denial_log = DenialLog()
        self.terminal = TerminalSurface()
        """Live terminal width shared with the kernel's width-aware
        surface-hint hook (#35). The app updates it from Textual resize
        events; the RealRuntime reads it at each provider:request."""
        self.app: Any = None
        self.bundle_name: str = ""
        self.bundle_uri: str = ""
        """The actually-resolved bundle URI/path (``RealRuntime.bundle_uri`` /
        ``ResolvedConfig.bundle_uri``) -- distinct from :attr:`bundle_name`,
        which is the short requested name and can differ from where the
        bundle actually loaded from. This is what the UI's one persistent
        bundle display (``TitleBar``) and ``/status`` must show so the
        "full active bundle path" claim (D4 AC1) is true, not just a name."""
        self.model_name: str = ""
        """Primary model id, possibly provider-qualified (``anthropic/x``)."""
        self.session_short: str = ""
        self.session_id: str = ""
        """Full stored-session id, surfaced on exit so the CLI can print the
        exact ``amplifier-tui resume SESSION_ID`` command (S4). Empty for demo
        sessions, which have no resumable store entry."""
        self.banner: tuple[str, str] = ("", "")
        self.session_cost_start: Decimal = Decimal("0")
        self.turn_base: int = 0
        """Restored-history user-message count on resume (checkpoint turn
        ids offset past it — DESIGN-SPEC §9); 0 for fresh/demo sessions."""
        self.restored_history: tuple[tuple[str, str], ...] = ()
        """(role, text) pairs replayed into the transcript on resume."""
        self.restored_events: tuple[ParsedEvent, ...] = ()
        """The resumed session's stored UIEvents, replayed through the
        reducer to rebuild the full transcript (digests, delegate
        summaries, turn rules — DESIGN-SPEC §3/§11); empty means the
        prose ``restored_history`` fallback renders instead."""
        self.startup_notices: tuple[str, ...] = ()
        self.gated_auto: bool = False
        self.mount_report: object | None = None
        """The boot mount report, surfaced for ``/doctor``'s mount check.
        ``None`` for the demo adapter and before ``start()``."""
        self.pending_directive: str = ""
        """A resumed fork child's primed starting directive (``/fork`` /
        ``session fork``), surfaced from ``RealRuntime.pending_directive`` at
        ``start()``. The app consumes it once via
        ``app_support.run_pending_directive``; empty for fresh/demo sessions
        and ordinary resumes."""
        self.compaction = CompactionConfig(auto_compact=True, compact_threshold=0.8)
        self._config_state: SessionConfigState = default_config_state()
        """Live ``/config`` state — shared by demo and real (invariant 4);
        real sessions reseed it from the mount plan at ``start()``."""
        self._config_project_dir: Path = Path.cwd()
        self.session_dir: Path | None = None
        """The live session's durable directory (B7 gap 1), once known --
        ``None`` for the demo adapter and before a real session starts.
        ``ui/app.py`` binds ``AttentionCenter`` to it right after boot."""

    def attach(self, app: Any) -> None:
        """Give the adapter its app handle (approval presentation etc.)."""
        self.app = app

    async def start(self, ready: Callable[[], None]) -> None:
        """Boot the runtime; call ``ready()`` once identity is known."""
        ready()

    async def submit(self, text: str, attachments: tuple[Any, ...] = ()) -> None:
        """Run *text* as a new user turn (with optional image attachments)."""

    async def submit_queued(self, text: str) -> None:
        """Run a queue-drained message as the next turn (spec §5).

        Default: same as :meth:`submit`. The demo adapter overrides it
        to skip its scripted mode notice — mockup ``drainQueue`` runs
        the drained turn without ``setMode``, so nothing overwrites the
        ``queued message picked up`` notice.
        """
        await self.submit(text)

    # -- persistent prompt history (cross-session ↑ recall) ------------------
    # The store is keyed per working directory (ADR-0007: the adapter seam
    # owns filesystem/session access so ``ui/`` stays core-free). The base
    # and demo adapters have no real project on disk, so both no-op — only
    # ``RealRuntimeAdapter`` persists.

    def record_prompt(self, text: str) -> None:
        """Persist a submitted prompt for future sessions in this directory."""

    def prompt_history(self) -> tuple[str, ...]:
        """Prompts submitted in this directory across sessions (oldest first)."""
        return ()

    # -- off-machine push routing (B7 gap 2) ----------------------------------

    def publish_attention(self, payload: Mapping[str, Any]) -> None:
        """Best-effort: mirror a normalized attention transition (*payload*
        from ``ui.notifications.attention_push_payload``) onto the runtime's
        hooks bus as ``attention:recorded``.

        Base/demo no-op: there is no real hooks bus without a live session.
        Never raises and never blocks -- see ``RealRuntimeAdapter``'s
        override for the fire-and-forget cross-thread contract.
        """

    # -- in-session op dispatch (ONE seam; see :class:`SessionOp`) -----------

    async def _run_op(self, op: SessionOp[_OpT], /, *args: Any) -> _OpT:
        """Dispatch a passthrough session op through the single seam.

        The base/demo adapter has no live session, so every op answers
        with its neutral ``demo`` value. :class:`RealRuntimeAdapter`
        overrides ONLY this method to guard the booting runtime and
        marshal the call into the runtime thread — collapsing what used
        to be a hand-written twin per op.
        """
        del args
        return op.demo

    async def interrupt(self) -> bool:
        """Request an interrupt; True when the runtime accepted it."""
        return await self._run_op(_INTERRUPT)

    async def list_native_modes(self) -> Any:
        """Bundle-composed mode catalog (real sessions); "" when absent.
        Typically a mapping with a ``modes`` list of {name, description,
        source} dicts — whatever the mounted mode tool reports."""
        return await self._run_op(_LIST_NATIVE_MODES)

    async def set_native_mode(self, name: str | None) -> tuple[bool, str]:
        """Activate/clear a bundle-provided mode via the native mode tool."""
        return await self._run_op(_SET_NATIVE_MODE, name)

    async def list_models(self) -> ModelListing:
        return await self._run_op(_LIST_MODELS)

    async def set_model(self, model: str) -> tuple[bool, str]:
        return await self._run_op(_SET_MODEL, model)

    async def get_effort(self) -> str | None:
        return await self._run_op(_GET_EFFORT)

    async def set_effort(self, level: str) -> tuple[bool, str]:
        return await self._run_op(_SET_EFFORT, level)

    async def compact(self, focus: str = "") -> tuple[bool, str]:
        return await self._run_op(_COMPACT, focus)

    async def clear_context(self) -> tuple[bool, int]:
        return await self._run_op(_CLEAR_CONTEXT)

    async def status(self) -> StatusInfo:
        return await self._run_op(_STATUS)

    async def list_tools(self) -> tuple[str, ...]:
        return await self._run_op(_LIST_TOOLS)

    async def list_agents(self) -> tuple[str, ...]:
        return await self._run_op(_LIST_AGENTS)

    async def diff(self, staged: bool = False) -> str | None:
        return await self._run_op(_DIFF, staged)

    async def workspace_files(self) -> tuple[str, ...]:
        """Relative paths available to composer ``@file`` autocomplete."""
        return await self._run_op(_WORKSPACE_FILES)

    async def list_skills(self) -> tuple[Any, ...]:
        return await self._run_op(_LIST_SKILLS)

    async def load_skill(self, name: str) -> tuple[bool, str]:
        return await self._run_op(_LOAD_SKILL, name)

    async def mcp_tools(self) -> tuple[str, ...]:
        return await self._run_op(_MCP_TOOLS)

    async def load_deferred_bundle(self, name: str) -> tuple[bool, str]:
        """Compose a deferred overlay into the live session (``/bundle load``)."""
        return await self._run_op(_LOAD_DEFERRED_BUNDLE, name)

    async def deferred_bundles(self) -> tuple[str, ...]:
        """Overlay URIs held back from boot (``bundle.deferred``); () for demo."""
        return ()

    async def rename_session(self, name: str) -> tuple[bool, str]:
        del name
        return (False, "renaming needs a real session")

    async def session_summaries(self) -> tuple[SessionSummary, ...]:
        return ()

    async def branch_session(self, name: str) -> tuple[bool, str]:
        del name
        return (False, "branching needs a real session")

    async def fork_with_directive(self, directive: str) -> tuple[bool, str]:
        del directive
        return (False, "forking needs a real session")

    async def session_tags(self) -> tuple[str, ...]:
        return ()

    async def sessions_by_tag(self, tag: str) -> tuple[SessionSummary, ...]:
        del tag
        return ()

    async def add_session_tags(self, tags: tuple[str, ...]) -> tuple[bool, str]:
        del tags
        return (False, "tagging needs a real session")

    async def remove_session_tags(self, tags: tuple[str, ...]) -> tuple[bool, str]:
        del tags
        return (False, "tagging needs a real session")

    async def directory_entries(self, kind: DirectoryKind) -> tuple[DirectoryEntry, ...]:
        del kind
        return ()

    async def update_directory(
        self, kind: DirectoryKind, operation: str, path: str
    ) -> tuple[bool, str]:
        del kind, operation, path
        return (False, "directory management needs a real session")

    async def fork(self, checkpoint_id: str, ledger: Any) -> None:
        """Fork the session at *checkpoint_id*, then trim *ledger* (spec §9).

        Confirm-then-trim (ADR-0007 §Rewind): the ledger trims only
        after the backend confirms the fork; raise
        :class:`~amplifier_app_tui.kernel.rewind.RewindError` on
        failure and leave everything untouched. The base/demo runtime
        keeps its conversation in memory only, so confirmation is
        immediate.
        """
        ledger.trim_to(checkpoint_id)

    def answer_approval(self, ticket_id: str, choice: str) -> None:
        """Route an approval-bar resolution back to the runtime."""

    # -- /config live session config (base: in-memory state) ----------------
    # The state is shared verbatim by demo and real (ADR-0007 invariant 4);
    # RealRuntimeAdapter reseeds it from the mount plan at start().

    async def config_view(self) -> ConfigSnapshotView:
        """Frozen, thread-hop-safe snapshot of the live config state."""
        return ConfigSnapshotView.of(self._config_state)

    async def config_toggle(self, category: str, name: str, enable: bool) -> tuple[bool, str]:
        """Enable/disable a config item in the session scope."""
        return self._config_state.toggle(category, name, enable=enable)

    async def config_set(self, path: str, value: str) -> tuple[bool, str]:
        """Set a config override (session scope) with type inference."""
        return self._config_state.set_value(path, value)

    async def config_diff(self) -> tuple[ConfigChange, ...]:
        """Changes to the config state since session start."""
        return self._config_state.diff()

    async def config_save(self, scope: str) -> tuple[bool, str]:
        """Persist the session config changes to a settings scope file."""
        from ..kernel.config_ops import save_config

        return save_config(self._config_state, scope=scope, project_dir=self._config_project_dir)

    def defer_approval(self, ticket_id: str, prompt: str, options: tuple[str, ...]) -> None:
        """Park a live approval ticket into the needs-you queue WITHOUT
        answering it (ctrl-y on the approval bar).

        The base/demo runtime has no kernel broker, so the deferred
        decision is parked here directly: the pending approval future is
        left untouched (deny-and-continue) and the item stays retro-
        answerable via ctrl-y (ADR-0007 resolution 5). The real adapter
        overrides this to route through the broker, which owns the
        ticket's structured detail. ``ticket_id`` names the ticket for
        that override; the base park keys off the visible prompt.
        """
        del ticket_id
        question = prompt.strip()
        if not question:
            return
        self.needs_you.defer(
            question,
            "deferred approval",
            choices=options,
            action=question,
        )

    # -- optional data hooks (demo fidelity / real telemetry) ---------------

    def turn_spec(self, prompt: str) -> TurnSpecLike | None:
        """Close-out spec for the turn started by *prompt* (demo parity)."""
        return None

    def lane_seed(self, agent_name: str) -> LaneSeed | None:
        """Initial lane presentation data for a spawned agent."""
        return None

    def lane_blocks(
        self, name: str, session_id: str, allocator: BlockIdAllocator
    ) -> list[TranscriptBlock] | None:
        """The focused-lane transcript block list (spec §8), if known."""
        return None

    def evidence_links(self, answer_text: str) -> tuple[EvidenceLink, ...]:
        """Evidence links grounding the final answer *answer_text* (spec §10)."""
        return ()

    def evidence_tool_call(self, tool_call_id: str) -> ToolCallRecord | None:
        """Durable provenance for *tool_call_id* (compliance item D7, AC2),
        or ``None`` when it cannot be resolved (AC5: the caller then shows
        an explicit "expired" fallback rather than a dead control)."""
        return None

    def deferred_decision(
        self, message: str, decision_id: str = ""
    ) -> tuple[str, str, tuple[str, ...], str, str]:
        """(question, reason, choices, highlight, action) for a
        deferred-decision event — ``highlight`` is the question substring
        rendered teal; ``action`` is the denied action key (the /improve
        override-evidence join against the DenialLog). ``decision_id`` is
        the already-parked NeedsYouQueue item when the deferral happened
        kernel-side; empty for message-only (scripted) deferrals."""
        del decision_id
        return (message, "", (), "", "")

    def decision_narration(self, choice: str, action: str = "") -> str:
        """The ``Applying decision: …`` narration for an acted-on choice.
        ``action`` is the decision's denied-action key, when it has one."""
        del action
        return f"Applying decision: {choice}"


class _AppLoopQueue:
    """``put_nowait`` shim marshalling runtime-thread emits to the app loop.

    ``asyncio.Queue`` is not thread-safe; the runtime thread's hooks emit
    UIEvents synchronously, so each put hops to the app loop via
    ``call_soon_threadsafe``. Only the producer half is proxied — the app
    keeps consuming the real queue.
    """

    def __init__(self, queue: asyncio.Queue[UIEvent], loop: asyncio.AbstractEventLoop) -> None:
        self._queue = queue
        self._loop = loop

    def put_nowait(self, event: UIEvent) -> None:
        self._loop.call_soon_threadsafe(self._queue.put_nowait, event)


class RealRuntimeAdapter(RuntimeAdapter):
    """Adapter over ``kernel/runtime.RealRuntime`` (real amplifier session).

    The runtime lives on its OWN thread + event loop: real sessions mount
    user-overlay hooks (memory briefings, context intelligence, …) that
    do seconds of synchronous work inside ``session.execute`` — on the UI
    loop that starved rendering completely (found live: the whole turn
    painted at once at the rule). Marshalling: UIEvents hop loops through
    :class:`_AppLoopQueue`; ``submit``/``interrupt``/``fork`` proxy in
    via ``run_coroutine_threadsafe``; approval answers hop in via
    ``call_soon_threadsafe``; approval presentation hops out to the app
    with ``call_soon_threadsafe`` on the app loop.
    """

    def __init__(
        self,
        *,
        bundle: str | None = None,
        resume_id: str | None = None,
        provider_override: str | None = None,
        model_override: str | None = None,
    ) -> None:
        super().__init__()
        self._bundle = bundle
        self._resume_id = resume_id
        # Per-launch, ephemeral overrides threaded into RealRuntime's plan seam
        # (never persisted): provider promotes/selects, model sets its default.
        self._provider_override = provider_override
        self._model_override = model_override
        self._runtime: Any = None
        self._presented: str | None = None
        self._app_loop: asyncio.AbstractEventLoop | None = None
        self._runtime_loop: asyncio.AbstractEventLoop | None = None
        self._thread: Any = None
        self._stop: asyncio.Event | None = None  # belongs to the runtime loop
        self._prompt_store: PromptHistoryStore | None = None

    def _history_store(self) -> PromptHistoryStore:
        """Lazily build the per-project prompt-history store (keyed to the
        real session's working directory, learned at ``start()``)."""
        if self._prompt_store is None:
            self._prompt_store = PromptHistoryStore(project_dir=self._config_project_dir)
        return self._prompt_store

    def record_prompt(self, text: str) -> None:
        self._history_store().append(text)

    def prompt_history(self) -> tuple[str, ...]:
        return tuple(self._history_store().load())

    async def start(self, ready: Callable[[], None]) -> None:
        import threading

        self._app_loop = asyncio.get_running_loop()
        started: asyncio.Future[None] = self._app_loop.create_future()
        self._thread = threading.Thread(
            target=self._thread_main, args=(started,), name="real-runtime", daemon=True
        )
        self._thread.start()
        await started  # runtime.start() finished (or raised) on its thread
        runtime = self._runtime
        self.bundle_name = runtime.bundle_name
        self.bundle_uri = runtime.bundle_uri
        self.gated_auto = runtime.gated_auto
        self.model_name = runtime.model_name
        self.session_short = runtime.session_short
        self.session_id = runtime.session_id
        self.banner = runtime.banner
        self.session_cost_start = runtime.session_cost_start
        self.turn_base = runtime.turn_base
        self.restored_history = runtime.restored_history
        self.restored_events = runtime.restored_events
        self.compaction = runtime.compaction
        self.pending_directive = runtime.pending_directive
        self.mount_report = runtime.mount_report
        self.session_dir = runtime.session_dir()
        if runtime.degraded_notice:
            self.startup_notices = (runtime.degraded_notice,)
        self._config_state = runtime.config_state()
        self._config_project_dir = runtime.project_dir
        runtime.broker.add_listener(self._on_broker_change)
        ready()

    def _thread_main(self, started: asyncio.Future[None]) -> None:
        asyncio.run(self._thread_body(started))

    async def _thread_body(self, started: asyncio.Future[None]) -> None:
        from ..kernel.runtime import RealRuntime  # lazy: --demo stays offline

        assert self._app_loop is not None
        self._runtime_loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()

        def _resolve(fn: Callable[[], None]) -> None:
            self._app_loop.call_soon_threadsafe(  # type: ignore[union-attr]
                lambda: fn() if not started.done() else None
            )

        try:
            runtime = RealRuntime(
                bundle=self._bundle,
                resume_id=self._resume_id,
                provider_override=self._provider_override,
                model_override=self._model_override,
                queue=_AppLoopQueue(self.queue, self._app_loop),  # type: ignore[arg-type]
                steering=self.steering,
                lane_steering=self.lane_steering,
                needs_you=self.needs_you,
                denial_log=self.denial_log,
                surface=self.terminal,
                mode=self._current_mode,
                permission_resolver=self._resolve_permission,
                capability_resolver=self._resolve_capability,
                on_progress=self._boot_progress,
            )
            await runtime.start()
        except BaseException as error:  # noqa: BLE001 — must resolve the boot future for ANY failure or the app-loop waiter hangs
            # Bind before the except block exits — Python unbinds the
            # handler name, and the lambda runs later on the app loop.
            failure = error
            _resolve(lambda: started.set_exception(failure))
            return
        self._runtime = runtime
        _resolve(lambda: started.set_result(None))
        await self._stop.wait()  # keep the loop alive for proxied calls
        await self._safe_cleanup(runtime)

    async def _safe_cleanup(self, runtime: Any) -> None:
        """Tear the runtime down on exit — best-effort, but never silent.

        This was the codebase's only bare ``except: pass``; a cleanup crash
        here would otherwise vanish without a trace. Teardown failures are
        non-fatal (we are exiting) so it logs at debug, but WITH the
        traceback so the failure stays recoverable.
        """
        try:
            await runtime.cleanup()
        except Exception:  # noqa: BLE001 — best-effort teardown on exit: logged with traceback, never silent
            logger.debug("runtime cleanup failed during teardown", exc_info=True)

    def _boot_progress(self, action: str, detail: str) -> None:
        # Fires on the runtime thread during start(); painting hops to
        # the app loop (boot can spend minutes in module prepare).
        app, loop = self.app, self._app_loop
        if app is not None and loop is not None:
            loop.call_soon_threadsafe(app.boot_progress, action, detail)

    async def _in_runtime(self, coro: Any) -> Any:
        assert self._runtime_loop is not None
        future = asyncio.run_coroutine_threadsafe(coro, self._runtime_loop)
        return await asyncio.wrap_future(future)

    def _current_mode(self) -> str:
        return self.app.mode_id if self.app is not None else "auto"

    def _resolve_permission(
        self, tool_name: str, tool_input: Mapping[str, object] | None
    ) -> TrustDecision:
        if self.app is not None:
            return self.app.permissions.resolve_call(tool_name, tool_input)
        return resolve(self._current_mode(), tool_name, tool_input)

    def _resolve_capability(self, capability: CapabilityClass) -> TrustDecision:
        if self.app is not None:
            return self.app.permissions.resolve_capability(capability)
        return resolve_capability(self._current_mode(), capability)

    def _on_broker_change(self) -> None:
        # Fires on the runtime thread — presentation hops to the app loop.
        head = self._runtime.broker.head if self._runtime else None
        if head is None:
            self._presented = None
            return
        if head.ticket_id != self._presented and self.app is not None:
            self._presented = head.ticket_id
            app, ticket = self.app, head
            if self._app_loop is not None:
                self._app_loop.call_soon_threadsafe(
                    app.present_approval, ticket.ticket_id, ticket.prompt, ticket.options
                )

    async def submit(self, text: str, attachments: tuple[Any, ...] = ()) -> None:
        if self._runtime is not None:
            await self._in_runtime(self._runtime.submit(text, attachments))

    async def _run_op(self, op: SessionOp[_OpT], /, *args: Any) -> _OpT:
        """The single marshalling seam (overrides the base dispatch).

        Before the runtime thread finishes booting ``_runtime`` is None
        and every op answers with its ``starting`` value; once live, the
        call hops onto the runtime loop via :meth:`_in_runtime` and lands
        on the same-named ``RealRuntime`` method. This one override
        replaces the seventeen hand-written thread-marshalling twins that
        used to live here.
        """
        if self._runtime is None:
            return op.starting
        return await self._in_runtime(getattr(self._runtime, op.name)(*args))

    async def set_model(self, model: str) -> tuple[bool, str]:
        """Marshal the switch, then keep the footer's model copy live."""
        result = await super().set_model(model)
        if self._runtime is not None:
            self.model_name = self._runtime.model_name
        return result

    async def rename_session(self, name: str) -> tuple[bool, str]:
        if self._runtime is None:
            return (False, "session still starting")
        return await self._in_runtime(self._runtime.rename_session(name))

    async def session_summaries(self) -> tuple[SessionSummary, ...]:
        if self._runtime is None:
            return ()

        async def read() -> tuple[SessionSummary, ...]:
            return self._runtime.session_summaries()

        return await self._in_runtime(read())

    async def deferred_bundles(self) -> tuple[str, ...]:
        if self._runtime is None:
            return ()

        async def read() -> tuple[str, ...]:
            return self._runtime.deferred_bundles()

        return await self._in_runtime(read())

    async def branch_session(self, name: str) -> tuple[bool, str]:
        if self._runtime is None:
            return (False, "session still starting")
        return await self._in_runtime(self._runtime.branch_session(name))

    async def fork_with_directive(self, directive: str) -> tuple[bool, str]:
        if self._runtime is None:
            return (False, "session still starting")
        return await self._in_runtime(self._runtime.fork_session(directive))

    async def session_tags(self) -> tuple[str, ...]:
        if self._runtime is None:
            return ()

        async def read() -> tuple[str, ...]:
            return self._runtime.session_tags()

        return await self._in_runtime(read())

    async def sessions_by_tag(self, tag: str) -> tuple[SessionSummary, ...]:
        if self._runtime is None:
            return ()

        async def read() -> tuple[SessionSummary, ...]:
            return self._runtime.sessions_by_tag(tag)

        return await self._in_runtime(read())

    async def add_session_tags(self, tags: tuple[str, ...]) -> tuple[bool, str]:
        if self._runtime is None:
            return (False, "session still starting")
        return await self._in_runtime(self._runtime.add_session_tags(tags))

    async def remove_session_tags(self, tags: tuple[str, ...]) -> tuple[bool, str]:
        if self._runtime is None:
            return (False, "session still starting")
        return await self._in_runtime(self._runtime.remove_session_tags(tags))

    async def directory_entries(self, kind: DirectoryKind) -> tuple[DirectoryEntry, ...]:
        if self._runtime is None:
            return ()

        async def read() -> tuple[DirectoryEntry, ...]:
            return self._runtime.directory_entries(kind)

        return await self._in_runtime(read())

    async def update_directory(
        self, kind: DirectoryKind, operation: str, path: str
    ) -> tuple[bool, str]:
        if self._runtime is None:
            return (False, "session still starting")
        return await self._in_runtime(self._runtime.update_session_directory(kind, operation, path))

    async def fork(self, checkpoint_id: str, ledger: Any) -> None:
        """Real fork: foundation in-memory fork + ``context.set_messages()``."""
        from ..kernel.rewind import RewindError

        if self._runtime is None:
            raise RewindError("session not started")
        await self._in_runtime(self._runtime.fork(checkpoint_id, ledger))

    def answer_approval(self, ticket_id: str, choice: str) -> None:
        if self._runtime is None or self._runtime_loop is None:
            return

        def _answer() -> None:
            try:
                self._runtime.broker.answer(ticket_id, choice)
            except KeyError:
                pass  # ticket already timed out / resolved

        self._runtime_loop.call_soon_threadsafe(_answer)

    def defer_approval(self, ticket_id: str, prompt: str, options: tuple[str, ...]) -> None:
        """ctrl-y park through the kernel broker (its ``defer`` owns the
        ticket's structured detail, parks the shared needs-you item, and
        fires the decision Notification the UI already handles). The
        ticket keeps its future — timing out to deny while the needs-you
        item stays retro-answerable (ADR-0007 resolution 5)."""
        del prompt, options  # broker.defer reads the ticket's own detail
        if self._runtime is None or self._runtime_loop is None:
            return

        def _defer() -> None:
            try:
                self._runtime.broker.defer(ticket_id)
            except (KeyError, ValueError, RuntimeError):
                pass  # ticket already resolved / deferred / no queue

        self._runtime_loop.call_soon_threadsafe(_defer)

    def publish_attention(self, payload: Mapping[str, Any]) -> None:
        """Fire-and-forget onto the runtime loop -- never blocks the UI
        thread, never raises (B7 gap 2).

        Mirrors :meth:`answer_approval`/:meth:`defer_approval`'s own
        cross-thread contract: a plain closure hops via
        ``call_soon_threadsafe`` and does its own work once ON the runtime
        loop. Scheduling the actual (async) hook emission as a task there
        -- rather than awaiting it from here -- is what keeps this call
        synchronous and instant from the caller's (UI thread's) point of
        view; ``RealRuntime.publish_attention`` itself never raises.
        """
        if self._runtime is None or self._runtime_loop is None:
            return
        runtime = self._runtime
        data = dict(payload)

        def _publish() -> None:
            try:
                self._runtime_loop.create_task(runtime.publish_attention(data))  # type: ignore[union-attr]
            except RuntimeError:
                pass  # loop closing / closed between the check and the call

        self._runtime_loop.call_soon_threadsafe(_publish)

    def shutdown(self) -> None:
        """Stop the runtime thread and WAIT for its cleanup (bounded).

        Signalling without joining let the process exit while the kernel's
        tokio workers were still mid-teardown — Python finalized under
        them and pyo3 panicked with "interpreter is not initialized" noise
        after the shell prompt returned (user report). Joining gives
        ``session.cleanup()`` and the Rust runtime a window to wind down
        before interpreter shutdown.

        A boot failure returns ``_thread_body`` early, so ``asyncio.run``
        has already closed ``_runtime_loop`` by the time on_unmount fires;
        calling into it then raised ``RuntimeError: Event loop is closed``
        and masked the real boot error. Guard the closed/finished loop.
        """
        loop, stop = self._runtime_loop, self._stop
        if loop is not None and stop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(stop.set)
            except RuntimeError:
                pass  # loop finished between the check and the call
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=8.0)

    def evidence_links(self, answer_text: str) -> tuple[EvidenceLink, ...]:
        """Claims derived from the turn's tool calls (spec §10; ADR-0007
        resolution 9 — same normalized stream ui-events.jsonl records)."""
        if self._runtime is None:
            return ()
        return self._runtime.evidence.links_for(answer_text)

    def evidence_tool_call(self, tool_call_id: str) -> ToolCallRecord | None:
        """The provenance record the same collector persisted for
        *tool_call_id* (D7) — independent of ``evidence_links`` above and
        of how the transcript currently renders ToolLine blocks."""
        if self._runtime is None:
            return None
        return self._runtime.evidence.record_for(tool_call_id)

    def lane_seed(self, agent_name: str) -> LaneSeed | None:
        """Seed a real lane with the delegate brief as its activity line.

        Real telemetry (elapsed/cost/tokens) starts at zero and accrues
        from the child-stamped events the spawner's re-attached bridge
        forwards; only the presentation seed comes from the spawn brief.
        Cross-thread read of the spawner's brief map (dict get under the
        GIL) — no marshalling needed for this synchronous lookup.
        """
        if self._runtime is None:
            return None
        brief = self._runtime.agent_brief(agent_name)
        if not brief:
            return None
        from .reducer import LaneSeed

        return LaneSeed(activity=brief)

    def deferred_decision(
        self, message: str, decision_id: str = ""
    ) -> tuple[str, str, tuple[str, ...], str, str]:
        """Resolve the kernel-parked NeedsYouItem by id.

        Real deferrals park their item in the shared queue at the point
        of deferral (broker/governance, fed by the native approval
        request payload); the decision Notification carries only the id.
        Nothing is re-parsed from the message string. An unknown/empty id
        degrades to the base message-only stub."""
        if decision_id:
            for item in self.needs_you.items:
                if item.decision_id == decision_id:
                    return (
                        item.question,
                        item.reason,
                        item.choices,
                        item.highlight,
                        item.action,
                    )
        return super().deferred_decision(message, decision_id)

    def decision_narration(self, choice: str, action: str = "") -> str:
        """Name the denied action being applied, when the item carries one."""
        if action:
            return f"Applying decision: {choice} · {action}"
        return super().decision_narration(choice)


__all__ = ["SESSION_OPS", "RealRuntimeAdapter", "RuntimeAdapter", "SessionOp"]
