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
from collections.abc import Callable
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from ..kernel.events import UIEvent
from ..model.blocks import BlockIdAllocator, TranscriptBlock
from ..model.evidence import EvidenceLink
from ..model.queues import NeedsYouQueue, SteeringQueue
from ..model.trust import DenialLog

if TYPE_CHECKING:
    from .reducer import LaneSeed, TurnSpecLike


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
        self.app: Any = None
        self.bundle_name: str = ""
        self.session_short: str = ""
        self.banner: tuple[str, str] = ("", "")
        self.session_cost_start: Decimal = Decimal("0")
        self.turn_base: int = 0
        """Restored-history user-message count on resume (checkpoint turn
        ids offset past it — DESIGN-SPEC §9); 0 for fresh/demo sessions."""
        self.restored_history: tuple[tuple[str, str], ...] = ()
        """(role, text) pairs replayed into the transcript on resume."""
        self.startup_notices: tuple[str, ...] = ()

    def attach(self, app: Any) -> None:
        """Give the adapter its app handle (approval presentation etc.)."""
        self.app = app

    async def start(self, ready: Callable[[], None]) -> None:
        """Boot the runtime; call ``ready()`` once identity is known."""
        ready()

    async def submit(self, text: str) -> None:
        """Run *text* as a new user turn."""

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
        self, message: str
    ) -> tuple[str, str, tuple[str, ...], str, str]:
        """(question, reason, choices, highlight, action) for a
        deferred-decision event — ``highlight`` is the question substring
        rendered teal; ``action`` is the denied action key (the /improve
        override-evidence join against the DenialLog)."""
        return (message, "", (), "", "")

    def decision_narration(self, choice: str) -> str:
        """The ``Applying decision: …`` narration for an acted-on choice."""
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
        self.session_short = runtime.session_short
        self.banner = runtime.banner
        self.session_cost_start = runtime.session_cost_start
        self.turn_base = runtime.turn_base
        self.restored_history = runtime.restored_history
        if runtime.degraded_notice:
            self.startup_notices = (runtime.degraded_notice,)
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
                mode=self._current_mode,
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
        try:
            await runtime.cleanup()
        except Exception:
            pass  # best-effort teardown on exit

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
        return self.app.mode_id if self.app is not None else "chat"

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

    async def submit(self, text: str) -> None:
        if self._runtime is not None:
            await self._in_runtime(self._runtime.submit(text))

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
        """Ask the runtime thread to clean up and exit (best-effort)."""
        if self._runtime_loop is not None and self._stop is not None:
            self._runtime_loop.call_soon_threadsafe(self._stop.set)

    def evidence_links(self, answer_text: str) -> tuple[EvidenceLink, ...]:
        """Claims derived from the turn's tool calls (spec §10; ADR-0007
        resolution 9 — same normalized stream events.jsonl records)."""
        if self._runtime is None:
            return ()
        return self._runtime.evidence.links_for(answer_text)


__all__ = ["RealRuntimeAdapter", "RuntimeAdapter"]
