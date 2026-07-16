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
    ) -> tuple[str, str, tuple[str, ...], str]:
        """(question, reason, choices, highlight) for a deferred-decision
        event — ``highlight`` is the question substring rendered teal."""
        return (message, "", (), "")

    def decision_narration(self, choice: str) -> str:
        """The ``Applying decision: …`` narration for an acted-on choice."""
        return f"Applying decision: {choice}"


class RealRuntimeAdapter(RuntimeAdapter):
    """Adapter over ``kernel/runtime.RealRuntime`` (real amplifier session)."""

    def __init__(self, *, bundle: str | None = None, resume_id: str | None = None) -> None:
        super().__init__()
        self._bundle = bundle
        self._resume_id = resume_id
        self._runtime: Any = None
        self._presented: str | None = None

    async def start(self, ready: Callable[[], None]) -> None:
        from ..kernel.runtime import RealRuntime  # lazy: --demo stays offline

        runtime = RealRuntime(
            bundle=self._bundle,
            resume_id=self._resume_id,
            queue=self.queue,
            steering=self.steering,
            needs_you=self.needs_you,
            denial_log=self.denial_log,
            mode=self._current_mode,
        )
        self._runtime = runtime
        await runtime.start()
        self.bundle_name = runtime.bundle_name
        self.session_short = runtime.session_short
        self.banner = runtime.banner
        self.session_cost_start = runtime.session_cost_start
        self.turn_base = runtime.turn_base
        if runtime.degraded_notice:
            self.startup_notices = (runtime.degraded_notice,)
        runtime.broker.add_listener(self._on_broker_change)
        ready()

    def _current_mode(self) -> str:
        return self.app.mode_id if self.app is not None else "chat"

    def _on_broker_change(self) -> None:
        head = self._runtime.broker.head if self._runtime else None
        if head is None:
            self._presented = None
            return
        if head.ticket_id != self._presented and self.app is not None:
            self._presented = head.ticket_id
            self.app.present_approval(head.ticket_id, head.prompt, head.options)

    async def submit(self, text: str) -> None:
        if self._runtime is not None:
            await self._runtime.submit(text)

    async def interrupt(self) -> bool:
        return await self._runtime.interrupt() if self._runtime else False

    async def fork(self, checkpoint_id: str, ledger: Any) -> None:
        """Real fork: foundation in-memory fork + ``context.set_messages()``."""
        from ..kernel.rewind import RewindError

        if self._runtime is None:
            raise RewindError("session not started")
        await self._runtime.fork(checkpoint_id, ledger)

    def answer_approval(self, ticket_id: str, choice: str) -> None:
        if self._runtime is not None:
            try:
                self._runtime.broker.answer(ticket_id, choice)
            except KeyError:
                pass  # ticket already timed out / resolved

    def evidence_links(self, answer_text: str) -> tuple[EvidenceLink, ...]:
        """Claims derived from the turn's tool calls (spec §10; ADR-0007
        resolution 9 — same normalized stream events.jsonl records)."""
        if self._runtime is None:
            return ()
        return self._runtime.evidence.links_for(answer_text)


__all__ = ["RealRuntimeAdapter", "RuntimeAdapter"]
