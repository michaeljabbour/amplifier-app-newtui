"""DemoRuntime: the mockup's five demo turns scripted as normalized UIEvents.

ADR-0007 §Runtimes: ``DemoRuntime`` (``--demo``) replays the exact
choreography of the ``Component`` script in
``docs/design-v3-cohesive.html`` — seed transcript, build turn
(``runTurn(false)``), auto turn (``runTurn(true)``), plan turn,
brainstorm turn, and the multi-agent turn — as a producer of typed
:mod:`amplifier_app_newtui.kernel.events` UIEvents pushed into the same
``asyncio.Queue`` contract the real runtime uses. The UI cannot tell the
difference.

Determinism
-----------
- **Timing** is virtual: the runtime advances an internal millisecond
  clock and calls an injectable ``sleep`` fn for real-time pacing. Tests
  inject a no-op sleep and the whole script plays instantly while every
  event still carries the exact virtual ``ts`` it would have in real
  time.
- **Token ticks** follow the mockup formulas — ``380 +
  floor(random()*260)`` per second for the store-refactor turns, flat
  ``900``/s for the agents turn — drawn from a per-turn seeded
  ``random.Random`` so the sequence is identical on every run
  (:func:`tick_tokens`).
- **Cost** follows the mockup: ``0.04 + secs * 0.01`` for the store
  turns; fixed $0.06 / $0.03 / $0.52 for plan / brainstorm / agents.
- Event ids (``demo-N``) and ``ts`` are stamped explicitly — no wall
  clock, no global counters.

Event mapping (demo conventions the UI layer keys on)
-----------------------------------------------------
- Assistant text (narration / final answer / recap / brainstorm idea) is
  a full Channel-A stream (``StreamBlockStart`` → ``StreamBlockDelta`` →
  ``StreamBlockEnd``) plus the durable ``ContentBlockEnd``. The demo
  role travels in ``StreamBlockStart.name`` and
  ``ContentBlockEnd.block["demo_role"]`` (``narration`` / ``answer`` /
  ``recap`` / ``idea``).
- Plan checklists are ``update_plan`` tool calls: ``tool_input =
  {"title", "read_only", "steps": [{"step", "status"}]}`` with statuses
  ``pending`` / ``active`` / ``done``.
- Shell commands are ``bash`` tool calls (``tool_input["command"]``);
  the live ``└ $ cmd`` line spans ToolPre→ToolPost.
- A governance block is ``ToolPre`` → ``ToolPost(result={"status":
  "denied", "reason": ...})`` + ``ApprovalDenied`` — deny-and-continue,
  never halting the turn. The auto-mode deferred decision additionally
  emits ``Notification(level="decision", source="needs_you")``.
- The chat-mode pytest approval emits ``ApprovalRequired`` with the
  verbatim ``Allow once`` / ``Allow always`` / ``Deny`` options and
  awaits the injectable ``approver`` (default: auto-``Allow once``).
- Mode switches emit ``Notification(source="mode")`` with the exact
  ``mode <id> · <trust>`` notice text.

Rule labels, checkpoint labels, lane-focus transcripts, evidence claims
and the deferred-decision block are exported as data (:data:`DEMO_TURNS`,
:data:`DEMO_LANES`, :data:`DEMO_EVIDENCE`, :data:`DEMO_DEFERRED_DECISION`)
for the UI to render verbatim.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable, Sequence
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .events import (
    AgentCompleted,
    AgentSpawned,
    ApprovalDenied,
    ApprovalGranted,
    ApprovalRequired,
    ContentBlockEnd,
    ExecutionEnd,
    ExecutionStart,
    Notification,
    OrchestratorComplete,
    PromptComplete,
    PromptSubmit,
    ProviderResponseUsage,
    SessionEnd,
    SessionStart,
    StreamBlockDelta,
    StreamBlockEnd,
    StreamBlockStart,
    ToolPost,
    ToolPre,
    UIEvent,
)

# --------------------------------------------------------------------------
# Session identity (mockup verbatim)
# --------------------------------------------------------------------------

DEMO_SEED = "amplifier-demo"
DEMO_SESSION_ID = "e07de0"
DEMO_SESSION_SHORT = "e07d"
DEMO_BUNDLE = "anchors"
DEMO_PROVIDER = "OpenAI"
DEMO_MODEL = "gpt-5.5"
DEMO_BANNER: tuple[str, str] = (
    "Amplifier 2026.07.13-87b93ef* · core 1.6.0",
    "Bundle: anchors | Provider: OpenAI | gpt-5.5 · session e07de0",
)
DEMO_SESSION_COST_START = Decimal("0.57")
"""Session spend at mount time (mockup ``this.cost = 0.57``); the seed
turn's $0.17 is already baked into it."""

APPROVAL_OPTIONS: tuple[str, str, str] = ("Allow once", "Allow always", "Deny")

TurnKey = Literal["seed", "build", "auto", "plan", "brainstorm", "agents"]
DemoRole = Literal["narration", "answer", "recap", "idea"]

SleepFn = Callable[[float], Awaitable[None]]
ApproverFn = Callable[[str, tuple[str, ...]], Awaitable[str]]
SteerSourceFn = Callable[[], str | None]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# --------------------------------------------------------------------------
# Token / cost / label formulas (mockup verbatim)
# --------------------------------------------------------------------------

_TICK_COUNTS: dict[str, int] = {"build": 9, "auto": 9, "agents": 6}


def tick_tokens(key: str, count: int | None = None) -> tuple[int, ...]:
    """Per-second output-token deltas for a ticking turn.

    Mockup formulas: ``380 + floor(random() * 260)`` for the store
    turns, flat ``900`` for the agents turn. Deterministic via a
    per-turn seeded ``random.Random(f"{DEMO_SEED}:{key}")``.
    """
    n = _TICK_COUNTS[key] if count is None else count
    if key == "agents":
        return (900,) * n
    rng = random.Random(f"{DEMO_SEED}:{key}")
    return tuple(380 + rng.randrange(260) for _ in range(n))


def store_turn_cost(secs: int) -> Decimal:
    """Mockup: ``turnCost = 0.04 + secs * 0.01``."""
    return Decimal("0.04") + Decimal(secs) * Decimal("0.01")


def format_k_tokens(tokens: int) -> str:
    """Mockup ``(toks / 1000).toFixed(1) + "k"``."""
    return f"{tokens / 1000:.1f}k"


def rule_label(
    secs_text: str,
    tokens: int,
    cached_pct: int | None,
    cost: Decimal,
    outcome: str,
) -> str:
    """Turn-rule label: ``<Ns> · <X.Xk> tok[, NN% cached] · $<cost> · <outcome>``."""
    token_part = f"{format_k_tokens(tokens)} tok"
    if cached_pct is not None:
        token_part += f", {cached_pct}% cached"
    return f"{secs_text} · {token_part} · ${cost:.2f} · {outcome}"


# --------------------------------------------------------------------------
# Script data (mockup verbatim strings)
# --------------------------------------------------------------------------

SEED_PROMPT = "explain what this repo is in simple terms"
SEED_NARRATION = "Reading the repo layout and entry points to ground the summary."
SEED_COMMANDS: tuple[str, str] = ("ls -la", "cat pyproject.toml | head -40")
SEED_TOOL_BODY = "$ ls -la && cat pyproject.toml | head -40"
SEED_ANSWER = (
    "This repo is the command-line app for Amplifier. If amplifier-core is the "
    "engine, this is the dashboard and steering wheel: the amplifier command "
    "starts sessions, configures providers, loads bundles, and renders this UI."
)

STORE_PLAN_TITLE = "Refactor session store"
STORE_STEPS: tuple[str, str, str] = (
    "Audit persistence paths",
    "Migrate history to durable store",
    "Verify and push",
)
STORE_NARRATIONS: tuple[str, str, str] = (
    "Mapping every read and write against the current session store.",
    "History paths found in three modules. Moving them behind one durable interface.",
    "Tests green. Preparing the push.",
)
STORE_COMMANDS: tuple[str, str, str] = (
    'grep -rn "session_store" amplifier/ | head -12',
    "uv run pytest tests/store/ -q",
    "git push origin mj/durable-store",
)

BUILD_PROMPT = "refactor the session store so history is durable offline and online"
PYTEST_APPROVAL_PROMPT = "Run uv run pytest tests/store/ -q?"
BUILD_RECAP = "Goal: durable session store. Next: open PR against main."
BUILD_END_NOTICE = "agents 1 done"
DENY_REASON = "denied by user"
DENY_CONTINUATION = "continuing without test run"
DENY_BLOCKED_CMD = "uv run pytest"
"""Mockup deny line: ``⊘ blocked · uv run pytest · denied by user ·
continuing without test run``."""

AUTO_PROMPT = "refactor the session store and push it up"
AUTO_MODE_NOTICE = "mode auto · auto read,write · classifier-gated"
FORCE_PUSH_COMMAND = "git push --force origin main"
AUTO_BLOCK_REASON = "outside user authorization"
AUTO_BLOCK_CONTINUATION = "finding safer path"
AUTO_DEFER_NARRATION = (
    "Force-push denied. Branch push also crosses the trust boundary; deferring "
    "the decision and finishing local verification."
)
AUTO_DEFER_NOTICE = "decision deferred to queue · run continues"
AUTO_ANSWER = (
    "Store refactor complete and verified locally: history behind one durable "
    "interface, tests green. The push crossed the trust boundary, so it is "
    "waiting in your decision queue."
)
AUTO_RECAP = "Goal: durable session store. Next: answer the deferred push decision (ctrl-y)."

PLAN_PROMPT = "how should we make session history durable?"
PLAN_MODE_NOTICE = "mode plan · read-only"
PLAN_NARRATION = "Reading the store modules — plan mode, no writes."
PLAN_TITLE = "Proposed plan · durable session history"
PLAN_STEPS: tuple[str, str, str] = (
    "Extract a SessionStore interface from the three call sites",
    "Back it with sqlite + journal replay",
    "Migrate history lazily on first read",
)
PLAN_RECAP = "Plan ready. shift+tab to build hands it over for execution."
PLAN_END_NOTICE = "plan mode: read-only · plan handed to build on mode switch"

BRAINSTORM_PROMPT = "how might we make long agent runs feel supervised?"
BRAINSTORM_MODE_NOTICE = "mode brainstorm · no tools"
BRAINSTORM_NARRATION = "No tools in brainstorm — pure divergence, cheapest turn there is."
BRAINSTORM_IDEAS: tuple[str, str, str, str] = (
    "1 Ambient tab color: orange while running, red when a decision waits",
    '2 A "confidence strip" under the plan: what the agent would bet on each step',
    "3 Turn rules as a film strip — scrub the session like a timeline",
    "4 Steer suggestions: the agent drafts the correction it suspects you want",
)
BRAINSTORM_RECAP = "Converge with /plan when one of these sticks."

AGENTS_PROMPT = "run the DTU reality check across provider docs, store, and tests"
AGENTS_MODE_NOTICE = "mode build · auto read,test · ask write,net,spend"
AGENTS_NARRATION = (
    "Fanning out: researcher, coder, tester. Lanes above the composer track each one."
)
AGENTS_ANSWER = (
    "Reality check passed: provider docs match runtime behavior, store migration "
    "verified, 41 tests green across three parallel agents."
)
AGENTS_END_NOTICE = "agents 3 done · click a lane to inspect its transcript"


def build_answer(denied: bool) -> str:
    """Mockup final-answer assembly for the build turn."""
    middle = " (tests skipped by your denial)" if denied else ", tests pass"
    return (
        "Session store refactor is in: history behind one durable interface"
        f"{middle}, branch pushed. Ready for review."
    )


# --------------------------------------------------------------------------
# Exported structured data: lanes, evidence, deferred decision
# --------------------------------------------------------------------------

LogRowKind = Literal["narration", "tool", "command", "answer"]


class DemoLogRow(_FrozenModel):
    """One row of a subagent's own transcript (mockup ``lane.log``)."""

    kind: LogRowKind
    text: str


class DemoLane(_FrozenModel):
    """One agent lane: panel line, focus transcript, and live-tree labels."""

    name: str
    glyph: str
    color_token: str
    sub_session_id: str
    panel_line: str
    """Lanes-panel row, spacing verbatim from the mockup."""
    brief: str
    """The delegated brief shown as the ``[delegated]`` user line on focus."""
    state_recap: str
    """State recap line at the bottom of the focus transcript."""
    tree_spawn: str
    """Live-tree label while running: ``<name> · <activity> · $<cost>``."""
    tree_done: str
    """Live-tree label when complete: ``<name> · done · <result> · <t> · $<cost>``."""
    done_at_ms: int
    """Virtual ms into the agents turn when this lane completes."""
    log: tuple[DemoLogRow, ...]


def _sub_session_id(index: int, name: str) -> str:
    return f"{DEMO_SESSION_ID}-{index:016x}_{name}"


DEMO_LANES: tuple[DemoLane, DemoLane, DemoLane] = (
    DemoLane(
        name="researcher",
        glyph="◐",
        color_token="teal",
        sub_session_id=_sub_session_id(1, "researcher"),
        panel_line="  ◐ researcher · scanning provider docs · 41s · $0.09",
        brief="Scan the provider docs and list every capability the runtime does not exercise.",
        state_recap="running · 41s · $0.09",
        tree_spawn="researcher · scanning provider docs · $0.09",
        tree_done="researcher · done · 3 findings · 4s · $0.11",
        done_at_ms=4400,
        log=(
            DemoLogRow(
                kind="narration",
                text="Fetching the provider capability matrix and diffing it against runtime calls.",
            ),
            DemoLogRow(kind="tool", text="Ran 3 web_fetch calls"),
            DemoLogRow(kind="command", text='grep -rn "capabilities" providers/ | head -20'),
            DemoLogRow(
                kind="narration",
                text="Two undocumented streaming flags found; verifying against the SDK.",
            ),
        ),
    ),
    DemoLane(
        name="coder",
        glyph="■",
        color_token="fg",
        sub_session_id=_sub_session_id(2, "coder"),
        panel_line="  ■ coder      · migrating store        · 2m  · $0.31",
        brief="Move session history behind the durable SessionStore interface.",
        state_recap="running · 2m 04s · $0.31",
        tree_spawn="coder · migrating store · $0.31",
        tree_done="coder · done · 2 files · 6s · $0.34",
        done_at_ms=6000,
        log=(
            DemoLogRow(
                kind="narration",
                text="Extracting the SessionStore interface from three call sites.",
            ),
            DemoLogRow(
                kind="command",
                text="uv run python -m amplifier_app_cli.session_store --check",
            ),
            DemoLogRow(kind="tool", text="Ran 4 edit calls · 2 files"),
            DemoLogRow(kind="narration", text="Wiring journal replay into resume; tests next."),
        ),
    ),
    DemoLane(
        name="tester",
        glyph="✔",
        color_token="dim",
        sub_session_id=_sub_session_id(3, "tester"),
        panel_line="  ✔ tester     · done · tests ✔         · 55s · $0.07",
        brief="Run the store test suite and report failures with evidence.",
        state_recap="completed · 55s · $0.07 · tests ✔",
        tree_spawn="tester · uv run pytest tests/ -q · $0.07",
        tree_done="tester · done · tests ✔ · 2s · $0.07",
        done_at_ms=2600,
        log=(
            DemoLogRow(kind="command", text="uv run pytest tests/store/ -q"),
            DemoLogRow(kind="tool", text="Ran 1 shell command · 41 passed"),
            DemoLogRow(
                kind="answer",
                text=(
                    "All 41 store tests pass. Slowest: test_journal_replay (1.2s). "
                    "No flakes across 3 runs."
                ),
            ),
        ),
    ),
)

DEMO_LANE_BY_NAME: dict[str, DemoLane] = {lane.name: lane for lane in DEMO_LANES}


class DemoEvidenceClaim(_FrozenModel):
    """One numbered evidence claim: ``"quote" → grounding tool call``."""

    quote: str
    source: str


DEMO_EVIDENCE: tuple[DemoEvidenceClaim, DemoEvidenceClaim] = (
    DemoEvidenceClaim(
        quote="dashboard and steering wheel",
        source="Ran 2 shell commands (pyproject entry points)",
    ),
    DemoEvidenceClaim(quote="loads bundles", source="grep amplifier_core bundle loader"),
)


class DemoDeferredDecision(_FrozenModel):
    """The auto-turn deferred push decision (needs-you queue item)."""

    text: str
    chip_label: str
    applied_narration: str


DEMO_DEFERRED_DECISION = DemoDeferredDecision(
    text=(
        "Push branch to origin was blocked (outside trust boundary). "
        "Push to fork mj/waypoint instead?"
    ),
    chip_label="yes · push to fork",
    applied_narration=(
        "Applying decision: pushing to fork mj/waypoint. "
        "Trust-slot suggestion queued for /improve."
    ),
)


# --------------------------------------------------------------------------
# Per-turn specs (telemetry, outcome, labels — mockup verbatim)
# --------------------------------------------------------------------------


class DemoTurnSpec(_FrozenModel):
    """Everything the UI needs to close out one scripted demo turn."""

    key: TurnKey
    mode: Literal["chat", "plan", "brainstorm", "build", "auto"]
    mode_notice: str | None = None
    prompt: str
    duration_ms: int
    secs_text: str
    tokens: int = Field(ge=0)
    cached_pct: int | None = None
    cost: Decimal
    cost_after: Decimal
    """Cumulative session spend after this turn (mockup ``this.cost``)."""
    outcome: str
    shipped: bool
    rule_label: str
    checkpoint_id: str
    checkpoint_label: str
    answer: str | None = None
    recap: str | None = None
    end_notice: str | None = None


def _build_turn_specs() -> tuple[DemoTurnSpec, ...]:
    build_tokens = sum(tick_tokens("build"))
    auto_tokens = sum(tick_tokens("auto"))
    agents_tokens = sum(tick_tokens("agents"))
    store_cost = store_turn_cost(9)  # both store turns run 9 virtual seconds
    shipped_outcome = "3 files · +142/−38 · tests ✔"
    cost = DEMO_SESSION_COST_START
    specs: list[DemoTurnSpec] = [
        DemoTurnSpec(
            key="seed",
            mode="chat",
            prompt=SEED_PROMPT,
            duration_ms=0,
            secs_text="6.1s",
            tokens=83_900,
            cached_pct=91,
            cost=Decimal("0.17"),
            cost_after=cost,
            outcome="answer",
            shipped=False,
            rule_label=rule_label("6.1s", 83_900, 91, Decimal("0.17"), "answer"),
            checkpoint_id="t1",
            checkpoint_label="repo explainer · answer",
            answer=SEED_ANSWER,
        )
    ]
    cost += store_cost
    specs.append(
        DemoTurnSpec(
            key="build",
            mode="chat",
            prompt=BUILD_PROMPT,
            duration_ms=9_300,
            secs_text="9s",
            tokens=build_tokens,
            cached_pct=88,
            cost=store_cost,
            cost_after=cost,
            outcome=shipped_outcome,
            shipped=True,
            rule_label=rule_label("9s", build_tokens, 88, store_cost, shipped_outcome),
            checkpoint_id="t2",
            checkpoint_label="store refactor · shipped",
            answer=build_answer(denied=False),
            recap=BUILD_RECAP,
            end_notice=BUILD_END_NOTICE,
        )
    )
    cost += store_cost
    specs.append(
        DemoTurnSpec(
            key="auto",
            mode="auto",
            mode_notice=AUTO_MODE_NOTICE,
            prompt=AUTO_PROMPT,
            duration_ms=9_700,
            secs_text="9s",
            tokens=auto_tokens,
            cached_pct=88,
            cost=store_cost,
            cost_after=cost,
            outcome=shipped_outcome,
            shipped=True,
            rule_label=rule_label("9s", auto_tokens, 88, store_cost, shipped_outcome),
            checkpoint_id="t3",
            checkpoint_label="store refactor · shipped",
            answer=AUTO_ANSWER,
            recap=AUTO_RECAP,
        )
    )
    cost += Decimal("0.06")
    specs.append(
        DemoTurnSpec(
            key="plan",
            mode="plan",
            mode_notice=PLAN_MODE_NOTICE,
            prompt=PLAN_PROMPT,
            duration_ms=3_600,
            secs_text="11s",
            tokens=9_400,
            cached_pct=93,
            cost=Decimal("0.06"),
            cost_after=cost,
            outcome="answer · plan ready",
            shipped=False,
            rule_label=rule_label(
                "11s", 9_400, 93, Decimal("0.06"), "answer · plan ready"
            ),
            checkpoint_id="t4",
            checkpoint_label="durable-history plan · answer",
            recap=PLAN_RECAP,
            end_notice=PLAN_END_NOTICE,
        )
    )
    cost += Decimal("0.03")
    specs.append(
        DemoTurnSpec(
            key="brainstorm",
            mode="brainstorm",
            mode_notice=BRAINSTORM_MODE_NOTICE,
            prompt=BRAINSTORM_PROMPT,
            duration_ms=3_000,
            secs_text="8s",
            tokens=4_100,
            cached_pct=None,
            cost=Decimal("0.03"),
            cost_after=cost,
            outcome="answer",
            shipped=False,
            rule_label=rule_label("8s", 4_100, None, Decimal("0.03"), "answer"),
            checkpoint_id="t5",
            checkpoint_label="supervision ideas · answer",
            recap=BRAINSTORM_RECAP,
        )
    )
    cost += Decimal("0.52")
    agents_outcome = "2 files · tests ✔ · 3 agents"
    specs.append(
        DemoTurnSpec(
            key="agents",
            mode="build",
            mode_notice=AGENTS_MODE_NOTICE,
            prompt=AGENTS_PROMPT,
            duration_ms=6_000,
            secs_text="6s",
            tokens=agents_tokens,
            cached_pct=None,
            cost=Decimal("0.52"),
            cost_after=cost,
            outcome=agents_outcome,
            shipped=True,
            rule_label=rule_label(
                "6s", agents_tokens, None, Decimal("0.52"), agents_outcome
            ),
            checkpoint_id="t6",
            checkpoint_label="DTU reality check · shipped",
            answer=AGENTS_ANSWER,
            end_notice=AGENTS_END_NOTICE,
        )
    )
    return tuple(specs)


DEMO_TURNS: tuple[DemoTurnSpec, ...] = _build_turn_specs()
DEMO_TURN_BY_KEY: dict[TurnKey, DemoTurnSpec] = {spec.key: spec for spec in DEMO_TURNS}


def build_denied_spec() -> DemoTurnSpec:
    """The build turn's alternate close-out when the pytest approval is denied.

    Mockup: the deny path skips the command (1400ms) and the step's
    trailing 400ms wait — 7 virtual seconds, $0.11, no ``tests ✔``.
    """
    secs = 7
    tokens = sum(tick_tokens("build", secs))
    cost = store_turn_cost(secs)
    outcome = "3 files · +142/−38"
    base = DEMO_TURN_BY_KEY["build"]
    return base.model_copy(
        update={
            "duration_ms": 7_500,
            "secs_text": f"{secs}s",
            "tokens": tokens,
            "cost": cost,
            "cost_after": DEMO_SESSION_COST_START + cost,
            "outcome": outcome,
            "rule_label": rule_label(f"{secs}s", tokens, 88, cost, outcome),
            "answer": build_answer(denied=True),
        }
    )


# --------------------------------------------------------------------------
# The runtime
# --------------------------------------------------------------------------


async def _auto_allow(prompt: str, options: tuple[str, ...]) -> str:
    """Default approver: grants ``Allow once`` immediately."""
    return APPROVAL_OPTIONS[0]


class DemoRuntime:
    """Plays the scripted demo turns as UIEvents on an ``asyncio.Queue``.

    Parameters
    ----------
    queue:
        Destination queue (created if omitted) — the same queue contract
        the real runtime's hook adapter feeds.
    approver:
        ``async (prompt, options) -> choice`` awaited for the chat-mode
        pytest approval. Defaults to auto-``Allow once`` so unattended
        demos run through. Returning ``"Deny"`` plays the mockup's deny
        branch.
    steer_source:
        ``() -> text | None`` polled once at every step boundary of the
        store turns (the mockup's steer check). A returned text is
        applied as the ``Applying steer: <text>`` narration — the caller
        removes it from its queue when handing it over (DESIGN-SPEC §5:
        consumed steer removed).
    sleep:
        ``async (seconds) -> None`` used for pacing. Inject a no-op for
        instant, zero-sleep test runs; virtual time is unaffected.
    start_ts:
        Virtual timestamp of the first event.
    """

    def __init__(
        self,
        *,
        queue: asyncio.Queue[UIEvent] | None = None,
        approver: ApproverFn | None = None,
        sleep: SleepFn | None = None,
        steer_source: SteerSourceFn | None = None,
        start_ts: float = 0.0,
    ) -> None:
        self.queue: asyncio.Queue[UIEvent] = queue if queue is not None else asyncio.Queue()
        self._approver: ApproverFn = approver or _auto_allow
        self._sleep: SleepFn = sleep or asyncio.sleep
        self._steer_source: SteerSourceFn | None = steer_source
        self._clock_ms: int = round(start_ts * 1000)
        self._seq = 0
        self._tool_seq = 0
        self._group_seq = 0
        self._turn_ms = 0
        self._block_index = 0
        self._request_id = ""
        self._ticks: list[int] | None = None

    # -- plumbing ---------------------------------------------------------

    @property
    def clock(self) -> float:
        """Current virtual time in seconds."""
        return self._clock_ms / 1000

    def _env(self) -> dict[str, Any]:
        self._seq += 1
        return {
            "event_id": f"demo-{self._seq}",
            "session_id": DEMO_SESSION_ID,
            "parent_id": None,
            "ts": self.clock,
        }

    async def _emit(self, event: UIEvent) -> None:
        await self.queue.put(event)

    async def _wait(self, ms: int) -> None:
        """Advance virtual time, pacing via the injected sleep and
        emitting one usage tick at every whole-second boundary while a
        tick schedule is active."""
        while ms > 0:
            step = min(ms, 1000 - self._turn_ms % 1000)
            await self._sleep(step / 1000)
            self._turn_ms += step
            self._clock_ms += step
            ms -= step
            if self._ticks and self._turn_ms % 1000 == 0:
                await self._emit(
                    ProviderResponseUsage(
                        **self._env(),
                        output_tokens=self._ticks.pop(0),
                        model=DEMO_MODEL,
                    )
                )

    async def _text(self, text: str, role: DemoRole) -> None:
        """One assistant text block on both channels (A + durable B)."""
        index = self._block_index
        self._block_index += 1
        common = {"request_id": self._request_id, "block_index": index, "block_type": "text"}
        await self._emit(StreamBlockStart(**self._env(), **common, name=role))
        await self._emit(StreamBlockDelta(**self._env(), **common, sequence=0, text=text))
        await self._emit(StreamBlockEnd(**self._env(), **common))
        await self._emit(
            ContentBlockEnd(
                **self._env(),
                block_type="text",
                block_index=index,
                block={"type": "text", "text": text, "demo_role": role},
            )
        )

    async def _tool_pre(
        self, tool_name: str, tool_input: dict[str, Any], *, group: str | None = None
    ) -> str:
        self._tool_seq += 1
        call_id = f"demo-call-{self._tool_seq}"
        await self._emit(
            ToolPre(
                **self._env(),
                tool_name=tool_name,
                tool_call_id=call_id,
                tool_input=tool_input,
                parallel_group_id=group,
            )
        )
        return call_id

    async def _tool_post(
        self,
        call_id: str,
        tool_name: str,
        tool_input: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        await self._emit(
            ToolPost(
                **self._env(),
                tool_name=tool_name,
                tool_call_id=call_id,
                tool_input=tool_input,
                result=result,
            )
        )

    async def _tool(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        result: dict[str, Any],
        *,
        group: str | None = None,
    ) -> None:
        call_id = await self._tool_pre(tool_name, tool_input, group=group)
        await self._tool_post(call_id, tool_name, tool_input, result)

    async def _plan(
        self,
        title: str,
        steps: Sequence[str],
        statuses: Sequence[str],
        *,
        read_only: bool = False,
    ) -> None:
        await self._tool(
            "update_plan",
            {
                "title": title,
                "read_only": read_only,
                "steps": [
                    {"step": step, "status": status}
                    for step, status in zip(steps, statuses, strict=True)
                ],
            },
            {"ok": True},
        )

    async def _apply_steer(self) -> None:
        """Step boundary: consume one queued steer (mockup lines 326-329).

        The narration is the DESIGN-SPEC §3 ``Applying steer: <text>``
        line; the caller's steer queue already dropped the item, which is
        what removes the ↳ echo (consumed steer removed, spec §5).
        """
        if self._steer_source is None:
            return
        text = self._steer_source()
        if text:
            await self._text(f"Applying steer: {text}", "narration")

    async def _begin_turn(self, key: TurnKey) -> DemoTurnSpec:
        spec = DEMO_TURN_BY_KEY[key]
        self._turn_ms = 0
        self._block_index = 0
        self._request_id = f"demo-req-{key}"
        self._ticks = list(tick_tokens(key)) if key in _TICK_COUNTS else None
        if spec.mode_notice:
            await self._emit(
                Notification(**self._env(), message=spec.mode_notice, source="mode")
            )
        await self._emit(PromptSubmit(**self._env(), prompt=spec.prompt))
        await self._emit(ExecutionStart(**self._env()))
        return spec

    async def _end_turn(
        self, spec: DemoTurnSpec, *, response: str = "", notice: str | None = None
    ) -> None:
        self._ticks = None
        await self._emit(
            OrchestratorComplete(
                **self._env(), orchestrator="demo", turn_count=1, status="success"
            )
        )
        await self._emit(ExecutionEnd(**self._env()))
        await self._emit(PromptComplete(**self._env(), response=response))
        if notice:
            await self._emit(Notification(**self._env(), message=notice, source="turn"))

    # -- turns --------------------------------------------------------------

    async def run_all(self) -> None:
        """Session start → seed + five demo turns → session end."""
        await self._emit(SessionStart(**self._env()))
        await self.run_seed()
        await self.run_build_turn()
        await self.run_auto_turn()
        await self.run_plan_turn()
        await self.run_brainstorm_turn()
        await self.run_agents_turn()
        await self._emit(SessionEnd(**self._env()))

    async def run_turn(self, key: TurnKey) -> None:
        """Dispatch a single scripted turn by key."""
        await {
            "seed": self.run_seed,
            "build": self.run_build_turn,
            "auto": self.run_auto_turn,
            "plan": self.run_plan_turn,
            "brainstorm": self.run_brainstorm_turn,
            "agents": self.run_agents_turn,
        }[key]()

    async def run_seed(self) -> None:
        """``seedTranscript()``: the pre-existing repo-explainer turn."""
        spec = await self._begin_turn("seed")
        await self._text(SEED_NARRATION, "narration")
        self._group_seq += 1
        group = f"demo-group-{self._group_seq}"
        call_ids = [
            await self._tool_pre("bash", {"command": command}, group=group)
            for command in SEED_COMMANDS
        ]
        for call_id, command in zip(call_ids, SEED_COMMANDS, strict=True):
            await self._tool_post(
                call_id, "bash", {"command": command}, {"output": "(output collapsed)"}
            )
        await self._text(SEED_ANSWER, "answer")
        await self._emit(
            ProviderResponseUsage(
                **self._env(), output_tokens=spec.tokens, model=DEMO_MODEL
            )
        )
        await self._end_turn(spec, response=SEED_ANSWER)

    async def run_build_turn(self) -> None:
        """``runTurn(false)`` in chat mode — pytest approval on step 2."""
        await self._run_store_turn(auto=False)

    async def run_auto_turn(self) -> None:
        """``runTurn(true)`` — force-push block + deferred decision."""
        await self._run_store_turn(auto=True)

    async def _run_store_turn(self, *, auto: bool) -> None:
        spec = await self._begin_turn("auto" if auto else "build")
        statuses = ["pending"] * len(STORE_STEPS)
        await self._plan(STORE_PLAN_TITLE, STORE_STEPS, statuses)
        denied = False
        for i, (step, narration, command) in enumerate(
            zip(STORE_STEPS, STORE_NARRATIONS, STORE_COMMANDS, strict=True)
        ):
            await self._apply_steer()
            statuses[i] = "active"
            await self._plan(STORE_PLAN_TITLE, STORE_STEPS, statuses)
            await self._text(narration, "narration")
            await self._wait(1300)
            if auto and i == 2:
                tool_input = {"command": FORCE_PUSH_COMMAND}
                call_id = await self._tool_pre("bash", tool_input)
                await self._wait(900)
                await self._tool_post(
                    call_id,
                    "bash",
                    tool_input,
                    {
                        "status": "denied",
                        "reason": AUTO_BLOCK_REASON,
                        "continuation": AUTO_BLOCK_CONTINUATION,
                    },
                )
                await self._emit(
                    ApprovalDenied(
                        **self._env(), prompt=FORCE_PUSH_COMMAND, reason=AUTO_BLOCK_REASON
                    )
                )
                await self._wait(900)
                await self._text(AUTO_DEFER_NARRATION, "narration")
                await self._emit(
                    Notification(
                        **self._env(),
                        message=AUTO_DEFER_NOTICE,
                        level="decision",
                        source="needs_you",
                    )
                )
            else:
                if not auto and i == 1:
                    await self._emit(
                        ApprovalRequired(
                            **self._env(),
                            prompt=PYTEST_APPROVAL_PROMPT,
                            options=APPROVAL_OPTIONS,
                        )
                    )
                    choice = await self._approver(PYTEST_APPROVAL_PROMPT, APPROVAL_OPTIONS)
                    if choice == "Deny":
                        await self._emit(
                            ApprovalDenied(
                                **self._env(),
                                prompt=PYTEST_APPROVAL_PROMPT,
                                reason=DENY_REASON,
                                command=DENY_BLOCKED_CMD,
                                continuation=DENY_CONTINUATION,
                            )
                        )
                        denied = True
                        statuses[i] = "done"
                        await self._plan(STORE_PLAN_TITLE, STORE_STEPS, statuses)
                        continue
                    await self._emit(
                        ApprovalGranted(
                            **self._env(), prompt=PYTEST_APPROVAL_PROMPT, choice=choice
                        )
                    )
                tool_input = {"command": command}
                call_id = await self._tool_pre("bash", tool_input)
                await self._wait(1400)
                await self._tool_post(
                    call_id, "bash", tool_input, {"output": "(output collapsed)"}
                )
            statuses[i] = "done"
            await self._plan(STORE_PLAN_TITLE, STORE_STEPS, statuses)
            await self._wait(400)
        self._ticks = None
        answer = AUTO_ANSWER if auto else build_answer(denied)
        recap = AUTO_RECAP if auto else BUILD_RECAP
        await self._text(answer, "answer")
        await self._text(recap, "recap")
        # Mockup: the auto (blocked) turn ends with no notice at all.
        await self._end_turn(spec, response=answer, notice=None if auto else spec.end_notice)

    async def run_plan_turn(self) -> None:
        """``runPlanTurn()``: read-only proposed plan, steps landing live."""
        spec = await self._begin_turn("plan")
        await self._text(PLAN_NARRATION, "narration")
        await self._wait(1400)
        await self._plan(PLAN_TITLE, (), (), read_only=True)
        for count in range(1, len(PLAN_STEPS) + 1):
            await self._wait(500)
            await self._plan(
                PLAN_TITLE,
                PLAN_STEPS[:count],
                ("pending",) * count,
                read_only=True,
            )
        await self._wait(700)
        await self._text(PLAN_RECAP, "recap")
        await self._emit(
            ProviderResponseUsage(**self._env(), output_tokens=spec.tokens, model=DEMO_MODEL)
        )
        await self._end_turn(spec, notice=spec.end_notice)

    async def run_brainstorm_turn(self) -> None:
        """``runBrainstormTurn()``: no tools, four ideas, recap."""
        spec = await self._begin_turn("brainstorm")
        await self._text(BRAINSTORM_NARRATION, "narration")
        await self._wait(1200)
        for idea in BRAINSTORM_IDEAS:
            await self._text(idea, "idea")
            await self._wait(450)
        await self._text(BRAINSTORM_RECAP, "recap")
        await self._emit(
            ProviderResponseUsage(**self._env(), output_tokens=spec.tokens, model=DEMO_MODEL)
        )
        await self._end_turn(spec)

    async def run_agents_turn(self) -> None:
        """``runAgentsTurn()``: researcher/coder/tester fan-out."""
        spec = await self._begin_turn("agents")
        await self._text(AGENTS_NARRATION, "narration")
        for lane in DEMO_LANES:
            await self._emit(
                AgentSpawned(
                    **self._env(),
                    agent=lane.name,
                    sub_session_id=lane.sub_session_id,
                    parent_session_id=DEMO_SESSION_ID,
                )
            )
        elapsed = 0
        for lane in sorted(DEMO_LANES, key=lambda lane: lane.done_at_ms):
            await self._wait(lane.done_at_ms - elapsed)
            elapsed = lane.done_at_ms
            await self._emit(
                AgentCompleted(
                    **self._env(),
                    agent=lane.name,
                    sub_session_id=lane.sub_session_id,
                    parent_session_id=DEMO_SESSION_ID,
                    success=True,
                )
            )
        self._ticks = None
        await self._text(AGENTS_ANSWER, "answer")
        await self._end_turn(spec, response=AGENTS_ANSWER, notice=spec.end_notice)


__all__ = [
    "APPROVAL_OPTIONS",
    "AGENTS_ANSWER",
    "AGENTS_END_NOTICE",
    "AGENTS_MODE_NOTICE",
    "AGENTS_NARRATION",
    "AGENTS_PROMPT",
    "AUTO_ANSWER",
    "AUTO_BLOCK_CONTINUATION",
    "AUTO_BLOCK_REASON",
    "AUTO_DEFER_NARRATION",
    "AUTO_DEFER_NOTICE",
    "AUTO_MODE_NOTICE",
    "AUTO_PROMPT",
    "AUTO_RECAP",
    "BRAINSTORM_IDEAS",
    "BRAINSTORM_MODE_NOTICE",
    "BRAINSTORM_NARRATION",
    "BRAINSTORM_PROMPT",
    "BRAINSTORM_RECAP",
    "BUILD_END_NOTICE",
    "BUILD_PROMPT",
    "BUILD_RECAP",
    "DEMO_BANNER",
    "DEMO_BUNDLE",
    "DEMO_DEFERRED_DECISION",
    "DEMO_EVIDENCE",
    "DEMO_LANES",
    "DEMO_LANE_BY_NAME",
    "DEMO_MODEL",
    "DEMO_PROVIDER",
    "DEMO_SEED",
    "DEMO_SESSION_COST_START",
    "DEMO_SESSION_ID",
    "DEMO_SESSION_SHORT",
    "DEMO_TURNS",
    "DEMO_TURN_BY_KEY",
    "DENY_BLOCKED_CMD",
    "DENY_CONTINUATION",
    "DENY_REASON",
    "DemoDeferredDecision",
    "DemoEvidenceClaim",
    "DemoLane",
    "DemoLogRow",
    "DemoRuntime",
    "DemoTurnSpec",
    "FORCE_PUSH_COMMAND",
    "PLAN_END_NOTICE",
    "PLAN_MODE_NOTICE",
    "PLAN_NARRATION",
    "PLAN_PROMPT",
    "PLAN_RECAP",
    "PLAN_STEPS",
    "PLAN_TITLE",
    "PYTEST_APPROVAL_PROMPT",
    "SEED_ANSWER",
    "SEED_COMMANDS",
    "SEED_NARRATION",
    "SEED_PROMPT",
    "SEED_TOOL_BODY",
    "STORE_COMMANDS",
    "STORE_NARRATIONS",
    "STORE_PLAN_TITLE",
    "STORE_STEPS",
    "TurnKey",
    "build_answer",
    "build_denied_spec",
    "format_k_tokens",
    "rule_label",
    "store_turn_cost",
    "tick_tokens",
]
