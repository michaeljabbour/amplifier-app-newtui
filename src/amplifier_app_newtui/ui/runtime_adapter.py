"""Runtime adapters: the seam between the Textual app and a runtime.

ADR-0007 §Runtimes: the app consumes one ``asyncio.Queue[UIEvent]`` and
cannot tell a :class:`~amplifier_app_newtui.kernel.demo.DemoRuntime`
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
from pathlib import Path
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from ..kernel.events import UIEvent
from ..kernel.compaction import CompactionConfig
from ..kernel.directory_permissions import DirectoryEntry, DirectoryKind
from ..kernel.session_ops import ModelListing, StatusInfo
from ..model.blocks import BlockIdAllocator, TranscriptBlock
from ..model.config import (
    ConfigChange,
    ConfigSnapshotView,
    SessionConfigState,
    default_config_state,
)
from ..model.evidence import EvidenceLink
from ..model.queues import NeedsYouQueue, SteeringQueue
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



class RuntimeAdapter:
    """Base adapter: owns the event queue and shared interaction queues.

    The app calls :meth:`attach` before :meth:`start`; ``start`` must
    call ``ready()`` once session identity (banner/bundle/session) is
    known and BEFORE producing turn events.
    """

    def __init__(self) -> None:
        self.queue: asyncio.Queue[UIEvent] = asyncio.Queue()
        self.steering = SteeringQueue()
        self.needs_you = NeedsYouQueue()
        self.denial_log = DenialLog()
        self.terminal = TerminalSurface()
        """Live terminal width shared with the kernel's width-aware
        surface-hint hook (#35). The app updates it from Textual resize
        events; the RealRuntime reads it at each provider:request."""
        self.app: Any = None
        self.bundle_name: str = ""
        self.model_name: str = ""
        """Primary model id, possibly provider-qualified (``anthropic/x``)."""
        self.session_short: str = ""
        self.banner: tuple[str, str] = ("", "")
        self.session_cost_start: Decimal = Decimal("0")
        self.turn_base: int = 0
        """Restored-history user-message count on resume (checkpoint turn
        ids offset past it — DESIGN-SPEC §9); 0 for fresh/demo sessions."""
        self.restored_history: tuple[tuple[str, str], ...] = ()
        """(role, text) pairs replayed into the transcript on resume."""
        self.restored_events: tuple[UIEvent, ...] = ()
        """The resumed session's stored UIEvents, replayed through the
        reducer to rebuild the full transcript (digests, delegate
        summaries, turn rules — DESIGN-SPEC §3/§11); empty means the
        prose ``restored_history`` fallback renders instead."""
        self.startup_notices: tuple[str, ...] = ()
        self.compaction = CompactionConfig(
            auto_compact=True, compact_threshold=0.8
        )
        self._config_state: SessionConfigState = default_config_state()
        """Live ``/config`` state — shared by demo and real (invariant 4);
        real sessions reseed it from the mount plan at ``start()``."""
        self._config_project_dir: Path = Path.cwd()

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

    async def interrupt(self) -> bool:
        """Request an interrupt; True when the runtime accepted it."""
        return False

    async def list_native_modes(self) -> Any:
        """Bundle-composed mode catalog (real sessions); "" when absent.
        Typically a mapping with a ``modes`` list of {name, description,
        source} dicts — whatever the mounted mode tool reports."""
        return ""

    async def set_native_mode(self, name: str | None) -> tuple[bool, str]:
        """Activate/clear a bundle-provided mode via the native mode tool."""
        del name
        return (False, "native modes need a real session")

    # -- in-session ops (base: no live session, neutral results) ------------

    async def list_models(self) -> ModelListing:
        return ModelListing(provider="", current="")

    async def set_model(self, model: str) -> tuple[bool, str]:
        del model
        return (False, "switching models needs a real session")

    async def get_effort(self) -> str | None:
        return None

    async def set_effort(self, level: str) -> tuple[bool, str]:
        del level
        return (False, "reasoning effort needs a real session")

    async def compact(self, focus: str = "") -> tuple[bool, str]:
        del focus
        return (False, "compaction needs a real session")

    async def clear_context(self) -> tuple[bool, int]:
        return (False, 0)

    async def status(self) -> StatusInfo:
        return StatusInfo()

    async def list_tools(self) -> tuple[str, ...]:
        return ()

    async def list_agents(self) -> tuple[str, ...]:
        return ()

    async def diff(self, staged: bool = False) -> str | None:
        del staged
        return None

    async def workspace_files(self) -> tuple[str, ...]:
        """Relative paths available to composer ``@file`` autocomplete."""
        return ()

    async def list_skills(self) -> tuple[Any, ...]:
        return ()

    async def load_skill(self, name: str) -> tuple[bool, str]:
        del name
        return (False, "skills need a real session")

    async def mcp_tools(self) -> tuple[str, ...]:
        return ()

    async def directory_entries(
        self, kind: DirectoryKind
    ) -> tuple[DirectoryEntry, ...]:
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
        :class:`~amplifier_app_newtui.kernel.rewind.RewindError` on
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

    async def config_toggle(
        self, category: str, name: str, enable: bool
    ) -> tuple[bool, str]:
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

        return save_config(
            self._config_state, scope=scope, project_dir=self._config_project_dir
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

    def __init__(self, *, bundle: str | None = None, resume_id: str | None = None) -> None:
        super().__init__()
        self._bundle = bundle
        self._resume_id = resume_id
        self._runtime: Any = None
        self._presented: str | None = None
        self._app_loop: asyncio.AbstractEventLoop | None = None
        self._runtime_loop: asyncio.AbstractEventLoop | None = None
        self._thread: Any = None
        self._stop: asyncio.Event | None = None  # belongs to the runtime loop

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
        self.model_name = runtime.model_name
        self.session_short = runtime.session_short
        self.banner = runtime.banner
        self.session_cost_start = runtime.session_cost_start
        self.turn_base = runtime.turn_base
        self.restored_history = runtime.restored_history
        self.restored_events = runtime.restored_events
        self.compaction = runtime.compaction
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
                queue=_AppLoopQueue(self.queue, self._app_loop),  # type: ignore[arg-type]
                steering=self.steering,
                needs_you=self.needs_you,
                denial_log=self.denial_log,
                surface=self.terminal,
                mode=self._current_mode,
                permission_resolver=self._resolve_permission,
                capability_resolver=self._resolve_capability,
                on_progress=self._boot_progress,
            )
            await runtime.start()
        except BaseException as error:  # surface boot failures on the app loop
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
        except Exception:
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

    async def interrupt(self) -> bool:
        if self._runtime is None:
            return False
        return await self._in_runtime(self._runtime.interrupt())

    async def list_native_modes(self) -> Any:
        if self._runtime is None:
            return ""
        return await self._in_runtime(self._runtime.list_native_modes())

    async def set_native_mode(self, name: str | None) -> tuple[bool, str]:
        if self._runtime is None:
            return (False, "session still starting")
        return await self._in_runtime(self._runtime.set_native_mode(name))

    async def list_models(self) -> ModelListing:
        if self._runtime is None:
            return ModelListing(provider="", current="")
        return await self._in_runtime(self._runtime.list_models())

    async def set_model(self, model: str) -> tuple[bool, str]:
        if self._runtime is None:
            return (False, "session still starting")
        result = await self._in_runtime(self._runtime.set_model(model))
        self.model_name = self._runtime.model_name  # keep the footer's copy live
        return result

    async def get_effort(self) -> str | None:
        if self._runtime is None:
            return None
        return await self._in_runtime(self._runtime.get_effort())

    async def set_effort(self, level: str) -> tuple[bool, str]:
        if self._runtime is None:
            return (False, "session still starting")
        return await self._in_runtime(self._runtime.set_effort(level))

    async def compact(self, focus: str = "") -> tuple[bool, str]:
        if self._runtime is None:
            return (False, "session still starting")
        return await self._in_runtime(self._runtime.compact(focus))

    async def clear_context(self) -> tuple[bool, int]:
        if self._runtime is None:
            return (False, 0)
        return await self._in_runtime(self._runtime.clear_context())

    async def status(self) -> StatusInfo:
        if self._runtime is None:
            return StatusInfo()
        return await self._in_runtime(self._runtime.status())

    async def list_tools(self) -> tuple[str, ...]:
        if self._runtime is None:
            return ()
        return await self._in_runtime(self._runtime.list_tools())

    async def list_agents(self) -> tuple[str, ...]:
        if self._runtime is None:
            return ()
        return await self._in_runtime(self._runtime.list_agents())

    async def diff(self, staged: bool = False) -> str | None:
        if self._runtime is None:
            return None
        return await self._in_runtime(self._runtime.diff(staged))

    async def workspace_files(self) -> tuple[str, ...]:
        if self._runtime is None:
            return ()
        return await self._in_runtime(self._runtime.workspace_files())

    async def list_skills(self) -> tuple[Any, ...]:
        if self._runtime is None:
            return ()
        return await self._in_runtime(self._runtime.list_skills())

    async def load_skill(self, name: str) -> tuple[bool, str]:
        if self._runtime is None:
            return (False, "session still starting")
        return await self._in_runtime(self._runtime.load_skill(name))

    async def mcp_tools(self) -> tuple[str, ...]:
        if self._runtime is None:
            return ()
        return await self._in_runtime(self._runtime.mcp_tools())

    async def directory_entries(
        self, kind: DirectoryKind
    ) -> tuple[DirectoryEntry, ...]:
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
        return await self._in_runtime(
            self._runtime.update_session_directory(kind, operation, path)
        )

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


__all__ = ["RealRuntimeAdapter", "RuntimeAdapter"]
