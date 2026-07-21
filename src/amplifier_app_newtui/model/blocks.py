"""The transcript block grammar: every visible transcript element as data.

This is the single vocabulary the transcript renderer understands
(DESIGN-SPEC §3). Blocks are frozen pydantic models — rendering is a pure
function of ``(blocks, width, theme)``. Colors are referenced ONLY by
theme-token *name* (``style_token`` fields naming DESIGN-SPEC §1 tokens);
hex values never appear in block state, so a runtime theme switch is a
repaint, not a rebuild (ADR-0007 resolution 11).

Stable IDs
==========
Every block carries a monotonic string ``id`` minted by :class:`BlockIdAllocator`
(``"b1"``, ``"b2"``, …). IDs are the contract for in-place mutation
(tool-line expand/collapse, live plan updates), click routing (turn rules →
rewind, answers → evidence) and rewind trimming — never reverse
string-matching on rendered text.

Discriminated union
===================
Each block declares a ``kind`` literal; :data:`TranscriptBlock` is the
pydantic discriminated union over ``kind``, so blocks round-trip through
JSON (events.jsonl replay) losslessly.
"""

from __future__ import annotations

from decimal import Decimal
from itertools import count
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from .evidence import EvidenceLink
from .turn import TurnTelemetry

# Spec glyphs (DESIGN-SPEC §1) — renderers must use these exact characters.
GLYPH_PROMPT = "❯"
GLYPH_BULLET = "●"
GLYPH_SPINNER_FRAMES = ("✳", "✦", "✧", "✦")
GLYPH_PLAN_DONE = "✔"
GLYPH_PLAN_ACTIVE = "■"
GLYPH_PLAN_PENDING = "□"
GLYPH_BLOCKED = "⊘"
GLYPH_LANE_RUNNING = "◐"
GLYPH_TREE_BRANCH = "├─"
GLYPH_TREE_END = "└"
GLYPH_STEER = "↳"
GLYPH_YIELD = "▲"
GLYPH_QUEUED = "▹"
GLYPH_REWIND_LEFT = "‹"
GLYPH_REWIND_RIGHT = "›"
GLYPH_ERROR = "✖"
GLYPH_CHEVRON_COLLAPSED = "▸"
GLYPH_CHEVRON_EXPANDED = "▾"

# Theme-token names a Segment may reference (DESIGN-SPEC §1 table rows).
StyleToken = Literal[
    "bg-page",
    "bg-term",
    "bg-chrome",
    "bg-tab",
    "fg",
    "bright",
    "dim",
    "dimmer",
    "green",
    "orange",
    "red",
    "blue",
    "teal",
    "rule",
]


class _FrozenModel(BaseModel):
    """Base for all block models: frozen, no unknown fields."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class Segment(_FrozenModel):
    """One styled run of text inside a rich block (e.g. an Answer).

    ``style_token``/``bg_token`` name DESIGN-SPEC §1 tokens; the renderer
    maps token name -> Textual theme variable at paint time. Inline code in
    answers is a Segment with ``style_token="teal"``.
    """

    text: str
    style_token: StyleToken = "fg"
    bold: bool = False
    italic: bool = False
    bg_token: StyleToken | None = None


class BlockIdAllocator:
    """Mints monotonic string block IDs (``b1``, ``b2``, …).

    One allocator per session transcript. Monotonicity gives stable
    ordering keys for rewind trimming; string form keeps them JSON-safe.
    """

    def __init__(self, start: int = 1) -> None:
        self._counter = count(start)

    def next_id(self) -> str:
        return f"b{next(self._counter)}"


class SessionBanner(_FrozenModel):
    """Session start banner (DESIGN-SPEC §11).

    Line 1 (bright bold): ``Amplifier <version> · core <core-version>``;
    line 2 (dim): ``Bundle: <bundle> | Provider: <provider> | <model> ·
    session <id6>``. For a focused subagent, ``focus_note`` carries the
    ``focused: <name> · subagent of …`` banner text instead.
    """

    id: str
    kind: Literal["session_banner"] = "session_banner"
    headline: str
    detail: str = ""
    focus_note: str = ""


class UserLine(_FrozenModel):
    """User prompt echo: ``❯ [mode] text`` (DESIGN-SPEC §3).

    The mode badge stamps scrollback permanently — ``mode`` is the mode id
    at submit time (``chat``/``plan``/``brainstorm``/``build``/``auto``,
    or ``delegated`` inside a focused subagent transcript).
    """

    id: str
    kind: Literal["user_line"] = "user_line"
    text: str
    mode: str = "chat"


class Narration(_FrozenModel):
    """Agent narration line: bright ``● `` bullet + fg text."""

    id: str
    kind: Literal["narration"] = "narration"
    text: str


ToolLineStatus = Literal["running", "completed", "failed", "blocked"]
ToolLineBodyStyle = Literal["plain", "diff"]


class ToolLine(_FrozenModel):
    """Collapsed/expandable tool activity line (DESIGN-SPEC §3).

    Collapsed: ``  ● <summary>`` dim + ``· click to expand`` dimmer.
    ``expanded=True`` shows the indented dimmer ``body`` lines below.
    One ToolLine may summarize a whole batch (``Ran 2 shell commands``);
    ``tool_call_ids`` keeps the correlation keys for evidence links.
    """

    id: str
    kind: Literal["tool_line"] = "tool_line"
    summary: str
    body: tuple[str, ...] = ()
    expanded: bool = False
    status: ToolLineStatus = "running"
    tool_call_ids: tuple[str, ...] = ()
    body_style: ToolLineBodyStyle = "plain"
    """``diff`` gives expanded +/-/@@ lines theme-aware patch styling."""


class LiveCommand(_FrozenModel):
    """Live executing command: ``  └ `` dimmer + ``$ <cmd>`` dim.

    Rendered only while executing; replaced by the collapsed ToolLine when
    the command completes (same transcript slot, new block id not needed —
    the ToolLine's id takes over).
    """

    id: str
    kind: Literal["live_command"] = "live_command"
    command: str


PlanItemState = Literal["pending", "active", "done"]


class PlanItem(_FrozenModel):
    """One plan checklist row: ``□`` pending / ``■`` active / ``✔`` done."""

    text: str
    state: PlanItemState = "pending"


class PlanBlock(_FrozenModel):
    """Plan checklist: ``· `` orange header + trailing live dim telemetry.

    ``read_only=True`` marks a plan produced in plan mode — the header is
    suffixed ``(read-only)`` and the recap offers the build handoff
    (DESIGN-SPEC §4).
    """

    id: str
    kind: Literal["plan"] = "plan"
    title: str
    telemetry: TurnTelemetry | None = None
    items: tuple[PlanItem, ...] = ()
    read_only: bool = False


TodoStatus = Literal["pending", "in_progress", "completed"]


class TodoItem(_FrozenModel):
    """One row of the ``todo`` tool's list, rendered by the ambient plan
    panel (``ui/plan_panel.py``): ``○`` pending / ``▶`` in-progress /
    ``✔`` completed."""

    content: str
    status: TodoStatus = "pending"


DelegateState = Literal["running", "done", "error", "cancelled"]


class DelegateEntry(_FrozenModel):
    """One agent row inside a :class:`DelegateSummaryBlock`.

    ``state`` maps to a glyph: ``✔`` done / ``✖`` error / ``⊘`` cancelled /
    ``◐`` running. ``snippet`` is the agent's short result summary
    (``AgentCompleted.result``), truncated by the renderer to fit the width.
    """

    agent: str
    state: DelegateState = "running"
    elapsed_s: float = 0.0
    snippet: str = ""


class DelegateSummaryBlock(_FrozenModel):
    """One durable, expandable summary per delegate fan-out (ambient-progress D5).

    Replaces the per-agent tree-line Answer rows. Lives in the transcript as
    a single line while running (``● N delegates running…``) and collapses at
    fan-out end to ``● Used N delegates · Plan X/Y · MmSSs ▸``. ``expanded``
    is UI-toggled (click/Enter) — the reducer always writes it False; see the
    ToolLine-digest precedent for why a mid-flight replace may collapse it.
    ``plan_final`` folds the turn's final todo state into the durable block
    (design D3); ``None`` means "no plan this turn" and the header omits the
    ``Plan X/Y`` segment.
    """

    id: str
    kind: Literal["delegate_summary"] = "delegate_summary"
    entries: tuple[DelegateEntry, ...] = ()
    plan_final: tuple[TodoItem, ...] | None = None
    duration_s: float = 0.0
    expanded: bool = False


class Blocked(_FrozenModel):
    """Deny-and-continue marker: ``  ⊘ blocked · <cmd>`` red + dim tail.

    Never halts the turn by itself (DESIGN-SPEC §3/§7): ``continuation``
    says what the agent does instead (``continuing without <thing>``).
    """

    id: str
    kind: Literal["blocked"] = "blocked"
    cmd: str
    reason: str
    continuation: str = ""


class ActivityBranch(_FrozenModel):
    """One row of the live activity tree beneath the working pulse.

    ``running=True`` is the in-flight op (brighter, ``●``); completed ops
    are dim. The reducer keeps a small bounded ring of the most recent
    branches so the supervisor feels the action without the transcript
    accumulating a durable line per tool (DESIGN-SPEC §3)."""

    text: str
    running: bool = False


class WorkingStatus(_FrozenModel):
    """Pulsing working line shown while a turn runs (DESIGN-SPEC §3).

    ``✳/✦/✧`` orange spinner + ``working · Ns · ↓ X.Xk tok · `` dim +
    ``esc to interrupt · type to steer`` dimmer, with a bounded live
    activity tree of recent ops rendered as ``└``/``├`` branches beneath.
    A fan-out turn (``agent_count > 1``) renders ``Coordinating N agents ·
    Ns · ↓ X.Xk tok · `` dim + ``esc to interrupt`` dimmer instead (mockup
    runAgentsTurn). Updated every second via the live tail; removed at
    turn end (never persisted to history)."""

    id: str
    kind: Literal["working_status"] = "working_status"
    telemetry: TurnTelemetry
    agent_count: int = 0
    activity: str = ""
    """Legacy single-op note (kept for compatibility); the live tree in
    ``activity_lines`` is the primary activity surface now."""
    activity_lines: tuple[ActivityBranch, ...] = ()
    """Bounded live tree of recent ops (newest last) — single-agent turns."""
    interrupt_hint: str = "esc to interrupt"
    steer_hint: str = "type to steer"
    spinner_frame: int = 0
    motion_frame: int = 0
    """Fast, presentation-only phase for the subtle label shimmer."""


class Recap(_FrozenModel):
    """Turn-end recap: ``✳ `` dimmer + italic dim ``Goal: …. Next: ….``"""

    id: str
    kind: Literal["recap"] = "recap"
    goal: str
    next: str


class Answer(_FrozenModel):
    """Final answer text: styled spans with teal inline code.

    ``spans`` carry selective bright/bold and teal code runs; a click on
    the answer opens the evidence block for ``evidence_refs``
    (DESIGN-SPEC §10).

    ``clickable`` is False for answer-shaped lines the mockup creates
    with ``click: null`` (agent tree lines, non-Goal/Next ✳ recap
    lines) — only true final answers are evidence click targets.
    """

    id: str
    kind: Literal["answer"] = "answer"
    spans: tuple[Segment, ...]
    evidence_refs: tuple[EvidenceLink, ...] = ()
    clickable: bool = True
    compact: bool = False
    """Suppress paragraph spacing for structural rows such as agent trees."""


class SteerEcho(_FrozenModel):
    """Steer acknowledgement: ``  ↳ steer queued: "<text>"`` teal +
    ``· applies at next step boundary`` dimmer."""

    id: str
    kind: Literal["steer_echo"] = "steer_echo"
    text: str
    note: str = "applies at next step boundary"


class TurnRule(_FrozenModel):
    """Turn separator rule + right-aligned telemetry label (DESIGN-SPEC §3).

    Label: ``<Ns> · <X.Xk> tok, <N>% cached · $<cost> · <outcome>`` — dim
    when ``shipped``, dimmer otherwise. Carries the checkpoint id stamped
    at emit time so a click opens the rewind picker at this exact
    checkpoint (never reverse string matching).
    """

    id: str
    kind: Literal["turn_rule"] = "turn_rule"
    checkpoint_id: str
    label: str
    shipped: bool = False


class EvidenceBlock(_FrozenModel):
    """Evidence panel printed on answer click (DESIGN-SPEC §10).

    Header ``· Evidence  1/N · ←/→ select · enter expand · esc close`` +
    numbered teal claims ``¹ "quote" → <tool call>``. ``selected`` is the
    0-based highlighted claim index.
    """

    id: str
    kind: Literal["evidence"] = "evidence"
    links: tuple[EvidenceLink, ...]
    selected: int = 0


class LedgerBlock(_FrozenModel):
    """Session ledger scrollback print (DESIGN-SPEC §10).

    ``· Session ledger  <session> · <bundle>`` +
    ``  N turns · $X.XX · N shipped · N answer-only · cache hit NN%``.
    """

    id: str
    kind: Literal["ledger"] = "ledger"
    session: str
    bundle: str
    turns: int
    spend: Decimal
    shipped: int
    answer_only: int
    cache_hit_pct: int


class ContextBlock(_FrozenModel):
    """``/context`` usage print: ``· Context  NN% of 200k`` + usage bar.

    ``segments`` are (label, cells) pairs for the ``████████░░`` bar in
    order conversation/tools/memory/free; cells sum to ``bar_width``.
    """

    id: str
    kind: Literal["context"] = "context"
    used_pct: int
    window_label: str = "200k"
    segments: tuple[tuple[str, int], ...] = ()
    bar_width: int = 10


class NeedsYouChoice(_FrozenModel):
    """One actionable chip on a needs-you decision, e.g. ``yes · push to fork``."""

    label: str
    answer: str


class NeedsYouEntry(_FrozenModel):
    """One numbered deferred decision rendered inside a NeedsYouBlock.

    (Named ``Entry`` to avoid colliding with the queue-side
    :class:`amplifier_app_newtui.model.queues.NeedsYouItem`.)
    """

    decision_id: str
    question: str
    reason: str = ""
    choices: tuple[NeedsYouChoice, ...] = ()
    highlight: str = ""
    """Substring of ``question`` rendered teal (mockup: ``mj/waypoint``)."""


class NeedsYouBlock(_FrozenModel):
    """``Needs you  N deferred decision`` orange block (DESIGN-SPEC §7).

    Lists numbered decisions with inline actionable choice chips; acting
    on one logs ``Applying decision: …`` narration and clears the footer
    badge.
    """

    id: str
    kind: Literal["needs_you"] = "needs_you"
    items: tuple[NeedsYouEntry, ...]


class DoctorFinding(_FrozenModel):
    """One numbered orange finding from ``/doctor``."""

    number: int
    text: str


class DoctorBlock(_FrozenModel):
    """``/doctor`` checkup: ``· Doctor  <headline>`` header + ``✔`` green
    healthy lines + numbered findings (orange number, dim text)."""

    id: str
    kind: Literal["doctor"] = "doctor"
    headline: str = ""
    healthy: tuple[str, ...] = ()
    findings: tuple[DoctorFinding, ...] = ()


class ImproveProposal(_FrozenModel):
    """One ``/improve`` proposal derived from the ledger + denial log.

    ``action`` (when set) is the concrete command named once in green
    after the dim ``title`` prefix (mockup: ``allowlist: `` +
    ``uv run pytest`` green + rationale); rows without an action render
    as one dim run ``<title> <rationale>``.
    """

    title: str
    rationale: str
    action: str = ""


class ImproveBlock(_FrozenModel):
    """``/improve`` proposals block — proposals only, never applied silently."""

    id: str
    kind: Literal["improve"] = "improve"
    proposals: tuple[ImproveProposal, ...] = ()


class BrainstormIdea(_FrozenModel):
    """One divergent idea line emitted in brainstorm mode."""

    id: str
    kind: Literal["brainstorm_idea"] = "brainstorm_idea"
    text: str
    number: int = 0


TranscriptBlock = Annotated[
    SessionBanner
    | UserLine
    | Narration
    | ToolLine
    | LiveCommand
    | PlanBlock
    | Blocked
    | WorkingStatus
    | Recap
    | Answer
    | SteerEcho
    | TurnRule
    | EvidenceBlock
    | LedgerBlock
    | ContextBlock
    | NeedsYouBlock
    | DoctorBlock
    | ImproveBlock
    | BrainstormIdea
    | DelegateSummaryBlock,
    Field(discriminator="kind"),
]
"""Discriminated union of every transcript block (discriminates on ``kind``)."""


__all__ = [
    "ActivityBranch",
    "Answer",
    "Blocked",
    "BlockIdAllocator",
    "BrainstormIdea",
    "ContextBlock",
    "DelegateEntry",
    "DelegateState",
    "DelegateSummaryBlock",
    "DoctorBlock",
    "DoctorFinding",
    "EvidenceBlock",
    "GLYPH_BLOCKED",
    "GLYPH_BULLET",
    "GLYPH_CHEVRON_COLLAPSED",
    "GLYPH_CHEVRON_EXPANDED",
    "GLYPH_ERROR",
    "GLYPH_LANE_RUNNING",
    "GLYPH_PLAN_ACTIVE",
    "GLYPH_PLAN_DONE",
    "GLYPH_PLAN_PENDING",
    "GLYPH_PROMPT",
    "GLYPH_QUEUED",
    "GLYPH_REWIND_LEFT",
    "GLYPH_REWIND_RIGHT",
    "GLYPH_SPINNER_FRAMES",
    "GLYPH_STEER",
    "GLYPH_TREE_BRANCH",
    "GLYPH_TREE_END",
    "GLYPH_YIELD",
    "ImproveBlock",
    "ImproveProposal",
    "LedgerBlock",
    "LiveCommand",
    "Narration",
    "NeedsYouBlock",
    "NeedsYouChoice",
    "NeedsYouEntry",
    "PlanBlock",
    "PlanItem",
    "TodoItem",
    "PlanItemState",
    "Recap",
    "Segment",
    "SessionBanner",
    "SteerEcho",
    "StyleToken",
    "ToolLine",
    "ToolLineStatus",
    "TranscriptBlock",
    "TurnRule",
    "UserLine",
    "WorkingStatus",
]
