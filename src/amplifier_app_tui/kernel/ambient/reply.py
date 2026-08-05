"""E7 -- the authenticated inbound reply channel (AC3).

> **AC3** -- a mobile notification or quick reply can answer a pending
> clarification and return control to the same session.

The correlation binds keys that already exist; neither is re-invented here:

- **Correlation key: B7's ``event_id``** -- stable, derived from
  ``(session_id, reason, occasion)``, idempotent by construction, so a
  re-render or a reconnect cannot mint a second identity for the same
  question.
- **Question key: the ``NeedsYouQueue`` decision id** -- the exact pending
  clarification the TUI and serve protocol already answer.
- **Blocking re-entry key (when present): B6's handoff ref** --
  ``amplifier-session:<sid>#<handoff>``, whose ``claim`` clears the pause and
  grants the lease in one step. Auto-mode clarifications keep working and do
  not invent a pause merely to manufacture a handoff.

What this module adds is the **authentication** between them.

-- What is NOT built, and why ------------------------------------------------

**No remotely reachable listener ships here.** A reachable HTTPS ingress needs
a TLS identity, a deployment story and an operational owner; none of that is
available locally, and silently opening a LAN/public write path would be worse
than shipping none.  The local integration seam *is* executable: this module
includes a loopback-only HTTP listener whose request body is the same signed
envelope :meth:`ReplyChannel.accept` verifies.  It cannot bind a non-loopback
address and it is never auto-started by importing the package.

- **Built and tested here:** the security core -- envelope authentication
  (HMAC-SHA256 over a canonical string, constant-time compare), replay
  rejection (durable nonce + freshness window), correlation ``event_id`` ->
  session -> decision/handoff, re-entry via ``handoff.claim``, submission to an
  explicit :class:`ReplySubmissionPort`, and attention acknowledgement only
  after that submission succeeds. :meth:`ReplyChannel.accept` remains
  transport-agnostic: the loopback listener and a future HTTPS ingress call the
  *same* method, so the security/order core does not move.
- **v1 default: reply-on-open.** :meth:`ReplyChannel.pending_for_open`
  resolves a notification's ``event_id`` to the exact session, pending
  decision, and optional handoff, with a runnable attach command. That delivers
  "the notification takes you to the right pending question in the right
  session" -- the whole correlation value -- with **zero** new network
  surface. It is not *quick* reply; it is *one-tap-to-the-right-place* reply,
  and this says so rather than claiming AC3 in full.

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
import ipaddress
import json
import logging
import os
import secrets
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Protocol

from ..attention_store import AttentionStore
from ..file_lock import locked as _file_lock
from ..session_control import Actor, SessionControl, attach_command, attach_ref
from .grants import default_ambient_root
from .principal import LocalPrincipal, PrincipalLike, actor_for, auth_provenance

logger = logging.getLogger(__name__)

DEVICES_FILENAME = "devices.json"
CORRELATIONS_FILENAME = "correlations.json"
DELIVERIES_FILENAME = "reply-deliveries.json"
SCHEMA_VERSION = 1

DEFAULT_FRESHNESS = 300.0
"""How old a signed envelope may be (5 minutes). Short enough that a captured
envelope is stale before it is useful; long enough for phone clock skew."""

MAX_NONCES = 512
"""Bounded replay ring. Larger than B6's idempotency ring because a reply
channel sees bursts; still bounded, because unbounded state is its own bug."""

METHOD_DEVICE_TOKEN = "device-token"
MAX_REPLY_BODY_BYTES = 64 * 1024

# Rejection reasons. Stable strings; deliberately uninformative about secrets.
REASON_ACCEPTED = "accepted"
REASON_UNKNOWN_DEVICE = "unknown_device"
REASON_BAD_SIGNATURE = "bad_signature"
REASON_STALE = "stale"
REASON_REPLAYED = "replayed"
REASON_UNKNOWN_EVENT = "unknown_event"
REASON_NO_HANDOFF = "no_handoff"
REASON_CONFLICT = "control_conflict"
REASON_SUBMISSION_UNAVAILABLE = "submission_unavailable"
REASON_SUBMISSION_FAILED = "submission_failed"
REASON_SESSION_MISMATCH = "session_mismatch"
REASON_UNKNOWN_DECISION = "unknown_decision"


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
class ReplySubmissionResult:
    """Result returned by the session-owned reply submission port."""

    accepted: bool
    reason: str = REASON_ACCEPTED
    decision_id: str = ""


class ReplySubmissionPort(Protocol):
    """The only write seam from an authenticated reply into a live session.

    The channel passes ``text`` through verbatim.  Implementations own the
    session-specific mechanics (today, answering the real ``NeedsYouQueue``;
    later, a process bridge).  Returning a result instead of raising keeps a
    failed delivery auditable and prevents an HTTP handler from guessing what
    happened inside the session.
    """

    def submit_reply(
        self,
        *,
        session_id: str,
        decision_id: str,
        text: str,
        event_id: str,
        lease_id: str,
    ) -> ReplySubmissionResult: ...


class NeedsYouReplySubmissionPort:
    """Submit replies to the existing pending-question queue.

    ``NeedsYouQueue`` is intentionally duck-typed here so the ambient kernel
    does not acquire a runtime/UI dependency.  Its ``answer`` method is the
    same path used by the TUI and the serve protocol.
    """

    def __init__(self, session_id: str, needs_you: Any) -> None:
        self._session_id = session_id
        self._needs_you = needs_you

    def submit_reply(
        self,
        *,
        session_id: str,
        decision_id: str,
        text: str,
        event_id: str,
        lease_id: str,
    ) -> ReplySubmissionResult:
        del event_id, lease_id
        if session_id != self._session_id:
            return ReplySubmissionResult(False, REASON_SESSION_MISMATCH, decision_id)
        if not decision_id:
            return ReplySubmissionResult(False, REASON_UNKNOWN_DECISION, decision_id)
        try:
            self._needs_you.answer(decision_id, text)
        except (KeyError, ValueError):
            return ReplySubmissionResult(False, REASON_UNKNOWN_DECISION, decision_id)
        return ReplySubmissionResult(True, REASON_ACCEPTED, decision_id)


@dataclass(frozen=True)
class PendingReply:
    """The reply-on-open answer: where a notification should take you."""

    event_id: str
    session_id: str
    project: str
    session_dir: str
    handoff_id: str
    decision_id: str
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
        handoff_id: str = "",
        decision_id: str = "",
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
                "decision_id": decision_id,
                "session_dir": str(session_dir),
                "project": project,
                "bound_at": self._now(),
            }
            self._save(rows)

    def resolve(self, event_id: str) -> dict[str, Any] | None:
        return self._load().get(event_id)

    def bind_clarification(
        self,
        *,
        event_id: str,
        session_id: str,
        decision_id: str,
        session_dir: Path,
        project: str = "",
        handoff_id: str = "",
    ) -> None:
        """Bind a newly-created clarification attention record.

        This is the narrow producer seam for B7's ``AttentionCenter.note``
        boundary: ``event_id`` is the record it just minted and
        ``decision_id`` is the same stable ``occasion`` supplied by the
        question producer.  ``handoff_id`` stays optional because Auto mode
        does not pause; blocking clarification flows may supply it.
        """
        if not event_id or not session_id or not decision_id:
            raise ValueError("a clarification correlation needs event, session, and decision ids")
        self.bind(
            event_id,
            session_id=session_id,
            decision_id=decision_id,
            handoff_id=handoff_id,
            session_dir=session_dir,
            project=project,
        )


class ReplyDeliveryStore:
    """Durable replay ring and content-free delivery outcomes.

    The state lives beside device/correlation state and uses the same locked,
    atomic replacement pattern.  Reply text and signatures are deliberately
    absent: operational evidence needs to know *whether* a nonce/event was
    delivered, never what the user said or how it was authenticated.
    """

    def __init__(self, root: Path | None = None, *, now: Callable[[], float] = time.time) -> None:
        self.root = Path(root) if root is not None else default_ambient_root()
        self._path = self.root / DELIVERIES_FILENAME
        self._now = now

    def _load(self) -> dict[str, Any]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"nonces": [], "deliveries": []}
        if not isinstance(raw, dict):
            return {"nonces": [], "deliveries": []}
        nonces = raw.get("nonces")
        deliveries = raw.get("deliveries")
        return {
            "nonces": [str(value) for value in nonces if value] if isinstance(nonces, list) else [],
            "deliveries": [dict(row) for row in deliveries if isinstance(row, Mapping)]
            if isinstance(deliveries, list)
            else [],
        }

    def _save(self, state: Mapping[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "nonces": list(state.get("nonces", ())),
            "deliveries": list(state.get("deliveries", ())),
        }
        tmp = self._path.with_name(f"{self._path.name}.tmp{os.getpid()}")
        tmp.write_text(json.dumps(payload, default=str), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._path)

    def seen(self, nonce: str) -> bool:
        if not nonce:
            return False
        return nonce in self._load()["nonces"]

    def reserve(self, nonce: str) -> bool:
        """Atomically reserve *nonce*. False means another process won."""
        if not nonce:
            return False
        with _file_lock(self._path):
            state = self._load()
            nonces = list(state["nonces"])
            if nonce in nonces:
                return False
            nonces.append(nonce)
            state["nonces"] = nonces[-MAX_NONCES:]
            self._save(state)
        return True

    def record(
        self,
        *,
        nonce: str,
        event_id: str,
        session_id: str,
        decision_id: str,
        accepted: bool,
        reason: str,
    ) -> None:
        """Persist one bounded, content-free delivery outcome."""
        with _file_lock(self._path):
            state = self._load()
            deliveries = list(state["deliveries"])
            deliveries.append(
                {
                    "nonce": nonce,
                    "event_id": event_id,
                    "session_id": session_id,
                    "decision_id": decision_id,
                    "accepted": accepted,
                    "reason": reason,
                    "recorded_at": self._now(),
                }
            )
            state["deliveries"] = deliveries[-MAX_NONCES:]
            self._save(state)

    def outcomes(self) -> tuple[dict[str, Any], ...]:
        """Content-free rows, newest last; useful for doctor/audit surfaces."""
        return tuple(self._load()["deliveries"])


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
        submitter: ReplySubmissionPort | None = None,
    ) -> None:
        self.root = Path(root) if root is not None else default_ambient_root()
        self.devices = DeviceRegistry(self.root, now=now)
        self.correlations = CorrelationTable(self.root, now=now)
        self.deliveries = ReplyDeliveryStore(self.root, now=now)
        self._now = now
        self._freshness = freshness
        self._control_factory = control_factory or _default_control_factory
        self._submitter = submitter

    # -- reply-on-open (v1 default, zero new network surface) --------------

    def pending_for_open(self, event_id: str) -> PendingReply | None:
        """Where a notification should take the user. No authentication needed.

        Safe without a credential because it grants nothing: it reads the
        user's own correlation table and returns a pointer. Acting on that
        pointer still requires an authenticated first-party surface; a paused
        correlation additionally goes through B6's ``handoff.claim``.
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
            decision_id=str(row.get("decision_id", "")),
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
        if self.deliveries.seen(envelope.nonce):
            return REASON_REPLAYED
        return REASON_ACCEPTED

    def accept(self, envelope: ReplyEnvelope) -> ReplyOutcome:
        """Verify, correlate, claim, submit, then acknowledge -- in that order.

        On success the exact signed ``envelope.text`` has reached the
        session-owned submission port. Only then is the attention record
        acknowledged, so a transport/queue failure never makes an unanswered
        question disappear. If the correlation carries a handoff, its claim
        happens before submission and returns the lease to the authenticated
        principal; non-blocking Auto-mode clarifications carry only a decision
        id and therefore need no synthetic pause/handoff.

        A **second** reply to the same notification conflicts with
        ``handoff_claimed`` rather than double-answering -- B6 already
        guarantees that, and it is surfaced here rather than swallowed.
        """
        reason = self.verify(envelope)
        if reason != REASON_ACCEPTED:
            self._audit_rejection(envelope, reason)
            return ReplyOutcome(False, reason, envelope.event_id)

        row = self.correlations.resolve(envelope.event_id)
        if row is None:
            self._audit_rejection(envelope, REASON_UNKNOWN_EVENT)
            return ReplyOutcome(False, REASON_UNKNOWN_EVENT, envelope.event_id)
        handoff_id = str(row.get("handoff_id", ""))
        decision_id = str(row.get("decision_id", ""))
        session_id = str(row.get("session_id", ""))
        session_dir = Path(str(row.get("session_dir", "")))
        if not handoff_id and not decision_id:
            self._audit_rejection(envelope, REASON_NO_HANDOFF)
            return ReplyOutcome(False, REASON_NO_HANDOFF, envelope.event_id, session_id)
        if self._submitter is None:
            self._audit_rejection(envelope, REASON_SUBMISSION_UNAVAILABLE)
            return ReplyOutcome(
                False, REASON_SUBMISSION_UNAVAILABLE, envelope.event_id, session_id, handoff_id
            )
        # ``verify`` is intentionally read-only; this locked reservation closes
        # the cross-thread/cross-process race where two handlers both verified
        # before either had recorded the nonce.
        if not self.deliveries.reserve(envelope.nonce):
            self._audit_rejection(envelope, REASON_REPLAYED)
            return ReplyOutcome(False, REASON_REPLAYED, envelope.event_id, session_id, handoff_id)

        principal = self.devices.principal_for(envelope.device_id)
        if principal is None:
            self._record_delivery(envelope, session_id, decision_id, False, REASON_UNKNOWN_DEVICE)
            return ReplyOutcome(False, REASON_UNKNOWN_DEVICE, envelope.event_id, session_id)
        control = self._control_factory(session_dir, session_id)
        records: tuple[dict[str, Any], ...] = ()
        lease_id = ""
        if handoff_id:
            records = tuple(control.claim_handoff(handoff_id, actor_for(principal)))
            conflict = next((r for r in records if r.get("type") == "control.conflict"), None)
            if conflict is not None:
                rejected_reason = str(conflict.get("reason", REASON_CONFLICT))
                control.note_ambient(
                    "reply.rejected",
                    actor_for(principal),
                    event_id=envelope.event_id,
                    handoff_id=handoff_id,
                    device_id=envelope.device_id,
                    why=rejected_reason,
                    auth=auth_provenance(principal),
                )
                self._record_delivery(envelope, session_id, decision_id, False, rejected_reason)
                return ReplyOutcome(
                    False,
                    rejected_reason,
                    envelope.event_id,
                    session_id,
                    handoff_id,
                    records=records,
                )
            lease_id = _lease_id_from(records)

        try:
            submitted = self._submitter.submit_reply(
                session_id=session_id,
                decision_id=decision_id,
                text=envelope.text,
                event_id=envelope.event_id,
                lease_id=lease_id,
            )
        except Exception:  # noqa: BLE001 -- a failed port must stay a surfaced refusal
            logger.debug("reply submission port failed", exc_info=True)
            submitted = ReplySubmissionResult(False, REASON_SUBMISSION_FAILED, decision_id)
        if not submitted.accepted:
            rejected_reason = submitted.reason or REASON_SUBMISSION_FAILED
            control.note_ambient(
                "reply.rejected",
                actor_for(principal),
                event_id=envelope.event_id,
                handoff_id=handoff_id,
                device_id=envelope.device_id,
                why=rejected_reason,
                auth=auth_provenance(principal),
            )
            self._record_delivery(envelope, session_id, decision_id, False, rejected_reason)
            return ReplyOutcome(
                False,
                rejected_reason,
                envelope.event_id,
                session_id,
                handoff_id,
                lease_id,
                records=records,
            )

        control.note_ambient(
            "reply.accepted",
            actor_for(principal),
            event_id=envelope.event_id,
            handoff_id=handoff_id,
            decision_id=decision_id,
            device_id=envelope.device_id,
            lease_id=lease_id,
            auth=auth_provenance(principal),
        )
        self._record_delivery(envelope, session_id, decision_id, True, REASON_ACCEPTED)
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

    def _record_delivery(
        self,
        envelope: ReplyEnvelope,
        session_id: str,
        decision_id: str,
        accepted: bool,
        reason: str,
    ) -> None:
        self.deliveries.record(
            nonce=envelope.nonce,
            event_id=envelope.event_id,
            session_id=session_id,
            decision_id=decision_id,
            accepted=accepted,
            reason=reason,
        )

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


class LoopbackReplyListener:
    """Small authenticated HTTP ingress restricted to a loopback address.

    The listener owns no auth, correlation, or session policy: it only parses
    JSON into :class:`ReplyEnvelope` and calls :meth:`ReplyChannel.accept`.
    It is explicit-lifecycle (``start``/``close`` or a context manager), never
    auto-started, and rejects non-loopback bind addresses before opening a
    socket. Runtime ownership, private port discovery, and teardown live in
    :mod:`.reply_listener`; importing this transport still opens nothing. A
    remotely reachable TLS adapter can replace it later without changing reply
    verification or ordering.
    """

    def __init__(
        self,
        channel: ReplyChannel,
        *,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        address = ipaddress.ip_address(host)
        if not address.is_loopback:
            raise ValueError("reply listener may bind only to a loopback address")
        if address.version != 4:
            raise ValueError("reply listener currently requires an IPv4 loopback address")
        self._channel = channel
        self._server = ThreadingHTTPServer((host, int(port)), self._handler_type(channel))
        self._server.daemon_threads = True
        self._thread: threading.Thread | None = None

    @staticmethod
    def _handler_type(channel: ReplyChannel) -> type[BaseHTTPRequestHandler]:
        class Handler(BaseHTTPRequestHandler):
            server_version = "AmplifierReply/1"

            def do_POST(self) -> None:  # noqa: N802 -- stdlib handler API
                if self.path != "/reply":
                    self._json(404, {"accepted": False, "reason": "not_found"})
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = -1
                if length < 1 or length > MAX_REPLY_BODY_BYTES:
                    self._json(413, {"accepted": False, "reason": "invalid_body"})
                    return
                try:
                    raw = json.loads(self.rfile.read(length).decode("utf-8"))
                    if not isinstance(raw, Mapping):
                        raise ValueError("reply body must be an object")
                    envelope = ReplyEnvelope(
                        event_id=str(raw.get("event_id", "")),
                        text=str(raw.get("text", "")),
                        device_id=str(raw.get("device_id", "")),
                        principal_id=str(raw.get("principal_id", "")),
                        issued_at=float(raw.get("issued_at", 0.0)),
                        nonce=str(raw.get("nonce", "")),
                        signature=str(raw.get("signature", "")),
                    )
                except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
                    self._json(400, {"accepted": False, "reason": "invalid_envelope"})
                    return
                outcome = channel.accept(envelope)
                status = 200 if outcome.accepted else 409
                self._json(
                    status,
                    {
                        "accepted": outcome.accepted,
                        "reason": outcome.reason,
                        "event_id": outcome.event_id,
                        "session_id": outcome.session_id,
                        "handoff_id": outcome.handoff_id,
                        "lease_id": outcome.lease_id,
                        "ref": outcome.ref,
                        "attach_command": outcome.attach_command,
                    },
                )

            def _json(self, status: int, payload: Mapping[str, Any]) -> None:
                body = json.dumps(dict(payload)).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                del format, args  # request bodies/auth material must never reach stderr

        return Handler

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    def start(self) -> LoopbackReplyListener:
        if self._thread is not None:
            return self
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="amplifier-reply-listener",
            daemon=True,
        )
        self._thread.start()
        return self

    def close(self) -> None:
        if self._thread is not None:
            self._server.shutdown()
            self._thread.join(timeout=5.0)
            self._thread = None
        self._server.server_close()

    def __enter__(self) -> LoopbackReplyListener:
        return self.start()

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        del exc_type, exc, traceback
        self.close()


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
    "DELIVERIES_FILENAME",
    "DEVICES_FILENAME",
    "LoopbackReplyListener",
    "METHOD_DEVICE_TOKEN",
    "NeedsYouReplySubmissionPort",
    "REASON_ACCEPTED",
    "REASON_BAD_SIGNATURE",
    "REASON_CONFLICT",
    "REASON_NO_HANDOFF",
    "REASON_REPLAYED",
    "REASON_SESSION_MISMATCH",
    "REASON_STALE",
    "REASON_SUBMISSION_FAILED",
    "REASON_SUBMISSION_UNAVAILABLE",
    "REASON_UNKNOWN_DEVICE",
    "REASON_UNKNOWN_DECISION",
    "REASON_UNKNOWN_EVENT",
    "CorrelationTable",
    "DeviceRegistry",
    "PendingReply",
    "ReplyChannel",
    "ReplyDeliveryStore",
    "ReplyEnvelope",
    "ReplyOutcome",
    "ReplySubmissionPort",
    "ReplySubmissionResult",
    "sign_reply",
]
