"""Demo glue: DemoRuntime + mockup data → the app's RuntimeAdapter seam.

``--demo`` boots :class:`DemoRuntimeAdapter`, which plays the seed
transcript on start and maps every composer submit to one scripted demo
turn (matching the mockup prompt when the user types it verbatim,
otherwise advancing build → auto → plan → brainstorm → agents). The
exported mockup data (`DEMO_LANES`, `DEMO_EVIDENCE`,
`DEMO_DEFERRED_DECISION`, `DEMO_TURNS`) is converted here into model
blocks / reducer seeds so the reducer and app stay runtime-agnostic.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from decimal import Decimal

from ..kernel.demo import (
    DEMO_BANNER,
    DEMO_BUNDLE,
    DEMO_DEFERRED_DECISION,
    DEMO_EVIDENCE,
    DEMO_LANE_BY_NAME,
    DEMO_SESSION_COST_START,
    DEMO_SESSION_ID,
    DEMO_SESSION_SHORT,
    DEMO_TURNS,
    DemoLane,
    DemoRuntime,
    DemoTurnSpec,
    TurnKey,
    build_denied_spec,
)
from ..model.blocks import (
    Answer,
    BlockIdAllocator,
    LiveCommand,
    Narration,
    Segment,
    SessionBanner,
    ToolLine,
    TranscriptBlock,
    UserLine,
)
from ..model.evidence import EvidenceLink
from .live_tail import answer_spans
from .needs_you import focused_lane_banner
from .reducer import LaneSeed
from .runtime_adapter import RuntimeAdapter

_TURN_ORDER: tuple[TurnKey, ...] = ("build", "auto", "plan", "brainstorm", "agents")
_PANEL_LINE_RE = re.compile(
    r"^\s*\S\s+(?P<name>\S+)\s*·\s*(?P<activity>.+?)\s*·\s*(?P<elapsed>\S+)\s*·\s*\$(?P<cost>[\d.]+)\s*$"
)


def _parse_elapsed(text: str) -> float:
    if text.endswith("m"):
        return float(text[:-1]) * 60
    if text.endswith("s"):
        return float(text[:-1])
    return 0.0


def lane_seed_for(name: str) -> LaneSeed | None:
    """Reducer LaneSeed from the mockup lane's verbatim panel line."""
    lane = DEMO_LANE_BY_NAME.get(name)
    if lane is None:
        return None
    match = _PANEL_LINE_RE.match(lane.panel_line)
    activity, elapsed, cost = "", 0.0, Decimal("0")
    if match:
        activity = match.group("activity")
        elapsed = _parse_elapsed(match.group("elapsed"))
        cost = Decimal(match.group("cost"))
    return LaneSeed(
        activity=activity,
        elapsed=elapsed,
        cost=cost,
        tree_spawn=lane.tree_spawn,
        tree_done=lane.tree_done,
    )


def lane_focus_blocks(lane: DemoLane, allocator: BlockIdAllocator) -> list[TranscriptBlock]:
    """The focused-lane transcript (DESIGN-SPEC §8) from DEMO_LANES data."""
    blocks: list[TranscriptBlock] = [
        SessionBanner(
            id=allocator.next_id(),
            headline="",
            focus_note=focused_lane_banner(lane.name, DEMO_SESSION_ID),
        ),
        UserLine(id=allocator.next_id(), text=lane.brief, mode="delegated"),
    ]
    for row in lane.log:
        if row.kind == "narration":
            blocks.append(Narration(id=allocator.next_id(), text=row.text))
        elif row.kind == "tool":
            blocks.append(
                ToolLine(id=allocator.next_id(), summary=row.text, status="completed")
            )
        elif row.kind == "command":
            blocks.append(LiveCommand(id=allocator.next_id(), command=row.text))
        else:  # answer
            blocks.append(Answer(id=allocator.next_id(), spans=answer_spans(row.text)))
    blocks.append(
        Answer(
            id=allocator.next_id(),
            spans=(Segment(text=lane.state_recap, style_token="dim", italic=True),),
        )
    )
    return blocks


def demo_evidence_links() -> tuple[EvidenceLink, ...]:
    return tuple(
        EvidenceLink(claim_quote=claim.quote, tool_ref=claim.source)
        for claim in DEMO_EVIDENCE
    )


class DemoRuntimeAdapter(RuntimeAdapter):
    """ADR-0007 DemoRuntime behind the same adapter contract as real sessions."""

    def __init__(self, *, instant: bool = False) -> None:
        super().__init__()
        self.bundle_name = DEMO_BUNDLE
        self.session_short = DEMO_SESSION_SHORT
        self.banner = DEMO_BANNER
        self.session_cost_start = DEMO_SESSION_COST_START
        sleep: Callable[[float], object] | None = None
        if instant:

            async def _instant(_seconds: float) -> None:
                await asyncio.sleep(0)

            sleep = _instant
        self._runtime = DemoRuntime(
            queue=self.queue,
            approver=self._approve,
            sleep=sleep,  # type: ignore[arg-type]
            steer_source=self._consume_steer,
        )
        self._by_prompt: dict[str, DemoTurnSpec] = {spec.prompt: spec for spec in DEMO_TURNS}
        self._played: set[TurnKey] = set()
        self._pending: dict[str, asyncio.Future[str]] = {}
        self._ticket_seq = 0
        self._build_denied = False

    # -- lifecycle ------------------------------------------------------------

    async def start(self, ready: Callable[[], None]) -> None:
        ready()
        self._played.add("seed")
        await self._runtime.run_seed()

    async def submit(self, text: str) -> None:
        key = self._key_for(text)
        self._played.add(key)
        await self._runtime.run_turn(key)

    def _key_for(self, text: str) -> TurnKey:
        spec = self._by_prompt.get(text.strip())
        if spec is not None:
            return spec.key
        for key in _TURN_ORDER:
            if key not in self._played:
                return key
        return "build"

    # -- approvals --------------------------------------------------------------

    async def _approve(self, prompt: str, options: tuple[str, ...]) -> str:
        if self.app is None:
            return options[0]
        self._ticket_seq += 1
        ticket_id = f"demo-ticket-{self._ticket_seq}"
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._pending[ticket_id] = future
        self.app.present_approval(ticket_id, prompt, tuple(options))
        try:
            return await future
        finally:
            self._pending.pop(ticket_id, None)

    def answer_approval(self, ticket_id: str, choice: str) -> None:
        if choice == "Deny":
            self._build_denied = True  # denied pytest → mockup deny close-out
        future = self._pending.get(ticket_id)
        if future is not None and not future.done():
            future.set_result(choice)

    # -- steering ---------------------------------------------------------------

    def _consume_steer(self) -> str | None:
        """DemoRuntime step-boundary hook: pop one queued steer (spec §5)."""
        message = self.steering.consume_next_steer()
        return message.text if message is not None else None

    # -- data hooks ------------------------------------------------------------------

    def turn_spec(self, prompt: str) -> DemoTurnSpec | None:
        spec = self._by_prompt.get(prompt.strip())
        if spec is not None and spec.key == "build" and self._build_denied:
            return build_denied_spec()
        return spec

    def lane_seed(self, agent_name: str) -> LaneSeed | None:
        return lane_seed_for(agent_name)

    def lane_blocks(
        self, name: str, session_id: str, allocator: BlockIdAllocator
    ) -> list[TranscriptBlock] | None:
        lane = DEMO_LANE_BY_NAME.get(name)
        if lane is None:
            for candidate in DEMO_LANE_BY_NAME.values():
                if candidate.sub_session_id == session_id:
                    lane = candidate
                    break
        if lane is None:
            return None
        return lane_focus_blocks(lane, allocator)

    def evidence_links(self) -> tuple[EvidenceLink, ...]:
        return demo_evidence_links()

    def deferred_decision(self, message: str) -> tuple[str, str, tuple[str, ...]]:
        del message
        return (DEMO_DEFERRED_DECISION.text, "", (DEMO_DEFERRED_DECISION.chip_label,))

    def decision_narration(self, choice: str) -> str:
        if choice == DEMO_DEFERRED_DECISION.chip_label:
            return DEMO_DEFERRED_DECISION.applied_narration
        return f"Applying decision: {choice}"


__all__ = [
    "DemoRuntimeAdapter",
    "demo_evidence_links",
    "lane_focus_blocks",
    "lane_seed_for",
]
