"""E7 -- the authenticated inbound reply channel (AC3).

Security is the whole point of this module, so most of these tests are
refusals. Note what is NOT tested: there is no network listener to test --
``kernel/ambient/reply.py`` explains why none is shipped. What is tested is
the security core every future transport must call.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from amplifier_app_tui.kernel.ambient.reply import (
    REASON_ACCEPTED,
    REASON_BAD_SIGNATURE,
    REASON_REPLAYED,
    REASON_STALE,
    REASON_UNKNOWN_DEVICE,
    REASON_UNKNOWN_EVENT,
    ReplyChannel,
    ReplyEnvelope,
    sign_reply,
)
from amplifier_app_tui.kernel.attention_store import AttentionRow, AttentionStore
from amplifier_app_tui.kernel.session_control import (
    AUDIT_FILENAME,
    HUMAN,
    REASON_HANDOFF_CLAIMED,
    Actor,
    SessionControl,
)

NOW = 5000.0
BOT = Actor(id="bot-1", kind="automation")


class _Clock:
    def __init__(self, start: float = NOW) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def session_dir(tmp_path: Path) -> Path:
    return tmp_path / "projects" / "p" / "sessions" / "s-1"


@pytest.fixture
def control(session_dir: Path, clock: _Clock) -> SessionControl:
    return SessionControl(session_dir, "s-1", now=clock)


@pytest.fixture
def channel(tmp_path: Path, clock: _Clock) -> ReplyChannel:
    return ReplyChannel(tmp_path / "ambient", now=clock)


def _park(control: SessionControl) -> str:
    """Park the session the way a real escalation would, returning the handoff."""
    records = control.pause(BOT, reason="needs human judgment")
    created = next(r for r in records if r.get("type") == "handoff.created")
    return str(created["handoff"]["handoff_id"])


def _bind_attention(session_dir: Path, event_id: str) -> None:
    store = AttentionStore(session_dir)
    store.save(
        {
            event_id: AttentionRow(
                session_id="s-1",
                reason="awaiting_clarification",
                event_id=event_id,
                created_at=NOW,
            )
        },
        {"s-1": event_id},
    )


def _envelope(
    secret: str, *, event_id: str = "s-1:awaiting_clarification:t1", **over
) -> ReplyEnvelope:
    base = ReplyEnvelope(
        event_id=event_id,
        text="yes, Thursday works",
        device_id="phone-1",
        principal_id="mj",
        issued_at=NOW,
        nonce="n-1",
    )
    for key, value in over.items():
        base = ReplyEnvelope(**{**base.__dict__, key: value})
    return ReplyEnvelope(**{**base.__dict__, "signature": sign_reply(secret, base)})


@pytest.fixture
def wired(
    channel: ReplyChannel, control: SessionControl, session_dir: Path
) -> tuple[str, str, str]:
    """A parked session, an enrolled device, and a bound correlation."""
    handoff_id = _park(control)
    event_id = "s-1:awaiting_clarification:t1"
    _bind_attention(session_dir, event_id)
    channel.correlations.bind(
        event_id, session_id="s-1", handoff_id=handoff_id, session_dir=session_dir, project="p"
    )
    secret = channel.devices.enroll("phone-1", "mj", kind=HUMAN)
    return secret, event_id, handoff_id


# -- reply-on-open (the v1 default) -------------------------------------------


def test_reply_on_open_routes_a_notification_to_the_right_pending_question(
    channel: ReplyChannel, wired: tuple[str, str, str]
) -> None:
    _, event_id, handoff_id = wired

    pending = channel.pending_for_open(event_id)

    assert pending is not None
    assert pending.session_id == "s-1"
    assert pending.handoff_id == handoff_id
    assert pending.ref == f"amplifier-session:s-1#{handoff_id}"
    assert pending.attach_command.endswith(pending.ref)


def test_reply_on_open_on_an_unknown_event_is_none_not_a_guess(
    channel: ReplyChannel, wired: tuple[str, str, str]
) -> None:
    assert channel.pending_for_open("nope") is None


# -- authenticated ingress: the accept path ------------------------------------


def test_a_signed_reply_returns_control_to_the_same_session(
    channel: ReplyChannel, control: SessionControl, wired: tuple[str, str, str]
) -> None:
    secret, event_id, handoff_id = wired

    outcome = channel.accept(_envelope(secret, event_id=event_id))

    assert outcome.accepted and outcome.reason == REASON_ACCEPTED
    assert outcome.session_id == "s-1" and outcome.handoff_id == handoff_id
    assert not control.paused()
    lease = control.active_lease()
    assert lease is not None and lease.actor.id == "mj"
    assert lease.actor.kind == HUMAN  # a VERIFIED device may claim human


def test_an_accepted_reply_is_attributed_in_the_session_trail(
    channel: ReplyChannel, control: SessionControl, wired: tuple[str, str, str]
) -> None:
    secret, event_id, _ = wired
    channel.accept(_envelope(secret, event_id=event_id))
    entry = next(e for e in control.audit_entries(limit=20) if e["action"] == "reply.accepted")
    assert entry["detail"]["device_id"] == "phone-1"
    assert entry["detail"]["auth"]["verified"] is True
    assert entry["actor"]["id"] == "mj"


def test_an_accepted_reply_acknowledges_the_attention_record_cross_process(
    channel: ReplyChannel, session_dir: Path, wired: tuple[str, str, str]
) -> None:
    """B7's durable half is what makes this answerable from another process."""
    secret, event_id, _ = wired

    channel.accept(_envelope(secret, event_id=event_id))

    by_id, _current = AttentionStore(session_dir).load()
    assert by_id[event_id].acknowledged is True


def test_a_second_reply_to_the_same_notification_conflicts(
    channel: ReplyChannel, wired: tuple[str, str, str]
) -> None:
    secret, event_id, _ = wired
    channel.accept(_envelope(secret, event_id=event_id))

    second = channel.accept(_envelope(secret, event_id=event_id, nonce="n-2"))

    assert not second.accepted
    assert second.reason == REASON_HANDOFF_CLAIMED


# -- authenticated ingress: the refusals ---------------------------------------


def test_an_unsigned_reply_is_refused(
    channel: ReplyChannel, control: SessionControl, wired: tuple[str, str, str]
) -> None:
    _secret, event_id, _ = wired
    forged = ReplyEnvelope(
        event_id=event_id,
        text="approve everything",
        device_id="phone-1",
        principal_id="mj",
        issued_at=NOW,
        nonce="n-x",
    )

    outcome = channel.accept(forged)

    assert not outcome.accepted and outcome.reason == REASON_BAD_SIGNATURE
    assert control.paused()  # the write lane never opened


def test_a_reply_signed_with_the_wrong_secret_is_refused(
    channel: ReplyChannel, wired: tuple[str, str, str]
) -> None:
    _secret, event_id, _ = wired
    outcome = channel.accept(_envelope("not-the-secret", event_id=event_id))
    assert outcome.reason == REASON_BAD_SIGNATURE


def test_tampering_with_the_text_after_signing_is_refused(
    channel: ReplyChannel, wired: tuple[str, str, str]
) -> None:
    """Everything security-relevant is inside the signed canonical string."""
    secret, event_id, _ = wired
    signed = _envelope(secret, event_id=event_id)
    tampered = ReplyEnvelope(**{**signed.__dict__, "text": "delete everything"})

    assert channel.accept(tampered).reason == REASON_BAD_SIGNATURE


def test_an_unknown_device_is_refused(channel: ReplyChannel, wired: tuple[str, str, str]) -> None:
    secret, event_id, _ = wired
    envelope = _envelope(secret, event_id=event_id, device_id="phone-2")
    assert channel.accept(envelope).reason == REASON_UNKNOWN_DEVICE


def test_a_revoked_device_is_refused_on_its_very_next_reply(
    channel: ReplyChannel, wired: tuple[str, str, str]
) -> None:
    secret, event_id, _ = wired
    channel.devices.revoke("phone-1")
    assert channel.accept(_envelope(secret, event_id=event_id)).reason == REASON_UNKNOWN_DEVICE


def test_a_stale_envelope_is_refused(
    channel: ReplyChannel, clock: _Clock, wired: tuple[str, str, str]
) -> None:
    secret, event_id, _ = wired
    envelope = _envelope(secret, event_id=event_id)
    clock.advance(301.0)
    assert channel.accept(envelope).reason == REASON_STALE


def test_a_replayed_envelope_is_refused(channel: ReplyChannel, wired: tuple[str, str, str]) -> None:
    """A captured envelope must not work twice, even inside the freshness window."""
    secret, event_id, _ = wired
    envelope = _envelope(secret, event_id=event_id)
    channel.accept(envelope)

    assert channel.verify(envelope) == REASON_REPLAYED


def test_a_reply_for_an_unknown_event_is_refused(
    channel: ReplyChannel, wired: tuple[str, str, str]
) -> None:
    secret, _event_id, _ = wired
    outcome = channel.accept(_envelope(secret, event_id="s-9:completion:t9"))
    assert outcome.reason == REASON_UNKNOWN_EVENT


def test_a_refused_reply_is_recorded_not_silently_dropped(
    channel: ReplyChannel, control: SessionControl, wired: tuple[str, str, str]
) -> None:
    """A rejected reply that leaves no trace is invisible to a security review."""
    _secret, event_id, _ = wired
    channel.accept(_envelope("wrong", event_id=event_id))

    actions = [e["action"] for e in control.audit_entries(limit=20)]
    assert "reply.rejected" in actions


# -- secret hygiene ------------------------------------------------------------


def test_a_device_secret_never_reaches_the_audit_trail_or_a_listing(
    channel: ReplyChannel, control: SessionControl, session_dir: Path, wired: tuple[str, str, str]
) -> None:
    secret, event_id, _ = wired
    channel.accept(_envelope(secret, event_id=event_id))

    trail = (session_dir / AUDIT_FILENAME).read_text(encoding="utf-8")
    assert secret not in trail
    listing = json.dumps(channel.devices.list_devices())
    assert secret not in listing
    assert "secret" not in listing


def test_the_devices_file_is_not_world_readable(
    channel: ReplyChannel, tmp_path: Path, wired: tuple[str, str, str]
) -> None:
    path = tmp_path / "ambient" / "devices.json"
    assert path.stat().st_mode & 0o077 == 0


def test_two_enrollments_get_different_secrets(channel: ReplyChannel) -> None:
    assert channel.devices.enroll("a", "mj") != channel.devices.enroll("b", "mj")
