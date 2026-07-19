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
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol, cast

from ..kernel import events as ev
from ..kernel.cost import CostTracker
from ..model.blocks import (
    ActivityBranch,
    Answer,
    BlockIdAllocator,
    Blocked,
    BrainstormIdea,
    Narration,
    PlanBlock,
    PlanItem,
    PlanItemState,
    Recap,
    Segment,
    ToolLine,
    TranscriptBlock,
    TurnRule,
    UserLine,
    WorkingStatus,
)
from ..model.evidence import EvidenceLink
from ..model.lanes import LaneRegistry, LaneStateName
from ..model.turn import OutcomeLedger, TurnOutcome, TurnTelemetry
from .live_tail import answer_spans

_RECAP_RE = re.compile(r"^Goal:\s*(?P<goal>.+?)\.\s*Next:\s*(?P<next>.+?)\.?\s*$", re.DOTALL)
_IDEA_RE = re.compile(r"^(\d+)\s+(.*)$", re.DOTALL)
_MODE_NOTICE_RE = re.compile(r"^mode (\w+)")

_PLAN_STATES = frozenset({"pending", "active", "done"})

_CHARS_PER_TOKEN = 4


def _plan_state(value: object) -> PlanItemState:
    """Coerce a raw plan-step ``status`` to a valid state (else pending)."""
    if isinstance(value, str) and value in _PLAN_STATES:
        return cast("PlanItemState", value)
    return "pending"


def _approx_tokens(*parts: object) -> int:
    """Rough token estimate for tool traffic (~4 chars/token heuristic).

    Provider usage events do not split tokens by bucket, so the /context
    ``tools`` bucket is accounted from the serialized tool inputs and
    results that actually occupy the window.
    """
    total = sum(len(str(part)) for part in parts if part)
    return max(1, total // _CHARS_PER_TOKEN) if total else 0


# -- activity humanization (rolling burst digest + live tree) ------------------

# tool name -> (verb, singular noun | None). ``None`` renders "verb N×".
_TOOL_VERBS: dict[str, tuple[str, str | None]] = {
    "bash": ("ran", "shell command"),
    "shell": ("ran", "shell command"),
    "read_file": ("read", "file"),
    "write_file": ("wrote", "file"),
    "edit_file": ("edited", "file"),
    "apply_patch": ("edited", "file"),
    "multi_edit": ("edited", "file"),
    "grep": ("searched", None),
    "glob": ("searched", None),
    "search": ("searched", None),
    "web_fetch": ("fetched", "page"),
    "web_search": ("searched web", None),
    "load_skill": ("loaded", "skill"),
    "todo": ("updated", "plan"),
}
# Reading order for the digest so it scans naturally, whatever order the
# model actually ran the tools in.
_VERB_ORDER = ("read", "searched", "searched web", "ran", "edited", "wrote", "fetched", "loaded")
_ACTIVITY_TAIL = 3  # live-tree rows kept beneath the pulse
_OP_LABEL_MAX = 52


def _verb_noun(tool: str) -> tuple[str, str | None]:
    return _TOOL_VERBS.get(tool, ("used", tool.replace("_", " ")))


def _basename(path: str) -> str:
    path = path.rstrip("/")
    return path.rsplit("/", 1)[-1] if "/" in path else path


def _op_target(tool: str, tool_input: dict[str, Any]) -> str:
    """Short human target for a tool call (for the live tree)."""
    if tool in ("bash", "shell"):
        cmd = str(tool_input.get("command", "")).strip().replace("\n", " ")
        return f"$ {cmd}"
    for key in ("file_path", "path", "filename", "notebook_path"):
        if tool_input.get(key):
            return _basename(str(tool_input[key]))
    for key in ("pattern", "query", "url", "skill", "name"):
        if tool_input.get(key):
            return str(tool_input[key])
    return ""


def _op_detail(tool: str, tool_input: dict[str, Any], result: dict[str, Any]) -> str:
    """One full detail line for the expandable digest body."""
    if tool in ("bash", "shell"):
        cmd = str(tool_input.get("command", "")).strip()
        return f"$ {cmd}" if cmd else "$ (command)"
    verb = _verb_noun(tool)[0]
    target = _op_target(tool, tool_input)
    return f"{verb} {target}".strip() if target else verb


def _truncate(text: str, width: int = _OP_LABEL_MAX) -> str:
    text = text.replace("\n", " ").strip()
    return text if len(text) <= width else f"{text[: width - 1]}…"


def _op_label(tool: str, tool_input: dict[str, Any]) -> str:
    """Compact one-liner for the live activity tree."""
    if tool in ("bash", "shell"):
        return _truncate(_op_target(tool, tool_input))
    verb = _verb_noun(tool)[0]
    target = _op_target(tool, tool_input)
    return _truncate(f"{verb} {_basename(target)}".strip() if target else verb)


def _digest_summary(counts: dict[tuple[str, str | None], int]) -> str:
    """``{('read','file'):4, ('ran','command'):6}`` -> ``Read 4 files · ran
    6 commands``. First segment capitalized; ordered for natural reading."""

    def sort_key(item: tuple[tuple[str, str | None], int]) -> int:
        verb = item[0][0]
        return _VERB_ORDER.index(verb) if verb in _VERB_ORDER else len(_VERB_ORDER)

    parts: list[str] = []
    for (verb, noun), n in sorted(counts.items(), key=sort_key):
        if noun is None:
            parts.append(f"{verb} {n}×")
        else:
            parts.append(f"{verb} {n} {noun}{'s' if n != 1 else ''}")
    if not parts:
        return ""
    summary = " · ".join(parts)
    return summary[0].upper() + summary[1:]


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
    tokens: int = 0
    state: LaneStateName = "running"
    tree_spawn: str = ""
    tree_done: str = ""


@dataclass
class _TreeLine:
    """One in-transcript agent tree line (spawn/done label + position)."""

    block_id: str
    label: str
    done: bool = False


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
    blocked: set[str] = field(default_factory=set)
    deferred: bool = False
    """Turn hit the trust boundary and deferred a decision to the queue."""
    cancelled: bool = False
    last_ts: float = 0.0
    agent_total: int = 0
    """Subagents spawned this turn — pins ``coordinating N agents``."""
    spinner_frame: int = 0
    """Working-line pulse frame, advanced by the app's 1s heartbeat."""
    activity: str = ""
    """Current work item for the working line (real turns): running
    tool / ``thinking`` — supervisor-facing context."""
    # -- rolling activity burst (DESIGN-SPEC §3) --------------------------
    digest_id: str | None = None
    """The current burst's in-place digest ToolLine (``Read 4 files · …``);
    reset when the model speaks or the turn ends so the next run of tools
    opens a fresh digest below the answer."""
    burst_counts: dict[tuple[str, str | None], int] = field(default_factory=dict)
    burst_detail: list[str] = field(default_factory=list)
    activity_ring: list[ActivityBranch] = field(default_factory=list)
    """Bounded newest-last live tree beneath the pulse (single-agent)."""


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
        self._evidence = evidence_lookup or (lambda text: ())
        self.session_cost = session_cost_start
        self.total_tokens = 0
        self.tool_tokens = 0  # /context "tools" bucket (estimated, §10)
        self.memory_tokens = 0
        """/context "memory" bucket (§10): the persistent cached prefix —
        system prompt, memory/instruction files and tool definitions —
        sized from provider cache traffic (largest cache_read+cache_write
        seen; reads cover the previously written prefix)."""
        self._cost = CostTracker()
        self._turn: _Turn | None = None
        self.turn_base = 0
        """User messages already in the live context before this session's
        ledger started counting (resume history). Foundation's fork ``turn``
        is 1-indexed over ALL user messages in the context — including
        persistent steering/decision injections — so checkpoint turn ids
        must offset past the restored history (spec §9)."""
        self._tree_lines: dict[str, _TreeLine] = {}
        self._tree_order: list[str] = []

    # -- public state -------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._turn is not None

    def title_state(self) -> str:
        """The title bar's ``<state>`` fragment (DESIGN-SPEC §2)."""
        turn = self._turn
        if turn is None:
            return "ready"
        if turn.agent_total:
            # Pinned for the whole multi-agent turn (mockup sets the
            # coordinating title once and never decrements it).
            noun = "agent" if turn.agent_total == 1 else "agents"
            return f"✳ coordinating {turn.agent_total} {noun}"
        if turn.active_step:
            return turn.active_step.lower()
        if turn.mode == "plan":
            return "planning"
        if turn.mode == "brainstorm":
            return "brainstorming"
        # Mockup: the title only changes at step activation — before the
        # first step (and on step-less turns) it keeps the idle text.
        return "ready"

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
                if event.block_type == "thinking":
                    self.set_activity("thinking")
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
            case ev.ContextInjected():
                self._context_injected()
            case ev.PromptComplete():
                self._finish_turn(event)
            case _:
                pass

    # -- turn lifecycle -------------------------------------------------------

    def _start_turn(self, event: ev.PromptSubmit) -> None:
        # Turn id = 1-indexed user-message position in the live context:
        # resume history, every ledger-recorded turn AND any persistent
        # mid-turn context injections (steers / deferred-decision answers
        # — each is one more user-role message foundation's fork counts).
        # Past injections are baked into the last checkpoint's turn_id,
        # so deriving from it (instead of a monotonic counter) both
        # carries the injection offset forward and rewinds it
        # automatically when a confirmed fork trims the ledger (spec §9).
        checkpoints = self.ledger.checkpoints
        last_turn_id = checkpoints[-1].turn_id if checkpoints else self.turn_base
        turn = _Turn(
            turn_id=last_turn_id + 1,
            prompt=event.prompt,
            start_ts=event.ts,
            last_ts=event.ts,
            mode=self._host.mode_id,
            spec=self._spec_lookup(event.prompt),
        )
        self._turn = turn
        self._cost.start_turn()
        self._tree_order = []
        self._host.append_block(
            UserLine(id=self._ids.next_id(), text=event.prompt, mode=turn.mode)
        )
        if turn.spec is None:
            # Real turn: the working line mounts IMMEDIATELY — pre-model
            # hook work and provider latency can run for seconds before
            # the first content block, and the supervisor needs a pulse
            # the whole time. (Scripted demo turns keep the mockup's
            # lazy mount under the first content block.)
            turn.working_id = self._ids.next_id()
            self._host.append_block(self._working_block(turn))
        # The working line mounts lazily under the turn's first content
        # block (mockup runTurn: after the plan header + items;
        # runAgentsTurn: after the fan-out narration) — see _append_content.
        self._host.turn_started()

    def _context_injected(self) -> None:
        """One persistent user-role message entered the context mid-turn.

        A consumed steer / answered deferred decisions injection is a real
        user message in the live transcript, and foundation's fork slicing
        counts EVERY user-role message as a turn boundary. Advance the
        running turn's id so its checkpoint addresses the LAST user message
        of the turn — forking there keeps the injection and the steered
        answer (spec §9).
        """
        if self._turn is not None:
            self._turn.turn_id += 1
        else:
            # Defensive: an injection outside a running turn still shifts
            # every later user-message position.
            self.turn_base += 1

    def _finish_turn(self, event: ev.PromptComplete) -> None:
        turn = self._turn
        if turn is None:
            return
        if turn.working_id is not None:
            self._host.remove_block(turn.working_id)
        # Tool calls that never got a post/error (a policy-denied tool
        # fires no tool:post; an interrupted turn abandons in-flight ops)
        # just close out the burst — the digest already reflects whatever
        # completed, and the ephemeral live tree vanished with the pulse.
        turn.calls.clear()
        self._flush_burst()
        usage = self._cost.end_turn()
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
            shipped = spec.shipped and not turn.cancelled
            if turn.cancelled:
                kind = "interrupted"
            elif shipped:
                kind = "shipped"
            else:
                kind = "plan_ready" if "plan ready" in spec.outcome else "answer"
            label = spec.checkpoint_label
        else:
            # Real-runtime close-out: per-turn cost and cache % come from
            # the provider usage recorded by the CostTracker (spec §11);
            # the yield (files/diffstat/tests ✔) rides on the runtime's
            # synthesized PromptComplete (git snapshot delta — spec §3).
            telemetry = TurnTelemetry(
                secs=max(0.0, (event.ts or turn.last_ts) - turn.start_ts),
                tokens_down=turn.tokens,
                cached_pct=usage.cached_pct,
                cost=usage.cost,
            )
            shipped = bool(event.files_changed) and not turn.cancelled
            if turn.cancelled:
                kind = "interrupted"
            elif shipped:
                kind = "shipped"
            elif turn.mode == "plan":
                kind = "plan_ready"
            else:
                kind = "answer"
            label = turn.prompt[:40]
        if spec is None:
            outcome = TurnOutcome(
                kind=kind,  # type: ignore[arg-type]
                files_changed=event.files_changed if shipped else 0,
                diffstat=event.diffstat if shipped else "",
                tests_ok=event.tests_ok if shipped else None,
            )
        else:
            outcome = TurnOutcome(kind=kind)  # type: ignore[arg-type]
        # Session spend is additive per turn (mockup ``this.cost += turnCost``);
        # checkpoint $ always equals the footer $ at rule time
        # (mockup ``cp.cost = this.cost``) — one session cost basis everywhere.
        self.session_cost += telemetry.cost
        recorded = self.ledger.record_turn(
            telemetry,
            outcome,
            turn_id=turn.turn_id,
            message_index=turn.turn_id,
            label=label,
            cost_at=self.session_cost,
        )
        if spec is not None:
            rule_label = spec.rule_label
        else:
            outcome_text = outcome.outcome_label()
            # ``· interrupted``/``· plan ready`` carry their own separator.
            joiner = " " if outcome_text.startswith("·") else " · "
            rule_label = f"{telemetry.label()}{joiner}{outcome_text}"
            if turn.cancelled:
                # Real interrupted close-out: the italic recap the demo
                # scripts as its own recap event (spec §11 — ``Interrupted.
                # Goal: <goal>. Context saved; resume or restate direction.``).
                self._host.append_block(
                    self._recap_line(
                        f"Interrupted. Goal: {turn.prompt[:40]}. "
                        "Context saved; resume or restate direction."
                    )
                )
        self._host.append_block(
            TurnRule(
                id=self._ids.next_id(),
                checkpoint_id=recorded.checkpoint.id,
                label=rule_label,
                shipped=shipped,
            )
        )
        self._turn = None
        self._host.turn_finished()
        if turn.deferred:
            # Mockup runTurn close-out ``if (!blocked) this.showNotice(...)``:
            # a turn that deferred a decision to the queue shows NO end
            # notice — even when interrupted — so the earlier ``decision
            # deferred to queue · run continues`` notice stays visible
            # (spec §11).
            pass
        elif turn.cancelled:
            # Mockup runTurn close-out: the interrupted turn's end notice
            # fires only once the turn actually stops (spec §11).
            self._host.show_notice("turn interrupted · context saved")
        elif spec is None:
            # Real runtime: the demo script carries its own end-notice
            # Notification events; here the reducer synthesizes spec §11's
            # ``agents N done`` success notice from the turn's fan-out.
            self._host.show_notice(f"agents {turn.agent_total or 1} done")

    def _append_content(self, block: TranscriptBlock) -> None:
        """Append turn content, keeping the working line directly below the
        turn's FIRST content block (mockup runTurn L313-315: plan header +
        items, then status; runAgentsTurn L466-467: fan-out narration, then
        status) — later content accumulates below the pinned status line."""
        self._host.append_block(block)
        turn = self._turn
        if turn is None:
            return
        if turn.working_id is not None:
            if turn.spec is None:
                # Real turn: keep the pulse at the BOTTOM, riding under
                # the newest content next to the composer.
                self._host.remove_block(turn.working_id)
                self._host.append_block(self._working_block(turn))
            return
        turn.working_id = self._ids.next_id()
        self._host.append_block(self._working_block(turn))

    # -- assistant text (durable Channel B) -------------------------------------

    def _durable_text(self, event: ev.ContentBlockEnd) -> None:
        if event.block_type != "text":
            return
        text = str(event.block.get("text", ""))
        if not text:
            return
        # The model spoke: freeze the preceding tool burst into its digest
        # above this text, and start a fresh burst below it (spec §3).
        self._flush_burst()
        role = str(event.block.get("demo_role") or "answer")
        if role == "narration":
            self._append_content(Narration(id=self._ids.next_id(), text=text))
        elif role == "idea":
            match = _IDEA_RE.match(text)
            number = int(match.group(1)) if match else 0
            body = match.group(2) if match else text
            self._append_content(
                BrainstormIdea(id=self._ids.next_id(), text=body, number=number)
            )
        elif role == "recap":
            self._append_recap(text)
        else:
            links: tuple[EvidenceLink, ...] = tuple(self._evidence(text))
            self._append_content(
                Answer(id=self._ids.next_id(), spans=answer_spans(text), evidence_refs=links)
            )

    def _append_recap(self, text: str) -> None:
        match = _RECAP_RE.match(text)
        if match:
            self._append_content(
                Recap(id=self._ids.next_id(), goal=match.group("goal"), next=match.group("next"))
            )
            return
        # Non Goal/Next recaps render as the same ✳ italic-dim line shape;
        # the mockup creates them with click: null (not evidence targets).
        self._append_content(self._recap_line(text))

    def _recap_line(self, text: str) -> Answer:
        """The ✳ italic-dim recap line shape (demo and real turns alike)."""
        return Answer(
            id=self._ids.next_id(),
            spans=(
                Segment(text="✳ ", style_token="dimmer"),
                Segment(text=text, style_token="dim", italic=True),
            ),
            clickable=False,
        )

    # -- tools -------------------------------------------------------------------

    def _tool_pre(self, event: ev.ToolPre) -> None:
        turn = self._turn
        if event.tool_name == "update_plan":
            self._update_plan(event)
            return
        tool_input = event.tool_input or {}
        command = str(tool_input.get("command", ""))
        # No durable per-tool line: the in-flight op shows as the active
        # branch in the live tree beneath the pulse, and rolls into the
        # burst digest on completion (DESIGN-SPEC §3).
        label = _op_label(event.tool_name, tool_input)
        self.set_activity(label)
        if turn is not None:
            turn.calls[event.tool_call_id] = {
                "tool": event.tool_name,
                "input": tool_input,
                "command": command,
            }
            self._push_activity(turn, label, running=True)
        self._update_working()

    def _push_activity(self, turn: _Turn, label: str, *, running: bool) -> None:
        """Add/replace the newest live-tree branch (bounded, newest last)."""
        # Drop the previous still-"running" placeholder — only one op is
        # ever in flight for the pulse's purposes.
        ring = [b for b in turn.activity_ring if not b.running]
        ring.append(ActivityBranch(text=label, running=running))
        turn.activity_ring = ring[-_ACTIVITY_TAIL:]

    def _settle_activity(self, turn: _Turn, label: str) -> None:
        """Mark the in-flight branch done (keeps it in the tail, dim)."""
        ring = [b for b in turn.activity_ring if not b.running]
        ring.append(ActivityBranch(text=label, running=False))
        turn.activity_ring = ring[-_ACTIVITY_TAIL:]

    def _tool_post(self, event: ev.ToolPost) -> None:
        turn = self._turn
        if event.tool_name == "update_plan" or turn is None:
            return
        info = turn.calls.pop(event.tool_call_id, None)
        if info is None:
            return
        self.set_activity("")  # tool finished — back to model time
        tool_input = info.get("input") or event.tool_input or {}
        self.tool_tokens += _approx_tokens(tool_input, event.result)
        command = info["command"] or str(tool_input.get("command", ""))
        tool = info["tool"]
        status = str(event.result.get("status", ""))
        if status == "denied":
            # A denial is load-bearing: it always gets its own durable ⊘
            # line (spec §3/§7), never folded into the digest.
            turn.blocked.add(command or _op_label(tool, tool_input))
            self._append_content(
                Blocked(
                    id=self._ids.next_id(),
                    cmd=command or _op_label(tool, tool_input),
                    reason=str(event.result.get("reason", "denied")),
                    continuation=str(event.result.get("continuation", "")),
                )
            )
            self._settle_activity(turn, _op_label(tool, tool_input))
            self._update_working()
            return
        # Success: roll into the burst tally + live tree, update the digest.
        self._settle_activity(turn, _op_label(tool, tool_input))
        key = _verb_noun(tool)
        turn.burst_counts[key] = turn.burst_counts.get(key, 0) + 1
        turn.burst_detail.append(_op_detail(tool, tool_input, event.result))
        self._render_digest(turn)
        self._update_working()

    def _render_digest(self, turn: _Turn) -> None:
        """Create or update this burst's single in-place digest line."""
        summary = _digest_summary(turn.burst_counts)
        if not summary:
            return
        body = tuple(turn.burst_detail)
        if turn.digest_id is None:
            turn.digest_id = self._ids.next_id()
            self._append_content(
                ToolLine(
                    id=turn.digest_id, summary=summary, body=body, status="completed"
                )
            )
        else:
            self._host.replace_block(
                ToolLine(
                    id=turn.digest_id, summary=summary, body=body, status="completed"
                )
            )

    def _flush_burst(self) -> None:
        """Freeze the current burst's digest and reset for the next run.

        Called when the model speaks (a durable answer/narration lands) and
        at turn end — the completed digest stays durable in place; the next
        tool opens a fresh digest below the answer (Claude-Code grammar)."""
        turn = self._turn
        if turn is None:
            return
        turn.digest_id = None
        turn.burst_counts = {}
        turn.burst_detail = []
        turn.activity_ring = []

    def _tool_error(self, event: ev.ToolError) -> None:
        turn = self._turn
        info = turn.calls.pop(event.tool_call_id, None) if turn else None
        self.tool_tokens += _approx_tokens(event.error_message)
        summary = f"{event.tool_name} failed · {event.error_message}".rstrip(" ·")
        if info is not None:
            self._host.replace_block(
                ToolLine(id=info["block_id"], summary=summary, status="failed")
            )
        else:
            self._append_content(
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
                state=_plan_state(step.get("status")),
            )
            for step in raw_steps
            if isinstance(step, dict)
        )
        read_only = bool(raw.get("read_only"))
        # Mockup: read-only (plan mode) headers never carry the live
        # telemetry suffix (runPlanTurn never calls setPlanTele).
        telemetry = None if read_only else self._live_telemetry()
        block_id = turn.plan_ids.get(title) if turn is not None else None
        block = PlanBlock(
            id=block_id or self._ids.next_id(),
            title=title,
            read_only=read_only,
            items=items,
            telemetry=telemetry,
        )
        if block_id is not None:
            self._host.replace_block(block)
        else:
            if turn is not None:
                turn.plan_ids[title] = block.id
            self._append_content(block)
        if turn is not None:
            active = next((i.text for i in items if i.state == "active"), None)
            if active is not None:
                # Title keeps the last step name between steps — it is
                # only reassigned at step activation (mockup line 332).
                turn.active_step = active

    # -- telemetry -------------------------------------------------------------------

    def _live_telemetry(self) -> TurnTelemetry:
        turn = self._turn
        if turn is None:
            return TurnTelemetry(secs=0)
        return TurnTelemetry(
            secs=max(0.0, turn.last_ts - turn.start_ts), tokens_down=turn.tokens
        )

    def _working_block(self, turn: _Turn) -> WorkingStatus:
        assert turn.working_id is not None
        # The live activity tree only rides single-agent turns; fan-out
        # turns get the dedicated agent tree (``_tree_block``) instead.
        lines = () if turn.agent_total > 1 else tuple(turn.activity_ring)
        return WorkingStatus(
            id=turn.working_id,
            telemetry=self._live_telemetry(),
            # Spec §3: ``N agent(s)`` — 1 on single-agent turns, the
            # fan-out total (never decaying) on multi-agent turns.
            agent_count=turn.agent_total or 1,
            spinner_frame=turn.spinner_frame,
            activity=turn.activity,
            activity_lines=lines,
        )

    def _update_working(self) -> None:
        turn = self._turn
        if turn is None or turn.working_id is None:
            return
        self._host.replace_block(self._working_block(turn))

    def tick(self, now: float) -> None:
        """App 1s heartbeat while a turn runs: pulse the working line.

        Real turns get their clock bumped to wall time (usage events only
        arrive at each content-block end, which froze the seconds counter
        during long provider calls); scripted demo turns keep their
        virtual-clock telemetry and only pulse the spinner.
        """
        turn = self._turn
        if turn is None or turn.working_id is None:
            return
        turn.spinner_frame += 1
        if turn.spec is None:
            turn.last_ts = max(turn.last_ts, now)
        self._update_working()
        # Per-agent lane clocks tick on the same heartbeat.
        if self.lanes.advance(now):
            self._host.lanes_changed()

    def set_activity(self, activity: str) -> None:
        """Update the working line's current-work note (real turns only)."""
        turn = self._turn
        if turn is None or turn.spec is not None or turn.activity == activity:
            return
        turn.activity = activity
        self._update_working()

    def _usage(self, event: ev.ProviderResponseUsage) -> None:
        self.total_tokens += event.output_tokens
        self.memory_tokens = max(self.memory_tokens, event.cache_read + event.cache_write)
        cost = self._cost.record(event)
        if self._turn is not None:
            self._turn.tokens += event.output_tokens
            self._update_working()
        # Route per-lane telemetry: usage stamped with a registered child
        # session id belongs to that subagent's lane. The root turn session
        # is never a registered lane, so it never matches (no double count).
        lane = self.lanes.get(event.session_id)
        if lane is not None:
            lane_cost = event.cost_usd if event.cost_usd is not None else cost
            self.lanes.update(
                event.session_id,
                tokens=lane.lane.tokens + event.output_tokens,
                cost=lane.lane.cost + lane_cost,
            )
            self._host.lanes_changed()

    # -- approvals / notifications -----------------------------------------------------

    def _approval_denied(self, event: ev.ApprovalDenied) -> None:
        turn = self._turn
        cmd = event.command or event.prompt
        if turn is not None and (cmd in turn.blocked or event.prompt in turn.blocked):
            return  # already rendered from the denied tool:post
        self._append_content(
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
            if self._turn is not None:
                # Mockup runTurn ``blocked = true`` — the deferral marks the
                # turn so its close-out fires no end notice, keeping this
                # deferred-decision notice visible (spec §11).
                self._turn.deferred = True
            self._host.decision_deferred(event.message)
            self._host.show_notice(event.message)
        elif event.message:
            self._host.show_notice(event.message)

    # -- agent lanes --------------------------------------------------------------------

    def _tree_block(self, sub_session_id: str) -> Answer:
        """Render one agent tree line (mockup lines 469-480).

        The last-spawned lane uses the ``└─`` corner glyph, earlier lanes
        ``├─``; spawn prefix is all dimmer, done prefix all green.
        """
        line = self._tree_lines[sub_session_id]
        last = bool(self._tree_order) and self._tree_order[-1] == sub_session_id
        branch = "└─" if last else "├─"
        if line.done:
            prefix = Segment(text=f"  {branch} ✔ ", style_token="green")
        else:
            prefix = Segment(text=f"  {branch} ● ", style_token="dimmer")
        return Answer(
            id=line.block_id,
            spans=(prefix, Segment(text=line.label, style_token="dim")),
            # Mockup tree lines are plain this.L(...) rows — click: null.
            clickable=False,
        )

    def _agent_spawned(self, event: ev.AgentSpawned) -> None:
        turn = self._turn
        if turn is not None:
            turn.agent_total += 1
        seed: LaneSeed = self._lane_seed(event.agent) or LaneSeed()
        self.lanes.register(
            event.sub_session_id,
            parent_id=event.parent_session_id or event.session_id or None,
            name=event.agent,
            activity=seed.activity or "running",
            state=seed.state,
            # A done lane re-spawning here is a replayed turn reusing its
            # sub-session ids (completions for unknown lanes are dropped, so
            # no spawn/complete race reaches this path) — reset it live.
            reopen=True,
            # Stamp the spawn wall-time so advance() can tick the lane's
            # per-agent elapsed live between sparse usage events.
            now=event.ts or time.time(),
        )
        if seed.elapsed or seed.cost or seed.tokens:
            self.lanes.update(
                event.sub_session_id,
                elapsed=seed.elapsed,
                cost=seed.cost,
                tokens=seed.tokens,
            )
        label = seed.tree_spawn or f"{event.agent} · running"
        previous_last = self._tree_order[-1] if self._tree_order else None
        self._tree_lines[event.sub_session_id] = _TreeLine(
            block_id=self._ids.next_id(), label=label
        )
        self._tree_order.append(event.sub_session_id)
        self._append_content(self._tree_block(event.sub_session_id))
        if previous_last is not None and previous_last in self._tree_lines:
            # The previous tail is no longer the last child: ├─ again.
            self._host.replace_block(self._tree_block(previous_last))
        self._update_working()
        self._host.lanes_changed()

    def _agent_completed(self, event: ev.AgentCompleted) -> None:
        seed: LaneSeed = self._lane_seed(event.agent) or LaneSeed()
        result = event.result or ("" if event.success else "failed")
        self.lanes.complete(event.sub_session_id, result=result)
        line = self._tree_lines.get(event.sub_session_id)
        if line is not None:
            line.label = seed.tree_done or f"{event.agent} · done"
            line.done = True
            self._host.replace_block(self._tree_block(event.sub_session_id))
        self._update_working()
        self._host.lanes_changed()


__all__ = ["LaneSeed", "ReducerHost", "TranscriptReducer", "TurnSpecLike"]
