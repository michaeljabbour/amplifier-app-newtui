"""Offline end-to-end test of the ``serve`` protocol loop.

Drives :func:`amplifier_app_tui.kernel.serve.serve_loop` against a REAL
``RealRuntime`` mounted on the fake-module bundle from ``test_runtime_offline``
(real foundation lifecycle, real ``ApprovalBroker`` through the Rust
``process_hook_result`` path) — no API key, no network.

Proves the two things a live smoke would: (1) a full turn streams to stdout as
the schema-v1 protocol and terminates with ``turn.completed``; (2) the
bidirectional approval round-trip — the backend emits ``approval.required`` with
a broker ticket id and parks until an ``approve`` submission answers it.
"""

from __future__ import annotations

import asyncio
import json
import queue
import threading
from decimal import Decimal
from pathlib import Path
from typing import IO, Any, cast

import pytest

from amplifier_app_tui.kernel import serve as serve_module
from amplifier_app_tui.kernel.approval import ALLOW_ONCE, DENY
from amplifier_app_tui.kernel.compaction import CompactionConfig
from amplifier_app_tui.kernel.cost import CostTracker, PricingTable
from amplifier_app_tui.kernel.events import (
    ContentBlockEnd,
    ContextCompacted,
    ProviderResponseUsage,
)
from amplifier_app_tui.kernel.serve import serve, serve_loop
from amplifier_app_tui.kernel.steering import StepBoundaryBridge
from amplifier_app_tui.model.queues import NeedsYouQueue, QueuedMessage, SteeringQueue

# Started-runtime + policy-hook helpers; the offline_env fixture comes from
# conftest (shared with test_runtime_offline).
from tests.test_runtime_offline import _register_policy_hook, _started_runtime

pytestmark = pytest.mark.asyncio


class _PipeStdin:
    """A blocking line source the test feeds on demand (request/response timing).

    ``serve_loop`` iterates it on a reader thread; ``feed`` enqueues a line,
    ``close`` signals EOF.
    """

    def __init__(self) -> None:
        self._q: queue.Queue[str | None] = queue.Queue()

    def feed(self, obj: dict[str, Any]) -> None:
        self._q.put(json.dumps(obj) + "\n")

    def close(self) -> None:
        self._q.put(None)

    def __iter__(self) -> _PipeStdin:
        return self

    def __next__(self) -> str:
        item = self._q.get()
        if item is None:
            raise StopIteration
        return item


class _Capture:
    """Collect emitted protocol lines (written only on the event loop thread)."""

    def __init__(self) -> None:
        self.lines: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def write(self, s: str) -> int:
        for part in s.splitlines():
            part = part.strip()
            if part:
                with self._lock:
                    self.lines.append(json.loads(part))
        return len(s)

    def flush(self) -> None:  # noqa: D401 — file-like
        pass

    def types(self) -> list[str]:
        with self._lock:
            return [r.get("type", "") for r in self.lines]

    def kinds(self) -> list[str]:
        with self._lock:
            return [
                r["event"].get("kind", "") for r in self.lines if r.get("type") == "runtime.event"
            ]

    def find(self, type_: str) -> dict[str, Any] | None:
        with self._lock:
            return next((r for r in self.lines if r.get("type") == type_), None)


async def _wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met within timeout")


async def _run_with_choice(offline_env, choice: str) -> _Capture:
    """Drive one real turn through serve_loop, answering its approval with
    *choice* over the protocol. Returns the captured protocol stream."""
    runtime = await _started_runtime(offline_env["project"])
    _register_policy_hook(runtime)  # makes write_file require a real ask_user
    stdin, out = _PipeStdin(), _Capture()
    server = asyncio.create_task(
        serve_loop(runtime, source=cast("IO[str]", stdin), out=cast("IO[str]", out))
    )

    stdin.feed({"op": "submit", "text": "please write hello.txt with hi"})

    # Streaming flows first, then the turn PARKS on a real broker ticket.
    await _wait_until(lambda: out.find("approval.required") is not None)
    approval = out.find("approval.required")
    assert approval is not None
    assert out.find("turn.completed") is None, "must still be parked before answer"

    stdin.feed({"op": "approve", "ticket_id": approval["ticket_id"], "choice": choice})
    await _wait_until(lambda: out.find("turn.completed") is not None)
    stdin.close()
    await server
    return out


async def test_serve_approval_allow(offline_env) -> None:
    """Allow: the parked turn resumes, the tool runs, turn.completed carries it."""
    out = await _run_with_choice(offline_env, ALLOW_ONCE)

    assert out.types()[0] == "session.started"
    approval = out.find("approval.required")
    assert approval is not None
    assert approval["ticket_id"] and approval["options"][0] == "Allow once"
    # Real normalized vocabulary streamed over the wire before/after the park.
    for expected in ("prompt_submit", "stream_block_delta", "tool_post"):
        assert expected in out.kinds(), f"missing {expected} in {out.kinds()}"
    completed = out.find("turn.completed")
    assert completed is not None
    assert "wrote hello.txt" in completed["response"]  # the tool ran post-approval


class _FakeBootRuntime:
    """A runtime whose ``start`` reports boot phases through ``on_progress``
    exactly as RealRuntime does (resolve_config / foundation call the callback
    synchronously in-loop during ``start``). Just enough surface for
    ``serve_loop`` to run to a clean EOF exit."""

    class _NoBroker:
        head = None

        def add_listener(self, listener) -> None:  # noqa: D401 — broker shim
            pass

    def __init__(self, **kwargs: Any) -> None:
        self._on_progress = kwargs.get("on_progress")
        self.queue: asyncio.Queue[Any] = asyncio.Queue()
        self.broker = self._NoBroker()
        self.session_id = "boot-01"
        self.bundle_name = "tui"
        self.model_name = "test-model"

    async def start(self) -> None:
        assert self._on_progress is not None, "serve must pass on_progress"
        self._on_progress("loading", "tui")
        self._on_progress("installing_package", "tool-bash")
        self._on_progress("creating", "session")

    async def cleanup(self) -> None:
        pass


async def test_serve_emits_boot_progress_records_before_session_started(monkeypatch) -> None:
    """The boot phases RealRuntime reports via on_progress reach the protocol
    stream as schema-v1 ``boot.progress`` records, all before
    ``session.started`` — a protocol client can show them on its splash."""
    monkeypatch.setattr(serve_module, "RealRuntime", _FakeBootRuntime)
    stdin, out = _PipeStdin(), _Capture()
    stdin.close()  # immediate EOF: boot + session.started, then a clean exit

    code = await serve(None, stdin=cast("IO[str]", stdin), stdout=cast("IO[str]", out))

    assert code == 0
    types = out.types()
    assert types[:4] == ["boot.progress"] * 3 + ["session.started"], types
    # The exact wire record, pinned (action/detail verbatim from on_progress).
    assert out.lines[0] == {
        "schema_version": 1,
        "type": "boot.progress",
        "action": "loading",
        "detail": "tui",
    }
    assert out.lines[1]["action"] == "installing_package"
    assert out.lines[1]["detail"] == "tool-bash"
    assert out.lines[2] == {
        "schema_version": 1,
        "type": "boot.progress",
        "action": "creating",
        "detail": "session",
    }


class _FakeSteerRuntime:
    """Just enough runtime surface for ``serve_loop`` to run a steerable turn:
    a REAL ``SteeringQueue`` + ``StepBoundaryBridge`` (the exact objects
    RealRuntime wires in ``start``), with a ``submit`` that parks mid-turn so
    the test can feed a ``steer`` op over the protocol before the next step
    boundary — the same fake-boundary pattern ``test_kernel_steering`` drives.
    ``_steer_applied`` mirrors ``RealRuntime._steer_applied`` verbatim (the
    durable ``Applying steer: …`` narration block)."""

    class _NoBroker:
        head = None

        def add_listener(self, listener) -> None:  # noqa: D401 — broker shim
            pass

    def __init__(self) -> None:
        self.queue: asyncio.Queue[Any] = asyncio.Queue()
        self.broker = self._NoBroker()
        self.session_id = "steer-01"
        self.bundle_name = "tui"
        self.model_name = "test-model"
        self.steering = SteeringQueue()
        self._bridge = StepBoundaryBridge(
            self.session_id, self.steering, on_applied=self._steer_applied
        )
        self.mid_turn = asyncio.Event()
        self.resume = asyncio.Event()

    def _steer_applied(self, steer: QueuedMessage) -> None:
        self.queue.put_nowait(
            ContentBlockEnd(
                session_id=self.session_id,
                block_type="text",
                block={
                    "type": "text",
                    "text": f"Applying steer: {steer.text}",
                    "demo_role": "narration",
                },
            )
        )

    async def submit(self, text: str) -> str:
        del text
        # First step boundary (nothing queued yet), then park mid-turn.
        await self._bridge.handle_event("provider:request", {"session_id": self.session_id})
        self.mid_turn.set()
        await self.resume.wait()
        # The NEXT step boundary — a steer fed over the wire meanwhile is
        # consumed here, exactly once (StepBoundaryBridge contract).
        await self._bridge.handle_event("provider:request", {"session_id": self.session_id})
        return "done"

    async def cleanup(self) -> None:
        pass


def _narration_texts(out: _Capture) -> list[str]:
    with out._lock:
        return [
            record["event"]["block"]["text"]
            for record in out.lines
            if record.get("type") == "runtime.event"
            and record["event"].get("kind") == "content_block_end"
            and record["event"].get("block", {}).get("demo_role") == "narration"
        ]


async def test_serve_steer_op_lands_in_runtime_queue_and_applies_at_step_boundary() -> None:
    """The additive ``steer`` op routes into the SAME SteeringQueue the
    in-process TUI shares with the runtime; a steer submitted mid-turn is
    consumed at the next step boundary and the runtime's own ``Applying
    steer: …`` narration reaches the protocol stream (serve emits nothing
    extra). Fixes the reported data loss: the Rust client parked steers in
    its local queue and a live backend never consumed them."""
    runtime = _FakeSteerRuntime()
    stdin, out = _PipeStdin(), _Capture()
    server = asyncio.create_task(
        serve_loop(runtime, source=cast("IO[str]", stdin), out=cast("IO[str]", out))  # type: ignore[arg-type]
    )

    stdin.feed({"op": "submit", "text": "build the parser"})
    await asyncio.wait_for(runtime.mid_turn.wait(), timeout=5.0)

    stdin.feed({"op": "steer", "text": "also create a dotgraph of the modules"})
    await _wait_until(lambda: len(runtime.steering.pending_steers) == 1)
    queued = runtime.steering.pending_steers[0]
    assert queued.text == "also create a dotgraph of the modules"
    assert queued.kind == "steer"

    runtime.resume.set()
    await _wait_until(lambda: out.find("turn.completed") is not None)
    # Consumed at the boundary: queue empty, narration on the wire.
    assert runtime.steering.pending_steers == ()
    assert _narration_texts(out) == ["Applying steer: also create a dotgraph of the modules"]

    stdin.close()
    assert await server == 0


async def test_serve_drains_leftover_steers_at_turn_end() -> None:
    """A steer the turn never reached a boundary for is DISCARDED at turn end
    (finish_turn_queues parity) — it must not inject into a later turn."""
    runtime = _FakeSteerRuntime()
    stdin, out = _PipeStdin(), _Capture()
    server = asyncio.create_task(
        serve_loop(runtime, source=cast("IO[str]", stdin), out=cast("IO[str]", out))  # type: ignore[arg-type]
    )

    stdin.feed({"op": "submit", "text": "build the parser"})
    await asyncio.wait_for(runtime.mid_turn.wait(), timeout=5.0)
    stdin.feed({"op": "steer", "text": "first"})
    stdin.feed({"op": "steer", "text": "second"})
    await _wait_until(lambda: len(runtime.steering.pending_steers) == 2)

    runtime.resume.set()  # one boundary left: "first" applies, "second" cannot
    await _wait_until(lambda: out.find("turn.completed") is not None)
    assert _narration_texts(out) == ["Applying steer: first"]
    assert runtime.steering.pending == ()  # leftover drained, not leaked

    stdin.close()
    assert await server == 0


class _FakeDecisionRuntime(_FakeSteerRuntime):
    """The steerable fake plus a REAL ``NeedsYouQueue`` wired into the same
    ``StepBoundaryBridge`` — exactly the objects RealRuntime mounts — so the
    additive ``decision`` op can be proven end-to-end: park → answer over
    the wire → consumed at the next step boundary."""

    def __init__(self) -> None:
        super().__init__()
        self.needs_you = NeedsYouQueue()
        self._bridge = StepBoundaryBridge(self.session_id, self.steering, needs_you=self.needs_you)


async def test_serve_decision_op_answers_deferred_decision() -> None:
    """The additive ``decision`` op answers a DEFERRED needs-you decision.

    Ticket-lifecycle finding: after a governance deferral there is NO live
    broker ticket — ``GovernanceHook._classify`` parks the item straight
    into ``NeedsYouQueue`` and returns deny (deny-and-continue), so the
    existing ``approve`` op can never reach it. ``decision`` answers the
    SAME kernel queue the in-process TUI's ``apply_decision`` does; the
    StepBoundaryBridge then consumes the answered item at the next
    ``provider:request`` (the answer injection)."""
    runtime = _FakeDecisionRuntime()
    item = runtime.needs_you.defer(
        "Allow cat > /tmp/diag/build2.py <<'PY' …?",
        "outside configured project boundary without explicit authorization",
        choices=("Allow once", "Allow always", "Deny"),
        action="cat > /tmp/diag/build2.py <<'PY' …",
    )
    stdin, out = _PipeStdin(), _Capture()
    server = asyncio.create_task(
        serve_loop(runtime, source=cast("IO[str]", stdin), out=cast("IO[str]", out))  # type: ignore[arg-type]
    )

    stdin.feed({"op": "submit", "text": "keep going"})
    await asyncio.wait_for(runtime.mid_turn.wait(), timeout=5.0)

    # Unknown ids and malformed ops are swallowed (client already told).
    stdin.feed({"op": "decision", "decision_id": "decision-999", "answer": "Allow once"})
    stdin.feed({"op": "decision", "decision_id": "", "answer": "Allow once"})
    stdin.feed({"op": "decision", "decision_id": item.decision_id, "answer": "Allow once"})
    await _wait_until(
        lambda: any(
            i.decision_id == item.decision_id and i.status == "answered"
            for i in runtime.needs_you.items
        )
    )
    answered = next(i for i in runtime.needs_you.items if i.decision_id == item.decision_id)
    assert answered.answer == "Allow once"

    # The next step boundary consumes it (the injection the model sees).
    runtime.resume.set()
    await _wait_until(lambda: out.find("turn.completed") is not None)
    consumed = next(i for i in runtime.needs_you.items if i.decision_id == item.decision_id)
    assert consumed.status == "consumed"
    # An answered decision no longer blocks dependents.
    assert not runtime.needs_you.dependency_blocked(item.action)

    stdin.close()
    assert await server == 0


async def test_serve_approval_deny_continues(offline_env) -> None:
    """Deny-and-continue: the turn still completes, but the tool never ran."""
    out = await _run_with_choice(offline_env, DENY)

    completed = out.find("turn.completed")
    assert completed is not None
    assert "Denied" in completed["response"]  # FakeLoop's deny branch
    assert "tool_post" not in out.kinds()  # write_file did not execute


class _HistoryRuntime:
    """Minimal real-surface runtime for the additive ``history.query`` READ op.

    ``serve_loop`` needs a queue/broker/ids + ``cleanup``; the history arm reads
    ``project_dir`` off the runtime to locate this project's prompt store. No
    turn is run (history.query answers without one)."""

    class _NoBroker:
        head = None

        def add_listener(self, listener) -> None:  # noqa: D401 -- broker shim
            pass

    def __init__(self, project_dir: Path) -> None:
        self.queue: asyncio.Queue[Any] = asyncio.Queue()
        self.broker = self._NoBroker()
        self.session_id = "history-01"
        self.bundle_name = "tui"
        self.model_name = "test-model"
        self.project_dir = project_dir

    async def cleanup(self) -> None:
        pass


def _seed_history(project: Path) -> None:
    from amplifier_app_tui.kernel.prompt_history import PromptHistoryStore

    store = PromptHistoryStore(project_dir=project)
    for prompt in (
        "deploy app",
        "run tests",
        "deploy app",
        "check logs",
        "deploy app",
        "delete branch",
    ):
        store.append(prompt)


async def test_serve_history_query_returns_frecency_ranked(tmp_path, monkeypatch) -> None:
    """The additive ``history.query`` op returns a ``history.list`` ranked by
    frecency: the thrice-used older ``deploy app`` outranks the once-used newer
    ``delete branch`` -- the inversion vs the chronological up-ring."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    project = tmp_path / "proj"
    project.mkdir()
    _seed_history(project)

    runtime = _HistoryRuntime(project)
    stdin, out = _PipeStdin(), _Capture()
    server = asyncio.create_task(
        serve_loop(runtime, source=cast("IO[str]", stdin), out=cast("IO[str]", out))  # type: ignore[arg-type]
    )

    stdin.feed({"op": "history.query", "prefix": "de", "limit": 10})
    await _wait_until(lambda: out.find("history.list") is not None)
    stdin.close()
    assert await server == 0

    record = out.find("history.list")
    assert record is not None
    assert record["schema_version"] == 1
    assert record["prefix"] == "de"
    entries = record["entries"]
    assert [e["text"] for e in entries] == ["deploy app", "delete branch"]
    assert entries[0]["frequency"] == 3
    assert entries[0]["age"] == 1
    assert entries[0]["score"] == 1.5
    assert entries[1]["age"] == 0  # newest, but ranked second
    assert entries[0]["score"] > entries[1]["score"]  # frequency beat recency
    # Prefix filter excluded the non-'de' prompts.
    assert "run tests" not in [e["text"] for e in entries]


async def test_serve_history_query_empty_prefix_returns_all(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    project = tmp_path / "proj"
    project.mkdir()
    _seed_history(project)

    runtime = _HistoryRuntime(project)
    stdin, out = _PipeStdin(), _Capture()
    server = asyncio.create_task(
        serve_loop(runtime, source=cast("IO[str]", stdin), out=cast("IO[str]", out))  # type: ignore[arg-type]
    )

    stdin.feed({"op": "history.query"})  # no prefix, no limit -> all, default cap
    await _wait_until(lambda: out.find("history.list") is not None)
    stdin.close()
    assert await server == 0

    record = out.find("history.list")
    assert record is not None
    assert record["prefix"] == ""
    texts = [e["text"] for e in record["entries"]]
    assert texts[0] == "deploy app"  # frecency top
    assert set(texts) == {"deploy app", "run tests", "check logs", "delete branch"}


# --------------------------------------------------------------------------
# Context/cost meter telemetry (additive: context.state record + context.get op)
# --------------------------------------------------------------------------


class _FakeUsageRuntime:
    """Minimal serve_loop surface + a real CostTracker/CompactionConfig whose
    ``submit`` pushes ``ProviderResponseUsage`` events onto the queue like a real
    turn — the seam the additive context.state telemetry meters. Carries the same
    ``.cost``/``.compaction`` a RealRuntime exposes so serve reads honest sources."""

    class _NoBroker:
        head = None

        def add_listener(self, listener) -> None:  # noqa: D401 — broker shim
            pass

    def __init__(self) -> None:
        self.queue: asyncio.Queue[Any] = asyncio.Queue()
        self.broker = self._NoBroker()
        self.session_id = "meter-01"
        self.bundle_name = "tui"
        self.model_name = "anthropic/claude-sonnet-4"
        self.steering = SteeringQueue()
        # Deterministic offline pricing (no live/network swap).
        self.cost = CostTracker(pricing=PricingTable())
        self.compaction = CompactionConfig(max_tokens=200_000)

    async def submit(self, text: str) -> str:
        del text
        self.queue.put_nowait(
            ProviderResponseUsage(
                session_id=self.session_id,
                input_tokens=1000,
                output_tokens=200,
                cache_read=0,
                cache_write=0,
                model="claude-sonnet-4",
            )
        )
        self.queue.put_nowait(
            ProviderResponseUsage(
                session_id=self.session_id,
                input_tokens=1200,
                output_tokens=340,
                cache_read=800,
                cache_write=100,
                model="claude-sonnet-4",
            )
        )
        return "ok"

    async def cleanup(self) -> None:
        pass


def _context_states(out: _Capture) -> list[dict[str, Any]]:
    with out._lock:
        return [dict(r) for r in out.lines if r.get("type") == "context.state"]


# Both provider responses priced through the SAME CostTracker math the footer uses.
_C1 = Decimal(1000) * Decimal("0.003") / 1000 + Decimal(200) * Decimal("0.015") / 1000
_C2 = (
    Decimal(1200) * Decimal("0.003") / 1000
    + Decimal(340) * Decimal("0.015") / 1000
    + Decimal(800) * (Decimal("0.003") * Decimal("0.1")) / 1000
    + Decimal(100) * Decimal("0.003") / 1000
)


async def test_serve_pushes_context_state_per_provider_response() -> None:
    """Each provider response advances the meter and pushes a context.state with
    honest tokens (the LAST response's sum, donor parity), % of the compaction
    window, and the running $ from the CostTracker."""
    runtime = _FakeUsageRuntime()
    stdin, out = _PipeStdin(), _Capture()
    server = asyncio.create_task(
        serve_loop(runtime, source=cast("IO[str]", stdin), out=cast("IO[str]", out))  # type: ignore[arg-type]
    )

    stdin.feed({"op": "submit", "text": "build it"})
    await _wait_until(lambda: out.find("turn.completed") is not None)
    await _wait_until(lambda: len(_context_states(out)) >= 2)

    states = _context_states(out)
    assert len(states) >= 2  # two responses -> two pushes
    last = states[1]
    assert last["type"] == "context.state"
    assert last["schema_version"] == 1
    assert last["session_id"] == "meter-01"
    assert last["model"] == "anthropic/claude-sonnet-4"
    assert last["context_tokens"] == 1200 + 340 + 100  # cache_read is inside gross input
    assert last["input_tokens"] == 1200
    assert last["output_tokens"] == 340
    assert last["cache_read"] == 800
    assert last["cache_write"] == 100
    assert last["context_window"] == 200_000
    assert last["window_source"] == "compaction"
    assert last["context_pct"] == round(1640 / 200_000 * 100)  # 1
    assert Decimal(last["cost_usd"]) == _C1 + _C2
    assert last["cost_estimated"] is False

    stdin.close()
    assert await server == 0


async def test_serve_context_meter_ignores_child_compaction_and_learns_root_budget() -> None:
    runtime = _FakeUsageRuntime()
    stdin, out = _PipeStdin(), _Capture()
    server = asyncio.create_task(
        serve_loop(runtime, source=cast("IO[str]", stdin), out=cast("IO[str]", out))  # type: ignore[arg-type]
    )

    stdin.feed({"op": "context.get"})
    await _wait_until(lambda: len(_context_states(out)) == 1)
    runtime.queue.put_nowait(
        ContextCompacted(
            session_id="child",
            parent_id=runtime.session_id,
            after_tokens=482_000,
            budget=963_104,
        )
    )
    await asyncio.sleep(0.05)
    assert len(_context_states(out)) == 1

    runtime.queue.put_nowait(
        ContextCompacted(
            session_id=runtime.session_id,
            after_tokens=482_452,
            budget=963_104,
            target_tokens=481_552,
            strategy_level=3,
        )
    )
    await _wait_until(lambda: len(_context_states(out)) == 2)
    learned = _context_states(out)[-1]
    assert learned["context_tokens"] == 482_452
    assert learned["context_window"] == 963_104
    assert learned["context_pct"] == 50

    stdin.close()
    assert await server == 0


async def test_serve_context_get_op_pulls_current_state() -> None:
    """The additive ``context.get`` op returns the current meter on demand (initial
    paint / refresh) — the same context.state record the pump pushes. Before any
    usage it is a valid snapshot with null tokens and a $0 floor."""
    runtime = _FakeUsageRuntime()
    stdin, out = _PipeStdin(), _Capture()
    server = asyncio.create_task(
        serve_loop(runtime, source=cast("IO[str]", stdin), out=cast("IO[str]", out))  # type: ignore[arg-type]
    )

    stdin.feed({"op": "context.get"})
    await _wait_until(lambda: len(_context_states(out)) >= 1)
    first = _context_states(out)[0]
    assert first["context_tokens"] is None
    assert first["context_pct"] is None
    assert first["cost_usd"] == "0"
    # Window is still reported when known, even before any usage.
    assert first["context_window"] == 200_000

    stdin.feed({"op": "submit", "text": "go"})
    await _wait_until(lambda: out.find("turn.completed") is not None)
    await _wait_until(lambda: len(_context_states(out)) >= 3)  # 1 pull + 2 pushes
    stdin.feed({"op": "context.get"})
    await _wait_until(lambda: len(_context_states(out)) >= 4)

    pull = _context_states(out)[-1]
    assert pull["context_tokens"] == 1640
    assert pull["context_pct"] == 1
    assert pull["context_window"] == 200_000
    assert Decimal(pull["cost_usd"]) == _C1 + _C2

    stdin.close()
    assert await server == 0
