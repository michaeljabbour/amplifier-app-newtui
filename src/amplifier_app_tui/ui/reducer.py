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
from collections.abc import Sequence
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
    DelegateEntry,
    DelegateSummaryBlock,
    Narration,
    PlanBlock,
    PlanItem,
    PlanItemState,
    Recap,
    Segment,
    Thinking,
    TodoItem,
    TodoStatus,
    ToolLine,
    TranscriptBlock,
    TurnRule,
    UnsupportedBlock,
    UserLine,
    WorkingStatus,
)
from ..model.codemode import CODE_MODE_TOOL
from ..model.evidence import EvidenceLink
from ..model.formatting import command_digest
from ..model.lanes import TERMINAL_LANE_STATES, LaneRegistry, LaneStateName
from ..model.turn import OutcomeLedger, TurnOutcome, TurnTelemetry
from .lane_reducer import (
    LANE_TAIL_NOTIFY_SECONDS as LANE_TAIL_NOTIFY_SECONDS,
    LaneNotifyKind,
    LaneReducer,
    _LANE_TRANSCRIPT_MAX_BLOCKS as _LANE_TRANSCRIPT_MAX_BLOCKS,
)
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


_TODO_STATES = frozenset({"pending", "in_progress", "completed"})


def _todo_status(value: object) -> TodoStatus:
    """Coerce a raw todo ``status`` to a valid state (else pending)."""
    if isinstance(value, str) and value in _TODO_STATES:
        return cast("TodoStatus", value)
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
}
# Reading order for the digest so it scans naturally, whatever order the
# model actually ran the tools in.
_VERB_ORDER = ("read", "searched", "searched web", "ran", "edited", "wrote", "fetched", "loaded")
_ACTIVITY_TAIL = 3  # live-tree rows kept beneath the pulse
_OP_LABEL_MAX = 52
_CHANGE_PREVIEW_LINES = 80
_CHANGE_DETAIL_LINES = 240
_CHANGE_TOOLS = frozenset({"write_file", "edit_file", "apply_patch"})

_LIVE_TOOL_VERBS: dict[str, str] = {
    "bash": "running",
    "shell": "running",
    "read_file": "reading",
    "write_file": "writing",
    "edit_file": "editing",
    "apply_patch": "editing",
    "multi_edit": "editing",
    "grep": "searching",
    "glob": "finding files",
    "search": "searching",
    "web_fetch": "fetching",
    "web_search": "searching web",
    "load_skill": "loading",
    "delegate": "delegating",
}
"""Present-tense labels for the compact per-agent activity ticker."""


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


_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MD_INLINE_RE = re.compile(r"(\*\*|__|~~|`+|\*|_(?=\w)|(?<=\w)_)")
_MD_BLOCK_PREFIX_RE = re.compile(r"^\s*(#{1,6}|[-*+>]|\d+[.)])\s+")


def _lane_result_summary(result: str, width: int = _OP_LABEL_MAX) -> str:
    """Distil a delegate's (often Markdown) result into a clean one-line lane
    summary: take the first non-empty line, drop a leading heading/list/quote
    marker and inline emphasis, collapse whitespace, prefer the first sentence
    when long, and truncate. Keeps the lane row readable instead of pasting raw
    Markdown (``## Foo **bar**…``) into it."""
    first = next((ln for ln in result.splitlines() if ln.strip()), "")
    first = _MD_BLOCK_PREFIX_RE.sub("", first)
    first = _MD_LINK_RE.sub(r"\1", first)
    first = _MD_INLINE_RE.sub("", first).strip()
    if len(first) > width:
        first = first.split(". ", 1)[0]
    return _truncate(first, width)


def _op_label(tool: str, tool_input: dict[str, Any]) -> str:
    """Compact one-liner for the live activity tree."""
    if tool in ("bash", "shell"):
        return _truncate(_op_target(tool, tool_input))
    verb = _verb_noun(tool)[0]
    target = _op_target(tool, tool_input)
    return _truncate(f"{verb} {_basename(target)}".strip() if target else verb)


def _live_op_label(tool: str, tool_input: dict[str, Any]) -> str:
    """Short present-tense child activity suitable for an in-place ticker."""

    verb = _LIVE_TOOL_VERBS.get(tool, f"using {tool.replace('_', ' ')}")
    target = _op_target(tool, tool_input)
    if tool in ("bash", "shell") and target.startswith("$ "):
        target = target[2:]
    return _truncate(f"{verb} {_basename(target)}".strip() if target else verb)


def _change_preview(
    tool: str, tool_input: dict[str, Any]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return ``(paths, bounded diff-like detail)`` for a native file write."""

    path = str(tool_input.get("file_path") or tool_input.get("path") or "").strip()
    if tool not in _CHANGE_TOOLS:
        return (), ()
    if tool == "apply_patch":
        patch = str(tool_input.get("patch") or tool_input.get("diff") or "")
        paths = tuple(
            dict.fromkeys(
                marker.split(" File:", 1)[1].strip()
                for marker in patch.splitlines()
                if marker.startswith(("*** Add File:", "*** Update File:", "*** Delete File:"))
            )
        )
        if path:
            paths = tuple(dict.fromkeys((*paths, path)))
        lines = tuple(patch.splitlines())
    elif not path:
        return (), ()
    elif tool == "edit_file":
        paths = (path,)
        old = str(tool_input.get("old_string", "")).splitlines()
        new = str(tool_input.get("new_string", "")).splitlines()
        lines = (
            f"--- {path}",
            f"+++ {path}",
            "@@ replaced text @@",
            *(f"-{line}" for line in old),
            *(f"+{line}" for line in new),
        )
    else:
        paths = (path,)
        content = str(tool_input.get("content", "")).splitlines()
        lines = (
            f"+++ {path}",
            f"@@ wrote file · {len(content)} lines @@",
            *(f"+{line}" for line in content),
        )
    if len(lines) > _CHANGE_PREVIEW_LINES:
        hidden = len(lines) - _CHANGE_PREVIEW_LINES
        lines = (*lines[:_CHANGE_PREVIEW_LINES], f"… {hidden} more lines")
    return paths, tuple(lines)


# -- code mode (execute) special-case render (donor: TUI <Execute>) ------------

_CODEMODE_PROGRAM_LINES = 80
"""Bound the inlined program source in the expandable body."""
_CODEMODE_OUTPUT_LINES = 40
"""Bound the inlined result/diagnostic tail in the expandable body."""


def _codemode_trace(result: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    """The bridged tool-call trace as ``(name, status)`` pairs.

    Tolerant of the honest seam (serve was not modified): reads ``tool_calls``
    (host ``ToolCall``) or ``metadata.toolCalls`` (donor ``CallEntry``), and
    accepts either a ``name`` or a ``tool`` key for the call label.
    """
    raw = result.get("tool_calls")
    if not isinstance(raw, list):
        meta = result.get("metadata")
        raw = meta.get("toolCalls") if isinstance(meta, dict) else None
    calls: list[tuple[str, str]] = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("tool") or "").strip()
            if not name:
                continue
            status = str(item.get("status") or "completed").strip() or "completed"
            calls.append((name, status))
    return tuple(calls)


def _codemode_is_error(result: dict[str, Any]) -> bool:
    """Whether the program/diagnostic failed (donor: ``metadata.error``)."""
    if result.get("error") is True or result.get("ok") is False:
        return True
    if str(result.get("status", "")).lower() in {"error", "failed"}:
        return True
    return bool(result.get("diagnostic"))


def _codemode_output(result: dict[str, Any]) -> str:
    """The result body: the rendered ``output`` string, else the diagnostic
    message (+ suggestions), else the JSON-ish program value."""
    out = result.get("output")
    if isinstance(out, str) and out.strip():
        return out
    diag = result.get("diagnostic")
    if isinstance(diag, dict):
        parts: list[str] = []
        message = str(diag.get("message", "")).strip()
        if message:
            parts.append(message)
        suggestions = diag.get("suggestions")
        if isinstance(suggestions, (list, tuple)):
            parts.extend(str(hint) for hint in suggestions if str(hint).strip())
        if parts:
            return "\n".join(parts)
    value = result.get("value")
    if isinstance(value, str):
        return value
    if value is not None:
        import json

        try:
            return json.dumps(value, indent=2, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(value)
    return ""


def _codemode_bounded(lines: list[str], limit: int) -> list[str]:
    if len(lines) > limit:
        hidden = len(lines) - limit
        return [*lines[:limit], f"\u2026 {hidden} more lines"]
    return lines


def codemode_execute_block(
    tool_input: dict[str, Any],
    result: dict[str, Any],
    *,
    block_id: str,
    tool_call_ids: tuple[str, ...] = (),
    expanded: bool = False,
) -> ToolLine:
    """One durable, expandable Code Mode ``execute`` line.

    Re-expresses the donor TUI ``<Execute>`` render for a host ``ToolLine``:
    the collapsed head names Code Mode and the bridged tool-call count; the
    expandable body shows the program source, the ``\u21b3`` bridged trace
    (with a failed-call marker), and the result/diagnostics. Pure: no host,
    no I/O \u2014 golden/behaviorally testable in isolation.
    """
    program = str(tool_input.get("code", "") or "")
    calls = _codemode_trace(result)
    error = _codemode_is_error(result)
    output = _codemode_output(result)

    count = len(calls)
    call_label = f"{count} tool call{'s' if count != 1 else ''}" if count else "no tool calls"
    summary = f"Code Mode \u00b7 execute \u00b7 {call_label}"
    if error:
        summary = f"{summary} \u00b7 failed"

    body: list[str] = []
    if program.strip():
        body.append("program")
        body.extend(
            f"  {line}" for line in _codemode_bounded(program.splitlines(), _CODEMODE_PROGRAM_LINES)
        )
    if calls:
        if body:
            body.append("")
        body.append("tool calls")
        for name, status in calls:
            suffix = "" if status == "completed" else f" \u00b7 {status}"
            body.append(f"  \u21b3 {name}{suffix}")
    if output.strip():
        if body:
            body.append("")
        body.append("result")
        body.extend(
            f"  {line}" for line in _codemode_bounded(output.splitlines(), _CODEMODE_OUTPUT_LINES)
        )

    return ToolLine(
        id=block_id,
        summary=summary,
        body=tuple(body),
        status="failed" if error else "completed",
        expanded=expanded,
        tool_call_ids=tool_call_ids,
    )


def _blocked_body(raw: str, reason: str) -> tuple[str, ...]:
    """Expandable blocked-line body: the WHY plus the raw command verbatim.

    The collapsed row carries only the verb-noun digest (a heredoc must
    never sprawl across it); this keeps every raw byte one click away.
    """
    lines: list[str] = []
    if reason:
        lines.append(f"why · {reason}")
    lines.extend(line for line in str(raw).splitlines() if line.strip())
    return tuple(lines)


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


@dataclass
class _DelegateRow:
    """Live state for one agent in the current fan-out summary (D5)."""

    agent: str
    spawned_ts: float
    state: str = "running"  # DelegateState
    elapsed_s: float = 0.0
    snippet: str = ""


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
    def plan_changed(self, items: tuple[TodoItem, ...]) -> None: ...
    def approval_opened(self, prompt: str, options: tuple[str, ...]) -> None: ...
    def decision_deferred(self, message: str, decision_id: str = "") -> None: ...
    def attention_error(self, detail: str, *, occasion: str) -> None: ...
    def stream_opened(self, block_type: str) -> None: ...
    def stream_delta(self, text: str) -> None: ...
    def stream_closed(self) -> None: ...
    def lane_tail_updated(self, text: str) -> None: ...
    def lane_tail_cleared(self) -> None: ...


REPLAY_SKIPPED_KINDS = frozenset(
    {
        # Channel A: the durable content_block_end records carry the text
        # (ADR-0007: never reconstruct one channel from the other), and a
        # live-tail replay would only churn the stream surface.
        "stream_block_start",
        "stream_block_delta",
        "stream_block_end",
        "stream_aborted",
        # Interactive/transient surfaces must not re-fire from history:
        # notifications (transient notices, mode flips, needs-you
        # deferrals — a stale decision must not resurrect in the queue),
        # approval presentation, and provider retry/throttle toasts all
        # belong to the moment they happened.
        "notification",
        "provider_notice",
        "approval_required",
        "approval_granted",
    }
)
"""Event kinds :meth:`TranscriptReducer.replay` never re-dispatches."""


class _ReplayHost:
    """ReducerHost proxy for resume replay (DESIGN-SPEC §3/§11).

    Chosen over a reducer-wide "replay mode" flag: dispatch stays one
    code path, and the whole suppression contract is visible here —
    durable block mutations and plan state pass through; everything
    interactive or ephemeral (notices, approval presentation, needs-you
    deferrals, turn timers/bells/queue drains, stream tail, per-event
    lane repaints) is silenced, so replaying history can never re-trigger
    a side effect the session already had live.
    """

    def __init__(self, host: ReducerHost) -> None:
        self._host = host
        self._working_ids: set[str] = set()

    @property
    def mode_id(self) -> str:
        return self._host.mode_id

    def append_block(self, block: TranscriptBlock) -> None:
        # The working pulse is running-turn chrome — a replayed
        # transcript has no running turn, so it never mounts. (Also
        # load-bearing: the live bottom-ride removes and re-appends the
        # pulse under the SAME id in one synchronous stretch, and
        # Textual's prune is deferred — a replayed ride would mount a
        # duplicate widget id.)
        if block.kind == "working_status":
            self._working_ids.add(block.id)
            return
        self._host.append_block(block)

    def replace_block(self, block: TranscriptBlock) -> None:
        if block.kind == "working_status":
            return
        self._host.replace_block(block)

    def remove_block(self, block_id: str) -> None:
        if block_id in self._working_ids:
            self._working_ids.discard(block_id)
            return
        self._host.remove_block(block_id)

    def plan_changed(self, items: tuple[TodoItem, ...]) -> None:
        # The final todo state is restored ambient state, not a side
        # effect — the plan panel reopens where the session left off.
        self._host.plan_changed(items)

    def show_notice(self, text: str) -> None:
        pass

    def set_mode_by_id(self, mode_id: str, *, notify: bool = True) -> None:
        pass

    def turn_started(self) -> None:
        pass

    def turn_finished(self) -> None:
        pass

    def lanes_changed(self) -> None:
        pass  # replay() repaints the lanes surface once at the end

    def approval_opened(self, prompt: str, options: tuple[str, ...]) -> None:
        pass

    def decision_deferred(self, message: str) -> None:
        pass

    def attention_error(self, detail: str, *, occasion: str) -> None:
        # Resume replay reconstructs lane/transcript state (agent_completed
        # is NOT in REPLAY_SKIPPED_KINDS -- lane state must rebuild), but a
        # historical failure must never re-ring the bell on every resume;
        # silenced here exactly like every other interactive side effect.
        pass

    def stream_opened(self, block_type: str) -> None:
        pass

    def stream_delta(self, text: str) -> None:
        pass

    def stream_closed(self) -> None:
        pass

    def lane_tail_updated(self, text: str) -> None:
        pass

    def lane_tail_cleared(self) -> None:
        pass


class _StaleTurnHost:
    """ReducerHost proxy for a turn stamped with a pre-clear generation (D3).

    Swapped in by :meth:`TranscriptReducer._dispatch_stale` for the
    duration of one event when the active turn's generation is behind the
    live counter (a ``/clear`` landed mid-turn, D3). Silences every
    transcript-visible effect — appends, replaces, removals, notices,
    stream deltas, turn start/finish, plan/approval/decision surfaces,
    lane tail — so a delayed tool result or streaming tail from BEFORE
    the clear can never resurrect a row in the freshly emptied view.
    ``lanes_changed`` still forwards: the lanes panel tracks real
    background-agent state independently of the transcript and stays
    accurate either way.
    """

    def __init__(self, host: ReducerHost) -> None:
        self._host = host

    @property
    def mode_id(self) -> str:
        return self._host.mode_id

    def append_block(self, block: TranscriptBlock) -> None:
        pass

    def replace_block(self, block: TranscriptBlock) -> None:
        pass

    def remove_block(self, block_id: str) -> None:
        pass

    def show_notice(self, text: str) -> None:
        pass

    def set_mode_by_id(self, mode_id: str, *, notify: bool = True) -> None:
        pass

    def turn_started(self) -> None:
        pass

    def turn_finished(self) -> None:
        pass

    def lanes_changed(self) -> None:
        self._host.lanes_changed()

    def plan_changed(self, items: tuple[TodoItem, ...]) -> None:
        pass

    def approval_opened(self, prompt: str, options: tuple[str, ...]) -> None:
        pass

    def decision_deferred(self, message: str, decision_id: str = "") -> None:
        pass

    def attention_error(self, detail: str, *, occasion: str) -> None:
        pass  # a stale/pre-clear turn's error is not worth pulling the user back to

    def stream_opened(self, block_type: str) -> None:
        pass

    def stream_delta(self, text: str) -> None:
        pass

    def stream_closed(self) -> None:
        pass

    def lane_tail_updated(self, text: str) -> None:
        pass

    def lane_tail_cleared(self) -> None:
        pass


@dataclass
class _Turn:
    turn_id: int
    session_id: str
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
    blocked_lines: list[tuple[str, Blocked]] = field(default_factory=list)
    """``(raw action, rendered Blocked block)`` newest-last — a following
    deferral notification upgrades ITS line to the ``needs your ok`` form."""
    deferred_actions: list[str] = field(default_factory=list)
    """Deferral notifications whose ⊘ line has NOT rendered yet (the real
    governance hook defers BEFORE it denies, so the decision notification
    can precede the blocked line). ``""`` matches the next blocked line."""
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
    phase: str = "submitted"
    """Liveness phase feeding the working line's empty-activity note
    (``submitted`` → ``executing`` → ``streaming``, see _PHASE_NOTES)."""
    # -- rolling activity burst (DESIGN-SPEC §3) --------------------------
    digest_id: str | None = None
    """The current burst's in-place digest ToolLine (``Read 4 files · …``);
    reset when the model speaks or the turn ends so the next run of tools
    opens a fresh digest below the answer."""
    burst_counts: dict[tuple[str, str | None], int] = field(default_factory=dict)
    burst_detail: list[str] = field(default_factory=list)
    activity_ring: list[ActivityBranch] = field(default_factory=list)
    """Bounded newest-last live tree beneath the pulse (single-agent)."""
    child_calls: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    """Child tool inputs retained until post so successful edits can be shown."""
    change_id: str | None = None
    change_files: set[str] = field(default_factory=set)
    change_detail: list[str] = field(default_factory=list)
    """One in-place, expandable change summary shared by root and children."""
    response_candidates: list[tuple[str, str]] = field(default_factory=list)
    """Production durable text as ``(text, block_id)`` candidates.

    Streaming orchestrators emit intermediate prose and the final response
    through the same ``content_block:end`` contract.  Keep those blocks as
    as styled, non-clickable candidates until ``PromptComplete.response``
    identifies the one final answer for the turn.
    """
    rendered_answers: set[str] = field(default_factory=set)
    """Normalized answer texts already rendered for exact-once close-out."""
    thinking_id: str | None = None
    """Open Thinking block awaiting its ``content_block:end`` prose (issue
    #129). The loop-streaming runtime brackets a thinking block with
    start/end, so the collapsed block is minted on start and populated in
    place on end; reset once populated."""
    todo_items: tuple[TodoItem, ...] = ()
    """Latest root-todo list this turn (ambient-progress D3) — folded into
    the delegate summary's ``plan_final`` at fan-out close (D5)."""
    generation: int = 0
    """The reducer's clear-generation counter at this turn's start (D3).

    Stamped once in :meth:`TranscriptReducer._start_turn`; :meth:`handle`
    compares it against the LIVE counter on every event so a ``/clear``
    mid-turn fences the rest of this turn's tail (see
    :meth:`TranscriptReducer.bump_generation`).
    """


_PHASE_NOTES = {
    "submitted": "starting turn",
    "executing": "waiting on model",
    "streaming": "thinking",
}
"""Working-line liveness notes for the silent stretches of a real turn
(validated against a real session's ui-events.jsonl):

- ``prompt_submit → execution_start`` (~15s of backend pre-turn hooks):
  submitted but not executing yet → ``starting turn``.
- ``execution_start → first content_block`` (~11s of model prefill):
  executing, no blocks yet → ``waiting on model``.
- first content/tool traffic onward: the long-standing ``thinking``
  fallback.

The phase only feeds the working line's EMPTY-activity note — any real
tool activity / live tree wins exactly as before. Labels are shared
verbatim with the Rust app (joint liveness enhancement)."""


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
        tail_clock: Any = None,
        schedule_flush: Any = None,
    ) -> None:
        self._host = host
        self._ids = allocator
        self.ledger = ledger
        self.lanes = lanes
        self._spec_lookup = spec_lookup or (lambda prompt: None)
        self._lane_seed = lane_seed_lookup or (lambda name: None)
        self._evidence = evidence_lookup or (lambda text: ())
        self.session_cost = session_cost_start
        self.unpriced_usage = 0
        """Usage records this session that could not be priced (real
        turns only — demo/spec turns carry scripted costs). Non-zero ⇒
        ``session_cost`` is a floor; the footer renders ``~$`` (never
        lie in the footer)."""
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
        # -- delegate fan-out summary (ambient-progress D5) -----------------
        # Reducer-held (not turn-held) so completions landing after turn end
        # still update the block, mirroring the old tree-line lifetime.
        self._delegate_summary_id: str | None = None
        self._delegate_rows: dict[str, _DelegateRow] = {}
        self._delegate_order: list[str] = []
        self._fanout_start_ts: float = 0.0
        self._fanout_duration_s: float = 0.0
        self._delegate_plan_final: tuple[TodoItem, ...] | None = None
        # -- agent lanes: live tail + focus transcripts (LaneReducer) -------
        # Lane presentation state (per-lane live tail, focused-lane
        # transcripts, pending delegate briefs) lives in its own unit; the
        # turn reducer routes diverted child events onto lanes and drives it.
        self._lane = LaneReducer(
            host,
            allocator=allocator,
            lanes=lanes,
            tail_clock=tail_clock,
            schedule_flush=schedule_flush,
        )
        self._generation = 0
        """Clear-generation counter (D3): bumped by :meth:`bump_generation`
        when ``/clear`` runs; each ``_Turn`` stamps the value live at its
        own start so :meth:`handle` can fence a pre-clear turn's tail.
        """

    # -- public state -------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._turn is not None

    @property
    def generation(self) -> int:
        """The current clear-generation counter (bumped by ``/clear``, D3).

        Every ``_Turn`` is stamped with the generation live at its start;
        :meth:`handle` compares that stamp against this counter to fence a
        pre-clear turn's remaining tail (see :meth:`bump_generation`).
        """
        return self._generation

    def bump_generation(self) -> int:
        """Start a new clear-generation (``/clear``, D3).

        Any turn already in flight keeps its OLD stamp, so :meth:`handle`
        dispatches its remaining events against a silenced host instead of
        the real one: the turn's own bookkeeping (cost, ledger, lanes)
        still completes normally, but a delayed delta/tool-result/notice
        can never append, replace or remove a row in the just-emptied
        view. Returns the new generation (mainly for tests).
        """
        self._generation += 1
        return self._generation

    @property
    def live_session_cost(self) -> Decimal:
        """Committed session spend plus usage received in the active turn."""
        if self._turn is not None and self._turn.spec is not None:
            return self.session_cost
        return self.session_cost + self._cost.turn.cost

    @property
    def live_cost_estimated(self) -> bool:
        """Whether the live total is only a floor because usage is unpriced."""
        if self._turn is not None and self._turn.spec is not None:
            return self.unpriced_usage > 0
        return self.unpriced_usage > 0 or self._cost.turn.unpriced > 0

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

    # -- resume replay (DESIGN-SPEC §3/§11) -----------------------------------

    def replay(
        self,
        events: Sequence[ev.UIEvent | UnsupportedBlock],
        *,
        turn_base: int = 0,
        session_cost: Decimal = Decimal("0"),
    ) -> bool:
        """Rebuild the transcript from a resumed session's stored events.

        The session store persists every normalized UIEvent; feeding them
        back through the same dispatch rebuilds exactly what rendered
        live — tool digests, ⊘ blocked lines, delegate summaries, lane
        focus transcripts, plan state, turn rules with real telemetry —
        instead of the prose-only fallback. Side effects are suppressed
        via :class:`_ReplayHost` + :data:`REPLAY_SKIPPED_KINDS`.

        ``events`` may interleave :class:`UnsupportedBlock` placeholders
        (S5) for persisted records ``kernel.events.parse_event`` could not
        type — a foreign writer's line, an unknown/removed ``kind``, or
        schema drift. Each one is appended directly, in its original log
        position, with a freshly minted id; it never reaches :meth:`handle`
        (it carries no turn semantics to dispatch), so one unrecognized
        record can never drop the rest of a rich, mixed transcript.

        ``turn_base``/``session_cost`` are the transcript-derived turn
        count and the kernel-restored cost baseline; both stay the
        post-replay authorities (see the reconciliation below). Returns
        ``False`` — with no state touched — when the log holds no
        replayable turn (absent/foreign log), so the caller can fall back
        to prose.
        """
        if not any(event.kind == "prompt_submit" for event in events):
            return False
        live_host = self._host
        self._host = cast("ReducerHost", _ReplayHost(live_host))
        # Replayed turns re-derive their own 1-indexed context positions
        # from zero, exactly as they did live (ContextInjected advances
        # included) — the LAST replayed checkpoint's turn_id must land on
        # *turn_base*; seeding it here as well would double the offset.
        self.turn_base = 0
        self.session_cost = Decimal("0")
        try:
            for event in events:
                if isinstance(event, UnsupportedBlock):
                    self._host.append_block(event.model_copy(update={"id": self._ids.next_id()}))
                    continue
                if event.kind in REPLAY_SKIPPED_KINDS:
                    continue
                self.handle(event)
            if self._turn is not None:
                # The log ended mid-turn (crash/kill before close-out):
                # settle it as interrupted — the same durable shape a
                # live Esc leaves. ts stays in the log's clock domain.
                self._turn.cancelled = True
                self.handle(
                    ev.PromptComplete(session_id=self._turn.session_id, ts=self._turn.last_ts)
                )
        finally:
            self._host = live_host
        # Lanes the log never completed (same crash case) must not keep
        # ticking against the wall clock after resume.
        for record in self.lanes.lanes:
            if record.lane.state not in TERMINAL_LANE_STATES:
                # "cancelled" (not the "done" default): the log ended
                # without this lane ever reporting a real outcome — a
                # crash/interruption is closer to a cancelled turn than a
                # successful completion (D5 AC1: no outcome folds into a
                # glyph that doesn't match what actually happened).
                self.lanes.complete(record.session_id, result="interrupted", state="cancelled")
        checkpoints = self.ledger.checkpoints
        if not checkpoints or checkpoints[-1].turn_id != turn_base:
            # Degrade explicitly: the event log disagrees with the stored
            # transcript (truncated log, or post-rewind ghost turns —
            # events.jsonl is append-only while a confirmed fork trims
            # the context). The replayed blocks stay as scrollback, but
            # their checkpoints would fork the live context at the wrong
            # turns; reset the ledger so new checkpoints fall back to the
            # transcript-derived turn_base (existing resume math, §9).
            self.ledger.clear()
        self.turn_base = turn_base
        # The kernel's restore_session_cost stays the single authority
        # for the resumed cost baseline (it carries the exactly-once
        # repair for logs older builds wrote) — replay's own accumulation
        # stamped self-consistent checkpoint cost_at values and is
        # reconciled to that authority here, never added on top of it.
        self.session_cost = session_cost
        live_host.lanes_changed()
        return True

    # -- dispatch -------------------------------------------------------------

    def handle(self, event: ev.UIEvent) -> None:
        """Apply one normalized event; unknown kinds are ignored.

        A turn stamped with an OLDER clear-generation than the live
        counter (``/clear`` landed mid-turn, D3) dispatches through a
        silenced host instead of the real one: internal bookkeeping
        (cost, ledger, lanes) still completes normally, but the turn's
        remaining tail can never append/replace/notify into the
        already-cleared view (see :meth:`bump_generation`).
        """
        # Any event stamped with a booting child's session id is that
        # child's first sign of life — bundle composition finished; flip
        # the lane to its normal running state (validated dead window:
        # spawn → child session_start runs ~tens of seconds).
        self._wake_booting_lane(event)
        if self._is_foreign_turn_event(event):
            self._track_child_activity(event)
            return
        if self._turn is not None:
            # The envelope always stamps ts — no falsy-zero guard (the demo's
            # virtual clock legitimately starts at 0.0).
            self._turn.last_ts = event.ts
            # A PromptSubmit always starts a FRESH _Turn stamped with the
            # CURRENT generation (see _start_turn) -- it must never be
            # fenced by whatever turn preceded it, or a /clear immediately
            # followed by a new prompt would silently swallow that new
            # turn's own UserLine + working line. Every OTHER event acts on
            # the EXISTING self._turn, so it inherits that turn's stamp.
            if not isinstance(event, ev.PromptSubmit) and self._turn.generation != self._generation:
                self._dispatch_stale(event)
                return
        self._dispatch(event)

    def _dispatch_stale(self, event: ev.UIEvent) -> None:
        """Run :meth:`_dispatch` against a silenced host (D3 fencing).

        Swapped in only for the duration of this one event: the pre-clear
        turn's internal state transitions (cost, ledger, lane completion)
        still happen exactly as before, but nothing it does can reach the
        real, already-cleared transcript.
        """
        live_host = self._host
        self._host = cast("ReducerHost", _StaleTurnHost(live_host))
        try:
            self._dispatch(event)
        finally:
            self._host = live_host

    def _dispatch(self, event: ev.UIEvent) -> None:  # noqa: C901 - one dispatch table
        match event:
            case ev.SessionStart() if event.parent_id:
                if self.lanes.bind_session(event.session_id, parent_id=event.parent_id):
                    self._host.lanes_changed()
            case ev.PromptSubmit():
                self._start_turn(event)
            case ev.ExecutionStart():
                self._execution_started(event)
            case ev.StreamBlockStart():
                self._mark_model_traffic()
                self._lane.root_streaming = True
                self._lane.clear_tail()
                self._host.stream_opened(event.block_type)
                if event.block_type == "thinking":
                    self.set_activity("thinking")
            case ev.StreamBlockDelta():
                self._host.stream_delta(event.text)
            case ev.StreamBlockEnd():
                self._lane.root_streaming = False
                self._host.stream_closed()
            case ev.StreamAborted():
                self._lane.root_streaming = False
                self._host.stream_closed()
                self._host.show_notice(f"stream aborted · {event.error_message}".rstrip(" ·"))
            case ev.ContentBlockStart():
                self._mark_model_traffic()
                if event.block_type == "thinking":
                    self._thinking_started(event)
            case ev.ContentBlockEnd():
                self._mark_model_traffic()
                if event.block_type == "thinking":
                    self._thinking_recorded(event)
                else:
                    self._durable_text(event)
            case ev.ToolPre():
                self._mark_model_traffic()
                self._tool_pre(event)
            case ev.ToolPost():
                self._tool_post(event)
            case ev.ToolError():
                self._tool_error(event)
            case ev.ProviderResponseUsage():
                self._usage(event)
            case ev.ProviderNotice():
                self._host.show_notice(f"provider {event.notice} · {event.message}".rstrip(" ·"))
                if event.notice == "error":
                    # B7 gap 3 (production error transition #2 -- a
                    # provider/runtime error): retry/throttle notices are
                    # transient noise, not attention-worthy; only "error"
                    # qualifies. ``event_id`` is this envelope's own stable
                    # per-instance id (never replayed -- provider_notice is
                    # in REPLAY_SKIPPED_KINDS -- so a fresh id per real
                    # occurrence is exactly right, not a dedup gap).
                    self._host.attention_error(
                        event.message, occasion=f"provider-error-{event.event_id}"
                    )
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
            case ev.ContextCompacted():
                self._context_compacted(event)
            case ev.PromptComplete():
                self._finish_turn(event)
            case _:
                pass

    def _is_foreign_turn_event(self, event: ev.UIEvent) -> bool:
        """Keep child execution traffic out of the root transcript.

        The runtime deliberately attaches the queue bridge to child sessions
        so their usage can feed lane telemetry.  Their streams, prose, tools,
        and orchestrator close-outs must not mutate the root turn, though.
        Empty session ids remain accepted for compatibility with synthetic
        events and older tests.
        """
        turn = self._turn
        if (
            turn is None
            or not turn.session_id
            or not event.session_id
            or event.session_id == turn.session_id
        ):
            return False
        return isinstance(
            event,
            (
                ev.StreamBlockStart,
                ev.StreamBlockDelta,
                ev.StreamBlockEnd,
                ev.StreamAborted,
                ev.ContentBlockStart,
                ev.ContentBlockEnd,
                ev.ToolPre,
                ev.ToolPost,
                ev.ToolError,
                ev.OrchestratorComplete,
            ),
        )

    def _track_child_activity(self, event: ev.UIEvent) -> None:
        """Project child execution into one compact lane/tree status line.

        Child prose and tools stay out of the parent transcript, but their
        high-signal lifecycle events make the existing lane and agent-tree
        labels useful as an in-place activity ticker.
        """

        record = self.lanes.get(event.session_id)
        if record is None or record.lane.state in TERMINAL_LANE_STATES:
            return
        activity: str | None = None
        # ``None`` is a sentinel meaning "ordinary narration — neither
        # enters nor clears attention" (resolved below): only a fresh tool
        # attempt (ToolPre) clears a prior ``attention`` back to ``working``,
        # and only a discrete failure (ToolError / a failed ToolPost) enters
        # it. Everything else (stream/content/orchestrator narration)
        # preserves whatever attention-ness the lane already has — an
        # attention row must survive the very next unrelated narration
        # beat, not flicker off before anyone can see it (D5 AC1).
        state: LaneStateName | None = None
        # D5 AC5: classifies the repaint notification below. "error" is a
        # discrete failure surfaced against a lane that keeps running (a
        # tool errored, or came back denied/failed) — distinct from
        # "final" (the lane itself completing, handled in
        # ``_agent_completed``). Everything else here is ordinary
        # narration churn and may be coalesced under high volume.
        kind: LaneNotifyKind = "progress"
        match event:
            case ev.ToolPre():
                if self._turn is not None:
                    self._turn.child_calls[(record.session_id, event.tool_call_id)] = {
                        "tool": event.tool_name,
                        "input": event.tool_input or {},
                        "actor": record.lane.name,
                    }
                activity = _live_op_label(event.tool_name, event.tool_input or {})
                state = "working"  # a fresh attempt always clears a prior attention
            case ev.ToolPost():
                call = (
                    self._turn.child_calls.pop((record.session_id, event.tool_call_id), None)
                    if self._turn is not None
                    else None
                )
                tool = str(call.get("tool", "")) if call else event.tool_name
                tool_input = dict(call.get("input", {})) if call else (event.tool_input or {})
                status = str(event.result.get("status", "")).lower()
                success = event.result.get("success", True)
                ok = success is not False and status not in {"denied", "error", "failed"}
                if ok and self._turn is not None:
                    self._record_change(self._turn, record.lane.name, tool, tool_input)
                self._lane.append_block(
                    record,
                    ToolLine(
                        id=self._ids.next_id(),
                        summary=_live_op_label(tool, tool_input),
                        status="completed" if ok else "failed",
                        tool_call_ids=(event.tool_call_id,) if event.tool_call_id else (),
                    ),
                )
                activity = "reviewing tool result"
                if not ok:
                    kind = "error"
                    state = "attention"  # a failed result needs notice (D5 AC1)
            case ev.ToolError():
                self._lane.append_block(
                    record,
                    ToolLine(
                        id=self._ids.next_id(),
                        summary=f"{event.tool_name.replace('_', ' ')} · "
                        f"{event.error_message}".rstrip(" ·"),
                        status="failed",
                        tool_call_ids=(event.tool_call_id,) if event.tool_call_id else (),
                    ),
                )
                activity = f"recovering from {event.tool_name.replace('_', ' ')} error"
                kind = "error"
                state = "attention"  # same discrete-failure signal as above
            case ev.StreamBlockStart():
                activity = "thinking" if event.block_type == "thinking" else "writing response"
            case ev.StreamBlockDelta():
                activity = "thinking" if event.block_type == "thinking" else "writing response"
                self._lane.tail_delta(record, event)
            case ev.StreamBlockEnd():
                activity = "reviewing response"
            case ev.ContentBlockEnd():
                if event.block_type == "text":
                    text = str(event.block.get("text", ""))
                    if text:
                        self._lane.append_block(
                            record,
                            Answer(
                                id=self._ids.next_id(),
                                spans=answer_spans(text),
                                clickable=False,
                            ),
                        )
                activity = "reporting findings" if event.block_type == "text" else "thinking"
            case ev.OrchestratorComplete():
                activity = "wrapping up"
            case _:
                return
        if state is None:
            state = "attention" if record.lane.state == "attention" else "running"
        if activity is None or (record.lane.activity == activity and record.lane.state == state):
            return
        updated = self.lanes.update(event.session_id, activity=activity, state=state)
        if updated is None:
            return
        self._lane.notify_lanes_changed(kind=kind)

    # -- agent lanes: focus transcripts + live tail (LaneReducer) ------------

    def lane_transcript(self, key: str) -> list[TranscriptBlock] | None:
        """A lane's accumulated focus transcript, by session id or name.

        The real-runtime counterpart of the demo adapter's
        ``lane_blocks`` — ``None`` (not ``[]``) when nothing is known so
        the caller's no-transcript notice stays meaningful. Owned by the
        LaneReducer; kept here as the reducer's public lane surface.
        """
        return self._lane.transcript(key)

    def repaint_lane_tail(self) -> None:
        """Paint the focused lane's buffered tail right now (ctrl+o).

        Cycling the pin must not wait for the new lane's next delta —
        otherwise the tail keeps showing the previous lane's text. Owned
        by the LaneReducer; kept here as the reducer's public lane surface.
        """
        self._lane.repaint_tail()

    def _record_change(
        self, turn: _Turn, actor: str, tool: str, tool_input: dict[str, Any]
    ) -> None:
        """Roll a successful native file write into one expandable diff row."""

        paths, preview = _change_preview(tool, tool_input)
        if not paths or not preview:
            return
        turn.change_files.update(paths)
        path_label = ", ".join(paths)
        detail = [f"{actor} · {tool.replace('_', ' ')} · {path_label}", *preview]
        remaining = _CHANGE_DETAIL_LINES - len(turn.change_detail)
        if remaining > 0:
            turn.change_detail.extend(detail[:remaining])
        count = len(turn.change_files)
        summary = f"Changed {count} file{'s' if count != 1 else ''}"
        block = ToolLine(
            id=turn.change_id or self._ids.next_id(),
            summary=summary,
            body=tuple(turn.change_detail),
            status="completed",
            body_style="diff",
        )
        if turn.change_id is None:
            turn.change_id = block.id
            self._append_content(block)
        else:
            self._host.replace_block(block)

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
            session_id=event.session_id,
            prompt=event.prompt,
            start_ts=event.ts,
            last_ts=event.ts,
            # The event carries the posture the turn was submitted under
            # (stamped into ui-events.jsonl), so resume replay stamps the
            # user line's ``[mode]`` badge with the HISTORICAL mode rather
            # than the current live one. Legacy logs (no mode) fall back to
            # the live posture — the pre-stamp behavior.
            mode=event.mode or self._host.mode_id,
            spec=self._spec_lookup(event.prompt),
            generation=self._generation,
        )
        self._turn = turn
        self._cost.start_turn()
        self._delegate_summary_id = None
        self._delegate_rows = {}
        self._delegate_order = []
        self._fanout_start_ts = 0.0
        self._fanout_duration_s = 0.0
        self._delegate_plan_final = None
        self._host.append_block(UserLine(id=self._ids.next_id(), text=event.prompt, mode=turn.mode))
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
        self._lane.clear_tail()
        self._lane.root_streaming = False
        # A cancelled turn strands running delegates: settle them as ⊘ so the
        # durable summary never claims work that was interrupted (edge-case
        # table, ambient-progress design).
        if turn.cancelled and any(row.state == "running" for row in self._delegate_rows.values()):
            lane_changed = False
            for sub_session_id, row in self._delegate_rows.items():
                if row.state == "running":
                    row.state = "cancelled"
                    row.elapsed_s = max(0.0, turn.last_ts - row.spawned_ts)
                    # Reconcile rather than duplicate (D5 AC1): the lane itself
                    # settles to the SAME "cancelled" outcome as the delegate
                    # row above, driven by the identical turn.cancelled signal
                    # — not a second, independently-derived notion.
                    record = self.lanes.get(sub_session_id)
                    if record is not None and record.lane.state not in TERMINAL_LANE_STATES:
                        self.lanes.complete(sub_session_id, state="cancelled")
                        lane_changed = True
            self._fanout_duration_s = max(0.0, turn.last_ts - self._fanout_start_ts)
            self._render_delegate_summary()
            if lane_changed:
                # A lane's terminal transition must never be coalesced away
                # (D5 AC5's "final" privilege applies here too).
                self._lane.notify_lanes_changed(kind="final")
        # Re-resolve at close: mid-turn events (e.g. a denied approval)
        # may have changed the adapter's close-out spec for this prompt.
        spec = self._spec_lookup(turn.prompt) or turn.spec
        if spec is None:
            self._finalize_response(event.response)
        if turn.working_id is not None:
            self._host.remove_block(turn.working_id)
        # Tool calls that never got a post/error (a policy-denied tool
        # fires no tool:post; an interrupted turn abandons in-flight ops)
        # just close out the burst — the digest already reflects whatever
        # completed, and the ephemeral live tree vanished with the pulse.
        turn.calls.clear()
        self._flush_burst()
        usage = self._cost.end_turn()
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
            self.unpriced_usage += usage.unpriced
            telemetry = TurnTelemetry(
                secs=max(0.0, event.ts - turn.start_ts),  # one clock domain, no fallback
                tokens_down=turn.tokens,
                cached_pct=usage.cached_pct,
                cost=usage.cost,
                estimated=usage.unpriced > 0,
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
                # the newest content next to the composer. The re-append
                # must mint a FRESH id: Textual prunes asynchronously, so
                # remove+append under the same id in one synchronous
                # stretch mounts a duplicate widget id (found by resume
                # replay; live turns logged "reducer failed on tool_post"
                # and lost the pulse).
                self._host.remove_block(turn.working_id)
                turn.working_id = self._ids.next_id()
                self._host.append_block(self._working_block(turn))
            return
        turn.working_id = self._ids.next_id()
        self._host.append_block(self._working_block(turn))

    # -- assistant text (durable Channel B) -------------------------------------

    def _thinking_started(self, event: ev.ContentBlockStart) -> None:
        """Open a collapsed Thinking block where the model began reasoning.

        The loop-streaming runtime carries no token deltas, so the block
        opens empty here and its prose lands via :meth:`_thinking_recorded`
        on the matching ``content_block:end``. The lane/working label stays
        task-level (``thinking``) — reasoning prose lives only in this
        durable transcript block, never in the lanes pane (issue #129).
        """
        turn = self._turn
        if turn is None:
            return
        self.set_activity("thinking")
        block = Thinking(id=self._ids.next_id())
        self._append_content(block)
        turn.thinking_id = block.id

    def _thinking_recorded(self, event: ev.ContentBlockEnd) -> None:
        """Populate a Thinking block from its ``content_block:end`` payload.

        Reads ``block["thinking"]`` (core's ThinkingBlock field) then falls
        back to ``block["text"]``. Degrades honestly on withheld reasoning:
        core's ``ThinkingBlock.visibility`` (LLM_ONLY/USER_ONLY) can strip
        the prose from UI-facing events, so the text may be empty — the
        block stays and renders "content withheld by provider" rather than
        vanishing. Replaces the open block in place (no working-line reflow);
        appends defensively if no start was seen (non-streaming provider).
        """
        turn = self._turn
        if turn is None:
            return
        text = str(event.block.get("thinking") or event.block.get("text") or "")
        if turn.thinking_id is not None:
            self._host.replace_block(Thinking(id=turn.thinking_id, text=text))
        else:
            self._append_content(Thinking(id=self._ids.next_id(), text=text))
        turn.thinking_id = None

    def _durable_text(self, event: ev.ContentBlockEnd) -> None:
        if event.block_type != "text":
            return
        text = str(event.block.get("text", ""))
        if not text:
            return
        # The model spoke: freeze the preceding tool burst into its digest
        # above this text, and start a fresh burst below it (spec §3).
        self._flush_burst()
        explicit_role = event.block.get("demo_role")
        if explicit_role is None:
            # Real-runtime text is provisional.  The orchestrator can speak
            # before tools and again at the end; PromptComplete.response is
            # the authoritative final-answer identity.
            # Commit the same formatted shape the streaming tail just showed.
            # It remains non-clickable/provisional until PromptComplete adds
            # evidence and authoritatively identifies the final response.
            block = Answer(id=self._ids.next_id(), spans=answer_spans(text), clickable=False)
            self._append_content(block)
            if self._turn is not None:
                self._turn.response_candidates.append((text.strip(), block.id))
            return

        role = str(explicit_role)
        if role == "narration":
            self._append_content(Narration(id=self._ids.next_id(), text=text))
        elif role == "idea":
            match = _IDEA_RE.match(text)
            number = int(match.group(1)) if match else 0
            body = match.group(2) if match else text
            self._append_content(BrainstormIdea(id=self._ids.next_id(), text=body, number=number))
        elif role == "recap":
            self._append_recap(text)
        else:
            links: tuple[EvidenceLink, ...] = tuple(self._evidence(text))
            # A scripted demo turn has no provisional/final distinction --
            # DemoTurnSpec knows the whole script up front, so its one
            # plain "answer"-role block IS the turn's final response the
            # moment it lands (AC2 anchor; see Answer.final's docstring).
            answer = Answer(
                id=self._ids.next_id(),
                spans=answer_spans(text),
                evidence_refs=links,
                final=True,
            )
            self._append_content(answer)
            if self._turn is not None:
                self._turn.rendered_answers.add(text.strip())

    def _finalize_response(self, response: str) -> None:
        """Promote or append the real turn's one authoritative answer."""
        turn = self._turn
        text = response.strip()
        if turn is None or not text or text in turn.rendered_answers:
            return

        self._flush_burst()
        links: tuple[EvidenceLink, ...] = tuple(self._evidence(text))
        for candidate_text, block_id in reversed(turn.response_candidates):
            if candidate_text != text:
                continue
            # PromptComplete.response just identified THIS candidate as the
            # turn's one authoritative answer -- stamp the AC2 start anchor
            # in the same replace that promotes it (Answer.final's docstring).
            self._host.replace_block(
                Answer(
                    id=block_id,
                    spans=answer_spans(response),
                    evidence_refs=links,
                    final=True,
                )
            )
            turn.rendered_answers.add(text)
            return

        # This fallback runs only during close-out. Appending through
        # _append_content would move/re-mount the working pulse immediately
        # before _finish_turn removes it, creating an avoidable Textual race
        # for non-streaming providers whose answer exists only here. It is
        # still the turn's one authoritative answer, so it still gets the
        # AC2 start anchor.
        self._host.append_block(
            Answer(
                id=self._ids.next_id(),
                spans=answer_spans(response),
                evidence_refs=links,
                final=True,
            )
        )
        turn.rendered_answers.add(text)

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
        if event.tool_name == "todo":
            self._update_todo(event)
            return
        tool_input = event.tool_input or {}
        command = str(tool_input.get("command", ""))
        if "delegate" in event.tool_name:
            # Remember the instruction so the spawned lane's focus
            # transcript can open with the delegated brief (the
            # normalized AgentSpawned event carries no instruction).
            agent = str(tool_input.get("agent") or tool_input.get("agent_name") or "")
            brief = str(
                tool_input.get("instruction")
                or tool_input.get("prompt")
                or tool_input.get("task")
                or ""
            )
            if agent and brief:
                self._lane.remember_brief(agent, brief)
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
        if event.tool_name in ("update_plan", "todo") or turn is None:
            # Plans are their own blocks (rendered from tool:pre); todos
            # feed the ambient plan panel — neither joins the digest.
            return
        info = turn.calls.pop(event.tool_call_id, None)
        if info is None:
            return
        self.set_activity("")  # tool finished — back to model time
        tool_input = info.get("input") or event.tool_input or {}
        self.tool_tokens += _approx_tokens(tool_input, event.result)
        command = info["command"] or str(tool_input.get("command", ""))
        tool = info["tool"]
        if tool == CODE_MODE_TOOL:
            # Code Mode replaces many round-trips with one program; render the
            # program + bridged trace + result as its own durable block instead
            # of folding an opaque `used execute` into the burst digest.
            self._append_content(
                codemode_execute_block(
                    tool_input,
                    event.result,
                    block_id=self._ids.next_id(),
                    tool_call_ids=(event.tool_call_id,) if event.tool_call_id else (),
                )
            )
            self._settle_activity(turn, _op_label(tool, tool_input))
            self._update_working()
            return
        status = str(event.result.get("status", ""))
        if status == "denied":
            # A denial is load-bearing: it always gets its own durable ⊘
            # line (spec §3/§7), never folded into the digest.
            raw = command or _op_label(tool, tool_input)
            turn.blocked.add(raw)
            self._append_blocked(
                turn,
                raw,
                str(event.result.get("reason", "denied")),
                str(event.result.get("continuation", "")),
            )
            self._settle_activity(turn, _op_label(tool, tool_input))
            self._update_working()
            return
        # Success: roll into the burst tally + live tree, update the digest.
        if event.result.get("success", True) is not False and status.lower() not in {
            "error",
            "failed",
        }:
            self._record_change(turn, "main agent", tool, tool_input)
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
                ToolLine(id=turn.digest_id, summary=summary, body=body, status="completed")
            )
        else:
            self._host.replace_block(
                ToolLine(id=turn.digest_id, summary=summary, body=body, status="completed")
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
            self._append_content(ToolLine(id=self._ids.next_id(), summary=summary, status="failed"))

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

    def _update_todo(self, event: ev.ToolPre) -> None:
        """Route the ``todo`` tool to the ambient plan panel — never the
        transcript (design 2026-07-21 D1/D3).

        The printing ``hooks-todo-display`` is stripped under the TUI, so
        tui renders the list itself from the tool call's ``todos``
        payload (``create``/``update`` ops carry the full list; ``list``
        carries none). Root-session only: child ToolPre events are
        diverted before dispatch (see ``_is_foreign_turn_event``).
        """
        raw = event.tool_input or {}
        raw_todos = raw.get("todos")
        if not isinstance(raw_todos, list) or not raw_todos:
            return  # a 'list' op or empty payload — nothing to redraw
        items = tuple(
            TodoItem(content=str(todo.get("content", "")), status=_todo_status(todo.get("status")))
            for todo in raw_todos
            if isinstance(todo, dict)
        )
        turn = self._turn
        if turn is not None:
            turn.todo_items = items
        self._host.plan_changed(items)
        if self._delegate_summary_id is not None:
            # The runtime closes the plan AFTER the last AgentCompleted
            # (demo beat order: agent_completed → todo) — fold the fresh
            # todo state into the durable summary so its ``Plan X/Y``
            # header ends true, not one beat behind (D3 plan-fold). Still
            # an in-turn replace: post-turn toggles are never clobbered.
            self._render_delegate_summary()

    # -- telemetry -------------------------------------------------------------------

    def _live_telemetry(self) -> TurnTelemetry:
        turn = self._turn
        if turn is None:
            return TurnTelemetry(secs=0)
        return TurnTelemetry(secs=max(0.0, turn.last_ts - turn.start_ts), tokens_down=turn.tokens)

    def _working_block(self, turn: _Turn) -> WorkingStatus:
        assert turn.working_id is not None
        # The live activity tree only rides single-agent turns; fan-out
        # turns get the dedicated DelegateSummaryBlock instead (D5).
        lines = () if turn.agent_total > 1 else tuple(turn.activity_ring)
        # Real turns with no explicit activity surface the liveness phase
        # (``starting turn`` / ``waiting on model`` / ``thinking``) so the
        # validated silent stretches — pre-turn hooks, model prefill —
        # never read as a dead app. Real tool activity always wins;
        # scripted demo turns keep their scripted (empty) note.
        activity = turn.activity
        if not activity and turn.spec is None:
            activity = _PHASE_NOTES[turn.phase]
        return WorkingStatus(
            id=turn.working_id,
            telemetry=self._live_telemetry(),
            # Spec §3: ``N agent(s)`` — 1 on single-agent turns, the
            # fan-out total (never decaying) on multi-agent turns.
            agent_count=turn.agent_total or 1,
            spinner_frame=turn.spinner_frame,
            activity=activity,
            activity_lines=lines,
        )

    def _update_working(self) -> None:
        """Repaint the live working-status row for the current turn.

        Generation-guarded (D3): unlike every other transcript mutation in
        this class, :meth:`tick` calls this directly from the app's 1s
        heartbeat — OUTSIDE :meth:`handle`'s dispatch, so it never passes
        through :class:`_StaleTurnHost`. Without this guard, a ``/clear``
        mid-turn would see its own just-unmounted pulse silently
        RE-APPENDED a second later (``TuiApp.replace_block``'s
        not-currently-mounted fallback treats the row as merely unmounted,
        not gone) — once a second, for as long as the pre-clear turn's own
        bookkeeping keeps running. That is exactly the resurrection D3
        promises can't happen, so the check has to live here rather than
        rely on the dispatch host swap.
        """
        turn = self._turn
        if turn is None or turn.working_id is None or turn.generation != self._generation:
            return
        self._host.replace_block(self._working_block(turn))

    def tick(self, now: float) -> None:
        """App 1s heartbeat while a turn runs: pulse the working line.

        Real turns get their clock bumped to wall time (usage events only
        arrive at each content-block end, which froze the seconds counter
        during long provider calls); scripted demo turns keep their
        virtual-clock telemetry and only pulse the spinner.

        Runs even for a turn stamped with a stale clear-generation (D3)
        — spinner_frame/last_ts keep advancing and lanes still tick
        (mirrors :meth:`bump_generation`'s "cost/ledger/lanes still
        complete normally" contract, and ``lanes_changed()`` is the same
        deliberate un-fenced pass-through used elsewhere); only the
        transcript-visible repaint in :meth:`_update_working` is fenced.
        """
        turn = self._turn
        if turn is None or turn.working_id is None:
            return
        turn.spinner_frame += 1
        if turn.spec is None:
            turn.last_ts = max(turn.last_ts, now)
        self._update_working()
        # Per-agent lane clocks tick on the same heartbeat — real turns only.
        # Scripted lanes were stamped with the demo's virtual clock; advancing
        # them with wall time paints epoch-scale elapsed in the panel.
        if turn.spec is None and self.lanes.advance(now):
            self._host.lanes_changed()

    def set_activity(self, activity: str) -> None:
        """Update the working line's current-work note (real turns only)."""
        turn = self._turn
        if turn is None or turn.spec is not None or turn.activity == activity:
            return
        turn.activity = activity
        self._update_working()

    def _execution_started(self, event: ev.ExecutionStart) -> None:
        """``execution:start`` for the running turn: pre-turn hooks are
        done, the engine now waits on the model's first content block
        (liveness phase ``starting turn`` → ``waiting on model``).

        Child sessions emit ``execution_start`` too (it is not in the
        foreign-event divert list), so the root phase only advances for
        the turn's own session; empty ids stay accepted for synthetic
        events, matching the divert rule.
        """
        turn = self._turn
        if (
            turn is None
            or turn.spec is not None
            or turn.phase != "submitted"
            or (turn.session_id and event.session_id and event.session_id != turn.session_id)
        ):
            return
        turn.phase = "executing"
        self._update_working()

    def _mark_model_traffic(self) -> None:
        """First root content/tool traffic of the turn: the model is
        producing — the empty-activity note settles on the long-standing
        ``thinking`` fallback for the rest of the turn."""
        turn = self._turn
        if turn is None or turn.spec is not None or turn.phase == "streaming":
            return
        turn.phase = "streaming"
        self._update_working()

    def _wake_booting_lane(self, event: ev.UIEvent) -> None:
        """Flip a booting lane to its normal running state on the child's
        first event (``session_start``, ``execution_start``, usage, …).

        A seeded delegate brief stays in place as the activity line; the
        plain ``booting`` placeholder becomes ``running``.
        """
        session_id = event.session_id
        if not session_id:
            return
        record = self.lanes.get(session_id)
        if record is None or record.lane.state != "booting":
            return
        self.lanes.update(
            session_id,
            state="running",
            activity="running" if record.lane.activity == "booting" else None,
        )
        self._host.lanes_changed()

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
            # D5 AC5: per-lane token/cost ticking is high-volume telemetry
            # churn, not a privileged event — safe to coalesce.
            self._lane.notify_lanes_changed(kind="progress")

    def _context_compacted(self, event: ev.ContextCompacted) -> None:
        """Persist a quiet but inspectable compaction boundary in history."""
        token_delta = f"{event.before_tokens:,} → {event.after_tokens:,} tokens"
        message_delta = (
            f" · {event.before_messages} → {event.after_messages} messages"
            if event.before_messages or event.after_messages
            else ""
        )
        level = f" · strategy {event.strategy_level}" if event.strategy_level else ""
        text = f"Context compacted · {token_delta}{message_delta}{level}"
        self._append_content(Narration(id=self._ids.next_id(), text=text))
        self._host.show_notice(text)

    # -- approvals / notifications -----------------------------------------------------

    def _approval_denied(self, event: ev.ApprovalDenied) -> None:
        turn = self._turn
        cmd = event.command or event.prompt
        if turn is not None and (cmd in turn.blocked or event.prompt in turn.blocked):
            return  # already rendered from the denied tool:post
        self._append_blocked(turn, cmd, event.reason or "denied by user", event.continuation)

    def _append_blocked(self, turn: _Turn | None, raw: str, reason: str, continuation: str) -> None:
        """Render one durable ⊘ line: compact digest on the row, the raw
        command (plus the why) behind the ToolLine-style expand body.

        The body attaches only when the digest actually hides something —
        short one-line denials keep their original single-line form. A
        deferral notification that follows upgrades the line in place
        (:meth:`_mark_blocked_deferred`)."""
        digest = command_digest(raw)
        deferred = False
        if turn is not None and turn.deferred_actions:
            # The deferral notification already arrived (real governance
            # defers BEFORE it denies): this line is born deferred.
            for index, action in enumerate(turn.deferred_actions):
                if action in ("", raw):
                    del turn.deferred_actions[index]
                    deferred = True
                    break
        block = Blocked(
            id=self._ids.next_id(),
            cmd=digest,
            reason=reason,
            continuation=continuation,
            body=_blocked_body(raw, reason) if (deferred or digest != raw) else (),
            deferred=deferred,
        )
        self._append_content(block)
        if turn is not None:
            turn.blocked_lines.append((raw, block))

    def _mark_blocked_deferred(self, action: str) -> bool:
        """Upgrade the deferral's ⊘ line to ``needs your ok — ctrl+y``.

        Matched by the deferral's denied ``action`` (real runtime); a
        deferral with no action key (scripted/demo notices) upgrades the
        turn's newest blocked line. Returns False when no line matched —
        the caller then parks the action so the line renders deferred the
        moment it appears (the real hook defers BEFORE it denies)."""
        turn = self._turn
        if turn is None or not turn.blocked_lines:
            return False
        index = len(turn.blocked_lines) - 1
        if action:
            matches = [i for i, (raw, _) in enumerate(turn.blocked_lines) if raw == action]
            if not matches:
                return False
            index = matches[-1]
        raw, block = turn.blocked_lines[index]
        if block.deferred:
            return True
        updated = block.model_copy(
            update={
                "deferred": True,
                # The deferred head hides the reason tail: make sure the
                # expand body carries WHY + the raw command even when the
                # digest didn't shorten anything.
                "body": block.body or _blocked_body(raw, block.reason),
            }
        )
        turn.blocked_lines[index] = (raw, updated)
        self._host.replace_block(updated)
        return True

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
            # The deferral's ⊘ line flips to ``needs your ok — ctrl+y``; if
            # it hasn't rendered yet (defer-before-deny ordering), park the
            # action so the line is born deferred.
            if not self._mark_blocked_deferred(event.action) and self._turn is not None:
                self._turn.deferred_actions.append(event.action)
            self._host.decision_deferred(event.message, event.decision_id)
            self._host.show_notice(event.message)
        elif event.message:
            self._host.show_notice(event.message)

    # -- agent lanes --------------------------------------------------------------------

    def _agent_spawned(self, event: ev.AgentSpawned) -> None:
        turn = self._turn
        if turn is not None:
            turn.agent_total += 1
        seed: LaneSeed = self._lane_seed(event.agent) or LaneSeed()
        # A spawn with no scripted state/telemetry is a real delegate whose
        # child session has produced nothing yet — bundle composition can
        # run ~tens of seconds, and a ``running · 0.0k tokens · $0.00`` row
        # reads as hung. Open the lane as ``booting`` (first child event
        # flips it — see _wake_booting_lane); scripted demo seeds keep
        # their mockup-verbatim presentation.
        booting = seed.state == "running" and not seed.elapsed and not seed.cost and not seed.tokens
        self.lanes.register(
            event.sub_session_id,
            parent_id=event.parent_session_id or event.session_id or None,
            name=event.agent,
            activity=seed.activity or ("booting" if booting else "running"),
            state="booting" if booting else seed.state,
            # A done lane re-spawning here is a replayed turn reusing its
            # sub-session ids (completions for unknown lanes are dropped, so
            # no spawn/complete race reaches this path) — reset it live.
            reopen=True,
            # Stamp the spawn time so advance() can tick the lane's
            # per-agent elapsed live between sparse usage events. The
            # envelope always stamps ts (default_factory) — no fallback:
            # the demo's virtual clock legitimately starts at 0.0, and an
            # `or time.time()` here mixes clock domains (0s durations).
            now=event.ts,
            # D6 AC4: every visible stream states its producing agent
            # AND its turn — 0 (never a real 1-indexed turn_id) is the
            # sentinel for the defensive no-active-turn case.
            turn=turn.turn_id if turn is not None else 0,
        )
        if seed.elapsed or seed.cost or seed.tokens:
            self.lanes.update(
                event.sub_session_id,
                elapsed=seed.elapsed,
                cost=seed.cost,
                tokens=seed.tokens,
            )
        # Peek the delegate brief BEFORE seeding pops it into the lane's
        # focus transcript — the chat marker names the task in a few words.
        brief = _lane_result_summary(self._lane.pending_brief(event.agent))
        self._lane.seed_transcript(event)
        now = event.ts
        if not self._delegate_rows:
            self._fanout_start_ts = now
        if event.sub_session_id not in self._delegate_rows:
            self._delegate_order.append(event.sub_session_id)
        # A known sub-session re-spawning is a replayed turn reusing its ids
        # (see lanes.register reopen above) — reset the row live either way.
        self._delegate_rows[event.sub_session_id] = _DelegateRow(agent=event.agent, spawned_ts=now)
        self._lifecycle_marker(f"{event.agent} started" + (f" · {brief}" if brief else ""))
        self._render_delegate_summary()
        self._update_working()
        self._host.lanes_changed()

    def _agent_completed(self, event: ev.AgentCompleted) -> None:
        result = event.result or ("" if event.success else "failed")
        record = self.lanes.get(event.sub_session_id)
        self._lane.clear_tail(record.session_id if record is not None else event.sub_session_id)
        if record is not None:
            # Focus-transcript close-out (mockup focusLane state recap):
            # ``✳ `` dimmer + dim italic state line, never clickable.
            recap = (
                "completed · result reported back to parent"
                if event.success
                else ("failed" if result in ("", "failed") else f"failed · {result}")
            )
            self._lane.append_block(
                record,
                Answer(
                    id=self._ids.next_id(),
                    spans=(
                        Segment(text="✳ ", style_token="dimmer"),
                        Segment(text=recap, style_token="dim", italic=True),
                    ),
                    clickable=False,
                ),
            )
        hint = _lane_result_summary(result)
        # A reasonless failure's ``result`` fallback IS the literal word
        # "failed" (see above) — showing it back as a "reason" would read
        # as "failed · failed". One shared, meaningful hint feeds BOTH the
        # chat's ✳ marker and the lane's own activity text, so neither
        # surface can independently regress the other (D5 AC1 reconciliation).
        meaningful_hint = hint if (event.success or hint != "failed") else ""
        settled = self.lanes.complete(
            event.sub_session_id,
            result=meaningful_hint,
            state="done" if event.success else "error",
        )
        if settled is not None and settled.lane.state == "error":
            # B7 gap 3 (production error transition #3 -- a failed
            # delegate): key off the SAME terminal lane-state signal
            # TERMINAL_LANE_STATES/lanes_changed already reads (D5 AC1),
            # not a second, independently-derived "was this a failure"
            # check -- narrower than TERMINAL_LANE_STATES on purpose:
            # "cancelled" is a deliberate user interrupt, never an error.
            # sub_session_id is the occasion: a delegate settles into
            # "error" exactly once (terminal), so a repeat delivery for
            # the SAME delegate dedupes (AC3) rather than re-notifying.
            self._host.attention_error(
                meaningful_hint or f"{event.agent} failed", occasion=event.sub_session_id
            )
        if event.success:
            marker = f"{event.agent} done" + (f" · {meaningful_hint}" if meaningful_hint else "")
        else:
            marker = f"{event.agent} failed" + (f" · {meaningful_hint}" if meaningful_hint else "")
        self._lifecycle_marker(marker)
        row = self._delegate_rows.get(event.sub_session_id)
        if row is not None:
            end_ts = event.ts  # same clock domain as spawned_ts — no fallback
            row.state = "done" if event.success else "error"
            row.elapsed_s = max(0.0, end_ts - row.spawned_ts)
            row.snippet = result
            if all(r.state != "running" for r in self._delegate_rows.values()):
                self._fanout_duration_s = max(0.0, end_ts - self._fanout_start_ts)
            self._render_delegate_summary()
        self._update_working()
        # D5 AC5: the lane's terminal lifecycle transition (success OR
        # failure) is the "final" privileged class — it must never be
        # coalesced away, however many progress frames preceded it.
        self._lane.notify_lanes_changed(kind="final")

    def _lifecycle_marker(self, text: str) -> None:
        """One compact dim ✳ line in the chat for a delegate lifecycle beat
        (started / done / failed) — the chat's view of cross-agent activity.

        Child thinking, prose and tool chatter stay lanes-only (the
        foreign-turn divert plus the lanes-panel tail); the root session's
        own narration renders exactly as before. Real turns only: scripted
        demo turns carry their own fan-out narration beats, and a straggler
        completion landing after close-out would render below the turn rule
        (the delegate summary already updates in place for those)."""
        turn = self._turn
        if turn is None or turn.spec is not None:
            return
        self._append_content(self._recap_line(text))

    def _render_delegate_summary(self) -> None:
        """Append-once / replace-in-place, keyed by ``_delegate_summary_id``.

        Always rendered expanded=False — expansion is UI-local state; the
        transcript's replace path preserves a live widget's expansion so
        neither a mid-flight replace nor a post-turn straggler completion
        collapses a summary the user has opened."""
        turn = self._turn
        if turn is not None and turn.todo_items:
            self._delegate_plan_final = turn.todo_items
        block = DelegateSummaryBlock(
            id=self._delegate_summary_id or self._ids.next_id(),
            entries=tuple(
                DelegateEntry(
                    agent=row.agent,
                    state=row.state,  # type: ignore[arg-type]
                    elapsed_s=row.elapsed_s,
                    snippet=row.snippet,
                )
                for key in self._delegate_order
                for row in (self._delegate_rows[key],)
            ),
            plan_final=self._delegate_plan_final,
            duration_s=self._fanout_duration_s,
        )
        if self._delegate_summary_id is None:
            self._delegate_summary_id = block.id
            self._append_content(block)
        else:
            self._host.replace_block(block)


__all__ = [
    "REPLAY_SKIPPED_KINDS",
    "LaneSeed",
    "ReducerHost",
    "TranscriptReducer",
    "TurnSpecLike",
]
