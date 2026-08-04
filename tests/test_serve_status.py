"""``session.status``: what a controller actually needs to decide anything.

AC1 asks that a controller can "retrieve current status through a stable
interface". What existed was ``lease.status``, which answers exactly one
question -- who holds the write token -- and nothing else. A controller reading
it could not tell whether a turn was running (so its ``submit`` would be
dropped as a re-submit), whether an approval was blocking the turn it was
waiting on (so it would wait forever), which model or reasoning tier was
actually in force after a mid-session change, what it had queued, how much
context and budget were left, or how far the durable ledger had got.

``lease.status`` is deliberately left byte-identical -- clients branch on it.
``session.status`` is the complete answer, and these tests pin each thing that
was missing, one assertion per decision a controller has to make.
"""

from __future__ import annotations

import asyncio
import json
import queue
from pathlib import Path
from typing import IO, Any, cast

import pytest

from amplifier_app_tui.kernel.persistence import SessionStore
from amplifier_app_tui.kernel.serve import serve_loop
from amplifier_app_tui.kernel.session_control import CONTROL_FILENAME
from amplifier_app_tui.model.queues import NeedsYouQueue, SteeringQueue

pytestmark = pytest.mark.asyncio

BOT = {"id": "bot-1", "kind": "automation"}


class _PipeStdin:
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
    def __init__(self) -> None:
        self.lines: list[dict[str, Any]] = []

    def write(self, s: str) -> int:
        for part in s.splitlines():
            text = part.strip()
            if text:
                self.lines.append(json.loads(text))
        return len(s)

    def flush(self) -> None:
        pass

    def all(self, type_: str) -> list[dict[str, Any]]:
        return [r for r in self.lines if r.get("type") == type_]

    def find(self, type_: str) -> dict[str, Any] | None:
        return next((r for r in self.lines if r.get("type") == type_), None)


class _Ticket:
    ticket_id = "approval-3"
    prompt = "Run `rm -rf build`?"
    options = ("Allow once", "Deny")


class _Broker:
    def __init__(self) -> None:
        self.head: _Ticket | None = None

    def add_listener(self, listener: Any) -> None:
        del listener


class _StatusRuntime:
    """A runtime with the knobs status has to report, all independently set."""

    def __init__(self, store: SessionStore, session_id: str) -> None:
        self.store = store
        self.session_id = session_id
        self.bundle_name = "tui"
        self.model_name = "anthropic/claude-sonnet-4"
        self.queue: asyncio.Queue[Any] = asyncio.Queue()
        self.broker = _Broker()
        self.steering = SteeringQueue()
        self.needs_you = NeedsYouQueue()
        self.effort: str | None = "high"
        self.submits: list[str] = []
        self.gate: asyncio.Event | None = None

    async def get_effort(self) -> str | None:
        return self.effort

    async def submit(self, text: str) -> str:
        self.submits.append(text)
        if self.gate is not None:
            await self.gate.wait()
        self.store.append_event(
            self.session_id,
            {"kind": "prompt_submit", "session_id": self.session_id, "ts": 7.0, "text": text},
        )
        return f"ok:{text}"

    async def interrupt(self) -> None:
        return None

    async def cleanup(self) -> None:
        return None


class _Connection:
    def __init__(self, runtime: _StatusRuntime, **kwargs: Any) -> None:
        self.stdin = _PipeStdin()
        self.out = _Capture()
        self.task = asyncio.create_task(
            serve_loop(
                cast("Any", runtime),
                source=cast("IO[str]", self.stdin),
                out=cast("IO[str]", self.out),
                **kwargs,
            )
        )

    def send(self, **op: Any) -> None:
        self.stdin.feed(op)

    async def wait(self, predicate: Any, timeout: float = 5.0) -> None:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            if predicate():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("condition not met within timeout")

    async def status(self, count: int = 1, **op: Any) -> dict[str, Any]:
        self.send(op="session.status", **op)
        await self.wait(lambda: len(self.out.all("session.status")) >= count)
        return self.out.all("session.status")[count - 1]

    async def drop(self) -> int:
        self.stdin.close()
        return await asyncio.wait_for(self.task, timeout=5.0)


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(base_dir=tmp_path / "sessions")


@pytest.fixture
def runtime(store: SessionStore) -> _StatusRuntime:
    session_id = "s" * 32
    store.save(session_id, [], {"session_id": session_id, "bundle": "tui"})
    return _StatusRuntime(store, session_id)


async def test_status_reports_the_model_provider_and_reasoning_tier(
    runtime: _StatusRuntime,
) -> None:
    """All three change mid-session, and a controller that assumes otherwise
    reasons about a session it is no longer in."""
    conn = _Connection(runtime)
    record = await conn.status()
    assert await conn.drop() == 0

    assert record["session"] == {
        "bundle": "tui",
        "model": "claude-sonnet-4",
        "provider": "anthropic",
        "effort": "high",
    }


async def test_status_says_whether_a_turn_is_running(runtime: _StatusRuntime) -> None:
    """The single most consequential omission.

    ``submit`` while a turn is live is silently ignored, so a controller that
    cannot see ``turn.active`` cannot tell "my input was refused" from "my
    input is running" -- and would sit waiting for a turn that never started.
    """
    runtime.gate = asyncio.Event()
    conn = _Connection(runtime)
    conn.send(op="submit", text="long one")
    await conn.wait(lambda: runtime.submits == ["long one"])

    busy = await conn.status()
    assert busy["turn"]["active"] is True
    assert busy["state"] == "busy"

    runtime.gate.set()
    await conn.wait(lambda: conn.out.find("turn.completed") is not None)
    idle = await conn.status(2)
    assert await conn.drop() == 0

    assert idle["turn"]["active"] is False
    assert idle["state"] == "idle"


async def test_status_surfaces_a_blocking_approval(runtime: _StatusRuntime) -> None:
    """A turn that cannot finish until someone answers is not "busy" -- it is
    waiting on a person, and status has to say which question."""
    runtime.broker.head = _Ticket()
    conn = _Connection(runtime)
    record = await conn.status()
    assert await conn.drop() == 0

    assert record["state"] == "awaiting_approval"
    assert record["pending"]["approval"] == {
        "ticket_id": "approval-3",
        "prompt": "Run `rm -rf build`?",
        "options": ["Allow once", "Deny"],
    }


async def test_status_surfaces_deferred_decisions(runtime: _StatusRuntime) -> None:
    """A deferral has no live approval ticket, so it is invisible to every
    other channel -- ``{"op":"approve"}`` can never reach it."""
    runtime.needs_you.defer("Which region?", choices=("eu", "us"))

    conn = _Connection(runtime)
    record = await conn.status()
    assert await conn.drop() == 0

    assert record["state"] == "awaiting_decision"
    assert record["pending"]["decision_count"] == 1
    assert record["pending"]["decisions"][0]["question"] == "Which region?"


async def test_status_reports_queued_input(runtime: _StatusRuntime) -> None:
    """A controller that queued a steer needs to know it is still queued."""
    conn = _Connection(runtime)
    runtime.steering.enqueue("go left")
    record = await conn.status()
    assert await conn.drop() == 0

    assert record["turn"]["queued_steers"] == 1


async def test_status_reports_the_lease_holder_and_time_remaining(
    runtime: _StatusRuntime,
) -> None:
    """ "Who holds it" is not enough: a controller heartbeats against the time
    left, and a human deciding whether to break in wants to know how long they
    would be waiting."""
    conn = _Connection(runtime)
    conn.send(op="lease.acquire", actor=BOT, ttl=600)
    await conn.wait(lambda: conn.out.find("lease.state") is not None)
    record = await conn.status()
    assert await conn.drop() == 0

    control = record["control"]
    assert control["holder"]["id"] == "bot-1"
    assert control["lease"]["expires_in"] == pytest.approx(600, abs=5)
    assert control["epoch"] == 1
    assert control["authz"]["policy"] == "local-pipe"


async def test_status_reports_a_pause_and_the_unclaimed_handoff(
    runtime: _StatusRuntime,
) -> None:
    """The escalation nobody has picked up is the one worth surfacing."""
    conn = _Connection(runtime)
    conn.send(op="session.pause", actor=BOT, reason="needs a human")
    await conn.wait(lambda: conn.out.find("handoff.created") is not None)
    record = await conn.status()
    assert await conn.drop() == 0

    control = record["control"]
    assert record["state"] == "paused"
    assert control["paused"] is True
    assert control["paused_by"]["id"] == "bot-1"
    assert control["handoffs"]["open"] == 1
    assert control["handoffs"]["pending"][0]["reason"] == "needs a human"


async def test_status_reports_the_ledger_cursor_and_last_event(
    runtime: _StatusRuntime,
) -> None:
    """A reattaching client wants a cursor and one recent fact, not a replay."""
    conn = _Connection(runtime)
    conn.send(op="submit", text="one")
    await conn.wait(lambda: conn.out.find("turn.completed") is not None)
    record = await conn.status()
    assert await conn.drop() == 0

    assert record["history"]["events"] == 1
    assert record["history"]["cursor"] == 1
    assert record["history"]["last"]["kind"] == "prompt_submit"


async def test_status_reports_the_audit_trail_position(runtime: _StatusRuntime) -> None:
    conn = _Connection(runtime)
    conn.send(op="lease.acquire", actor=BOT)
    await conn.wait(lambda: conn.out.find("lease.state") is not None)
    record = await conn.status()
    assert await conn.drop() == 0

    audit = record["control"]["audit"]
    assert audit["seq"] == 1
    assert audit["last"]["action"] == "lease.granted"


async def test_status_answers_before_anyone_opts_in_without_writing_files(
    runtime: _StatusRuntime, tmp_path: Path
) -> None:
    """Reading status must not be the thing that materializes the control plane
    -- otherwise "check before you commit" would itself be a commitment."""
    conn = _Connection(runtime)
    record = await conn.status()
    assert await conn.drop() == 0

    assert record["control"] is None
    assert record["state"] == "idle"
    assert record["session"]["model"] == "claude-sonnet-4"
    assert not (runtime.store.session_dir(runtime.session_id) / CONTROL_FILENAME).exists()


async def test_status_never_mutates_the_session(runtime: _StatusRuntime) -> None:
    """Safe to poll: no audit entry, no epoch bump, no ledger write."""
    conn = _Connection(runtime)
    conn.send(op="lease.acquire", actor=BOT)
    await conn.wait(lambda: conn.out.find("lease.state") is not None)
    before = (runtime.store.session_dir(runtime.session_id) / "control.json").read_bytes()

    for _ in range(3):
        await conn.status(len(conn.out.all("session.status")) + 1)
    assert await conn.drop() == 0

    after = (runtime.store.session_dir(runtime.session_id) / "control.json").read_bytes()
    assert after == before
    assert len(conn.out.all("control.audit")) == 1  # only the acquire


async def test_lease_status_is_unchanged(runtime: _StatusRuntime) -> None:
    """The old record keeps its exact shape -- existing clients branch on it,
    and completeness is an additive op, not a rewrite."""
    conn = _Connection(runtime)
    conn.send(op="lease.status")
    await conn.wait(lambda: conn.out.find("lease.state") is not None)
    record = conn.out.find("lease.state")
    assert await conn.drop() == 0

    assert record is not None
    assert set(record) == {
        "schema_version",
        "type",
        "ok",
        "session_id",
        "handle_id",
        "epoch",
        "paused",
        "now",
        "lease",
    }
