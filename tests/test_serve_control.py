"""Protocol-level tests for the serve control plane (item B6).

Drives :func:`amplifier_app_tui.kernel.serve.serve_loop` with a minimal fake
runtime over a REAL :class:`SessionStore` in a tmp dir -- the same seam the
live CLI ``serve`` uses -- and proves the contract an out-of-process
controller (and item B8 on top of it) depends on:

* a legacy client that never opts in sees the byte-identical old protocol;
* exactly one holder may write; conflicting input is refused, never interleaved;
* takeover is deterministic and invalidates the loser's token;
* an ``idem`` retry after a dropped connection does not double-submit;
* a reattached client replays the same history without touching the ledger;
* an abandoned lease expires, so a session is never permanently locked;
* pause mints a durable handoff a human can claim, and it is all audited.

A "reconnect" here is a second ``serve_loop`` over the same session directory
-- exactly what a dropped stdio pipe means for a protocol client.
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
from amplifier_app_tui.kernel.session_control import (
    AUDIT_FILENAME,
    CONTROL_FILENAME,
    REASON_LEASE_HELD,
    REASON_NOT_HOLDER,
    REASON_SESSION_PAUSED,
    Actor,
)
from amplifier_app_tui.model.queues import SteeringQueue

pytestmark = pytest.mark.asyncio

BOT = {"id": "bot-1", "kind": "automation"}
MJ = {"id": "mj", "kind": "human"}


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

    def types(self) -> list[str]:
        return [r.get("type", "") for r in self.lines]

    def find(self, type_: str) -> dict[str, Any] | None:
        return next((r for r in self.lines if r.get("type") == type_), None)

    def all(self, type_: str) -> list[dict[str, Any]]:
        return [r for r in self.lines if r.get("type") == type_]

    def conflicts(self) -> list[dict[str, Any]]:
        return self.all("control.conflict")

    def audits(self) -> list[dict[str, Any]]:
        return [r["entry"] for r in self.all("control.audit")]


class _NoBroker:
    head = None

    def add_listener(self, listener: Any) -> None:
        del listener


class _ControlRuntime:
    """Minimal serve_loop surface + a real store.

    ``submit`` records the text and appends to the durable UIEvent ledger the
    way ``RealRuntime`` does, so ``history.replay`` has honest history to
    stream on a reattach.
    """

    def __init__(self, store: SessionStore, session_id: str) -> None:
        self.store = store
        self.session_id = session_id
        self.bundle_name = "tui"
        self.model_name = "test-model"
        self.queue: asyncio.Queue[Any] = asyncio.Queue()
        self.broker = _NoBroker()
        self.steering = SteeringQueue()
        self.submits: list[str] = []
        self.interrupts = 0

    async def submit(self, text: str) -> str:
        self.submits.append(text)
        self.store.append_event(
            self.session_id,
            {"kind": "prompt_submit", "session_id": self.session_id, "ts": 1.0, "text": text},
        )
        return f"ok:{text}"

    async def interrupt(self) -> None:
        self.interrupts += 1

    async def cleanup(self) -> None:
        pass


async def _wait_until(predicate, timeout: float = 5.0) -> None:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met within timeout")


class _Connection:
    """One client connection to a session (a serve_loop over a pipe)."""

    def __init__(self, runtime: _ControlRuntime, **kwargs: Any) -> None:
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

    async def wait(self, predicate, timeout: float = 5.0) -> None:
        await _wait_until(predicate, timeout)

    async def drop(self) -> int:
        """Close the pipe -- what a dropped controller looks like to serve."""
        self.stdin.close()
        return await asyncio.wait_for(self.task, timeout=5.0)


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(base_dir=tmp_path / "sessions")


@pytest.fixture
def runtime(store: SessionStore) -> _ControlRuntime:
    session_id = "s" * 32
    store.save(session_id, [], {"session_id": session_id, "bundle": "tui"})
    return _ControlRuntime(store, session_id)


def _session_dir(runtime: _ControlRuntime) -> Path:
    return runtime.store.session_dir(runtime.session_id)


# -- opt-in ------------------------------------------------------------------


async def test_a_legacy_client_sees_the_unchanged_protocol(runtime: _ControlRuntime) -> None:
    """No actor, no lease, no idem -> no control records and no control files.
    The plane is opt-in; an existing front-end notices nothing."""
    conn = _Connection(runtime)
    conn.send(op="submit", text="hello")
    await conn.wait(lambda: conn.out.find("turn.completed") is not None)
    assert await conn.drop() == 0

    assert runtime.submits == ["hello"]
    assert conn.out.types() == ["session.started", "turn.completed"]
    assert not (_session_dir(runtime) / CONTROL_FILENAME).exists()
    assert not (_session_dir(runtime) / AUDIT_FILENAME).exists()


async def test_session_handle_is_stable_across_connections(runtime: _ControlRuntime) -> None:
    """The durable handle a controller hands out survives a reconnect, so a
    reference minted in one process still names this session in the next."""
    first = _Connection(runtime)
    first.send(op="session.handle")
    await first.wait(lambda: first.out.find("session.handle") is not None)
    handle = first.out.find("session.handle")
    assert handle is not None
    await first.drop()

    second = _Connection(runtime)
    second.send(op="session.handle")
    await second.wait(lambda: second.out.find("session.handle") is not None)
    again = second.out.find("session.handle")
    assert again is not None
    await second.drop()

    assert again["handle"]["handle_id"] == handle["handle"]["handle_id"]
    assert again["handle"]["ref"] == f"amplifier-session:{runtime.session_id}"


# -- single writer -----------------------------------------------------------


async def test_a_second_writer_is_refused_not_interleaved(runtime: _ControlRuntime) -> None:
    """The AC3 guarantee: with a lease held, a competing submit is rejected
    deterministically -- it never reaches the runtime."""
    conn = _Connection(runtime)
    conn.send(op="lease.acquire", actor=BOT, ttl=60)
    await conn.wait(lambda: conn.out.find("lease.state") is not None)
    lease = conn.out.find("lease.state")["lease"]["lease_id"]  # type: ignore[index]

    conn.send(op="submit", text="from a stranger", actor=MJ)  # no lease presented
    conn.send(op="submit", text="from the holder", lease=lease)
    await conn.wait(lambda: conn.out.find("turn.completed") is not None)
    assert await conn.drop() == 0

    assert runtime.submits == ["from the holder"]
    conflict = conn.out.conflicts()[0]
    assert conflict["reason"] == REASON_LEASE_HELD
    assert conflict["op"] == "submit"
    assert conflict["holder"]["id"] == "bot-1"


async def test_takeover_invalidates_the_previous_holders_token(
    runtime: _ControlRuntime,
) -> None:
    """A human takes the pen from the bot; the bot's stale lease stops working
    immediately, and the human's writes go through."""
    conn = _Connection(runtime)
    conn.send(op="lease.acquire", actor=BOT)
    await conn.wait(lambda: conn.out.find("lease.state") is not None)
    stale = conn.out.find("lease.state")["lease"]["lease_id"]  # type: ignore[index]

    conn.send(op="lease.takeover", actor=MJ, reason="taking it from here")
    await conn.wait(lambda: len(conn.out.all("lease.state")) >= 2)
    human_lease = conn.out.all("lease.state")[-1]["lease"]
    assert human_lease["actor"]["id"] == "mj"

    conn.send(op="submit", text="bot still trying", lease=stale)
    conn.send(op="submit", text="human speaking", lease=human_lease["lease_id"])
    await conn.wait(lambda: conn.out.find("turn.completed") is not None)
    assert await conn.drop() == 0

    assert runtime.submits == ["human speaking"]
    assert conn.out.conflicts()[0]["reason"] == REASON_NOT_HOLDER
    assert "lease.revoked" in [entry["action"] for entry in conn.out.audits()]


async def test_an_automated_client_cannot_take_the_lease_from_a_human(
    runtime: _ControlRuntime,
) -> None:
    conn = _Connection(runtime)
    conn.send(op="lease.acquire", actor=MJ)
    await conn.wait(lambda: conn.out.find("lease.state") is not None)
    conn.send(op="lease.takeover", actor=BOT, force=True)
    await conn.wait(lambda: conn.out.find("control.conflict") is not None)
    assert await conn.drop() == 0

    assert conn.out.conflicts()[0]["reason"] == "takeover_denied"
    assert conn.out.all("lease.state")[-1]["lease"]["actor"]["id"] == "mj"


# -- idempotency + reconnect -------------------------------------------------


async def test_idempotent_submit_survives_a_dropped_connection(
    runtime: _ControlRuntime,
) -> None:
    """The retry a reconnecting controller sends must NOT run the turn twice.
    The key is durable, so the replay works from a brand-new connection."""
    first = _Connection(runtime)
    first.send(op="submit", text="deploy", actor=BOT, idem="req-42")
    await first.wait(lambda: first.out.find("turn.completed") is not None)
    await first.drop()
    assert first.out.find("control.ack")["op"] == "submit"  # type: ignore[index]

    second = _Connection(runtime)
    second.send(op="submit", text="deploy", actor=BOT, idem="req-42")
    await second.wait(lambda: second.out.find("control.ack") is not None)
    assert await second.drop() == 0

    assert runtime.submits == ["deploy"], "the retry must not double-submit"
    ack = second.out.find("control.ack")
    assert ack is not None and ack["replay"] is True
    assert second.out.find("turn.completed") is None


async def test_reattach_replays_the_same_history_without_touching_it(
    runtime: _ControlRuntime,
) -> None:
    """AC5: a reconnecting participant observes the same event history, and
    replay is read-only -- the durable ledger is byte-identical afterwards."""
    first = _Connection(runtime)
    first.send(op="submit", text="one")
    await first.wait(lambda: first.out.find("turn.completed") is not None)
    first.send(op="submit", text="two")
    await first.wait(lambda: len(first.out.all("turn.completed")) == 2)
    await first.drop()

    ledger = _session_dir(runtime) / "ui-events.jsonl"
    before = ledger.read_bytes()

    second = _Connection(runtime)
    second.send(op="history.replay")
    await second.wait(lambda: second.out.find("history.end") is not None)
    second.send(op="history.replay", since=1)
    await second.wait(lambda: len(second.out.all("history.end")) == 2)
    assert await second.drop() == 0

    replayed = [r for r in second.out.all("runtime.event") if r.get("replay")]
    assert [r["event"]["text"] for r in replayed[:2]] == ["one", "two"]
    assert second.out.all("history.end")[0] == {
        "schema_version": 1,
        "type": "history.end",
        "session_id": runtime.session_id,
        "count": 2,
        "cursor": 2,
    }
    # The cursor lets a client resume where it stopped.
    assert second.out.all("history.begin")[1]["since"] == 1
    assert second.out.all("history.end")[1]["count"] == 1
    assert ledger.read_bytes() == before, "replay must never write the transcript"


async def test_an_abandoned_lease_expires_so_the_session_is_never_locked(
    runtime: _ControlRuntime,
) -> None:
    """AC5's hard edge: the controller vanishes mid-session holding the lease.
    Writes are refused while it is live, and freed the moment it expires --
    no unlock request, no operator intervention."""
    controller = _Connection(runtime)
    controller.send(op="lease.acquire", actor=BOT, ttl=0.2)
    await controller.wait(lambda: controller.out.find("lease.state") is not None)
    await controller.drop()  # dropped without releasing

    human = _Connection(runtime)
    human.send(op="submit", text="too early", actor=MJ)
    await human.wait(lambda: human.out.find("control.conflict") is not None)
    assert human.out.conflicts()[0]["reason"] == REASON_LEASE_HELD
    assert runtime.submits == []

    await asyncio.sleep(0.25)  # the lease TTL elapses with nobody heartbeating
    human.send(op="submit", text="now mine", actor=MJ)
    await human.wait(lambda: human.out.find("turn.completed") is not None)
    assert await human.drop() == 0

    assert runtime.submits == ["now mine"]
    assert "lease.expired" in [entry["action"] for entry in human.out.audits()]


# -- pause / handoff / audit -------------------------------------------------


async def test_pause_escalates_with_a_durable_handoff_a_human_claims(
    runtime: _ControlRuntime,
) -> None:
    """AC2 end to end: the controller pauses, gets a durable reference, and a
    human uses it to attach to the SAME session and take the write lease."""
    bot = _Connection(runtime)
    bot.send(op="lease.acquire", actor=BOT)
    await bot.wait(lambda: bot.out.find("lease.state") is not None)
    lease = bot.out.find("lease.state")["lease"]["lease_id"]  # type: ignore[index]
    bot.send(
        op="session.pause",
        actor=BOT,
        lease=lease,
        reason="needs human judgment",
        note="approve the prod deploy?",
        interrupt=True,
    )
    await bot.wait(lambda: bot.out.find("handoff.created") is not None)
    handoff = bot.out.find("handoff.created")["handoff"]  # type: ignore[index]
    assert handoff["ref"] == f"amplifier-session:{runtime.session_id}#{handoff['handoff_id']}"
    assert handoff["attach_command"] == f"amplifier-tui serve --attach {handoff['ref']}"

    # Paused: even the pauser cannot write until a human takes it.
    bot.send(op="submit", text="carry on anyway", actor=BOT)
    await bot.wait(lambda: bot.out.find("control.conflict") is not None)
    assert bot.out.conflicts()[0]["reason"] == REASON_SESSION_PAUSED
    await bot.wait(lambda: runtime.interrupts == 1)  # "interrupt": true honored
    await bot.drop()
    assert runtime.submits == []

    # The human arrives on a NEW connection with only the durable ref.
    human = _Connection(runtime)
    human.send(op="handoff.claim", handoff=handoff["handoff_id"], actor=MJ)
    await human.wait(lambda: human.out.find("handoff.claimed") is not None)
    granted = human.out.all("lease.state")[-1]["lease"]
    assert granted["actor"]["id"] == "mj"
    human.send(op="submit", text="I'll take it from here", lease=granted["lease_id"])
    await human.wait(lambda: human.out.find("turn.completed") is not None)
    assert await human.drop() == 0

    assert runtime.submits == ["I'll take it from here"]


async def test_attach_boot_claims_the_handoff_and_hands_over_the_lease(
    runtime: _ControlRuntime,
) -> None:
    """The CLI ``--attach <ref>`` adapter: the arriving human holds the write
    lease before their first keystroke."""
    bot = _Connection(runtime)
    bot.send(op="session.pause", actor=BOT, reason="escalate")
    await bot.wait(lambda: bot.out.find("handoff.created") is not None)
    handoff_id = bot.out.find("handoff.created")["handoff"]["handoff_id"]  # type: ignore[index]
    await bot.drop()

    human = _Connection(
        runtime,
        default_actor=Actor(id="mj", kind="human"),
        attach_handoff=handoff_id,
    )
    await human.wait(lambda: human.out.find("handoff.claimed") is not None)
    lease = human.out.all("lease.state")[-1]["lease"]
    assert lease["actor"] == {"id": "mj", "kind": "human"}
    human.send(op="submit", text="hello", lease=lease["lease_id"])
    await human.wait(lambda: human.out.find("turn.completed") is not None)
    assert await human.drop() == 0
    assert runtime.submits == ["hello"]


async def test_every_automated_action_and_handoff_is_attributable(
    runtime: _ControlRuntime,
) -> None:
    """AC4: the durable trail names an actor for each action, and any client
    can read it back over the protocol with ``audit.query``."""
    conn = _Connection(runtime)
    conn.send(op="lease.acquire", actor=BOT)
    await conn.wait(lambda: conn.out.find("lease.state") is not None)
    lease = conn.out.find("lease.state")["lease"]["lease_id"]  # type: ignore[index]
    conn.send(op="submit", text="ship it", lease=lease)
    await conn.wait(lambda: conn.out.find("turn.completed") is not None)
    conn.send(op="session.pause", actor=BOT, lease=lease, reason="escalate")
    await conn.wait(lambda: conn.out.find("handoff.created") is not None)
    handoff_id = conn.out.find("handoff.created")["handoff"]["handoff_id"]  # type: ignore[index]
    conn.send(op="handoff.claim", handoff=handoff_id, actor=MJ)
    await conn.wait(lambda: conn.out.find("handoff.claimed") is not None)
    conn.send(op="audit.query", limit=50)
    await conn.wait(lambda: conn.out.find("audit.list") is not None)
    assert await conn.drop() == 0

    entries = conn.out.find("audit.list")["entries"]  # type: ignore[index]
    pairs = [(e["action"], e["actor"]["id"]) for e in entries]
    assert pairs == [
        ("lease.granted", "bot-1"),
        ("write.accepted", "bot-1"),
        ("lease.released", "bot-1"),
        ("session.paused", "bot-1"),
        ("handoff.created", "bot-1"),
        ("handoff.claimed", "mj"),
    ]
    # The same trail is durable on disk, not just on the wire.
    lines = (_session_dir(runtime) / AUDIT_FILENAME).read_text().splitlines()
    assert [json.loads(line)["action"] for line in lines if line.strip()] == [
        action for action, _ in pairs
    ]
