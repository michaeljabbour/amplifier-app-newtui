"""Two REAL processes contending over one session (item B6, AC3/AC5).

Every other control-plane test drives ``serve_loop`` in-process. That proves
the state machine, but it cannot prove the thing AC3 and AC5 are actually
about: a human and an automation, in **separate OS processes**, reaching for
the same session at the same time. The guarantees that matter here -- one
writer wins and the other is refused rather than interleaved, a human takes
the pen from a bot across a process boundary, an abandoned lease frees a wedged
session, a hard-killed owner leaves a transcript another process can reattach
to -- are all cross-process guarantees carried by files on disk. In one process
they are, at best, plausible.

So these spawn ``tests/helpers/serve_process.py`` under ``sys.executable`` and
let the real ``serve_loop``, the real ``SessionControl``, the real ``O_EXCL``
lock and the real ledger settle it.

**Determinism, and the absence of sleeps.** Two devices, no timers raced:

*Barriers, not waits.* Every step blocks until a specific record appears on
that process's stdout (``_Proc.expect``). A timeout is the failure bound, never
the synchronisation -- if the record arrives in 3ms the test proceeds in 3ms.

*A clock the test owns.* Lease expiry is the one guarantee that is inherently
about time, and sleeping past a real TTL is exactly the flake this suite must
not ship. The helper points the control plane's ``now()`` at a file; the test
writes a later value and the lease is expired -- deterministically, in the
child, at the moment the next op reads the state. Nothing is slept through.
"""

from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

from amplifier_app_tui.kernel.persistence import SessionStore
from amplifier_app_tui.kernel.session_authz import AUTHZ_FILENAME, TokenStore
from amplifier_app_tui.kernel.session_control import (
    AUDIT_FILENAME,
    REASON_IDENTITY_UNVERIFIED,
    REASON_LEASE_HELD,
    REASON_NOT_HOLDER,
    REASON_TAKEOVER_DENIED,
    REASON_UNAUTHENTICATED,
)

HELPER = Path(__file__).parent / "helpers" / "serve_process.py"
SESSION_ID = "b6" + "0" * 30
START = 1_000_000.0
TIMEOUT = 30.0
"""Generous per-record bound. It is a failure detector, not a synchroniser --
a passing run never spends it."""

BOT = {"id": "bot-1", "kind": "automation"}
MJ = {"id": "mj", "kind": "human"}

pytestmark = pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="POSIX process control (SIGKILL) and AF_UNIX are required",
)


class _Proc:
    """One spawned participant, with a record-driven barrier instead of sleeps."""

    def __init__(self, *args: str, store: Path, clock: Path | None = None) -> None:
        cmd = [sys.executable, str(HELPER), "--store", str(store), "--session", SESSION_ID]
        if clock is not None:
            cmd += ["--clock", str(clock)]
        cmd += list(args)
        self.proc = subprocess.Popen(  # noqa: S603 -- fixed argv, no shell
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        self.records: list[dict[str, Any]] = []
        self._inbox: queue.Queue[dict[str, Any] | None] = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            text = line.strip()
            if not text:
                continue
            try:
                self._inbox.put(json.loads(text))
            except json.JSONDecodeError:
                continue
        self._inbox.put(None)

    def send(self, **op: Any) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(op) + "\n")
        self.proc.stdin.flush()

    def expect(self, type_: str, **fields: Any) -> dict[str, Any]:
        """Block until a record of *type_* matching *fields* arrives.

        THE synchronisation primitive: every ordering in this file is "the
        other process has demonstrably reached this state", never "enough time
        has probably passed".
        """
        while True:
            record = self._inbox.get(timeout=TIMEOUT)
            if record is None:
                stderr = self.proc.stderr.read() if self.proc.stderr else ""
                raise AssertionError(f"process exited before {type_!r}; stderr:\n{stderr}")
            self.records.append(record)
            if record.get("type") != type_:
                continue
            if all(record.get(key) == value for key, value in fields.items()):
                return record

    def kill(self) -> None:
        """SIGKILL: no cleanup, no lease release -- the crash we must survive."""
        self.proc.send_signal(signal.SIGKILL)
        self.proc.wait(timeout=TIMEOUT)

    def close(self) -> int:
        if self.proc.poll() is None and self.proc.stdin is not None:
            self.proc.stdin.close()
        return self.proc.wait(timeout=TIMEOUT)


@pytest.fixture
def store_dir(tmp_path: Path) -> Path:
    base = tmp_path / "sessions"
    SessionStore(base_dir=base).save(SESSION_ID, [], {"session_id": SESSION_ID, "bundle": "tui"})
    return base


@pytest.fixture
def clock(tmp_path: Path) -> Path:
    path = tmp_path / "clock"
    path.write_text(str(START), encoding="utf-8")
    return path


def _advance(clock: Path, seconds: float) -> None:
    """Move the shared virtual clock forward. Instant, and observable."""
    clock.write_text(str(float(clock.read_text().strip()) + seconds), encoding="utf-8")


def _submitted(store_dir: Path) -> list[str]:
    """What actually landed in the durable ledger -- the only honest answer to
    "whose write won"."""
    store = SessionStore(base_dir=store_dir)
    return [
        str(event.get("text", ""))
        for event in store.read_events(SESSION_ID)
        if event.get("kind") == "prompt_submit"
    ]


def _audit_actions(store_dir: Path) -> list[str]:
    path = SessionStore(base_dir=store_dir).session_dir(SESSION_ID) / AUDIT_FILENAME
    if not path.exists():
        return []
    return [json.loads(line)["action"] for line in path.read_text().splitlines() if line.strip()]


# -- AC3: two writers, one transcript ----------------------------------------


def test_two_processes_cannot_both_write_the_same_session(store_dir: Path, clock: Path) -> None:
    """One holder, one refusal -- and the loser's text never reaches the ledger.

    The single-writer guarantee is only interesting when the two writers cannot
    see each other. Here they genuinely cannot: separate processes, separate
    memory, arbitrating through ``control.json``.
    """
    bot = _Proc(store=store_dir, clock=clock)
    human = _Proc(store=store_dir, clock=clock)
    try:
        bot.expect("session.started")
        human.expect("session.started")

        bot.send(op="lease.acquire", actor=BOT, ttl=600)
        lease = bot.expect("lease.state")["lease"]["lease_id"]

        # The other process now tries to write without the token.
        human.send(op="submit", text="from the other process", actor=MJ)
        conflict = human.expect("control.conflict")
        assert conflict["reason"] == REASON_LEASE_HELD
        assert conflict["holder"]["id"] == "bot-1"

        bot.send(op="submit", text="from the holder", lease=lease)
        bot.expect("turn.completed")
    finally:
        human.close()
        bot.close()

    assert _submitted(store_dir) == ["from the holder"]


def test_a_human_process_takes_the_lease_from_a_bot_process(store_dir: Path, clock: Path) -> None:
    """Takeover crosses the process boundary, and kills the loser's token.

    The bot keeps a perfectly valid-looking lease id in its own memory; the
    epoch bump in the shared file is what makes it worthless.
    """
    bot = _Proc(store=store_dir, clock=clock)
    human = _Proc(store=store_dir, clock=clock)
    try:
        bot.expect("session.started")
        human.expect("session.started")

        bot.send(op="lease.acquire", actor=BOT, ttl=600)
        stale = bot.expect("lease.state")["lease"]["lease_id"]

        human.send(op="lease.takeover", actor=MJ, reason="I'll drive")
        granted = human.expect("lease.state", detail="lease.takeover")["lease"]
        assert granted["actor"]["id"] == "mj"

        bot.send(op="submit", text="bot carries on", lease=stale)
        assert bot.expect("control.conflict")["reason"] == REASON_NOT_HOLDER

        human.send(op="submit", text="human speaking", lease=granted["lease_id"])
        human.expect("turn.completed")
    finally:
        human.close()
        bot.close()

    assert _submitted(store_dir) == ["human speaking"]
    assert "lease.revoked" in _audit_actions(store_dir)


# -- AC5: nothing stays wedged ------------------------------------------------


def test_an_abandoned_lease_expires_and_frees_a_wedged_session(
    store_dir: Path, clock: Path
) -> None:
    """The controller is SIGKILLed holding the lease. Nobody unlocks anything.

    Expiry -- not a release, not an operator -- is what frees the session, and
    the surviving process observes it the moment the clock says so. No sleep:
    the test moves the clock and the next op reaps the lease.
    """
    bot = _Proc(store=store_dir, clock=clock)
    human = _Proc(store=store_dir, clock=clock)
    try:
        bot.expect("session.started")
        human.expect("session.started")
        bot.send(op="lease.acquire", actor=BOT, ttl=60)
        bot.expect("lease.state")
        bot.kill()  # vanishes mid-session, lease still held

        human.send(op="submit", text="too early", actor=MJ)
        assert human.expect("control.conflict")["reason"] == REASON_LEASE_HELD
        assert _submitted(store_dir) == []

        _advance(clock, 61.0)  # the TTL elapses; nobody heartbeated

        human.send(op="submit", text="now mine", actor=MJ)
        human.expect("turn.completed")
    finally:
        human.close()

    assert _submitted(store_dir) == ["now mine"]
    assert "lease.expired" in _audit_actions(store_dir)


def test_reattach_after_a_hard_kill_replays_history_uncorrupted(
    store_dir: Path, clock: Path
) -> None:
    """Kill -9 the owner mid-session; the next process picks up a clean session.

    Three things are checked, because "reattach worked" is too weak a claim:
    the replayed history is exactly what was written, the ledger is byte-for-byte
    unchanged by the replay, and the durable control state the dead process left
    behind still parses and still governs.
    """
    first = _Proc(store=store_dir, clock=clock)
    first.expect("session.started")
    first.send(op="lease.acquire", actor=BOT, ttl=60)
    lease = first.expect("lease.state")["lease"]["lease_id"]
    first.send(op="submit", text="one", lease=lease)
    first.expect("turn.completed")
    first.send(op="submit", text="two", lease=lease)
    first.expect("turn.completed")
    first.kill()

    session_dir = SessionStore(base_dir=store_dir).session_dir(SESSION_ID)
    ledger = session_dir / "ui-events.jsonl"
    before = ledger.read_bytes()

    second = _Proc(store=store_dir, clock=clock)
    try:
        second.expect("session.started")
        second.send(op="history.replay")
        second.expect("history.end", count=2)
        replayed = [r for r in second.records if r.get("type") == "runtime.event"]
        assert [r["event"]["text"] for r in replayed] == ["one", "two"]
        assert ledger.read_bytes() == before, "replay must never write the transcript"

        # The dead process's lease still governs until it expires...
        second.send(op="submit", text="three", actor=MJ)
        assert second.expect("control.conflict")["reason"] == REASON_LEASE_HELD
        # ...and then the session is usable again, from the new process.
        _advance(clock, 61.0)
        second.send(op="submit", text="three", actor=MJ)
        second.expect("turn.completed")
    finally:
        second.close()

    assert _submitted(store_dir) == ["one", "two", "three"]
    assert json.loads((session_dir / "control.json").read_text())["handle_id"]
    for line in ledger.read_text().splitlines():
        if line.strip():
            json.loads(line)  # every record still parses: no torn writes


# -- Gap 3: attaching to a LIVE runtime, not just to session state ------------


def test_a_second_process_attaches_to_the_live_runtime_and_detaches_cleanly(
    store_dir: Path, clock: Path
) -> None:
    """The attaching process drives the OWNER's runtime, then leaves it running.

    This is the distinction the old ``--attach`` blurred: the second process
    boots no runtime of its own (so there is no second writer), sees the same
    record stream, and its submission is executed by the first process. On
    detach the owner is untouched and still serving.
    """
    owner = _Proc("--attachable", store=store_dir, clock=clock)
    try:
        owner.expect("session.started")
        endpoint = owner.expect("attach.listening")
        assert endpoint["pid"] == owner.proc.pid

        peer = _Proc("--mode", "attach", store=store_dir, clock=clock)
        peer.expect("session.attached", pid=owner.proc.pid)

        peer.send(op="submit", text="typed by the attached process", actor=MJ)
        # Both participants observe the same records -- one shared session.
        peer.expect("turn.completed")
        owner.expect("turn.completed")
        peer.close()

        # Clean detach: the owner is unaffected and still accepting work.
        owner.send(op="submit", text="owner still serving", actor=MJ)
        owner.expect("turn.completed")
    finally:
        owner.close()

    assert _submitted(store_dir) == [
        "typed by the attached process",
        "owner still serving",
    ]


# -- Gap 1: identity across a process boundary --------------------------------


def test_a_process_cannot_claim_to_be_human_without_a_human_credential(
    store_dir: Path, clock: Path
) -> None:
    """The escalation the lease's own rules cannot stop.

    ``human`` beats ``automation`` for the lease by design. Without
    authorization, any process could type ``kind: "human"`` and take the pen
    from a real person -- the takeover rule turned into a privilege-escalation
    path. Here the bot process holds a real credential and is still refused,
    because the credential says ``automation``.
    """
    tokens = TokenStore(store_dir / AUTHZ_FILENAME)
    human_token, _ = tokens.issue("mj", kind="human")
    bot_token, _ = tokens.issue("bot-1", kind="automation")

    human = _Proc(store=store_dir, clock=clock)
    bot = _Proc(store=store_dir, clock=clock)
    try:
        human.expect("session.started")
        bot.expect("session.started")

        human.send(op="lease.acquire", actor=MJ, auth={"token": human_token}, ttl=600)
        held = human.expect("lease.state")["lease"]
        assert held["actor"]["id"] == "mj"
        # Provenance is on the record: an authenticated human, not a claim.
        assert held["actor"]["auth"] == {
            "method": "token",
            "verified": True,
            "principal": "mj",
        }

        # 1. A credentialled bot claiming to be human: refused on identity.
        bot.send(
            op="lease.takeover",
            actor={"id": "bot-1", "kind": "human"},
            auth={"token": bot_token},
            force=True,
        )
        assert bot.expect("control.conflict")["reason"] == REASON_IDENTITY_UNVERIFIED

        # 2. The same bot being honest: refused by precedence, as always.
        bot.send(op="lease.takeover", actor=BOT, auth={"token": bot_token}, force=True)
        assert bot.expect("control.conflict")["reason"] == REASON_TAKEOVER_DENIED

        # 3. No credential at all, once a store exists: default deny.
        bot.send(op="lease.takeover", actor=MJ, force=True)
        assert bot.expect("control.conflict")["reason"] == REASON_UNAUTHENTICATED

        # The human still holds the lease and can still write.
        human.send(
            op="submit", text="still mine", lease=held["lease_id"], auth={"token": human_token}
        )
        human.expect("turn.completed")
    finally:
        bot.close()
        human.close()

    assert _submitted(store_dir) == ["still mine"]
    assert "auth.denied" in _audit_actions(store_dir)
