"""Unit tests for the session control plane (kernel/session_control.py).

The state machine behind item B6: durable handle, single-writer lease,
deterministic takeover, actor attribution, idempotency and reconnect safety.
Everything runs against a ``tmp_path`` session directory with an INJECTED
clock, so lease expiry is exact rather than slept for.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from amplifier_app_tui.kernel.session_control import (
    AUDIT_FILENAME,
    AUTOMATION,
    CONTROL_FILENAME,
    HUMAN,
    REASON_HANDOFF_CLAIMED,
    REASON_LEASE_EXPIRED,
    REASON_LEASE_HELD,
    REASON_NO_ACTOR,
    REASON_NOT_HOLDER,
    REASON_SESSION_PAUSED,
    REASON_TAKEOVER_DENIED,
    REASON_UNKNOWN_HANDOFF,
    Actor,
    SessionControl,
    attach_command,
    attach_ref,
    parse_attach_ref,
)


class _Clock:
    """A hand-cranked wall clock (control TTLs are wall-clock by design)."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


BOT = Actor(id="bot-1", kind=AUTOMATION, display="Controller")
OTHER_BOT = Actor(id="bot-2", kind=AUTOMATION)
MJ = Actor(id="mj", kind=HUMAN)
OTHER_HUMAN = Actor(id="sam", kind=HUMAN)


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def control(tmp_path: Path, clock: _Clock) -> SessionControl:
    return SessionControl(tmp_path / "sess-1", "sess-1", now=clock, default_ttl=60.0)


def _types(records: list[dict]) -> list[str]:
    return [r.get("type", "") for r in records]


def _first(records: list[dict], type_: str) -> dict:
    match = next((r for r in records if r.get("type") == type_), None)
    assert match is not None, f"no {type_} in {_types(records)}"
    return match


def _actions(control: SessionControl) -> list[str]:
    return [str(e.get("action", "")) for e in control.audit_entries(limit=200)]


# -- handle ------------------------------------------------------------------


def test_handle_is_minted_once_and_survives_reattach(tmp_path: Path, clock: _Clock) -> None:
    """A reconnecting process observes the SAME durable handle it left."""
    first = SessionControl(tmp_path / "s", "s", now=clock)
    handle_id = first.handle.handle_id
    assert handle_id.startswith("h-")

    reattached = SessionControl(tmp_path / "s", "s", now=clock)
    assert reattached.handle.handle_id == handle_id
    assert (tmp_path / "s" / CONTROL_FILENAME).is_file()

    record = reattached.handle_record()
    assert record["type"] == "session.handle"
    assert record["handle"]["ref"] == attach_ref("s")
    assert record["handle"]["attach_command"] == attach_command("s")


@pytest.mark.parametrize(
    ("ref", "expected"),
    [
        ("amplifier-session:abc", ("abc", None)),
        ("amplifier-session:abc#ho-9", ("abc", "ho-9")),
        ("abc", ("abc", None)),  # a bare id is a valid ref
        ("  amplifier-session:abc#ho-9  ", ("abc", "ho-9")),
    ],
)
def test_parse_attach_ref(ref: str, expected: tuple[str, str | None]) -> None:
    assert parse_attach_ref(ref) == expected


# -- lease lifecycle ---------------------------------------------------------


def test_acquire_grants_then_blocks_a_second_actor(control: SessionControl) -> None:
    granted = control.acquire(BOT)
    state = _first(granted, "lease.state")
    assert state["lease"]["actor"]["id"] == "bot-1"
    assert state["epoch"] == 1

    denied = control.acquire(OTHER_BOT)
    conflict = _first(denied, "control.conflict")
    assert conflict["reason"] == REASON_LEASE_HELD
    assert conflict["holder"]["id"] == "bot-1"
    # The incumbent still holds it -- a denial changes nothing.
    held = control.active_lease()
    assert held is not None and held.actor.id == "bot-1"


def test_acquire_without_an_actor_is_refused(control: SessionControl) -> None:
    """Attribution is mandatory: an unnamed holder could never be audited."""
    records = control.acquire(None)
    assert _first(records, "control.conflict")["reason"] == REASON_NO_ACTOR
    assert control.active_lease() is None


def test_reacquire_by_the_same_actor_renews(control: SessionControl, clock: _Clock) -> None:
    first = _first(control.acquire(BOT), "lease.state")["lease"]
    clock.advance(10)
    second = _first(control.acquire(BOT), "lease.state")["lease"]
    assert second["expires_at"] > first["expires_at"]
    assert "lease.renewed" in _actions(control)


def test_heartbeat_extends_and_a_stale_lease_id_conflicts(
    control: SessionControl, clock: _Clock
) -> None:
    lease = _first(control.acquire(BOT, ttl=30), "lease.state")["lease"]
    clock.advance(20)
    renewed = _first(control.heartbeat(lease["lease_id"], ttl=30), "lease.state")["lease"]
    assert renewed["expires_at"] == clock.now + 30

    conflict = _first(control.heartbeat("l-not-mine"), "control.conflict")
    assert conflict["reason"] == REASON_NOT_HOLDER


def test_release_frees_the_lease_and_is_safely_retryable(control: SessionControl) -> None:
    lease = _first(control.acquire(BOT), "lease.state")["lease"]
    released = _first(control.release(lease["lease_id"]), "lease.state")
    assert released["lease"] is None
    # A reconnecting client may retry the release; it is a no-op success.
    again = _first(control.release(lease["lease_id"]), "lease.state")
    assert again["lease"] is None and again["ok"] is True
    assert "lease.released" in _actions(control)


def test_expiry_frees_a_session_a_dead_controller_left_locked(
    control: SessionControl, clock: _Clock
) -> None:
    """AC5's backstop: no release, no heartbeat, no unlock request -- and the
    session still frees itself, so it can never be permanently locked."""
    control.acquire(BOT, ttl=30)
    clock.advance(31)
    assert control.active_lease() is None

    records = control.acquire(MJ)
    assert _first(records, "lease.state")["lease"]["actor"]["id"] == "mj"
    assert "lease.expired" in _actions(control)


# -- takeover ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("holder", "challenger", "force", "granted"),
    [
        (BOT, MJ, False, True),  # a human always wins over a bot
        (MJ, BOT, False, False),  # a bot never wins over a human
        (MJ, BOT, True, False),  # ... not even with force
        (BOT, OTHER_BOT, False, False),  # bots do not fight each other
        (BOT, OTHER_BOT, True, False),  # force is a human-only lever
        (MJ, OTHER_HUMAN, False, False),  # human-from-human needs intent
        (MJ, OTHER_HUMAN, True, True),  # ... which force expresses
    ],
)
def test_takeover_is_deterministic_by_precedence(
    control: SessionControl, holder: Actor, challenger: Actor, force: bool, granted: bool
) -> None:
    control.acquire(holder)
    records = control.takeover(challenger, force=force, reason="escalation")
    if granted:
        assert _first(records, "lease.state")["lease"]["actor"]["id"] == challenger.id
        assert "lease.revoked" in _actions(control)
    else:
        assert _first(records, "control.conflict")["reason"] == REASON_TAKEOVER_DENIED
        held = control.active_lease()
        assert held is not None and held.actor.id == holder.id


def test_takeover_invalidates_the_previous_lease_id(control: SessionControl) -> None:
    """The old holder cannot write with its stale token after being taken over
    -- that is what stops two writers silently interleaving."""
    stale = _first(control.acquire(BOT), "lease.state")["lease"]["lease_id"]
    control.takeover(MJ)

    decision = control.authorize("submit", {"lease": stale, "actor": BOT.as_dict()})
    assert not decision.allowed
    assert decision.reason == REASON_NOT_HOLDER


# -- write gating ------------------------------------------------------------


def test_open_mode_allows_an_unleased_write(control: SessionControl) -> None:
    """Nobody has claimed ownership yet: the legacy single-client contract."""
    decision = control.authorize("submit", {})
    assert decision.allowed
    assert "write.accepted" in _actions(control)


def test_a_held_lease_blocks_an_unleased_write(control: SessionControl) -> None:
    control.acquire(BOT)
    decision = control.authorize("submit", {"actor": {"id": "stranger", "kind": "human"}})
    assert not decision.allowed
    assert decision.reason == REASON_LEASE_HELD
    conflict = _first(decision.records, "control.conflict")
    assert conflict["holder"]["id"] == "bot-1"
    assert conflict["op"] == "submit"


def test_the_holder_writes_and_an_expired_token_does_not(
    control: SessionControl, clock: _Clock
) -> None:
    lease = _first(control.acquire(BOT, ttl=30), "lease.state")["lease"]["lease_id"]
    assert control.authorize("submit", {"lease": lease}).allowed

    clock.advance(31)
    decision = control.authorize("submit", {"lease": lease})
    assert not decision.allowed
    assert decision.reason == REASON_LEASE_EXPIRED


def test_writes_are_attributed_to_the_lease_holder(control: SessionControl) -> None:
    lease = _first(control.acquire(BOT), "lease.state")["lease"]["lease_id"]
    decision = control.authorize("submit", {"lease": lease})
    assert decision.actor.id == "bot-1"
    entry = control.audit_entries()[-1]
    assert entry["action"] == "write.accepted"
    assert entry["actor"] == {"id": "bot-1", "kind": "automation", "display": "Controller"}
    assert entry["detail"] == {"op": "submit"}


# -- pause / handoff ---------------------------------------------------------


def test_pause_mints_a_durable_handoff_and_blocks_writes(control: SessionControl) -> None:
    lease = _first(control.acquire(BOT), "lease.state")["lease"]["lease_id"]
    records = control.pause(BOT, reason="needs a human", note="approve the deploy?")

    created = _first(records, "handoff.created")["handoff"]
    assert created["reason"] == "needs a human"
    assert created["ref"] == attach_ref("sess-1", created["handoff_id"])
    assert created["attach_command"].startswith("amplifier-tui serve --attach")
    assert created["claimed"] is False
    # The pen is down: the pauser's own lease is gone and writes are refused.
    assert control.active_lease() is None
    denied = control.authorize("submit", {"lease": lease})
    assert denied.reason == REASON_SESSION_PAUSED
    assert control.paused() is True


def test_claiming_a_handoff_attaches_the_human_and_grants_the_lease(
    control: SessionControl,
) -> None:
    control.acquire(BOT)
    handoff_id = _first(control.pause(BOT, reason="escalate"), "handoff.created")["handoff"][
        "handoff_id"
    ]

    records = control.claim_handoff(handoff_id, MJ)
    claimed = _first(records, "handoff.claimed")["handoff"]
    assert claimed["claimed"] is True and claimed["claimed_by"]["id"] == "mj"
    assert _first(records, "lease.state")["lease"]["actor"]["id"] == "mj"
    assert control.paused() is False
    assert control.authorize("submit", {"lease": control.active_lease().lease_id}).allowed  # type: ignore[union-attr]


def test_a_handoff_is_one_shot_and_unknown_refs_conflict(control: SessionControl) -> None:
    handoff_id = _first(control.pause(BOT), "handoff.created")["handoff"]["handoff_id"]
    control.claim_handoff(handoff_id, MJ)

    second = _first(control.claim_handoff(handoff_id, OTHER_HUMAN), "control.conflict")
    assert second["reason"] == REASON_HANDOFF_CLAIMED
    unknown = _first(control.claim_handoff("ho-nope", MJ), "control.conflict")
    assert unknown["reason"] == REASON_UNKNOWN_HANDOFF


def test_resume_lifts_a_pause_without_a_handoff(control: SessionControl) -> None:
    control.pause(BOT, reason="wait")
    control.resume(MJ)
    assert control.paused() is False
    assert control.authorize("submit", {}).allowed


# -- idempotency -------------------------------------------------------------


def test_idempotent_replay_survives_a_new_process(tmp_path: Path, clock: _Clock) -> None:
    """The retry a dropped connection provokes must not act twice -- even when
    the client reconnects into a brand new process."""
    first = SessionControl(tmp_path / "s", "s", now=clock)
    records = first.acquire(BOT)
    first.remember("idem-1", records)

    reconnected = SessionControl(tmp_path / "s", "s", now=clock)
    replayed = reconnected.replay("idem-1")
    assert replayed is not None
    assert all(record["replay"] is True for record in replayed)
    assert _first(replayed, "lease.state")["lease"]["actor"]["id"] == "bot-1"
    assert reconnected.replay("never-seen") is None


def test_idempotency_ring_is_bounded(control: SessionControl) -> None:
    from amplifier_app_tui.kernel.session_control import MAX_IDEMPOTENCY_ENTRIES

    for index in range(MAX_IDEMPOTENCY_ENTRIES + 5):
        control.remember(f"k{index}", [{"type": "control.ack", "n": index}])
    assert control.replay("k0") is None  # evicted
    assert control.replay(f"k{MAX_IDEMPOTENCY_ENTRIES + 4}") is not None


# -- audit trail -------------------------------------------------------------


def test_audit_trail_records_every_actor_and_action(
    control: SessionControl, tmp_path: Path
) -> None:
    control.acquire(BOT)
    control.authorize("submit", {"lease": control.active_lease().lease_id})  # type: ignore[union-attr]
    control.pause(BOT, reason="escalate")
    handoff_id = control.handoffs()[0].handoff_id
    control.claim_handoff(handoff_id, MJ)
    control.authorize("submit", {"actor": OTHER_BOT.as_dict()})  # rejected: mj holds it

    path = tmp_path / "sess-1" / AUDIT_FILENAME
    entries = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    assert [e["action"] for e in entries] == [
        "lease.granted",
        "write.accepted",
        "lease.released",
        "session.paused",
        "handoff.created",
        "handoff.claimed",
        "write.rejected",
    ]
    assert [e["seq"] for e in entries] == list(range(1, len(entries) + 1))
    assert [e["actor"]["id"] for e in entries] == [
        "bot-1",
        "bot-1",
        "bot-1",
        "bot-1",
        "bot-1",
        "mj",
        "bot-2",
    ]
    assert entries[-1]["detail"]["why"] == REASON_LEASE_HELD
    # Every entry names the session and the durable handle it happened under.
    assert {e["handle_id"] for e in entries} == {control.handle.handle_id}


def test_audit_query_record_is_bounded_and_newest_last(control: SessionControl) -> None:
    for _ in range(5):
        control.authorize("submit", {})
    record = control.audit_record(limit=3)
    assert record["type"] == "audit.list"
    assert len(record["entries"]) == 3
    assert record["entries"][-1]["seq"] == 5


# -- actor parsing -----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("bot", Actor(id="bot", kind=AUTOMATION)),
        ({"id": "mj", "kind": "human"}, Actor(id="mj", kind=HUMAN)),
        ({"id": "x", "kind": "martian"}, Actor(id="x", kind="unknown")),
        ({"kind": "human"}, None),
        ("", None),
        (None, None),
        (7, None),
    ],
)
def test_actor_parse(raw: object, expected: Actor | None) -> None:
    assert Actor.parse(raw) == expected
