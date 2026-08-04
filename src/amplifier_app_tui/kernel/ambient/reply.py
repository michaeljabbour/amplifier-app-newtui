"""E7 -- the authenticated inbound reply channel (AC3).

> **AC3** -- a mobile notification or quick reply can answer a pending
> clarification and return control to the same session.

Two keys, both of which already exist and neither of which is re-invented here:

- **Correlation key: B7's ``event_id``** -- stable, derived from
  ``(session_id, reason, occasion)``, idempotent by construction, so a
  re-render or a reconnect cannot mint a second identity for the same
  question.
- **Re-entry key: B6's handoff ref** -- ``amplifier-session:<sid>#<handoff>``,
  whose ``claim`` clears the pause and grants the lease in one step.

What this module adds is the **authentication** between them.

-- What is NOT built, and why ------------------------------------------------

**No network listener ships here, and none can be verified in this
environment.** A reachable HTTPS ingress needs a bound port, a TLS identity,
a deployment story and an operational owner; none of that is testable offline,
and shipping an untested listener into a security-critical path would be worse
than shipping none. So the split is:

- **Built and tested here:** the security core -- envelope authentication
  (HMAC-SHA256 over a canonical string, constant-time compare), replay
  rejection (nonce + freshness window), correlation ``event_id`` -> session ->
  handoff, and re-entry via ``handoff.claim`` + attention acknowledgement.
  :meth:`ReplyChannel.accept` is transport-agnostic on purpose: a real HTTPS
  handler, a Unix-socket daemon or a local CLI all call the *same* method, so
  the transport can be added later without the security core moving.
- **v1 default: reply-on-open.** :meth:`ReplyChannel.pending_for_open`
  resolves a notification's ``event_id`` to the exact session and pending
  handoff, with a runnable attach command. That delivers "the notification
  takes you to the right pending question in the right session" -- the whole
  correlation value -- with **zero** new network surface. It is not *quick*
  reply; it is *one-tap-to-the-right-place* reply, and this says so rather
  than claiming AC3 in full.

**The ntfy reply-topic option stays rejected.** An ntfy topic is a shared
secret and a public topic is world-readable. Subscribing to a reply topic
would make a world-readable channel a write path into a live session. A
world-readable channel must never be a write path -- full stop. That is why
this module requires a per-device secret and a signature, and why a reply that
fails verification is audited (``reply.rejected``) rather than dropped.

-- Secret hygiene -----------------------------------------------------------

Device secrets are generated with :mod:`secrets`, stored ``0600``, and are
**never** logged, echoed, returned in a rejection reason, or written to any
audit entry -- only the ``device_id`` appears. :func:`sign_reply` is the only
function that touches secret material.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..attention_store import AttentionStore
from ..file_lock import locked as _file_lock
from ..session_control import Actor, SessionControl, attach_command, attach_ref
from .grants import default_ambient_root
from .principal import LocalPrincipal, PrincipalLike, actor_for, auth_provenance

logger = logging.getLogger(__name__)

DEVICES_FILENAME = "devices.json"
CORRELATIONS_FILENAME = "correlations.json"
SCHEMA_VERSION = 1

DEFAULT_FRESHNESS = 300.0
"""How old a signed envelope may be (5 minutes). Short enough that a captured
envelope is stale before it is useful; long enough for phone clock skew."""

MAX_NONCES = 512
"""Bounded replay ring. Larger than B6's idempotency ring because a reply
channel sees bursts; still bounded, because unbounded state is its own bug."""

METHOD_DEVICE_TOKEN = "device-token"

# Rejection reasons. Stable strings; deliberately uninformative about secrets.
REASON_ACCEPTED = "accepted"
REASON_UNKNOWN_DEVICE = "unknown_device"
REASON_BAD_SIGNATURE = "bad_signature"
REASON_STALE = "stale"
REASON_REPLAYED = "replayed"
REASON_UNKNOWN_EVENT = "unknown_event"
REASON_NO_HANDOFF = "no_handoff"
REASON_CONFLICT = "control_conflict"


@dataclass(frozen=True)
class ReplyEnvelope:
    """One authenticated inbound reply, independent of any transport."""

    event_id: str
    text: str
    device_id: str
    principal_id: str
    issued_at: float
    nonce: str
    signature: str = ""

    def signing_payload(self) -> str:
        """The canonical string that is signed.

        Every security-relevant field is included and the separator (``\\n``)
        cannot appear in an id, so two different envelopes cannot canonicalize
        to the same string -- the classic signature-confusion bug.
        """
        return "\n".join(
            [
                str(SCHEMA_VERSION),
                self.event_id,
                self.device_id,
                self.principal_id,
                f"{self.issued_at:.6f}",
                self.nonce,
                self.text,
            ]
        )


@dataclass(frozen=True)
class ReplyOutcome:
    """What the channel did with a reply. ``reason`` is always populated."""

    accepted: bool
    reason: str
    event_id: str = ""
    session_id: str = ""
    handoff_id: str = ""
    lease_id: str = ""
    ref: str = ""
    attach_command: str = ""
    records: tuple[dict[str, Any], ...] = field(default=())


@dataclass(frozen=True)
class PendingReply:
    """The reply-on-open answer: where a notification should take you."""

    event_id: str
    session_id: str
    project: str
    session_dir: str
    handoff_id: str
    ref: str
    attach_command: str


def sign_reply(secret: str, envelope: ReplyEnvelope) -> str:
    """HMAC-SHA256 of the canonical payload. The only function using a secret."""
    return hmac.new(
        secret.encode("utf-8"), envelope.signing_payload().encode("utf-8"), hashlib.sha256
    ).hexdigest()


class DeviceRegistry:
    """Per-device shared secrets for the reply channel.

    First-party minting only, by construction: :meth:`enroll` is a local call
    against the user's own ``~/.amplifier`` -- there is no remote enrollment
    path, because a channel that can enrol its own device is not an
    authentication boundary.
    """

    def __init__(self, root: Path | None = None, *, now: Callable[[], float] = time.time) -> None:
        self.root = Path(root) if root is not None else default_ambient_root()
        self._path = self.root / DEVICES_FILENAME
        self._now = now

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        rows = raw.get("devices") if isinstance(raw, dict) else None
        if not isinstance(rows, dict):
            return {}
        return {str(k): dict(v) for k, v in rows.items() if isinstance(v, Mapping)}

    def _save(self, devices: Mapping[str, Mapping[str, Any]]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {"schema_version": SCHEMA_VERSION, "devices": dict(devices)}
        tmp = self._path.with_name(f"{self._path.name}.tmp{os.getpid()}")
        tmp.write_text(json.dumps(payload, default=str), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._path)

    def enroll(self, device_id: str, principal_id: str, *, kind: str = "human") -> str:
        """Register a device and return its secret **once**.

        The secret is returned here and never again: the caller transfers it to
        the device out of band. Nothing else in this package ever reads it back
        out to a caller.
        """
        if not device_id.strip() or not principal_id.strip():
            raise ValueError("a device enrollment needs a device id and a principal id")
        secret = secrets.token_urlsafe(32)
        with _file_lock(self._path):
            devices = self._load()
            devices[device_id] = {
                "device_id": device_id,
                "principal_id": principal_id,
                "kind": kind,
                "secret": secret,
                "enrolled_at": self._now(),
                "revoked_at": None,
            }
            self._save(devices)
        return secret

    def revoke(self, device_id: str) -> bool:
        with _file_lock(self._path):
            devices = self._load()
            row = devices.get(device_id)
            if row is None or row.get("revoked_at") is not None:
                return False
            row["revoked_at"] = self._now()
            row["secret"] = ""  # the secret is destroyed, not merely flagged
            devices[device_id] = row
            self._save(devices)
        return True

    def principal_for(self, device_id: str) -> PrincipalLike | None:
        """The **verified** principal a live device authenticates as."""
        row = self._load().get(device_id)
        if row is None or row.get("revoked_at") is not None:
            return None
        return LocalPrincipal(
            principal_id=str(row.get("principal_id", "")),
            kind=str(row.get("kind", "human")),
            method=METHOD_DEVICE_TOKEN,
            verified=True,
        )

    def _secret_for(self, device_id: str) -> str:
        row = self._load().get(device_id)
        if row is None or row.get("revoked_at") is not None:
            return ""
        return str(row.get("secret", ""))

    def list_devices(self) -> list[dict[str, Any]]:
        """Device metadata with secrets stripped -- safe to display or log."""
        return [{k: v for k, v in row.items() if k != "secret"} for row in self._load().values()]


class CorrelationTable:
    """Durable ``event_id -> (session, handoff)`` bindings, per user.

    Lives in the ambient layer rather than in either contract, because it is
    exactly the memory the design doc forbids an adapter from holding: a phone
    that taps a notification knows only an ``event_id`` and must not have to
    know which project the session lives in.
    """

    def __init__(self, root: Path | None = None, *, now: Callable[[], float] = time.time) -> None:
        self.root = Path(root) if root is not None else default_ambient_root()
        self._path = self.root / CORRELATIONS_FILENAME
        self._now = now

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        rows = raw.get("correlations") if isinstance(raw, dict) else None
        if not isinstance(rows, dict):
            return {}
        return {str(k): dict(v) for k, v in rows.items() if isinstance(v, Mapping)}

    def _save(self, rows: Mapping[str, Mapping[str, Any]]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {"schema_version": SCHEMA_VERSION, "correlations": dict(rows)}
        tmp = self._path.with_name(f"{self._path.name}.tmp{os.getpid()}")
        tmp.write_text(json.dumps(payload, default=str), encoding="utf-8")
        os.replace(tmp, self._path)

    def bind(
        self,
        event_id: str,
        *,
        session_id: str,
        handoff_id: str,
        session_dir: Path,
        project: str = "",
    ) -> None:
        if not event_id:
            return
        with _file_lock(self._path):
            rows = self._load()
            rows[event_id] = {
                "event_id": event_id,
                "session_id": session_id,
                "handoff_id": handoff_id,
                "session_dir": str(session_dir),
                "project": project,
                "bound_at": self._now(),
            }
            self._save(rows)

    def resolve(self, event_id: str) -> dict[str, Any] | None:
        return self._load().get(event_id)


class ReplyChannel:
    """Authenticate an inbound reply, then re-enter the session it belongs to.

    Transport-agnostic: :meth:`accept` takes an already-parsed
    :class:`ReplyEnvelope`, so an HTTPS handler, a Unix-socket daemon, or a
    local CLI all reuse one verification path (module docstring).
    """

    def __init__(
        self,
        root: Path | None = None,
        *,
        now: Callable[[], float] = time.time,
        freshness: float = DEFAULT_FRESHNESS,
        control_factory: Callable[[Path, str], SessionControl] | None = None,
    ) -> None:
        self.root = Path(root) if root is not None else default_ambient_root()
        self.devices = DeviceRegistry(self.root, now=now)
        self.correlations = CorrelationTable(self.root, now=now)
        self._now = now
        self._freshness = freshness
        self._nonces: list[str] = []
        self._control_factory = control_factory or _default_control_factory

    # -- reply-on-open (v1 default, zero new network surface) --------------

    def pending_for_open(self, event_id: str) -> PendingReply | None:
        """Where a notification should take the user. No authentication needed.

        Safe without a credential because it grants nothing: it reads the
        user's own correlation table and returns a pointer. Acting on that
        pointer still goes through B6's ``handoff.claim`` on an authenticated
        first-party surface.
        """
        row = self.correlations.resolve(event_id)
        if row is None:
            return None
        session_id = str(row.get("session_id", ""))
        handoff_id = str(row.get("handoff_id", ""))
        return PendingReply(
            event_id=event_id,
            session_id=session_id,
            project=str(row.get("project", "")),
            session_dir=str(row.get("session_dir", "")),
            handoff_id=handoff_id,
            ref=attach_ref(session_id, handoff_id or None),
            attach_command=attach_command(session_id, handoff_id or None),
        )

    # -- authenticated ingress --------------------------------------------

    def verify(self, envelope: ReplyEnvelope) -> str:
        """Authenticate an envelope. Returns :data:`REASON_ACCEPTED` or a reason.

        Order matters: identity, then signature, then freshness, then replay.
        Every failure returns a bare reason string that reveals nothing about
        the secret or which check the attacker got closest to passing.
        """
        secret = self.devices._secret_for(envelope.device_id)  # noqa: SLF001 -- same module
        if not secret:
            return REASON_UNKNOWN_DEVICE
        expected = sign_reply(secret, envelope)
        if not hmac.compare_digest(expected, envelope.signature or ""):
            return REASON_BAD_SIGNATURE
        age = self._now() - envelope.issued_at
        if abs(age) > self._freshness:
            return REASON_STALE
        if envelope.nonce in self._nonces:
            return REASON_REPLAYED
        return REASON_ACCEPTED

    def accept(self, envelope: ReplyEnvelope) -> ReplyOutcome:
        """Verify, correlate, and hand the lease back to the replying human.

        On success the session is no longer paused, the lease belongs to the
        authenticated principal, the reply is attributed in
        ``control-audit.jsonl``, and the attention record is acknowledged so a
        second device stops showing the same question.

        A **second** reply to the same notification conflicts with
        ``handoff_claimed`` rather than double-answering -- B6 already
        guarantees that, and it is surfaced here rather than swallowed.
        """
        reason = self.verify(envelope)
        if reason != REASON_ACCEPTED:
            self._audit_rejection(envelope, reason)
            return ReplyOutcome(False, reason, envelope.event_id)
        self._remember_nonce(envelope.nonce)

        row = self.correlations.resolve(envelope.event_id)
        if row is None:
            self._audit_rejection(envelope, REASON_UNKNOWN_EVENT)
            return ReplyOutcome(False, REASON_UNKNOWN_EVENT, envelope.event_id)
        handoff_id = str(row.get("handoff_id", ""))
        session_id = str(row.get("session_id", ""))
        session_dir = Path(str(row.get("session_dir", "")))
        if not handoff_id:
            return ReplyOutcome(False, REASON_NO_HANDOFF, envelope.event_id, session_id)

        principal = self.devices.principal_for(envelope.device_id)
        if principal is None:
            return ReplyOutcome(False, REASON_UNKNOWN_DEVICE, envelope.event_id, session_id)
        control = self._control_factory(session_dir, session_id)
        records = tuple(control.claim_handoff(handoff_id, actor_for(principal)))
        conflict = next((r for r in records if r.get("type") == "control.conflict"), None)
        if conflict is not None:
            control.note_ambient(
                "reply.rejected",
                actor_for(principal),
                event_id=envelope.event_id,
                handoff_id=handoff_id,
                device_id=envelope.device_id,
                why=str(conflict.get("reason", REASON_CONFLICT)),
                auth=auth_provenance(principal),
            )
            return ReplyOutcome(
                False,
                str(conflict.get("reason", REASON_CONFLICT)),
                envelope.event_id,
                session_id,
                handoff_id,
                records=records,
            )
        lease_id = _lease_id_from(records)
        control.note_ambient(
            "reply.accepted",
            actor_for(principal),
            event_id=envelope.event_id,
            handoff_id=handoff_id,
            device_id=envelope.device_id,
            lease_id=lease_id,
            auth=auth_provenance(principal),
        )
        _acknowledge_attention(session_dir, session_id, envelope.event_id)
        return ReplyOutcome(
            True,
            REASON_ACCEPTED,
            envelope.event_id,
            session_id,
            handoff_id,
            lease_id,
            attach_ref(session_id, handoff_id),
            attach_command(session_id, handoff_id),
            records,
        )

    # -- internals ---------------------------------------------------------

    def _remember_nonce(self, nonce: str) -> None:
        self._nonces.append(nonce)
        if len(self._nonces) > MAX_NONCES:
            del self._nonces[: len(self._nonces) - MAX_NONCES]

    def _audit_rejection(self, envelope: ReplyEnvelope, reason: str) -> None:
        """Record a refused reply against the session, when we know which one.

        A rejected reply that leaves no trace is indistinguishable from a
        reply that was never sent -- and the difference is exactly what a
        security review needs to see. Note the deliberate asymmetry with B6's
        "rejections are not remembered" idempotency rule: that is about not
        *replaying* a refusal, not about failing to record it.
        """
        row = self.correlations.resolve(envelope.event_id)
        if row is None:
            logger.debug("reply rejected (%s) for an unknown event", reason)
            return
        try:
            control = self._control_factory(
                Path(str(row.get("session_dir", ""))), str(row.get("session_id", ""))
            )
            control.note_ambient(
                "reply.rejected",
                Actor(id=envelope.principal_id or "unknown", kind="unknown"),
                event_id=envelope.event_id,
                device_id=envelope.device_id,
                why=reason,
            )
        except OSError:
            logger.debug("reply rejection audit failed (non-fatal)", exc_info=True)


def _default_control_factory(session_dir: Path, session_id: str) -> SessionControl:
    return SessionControl(session_dir, session_id)


def _lease_id_from(records: Sequence[Mapping[str, Any]]) -> str:
    for record in records:
        lease = record.get("lease")
        if isinstance(lease, Mapping):
            return str(lease.get("lease_id", ""))
    return ""


def _acknowledge_attention(session_dir: Path, session_id: str, event_id: str) -> None:
    """Mark the answered attention record acknowledged, cross-process.

    Writes through :class:`kernel.attention_store.AttentionStore` -- B7's
    durable half -- so a reply that arrives in a *different* process than the
    TUI still clears the "needs you" state everywhere. Best-effort: an
    acknowledgement that fails to persist must never undo a reply that
    succeeded.
    """
    store = AttentionStore(session_dir)
    by_id, current = store.load()
    row = by_id.get(event_id)
    if row is None or row.acknowledged:
        return
    by_id[event_id] = type(row)(
        session_id=row.session_id,
        reason=row.reason,
        event_id=row.event_id,
        detail=row.detail,
        created_at=row.created_at,
        acknowledged=True,
    )
    current.setdefault(session_id, event_id)
    store.save(by_id, current)


__all__ = [
    "CORRELATIONS_FILENAME",
    "DEFAULT_FRESHNESS",
    "DEVICES_FILENAME",
    "METHOD_DEVICE_TOKEN",
    "REASON_ACCEPTED",
    "REASON_BAD_SIGNATURE",
    "REASON_CONFLICT",
    "REASON_NO_HANDOFF",
    "REASON_REPLAYED",
    "REASON_STALE",
    "REASON_UNKNOWN_DEVICE",
    "REASON_UNKNOWN_EVENT",
    "CorrelationTable",
    "DeviceRegistry",
    "PendingReply",
    "ReplyChannel",
    "ReplyEnvelope",
    "ReplyOutcome",
    "sign_reply",
]
