"""The event reducer: normalized UIEvents → transcript blocks + host actions.

The Textual app consumes the runtime's ``asyncio.Queue[UIEvent]`` and
feeds every event to :meth:`TranscriptReducer.handle`. The reducer owns
turn-shaped state (tool correlation by ``tool_call_id``, plan blocks
keyed by title, working-status telemetry, lane tree lines, ledger
close-out) and acts on the app exclusively through the narrow
:class:`ReducerHost` protocol — it never touches widgets directly, so
the whole turn lifecycle is unit-testable with a fake host.

Demo conventions honored (see ``kernel/demo.py`` module docstring):
role markers in ``ContentBlockEnd.block["demo_role"]``, ``update_plan``
tool calls as plan checklists, ``bash`` denials as ⊘ blocked lines, and
``DemoTurnSpec`` close-out labels via the adapter's ``turn_spec`` hook.
The real runtime flows through the same paths with generic fallbacks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from ..kernel import events as ev
from ..model.blocks import (
    Answer,
    BlockIdAllocator,
    Blocked,
    BrainstormIdea,
    LiveCommand,
    Narration,
    PlanBlock,
    PlanItem,
    Recap,
    Segment,
    ToolLine,
    TranscriptBlock,
    TurnRule,
    UserLine,
    WorkingStatus,
)
from ..model.evidence import EvidenceLink
from ..model.lanes import LaneRegistry
from ..model.turn import OutcomeLedger, TurnOutcome, TurnTelemetry
from .live_tail import answer_spans

_RECAP_RE = re.compile(r"^Goal:\s*(?P<goal>.+?)\.\s*Next:\s*(?P<next>.+?)\.?\s*$", re.DOTALL)
_IDEA_RE = re.compile(r"^(\d+)\s+(.*)$", re.DOTALL)
_MODE_NOTICE_RE = re.compile(r"^mode (\w+)")

_PLAN_STATES = frozenset({"pending", "active", "done"})


class TurnSpecLike(Protocol):
    """Close-out data for one turn (structurally ``kernel.demo.DemoTurnSpec``)."""

    duration_ms: int
    tokens: int
    cached_pct: int | None
    cost: Decimal
    cost_after: Decimal
    outcome: str
    shipped: bool
    rule_label: str
    checkpoint_label: str


@dataclass(frozen=True)
class LaneSeed:
    """Initial lane presentation supplied by the adapter (demo fidelity)."""

    activity: str = ""
    elapsed: float = 0.0
    cost: Decimal = Decimal("0")
    tree_spawn: str = ""
    tree_done: str = ""


class ReducerHost(Protocol):
    """The narrow surface the reducer drives (implemented by the app)."""

    @property
    def mode_id(self) -> str: ...
    def append_block(self, block: TranscriptBlock) -> None: ...
    def replace_block(self, block: TranscriptBlock) -> None: ...
    def remove_block(self, block_id: str) -> None: ...
    def show_notice(self, text: str) -> None: ...
    def set_mode_by_id(self, mode_id: str, *, notify: bool = True) -> None: ...
    def turn_started(self) -> None: ...
    def turn_finished(self) -> None: ...
    def lanes_changed(self) -> None: ...
    def approval_opened(self, prompt: str, options: tuple[str, ...]) -> None: ...
    def decision_deferred(self, message: str) -> None: ...
    def stream_opened(self, block_type: str) -> None: ...
    def stream_delta(self, text: str) -> None: ...
    def stream_closed(self) -> None: ...


@dataclass
class _Group:
    pending: set[str] = field(default_factory=set)
    commands: list[str] = field(default_factory=list)
    block_ids: list[str] = field(default_factory=list)


@dataclass
class _Turn:
    turn_id: int
    prompt: str
    start_ts: float
    mode: str
    spec: TurnSpecLike | None = None
    tokens: int = 0
    working_id: str | None = None
    plan_ids: dict[str, str] = field(default_factory=dict)
    active_step: str | None = None
    calls: dict[str, dict[str, Any]] = field(default_factory=dict)
    groups: dict[str, _Group] = field(default_factory=dict)
    blocked: set[str] = field(default_factory=set)
    cancelled: bool = False
    last_ts: float = 0.0


class TranscriptReducer:
    """UIEvent stream → block mutations on a :class:`ReducerHost`."""

    def __init__(
        self,
        host: ReducerHost,
        *,
        allocator: BlockIdAllocator,
        ledger: OutcomeLedger,
        lanes: LaneRegistry,
        spec_lookup: Any = None,
        lane_seed_lookup: Any = None,
        evidence_lookup: Any = None,
        session_cost_start: Decimal = Decimal("0"),
    ) -> None:
        self._host = host
        self._ids = allocator
        self.ledger = ledger
        self.lanes = lanes
        self._spec_lookup = spec_lookup or (lambda prompt: None)
        self._lane_seed = lane_seed_lookup or (lambda name: None)
        self._evidence = evidence_lookup or (lambda: ())
        self.session_cost = session_cost_start
        self.total_tokens = 0
        self._turn: _Turn | None = None
        self._turn_seq = 0
        self._tree_ids: dict[str, str] = {}

    # -- public state -------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._turn is not None

    def title_state(self) -> str:
        """The title bar's ``<state>`` fragment (DESIGN-SPEC §2)."""
        turn = self._turn
        if turn is None:
            return "ready"
        active = self.lanes.active_count
        if active:
            return f"✳ coordinating {active} agents"
        if turn.active_step:
            return turn.active_step.lower()
        if turn.mode == "plan":
            return "planning"
        if turn.mode == "brainstorm":
            return "brainstorming"
        return "working"

    # -- dispatch -------------------------------------------------------------

    def handle(self, event: ev.UIEvent) -> None:  # noqa: C901 - one dispatch table
        """Apply one normalized event; unknown kinds are ignored."""
        if self._turn is not None and event.ts:
            self._turn.last_ts = event.ts
        match event:
            case ev.PromptSubmit():
                self._start_turn(event)
            case ev.StreamBlockStart():
                self._host.stream_opened(event.block_type)
            case ev.StreamBlockDelta():
                self._host.stream_delta(event.text)
            case ev.StreamBlockEnd():
                self._host.stream_closed()
            case ev.StreamAborted():
                self._host.stream_closed()
                self._host.show_notice(f"stream aborted · {event.error_message}".rstrip(" ·"))
            case ev.ContentBlockEnd():
                self._durable_text(event)
            case ev.ToolPre():
                self._tool_pre(event)
            case ev.ToolPost():
                self._tool_post(event)
            case ev.ToolError():
                self._tool_error(event)
            case ev.ProviderResponseUsage():
                self._usage(event)
            case ev.ProviderNotice():
                self._host.show_notice(f"provider {event.notice} · {event.message}".rstrip(" ·"))
            case ev.ApprovalRequired():
                self._host.approval_opened(event.prompt, event.options)
            case ev.ApprovalDenied():
                self._approval_denied(event)
            case ev.Notification():
                self._notification(event)
            case ev.AgentSpawned():
                self._agent_spawned(event)
            case ev.AgentCompleted():
                self._agent_completed(event)
            case ev.OrchestratorComplete():
                if event.status == "cancelled" and self._turn is not None:
                    self._turn.cancelled = True
            case ev.CancelCompleted():
                if self._turn is not None:
                    self._turn.cancelled = True
            case ev.PromptComplete():
                self._finish_turn(event)
            case _:
                pass

    # -- turn lifecycle -------------------------------------------------------

    def _start_turn(self, event: ev.PromptSubmit) -> None:
        self._turn_seq += 1
        turn = _Turn(
            turn_id=self._turn_seq,
            prompt=event.prompt,
            start_ts=event.ts,
            last_ts=event.ts,
            mode=self._host.mode_id,
            spec=self._spec_lookup(event.prompt),
        )
        self._turn = turn
        self._host.append_block(
            UserLine(id=self._ids.next_id(), text=event.prompt, mode=turn.mode)
        )
        turn.working_id = self._ids.next_id()
        self._host.append_block(
            WorkingStatus(
                id=turn.working_id,
                telemetry=TurnTelemetry(secs=0, tokens_down=0),
                agent_count=self.lanes.active_count,
            )
        )
        self._host.turn_started()

    def _finish_turn(self, event: ev.PromptComplete) -> None:
        turn = self._turn
        if turn is None:
            return
        if turn.working_id is not None:
            self._host.remove_block(turn.working_id)
        # Re-resolve at close: mid-turn events (e.g. a denied approval)
        # may have changed the adapter's close-out spec for this prompt.
        spec = self._spec_lookup(turn.prompt) or turn.spec
        if spec is not None:
            telemetry = TurnTelemetry(
                secs=spec.duration_ms / 1000,
                tokens_down=spec.tokens,
                cached_pct=spec.cached_pct,
                cost=spec.cost,
            )
            shipped = spec.shipped
            kind = (
                "shipped"
                if shipped
                else ("plan_ready" if "plan ready" in spec.outcome else "answer")
            )
            label = spec.checkpoint_label
        else:
            telemetry = TurnTelemetry(
                secs=max(0.0, (event.ts or turn.last_ts) - turn.start_ts),
                tokens_down=turn.tokens,
            )
            shipped = False
            kind = "interrupted" if turn.cancelled else "answer"
            label = turn.prompt[:40]
        outcome = TurnOutcome(kind=kind)  # type: ignore[arg-type]
        recorded = self.ledger.record_turn(
            telemetry,
            outcome,
            turn_id=turn.turn_id,
            message_index=turn.turn_id,
            label=label,
        )
        rule_label = (
            spec.rule_label
            if spec is not None
            else f"{telemetry.label()} · {outcome.outcome_label()}"
        )
        self._host.append_block(
            TurnRule(
                id=self._ids.next_id(),
                checkpoint_id=recorded.checkpoint.id,
                label=rule_label,
                shipped=shipped,
            )
        )
        self.session_cost = (
            spec.cost_after if spec is not None else self.session_cost + telemetry.cost
        )
        self._turn = None
        self._host.turn_finished()

    # -- assistant text (durable Channel B) -------------------------------------

    def _durable_text(self, event: ev.ContentBlockEnd) -> None:
        if event.block_type != "text":
            return
        text = str(event.block.get("text", ""))
        if not text:
            return
        role = str(event.block.get("demo_role") or "answer")
        if role == "narration":
            self._host.append_block(Narration(id=self._ids.next_id(), text=text))
        elif role == "idea":
            match = _IDEA_RE.match(text)
            number = int(match.group(1)) if match else 0
            body = match.group(2) if match else text
            self._host.append_block(
                BrainstormIdea(id=self._ids.next_id(), text=body, number=number)
            )
        elif role == "recap":
            self._append_recap(text)
        else:
            links: tuple[EvidenceLink, ...] = tuple(self._evidence())
            self._host.append_block(
                Answer(id=self._ids.next_id(), spans=answer_spans(text), evidence_refs=links)
            )

    def _append_recap(self, text: str) -> None:
        match = _RECAP_RE.match(text)
        if match:
            self._host.append_block(
                Recap(id=self._ids.next_id(), goal=match.group("goal"), next=match.group("next"))
            )
            return
        # Non Goal/Next recaps render as the same ✳ italic-dim line shape.
        self._host.append_block(
            Answer(
                id=self._ids.next_id(),
                spans=(
                    Segment(text="✳ ", style_token="dimmer"),
                    Segment(text=text, style_token="dim", italic=True),
                ),
            )
        )

    # -- tools -------------------------------------------------------------------

    def _tool_pre(self, event: ev.ToolPre) -> None:
        turn = self._turn
        if event.tool_name == "update_plan":
            self._update_plan(event)
            return
        block_id = self._ids.next_id()
        command = str(event.tool_input.get("command", "")) if event.tool_input else ""
        if event.tool_name == "bash" and command:
            self._host.append_block(LiveCommand(id=block_id, command=command))
        else:
            self._host.append_block(
                ToolLine(
                    id=block_id,
                    summary=f"Running {event.tool_name}",
                    status="running",
                    tool_call_ids=(event.tool_call_id,),
                )
            )
        if turn is not None:
            turn.calls[event.tool_call_id] = {
                "tool": event.tool_name,
                "command": command,
                "block_id": block_id,
                "group": event.parallel_group_id,
            }
            if event.parallel_group_id:
                group = turn.groups.setdefault(event.parallel_group_id, _Group())
                group.pending.add(event.tool_call_id)
                group.block_ids.append(block_id)
        self._update_working()

    def _tool_post(self, event: ev.ToolPost) -> None:
        turn = self._turn
        if event.tool_name == "update_plan" or turn is None:
            return
        info = turn.calls.pop(event.tool_call_id, None)
        if info is None:
            return
        command = info["command"] or str(event.tool_input.get("command", ""))
        status = str(event.result.get("status", ""))
        if status == "denied":
            turn.blocked.add(command)
            self._host.replace_block(
                Blocked(
                    id=info["block_id"],
                    cmd=command,
                    reason=str(event.result.get("reason", "denied")),
                    continuation=str(event.result.get("continuation", "")),
                )
            )
            self._update_working()
            return
        group_id = info["group"]
        if group_id and group_id in turn.groups:
            group = turn.groups[group_id]
            group.pending.discard(event.tool_call_id)
            group.commands.append(command)
            if not group.pending:
                for block_id in group.block_ids:
                    self._host.remove_block(block_id)
                self._host.append_block(
                    ToolLine(
                        id=self._ids.next_id(),
                        summary=f"Ran {len(group.commands)} shell commands",
                        body=(f"$ {' && '.join(group.commands)}",),
                        status="completed",
                    )
                )
                del turn.groups[group_id]
        elif info["tool"] == "bash":
            self._host.replace_block(
                ToolLine(
                    id=info["block_id"],
                    summary="Ran 1 shell command",
                    body=(f"$ {command}",),
                    status="completed",
                    tool_call_ids=(event.tool_call_id,),
                )
            )
        else:
            self._host.replace_block(
                ToolLine(
                    id=info["block_id"],
                    summary=f"Ran {info['tool']}",
                    status="completed",
                    tool_call_ids=(event.tool_call_id,),
                )
            )
        self._update_working()

    def _tool_error(self, event: ev.ToolError) -> None:
        turn = self._turn
        info = turn.calls.pop(event.tool_call_id, None) if turn else None
        summary = f"{event.tool_name} failed · {event.error_message}".rstrip(" ·")
        if info is not None:
            self._host.replace_block(
                ToolLine(id=info["block_id"], summary=summary, status="failed")
            )
        else:
            self._host.append_block(
                ToolLine(id=self._ids.next_id(), summary=summary, status="failed")
            )

    def _update_plan(self, event: ev.ToolPre) -> None:
        turn = self._turn
        raw = event.tool_input or {}
        title = str(raw.get("title") or "Plan")
        raw_steps = raw.get("steps") or []
        items = tuple(
            PlanItem(
                text=str(step.get("step", "")),
                state=(
                    step.get("status")
                    if step.get("status") in _PLAN_STATES
                    else "pending"
                ),
            )
            for step in raw_steps
            if isinstance(step, dict)
        )
        telemetry = self._live_telemetry()
        block_id = turn.plan_ids.get(title) if turn is not None else None
        block = PlanBlock(
            id=block_id or self._ids.next_id(),
            title=title,
            read_only=bool(raw.get("read_only")),
            items=items,
            telemetry=telemetry,
        )
        if block_id is not None:
            self._host.replace_block(block)
        else:
            if turn is not None:
                turn.plan_ids[title] = block.id
            self._host.append_block(block)
        if turn is not None:
            turn.active_step = next((i.text for i in items if i.state == "active"), None)

    # -- telemetry -------------------------------------------------------------------

    def _live_telemetry(self) -> TurnTelemetry:
        turn = self._turn
        if turn is None:
            return TurnTelemetry(secs=0)
        return TurnTelemetry(
            secs=max(0.0, turn.last_ts - turn.start_ts), tokens_down=turn.tokens
        )

    def _update_working(self) -> None:
        turn = self._turn
        if turn is None or turn.working_id is None:
            return
        self._host.replace_block(
            WorkingStatus(
                id=turn.working_id,
                telemetry=self._live_telemetry(),
                agent_count=self.lanes.active_count,
            )
        )

    def _usage(self, event: ev.ProviderResponseUsage) -> None:
        self.total_tokens += event.output_tokens
        if self._turn is not None:
            self._turn.tokens += event.output_tokens
            self._update_working()

    # -- approvals / notifications -----------------------------------------------------

    def _approval_denied(self, event: ev.ApprovalDenied) -> None:
        turn = self._turn
        cmd = event.command or event.prompt
        if turn is not None and (cmd in turn.blocked or event.prompt in turn.blocked):
            return  # already rendered from the denied tool:post
        self._host.append_block(
            Blocked(
                id=self._ids.next_id(),
                cmd=cmd,
                reason=event.reason or "denied by user",
                continuation=event.continuation,
            )
        )

    def _notification(self, event: ev.Notification) -> None:
        if event.source == "mode":
            match = _MODE_NOTICE_RE.match(event.message)
            if match:
                self._host.set_mode_by_id(match.group(1), notify=False)
            self._host.show_notice(event.message)
        elif event.source == "needs_you" or event.level == "decision":
            self._host.decision_deferred(event.message)
            self._host.show_notice(event.message)
        elif event.message:
            self._host.show_notice(event.message)

    # -- agent lanes --------------------------------------------------------------------

    def _agent_spawned(self, event: ev.AgentSpawned) -> None:
        seed: LaneSeed = self._lane_seed(event.agent) or LaneSeed()
        self.lanes.register(
            event.sub_session_id,
            parent_id=event.parent_session_id or event.session_id or None,
            name=event.agent,
            activity=seed.activity or "running",
        )
        if seed.elapsed or seed.cost:
            self.lanes.update(event.sub_session_id, elapsed=seed.elapsed, cost=seed.cost)
        label = seed.tree_spawn or f"{event.agent} · running"
        block = Answer(
            id=self._ids.next_id(),
            spans=(
                Segment(text="  ├─ ● ", style_token="dim"),
                Segment(text=label, style_token="dim"),
            ),
        )
        self._tree_ids[event.sub_session_id] = block.id
        self._host.append_block(block)
        self._update_working()
        self._host.lanes_changed()

    def _agent_completed(self, event: ev.AgentCompleted) -> None:
        seed: LaneSeed = self._lane_seed(event.agent) or LaneSeed()
        self.lanes.complete(event.sub_session_id, result="ok" if event.success else "failed")
        label = seed.tree_done or f"{event.agent} · done"
        block_id = self._tree_ids.get(event.sub_session_id)
        if block_id is not None:
            self._host.replace_block(
                Answer(
                    id=block_id,
                    spans=(
                        Segment(text="  ├─ ", style_token="dim"),
                        Segment(text="✔ ", style_token="green"),
                        Segment(text=label, style_token="dim"),
                    ),
                )
            )
        self._update_working()
        self._host.lanes_changed()


__all__ = ["LaneSeed", "ReducerHost", "TranscriptReducer", "TurnSpecLike"]
