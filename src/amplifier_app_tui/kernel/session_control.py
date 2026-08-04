"""Session control plane: durable handle, single-writer lease, takeover, audit.

``kernel/serve.py`` externalizes a session as a bidirectional JSONL protocol.
That contract answers *what* can be driven (submit / steer / approve / ...) but
says nothing about **who** may drive it. This module is the missing half: the
ownership semantics that let an automated controller and a human share ONE
live session without stepping on each other.

Five guarantees, all durable on disk so they survive a dropped connection, a
process restart, and a second client attaching from elsewhere:

1. **Session handle** (:class:`SessionHandle`) -- a stable id for a live
   session plus an ``attach_ref`` any process can re-open or attach to.
   Minted once per session directory and re-read thereafter, so a reconnect
   observes the SAME handle it left.
2. **Single-writer lease** (:class:`Lease`) -- at most one holder may submit
   input. Acquire -> heartbeat -> release, with a TTL so a holder that dies
   silently cannot lock the session forever (the AC5 backstop). Every grant
   bumps ``epoch`` and mints a fresh ``lease_id``, so a stale holder's writes
   are rejected rather than silently interleaved.
3. **Deterministic takeover** -- a *human* actor may always take the lease from
   an *automation* holder; automation may NEVER take it from a human; a
   same-precedence takeover requires an explicit ``force`` (and only a human
   may force). No timing races, no "last writer wins".
4. **Actor attribution + audit trail** -- every mutating control message names
   an :class:`Actor`; every grant, denial, takeover, pause, handoff, accepted
   write and rejected write appends an entry to ``control-audit.jsonl``.
5. **Idempotency** -- a control op may carry an ``idem`` key; the response
   records are remembered durably, so a retry after a dropped connection
   replays the original answer instead of double-submitting.

Layering (ADR-0007): pure ``kernel/`` logic over the filesystem -- no Textual,
no amplifier-core, no runtime import. Every method is a plain call over a
session directory, so the whole state machine unit-tests against ``tmp_path``
with an injected clock. :mod:`amplifier_app_tui.kernel.serve` is one adapter
over it; a Rust client, a phone bridge (item B8) or a tmux attachment are
others.

**Opt-in by design.** ``serve`` materializes the control plane only when a
client actually uses it (a ``lease.*`` / ``session.*`` / ``handoff.*`` op, or
any op carrying ``actor`` / ``lease`` / ``idem``). A client that never sends
those sees the byte-identical legacy protocol and writes no control files.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from .file_lock import locked as _file_lock

CONTROL_FILENAME = "control.json"
"""Durable control state (handle, lease, pause flag, handoffs, idem keys)."""

AUDIT_FILENAME = "control-audit.jsonl"
"""Append-only attribution trail, one JSON object per line."""

SCHEMA_VERSION = 1

DEFAULT_LEASE_TTL = 120.0
"""Seconds a lease survives without a heartbeat. Long enough for a human to
think, short enough that a dead controller frees the session on its own."""

MIN_LEASE_TTL = 0.01
MAX_LEASE_TTL = 3600.0

MAX_IDEMPOTENCY_ENTRIES = 128
"""Bounded ring of remembered ``idem`` keys (oldest evicted first)."""

MAX_AUDIT_REPLY = 200
"""Cap on entries returned by one ``audit.query``."""

ATTACH_SCHEME = "amplifier-session"
"""URI scheme of the durable attach/handoff reference: ``<scheme>:<sid>#<ref>``."""

HUMAN = "human"
AUTOMATION = "automation"
UNKNOWN = "unknown"

_PRECEDENCE = {UNKNOWN: 0, AUTOMATION: 1, HUMAN: 2}
"""Takeover precedence. Higher always wins; equal requires an explicit force."""

ANONYMOUS_ID = "anonymous"

# Reasons a control decision says "no". Stable strings -- clients branch on them.
REASON_NO_ACTOR = "no_actor"
REASON_LEASE_HELD = "lease_held"
REASON_NOT_HOLDER = "not_holder"
REASON_LEASE_EXPIRED = "lease_expired"
REASON_TAKEOVER_DENIED = "takeover_denied"
REASON_SESSION_PAUSED = "session_paused"
REASON_UNKNOWN_HANDOFF = "unknown_handoff"
REASON_HANDOFF_CLAIMED = "handoff_claimed"

# Additive audit vocabulary for the ambient delegation layer (item B8, E2/E3/E7).
# B6's own action list is closed at thirteen session-control actions; cross-context
# access, the interpretation loop and the authenticated reply channel each need to
# land in the SAME trail (so "which grant authorized this" and "who answered this"
# are answerable from one file), but they must not be able to forge a control
# action. Hence a separate, explicitly enumerated set, writable only through
# :meth:`SessionControl.note_ambient`.
AMBIENT_ACTIONS: frozenset[str] = frozenset(
    {
        "source.read",
        "source.send",
        "source.denied",
        "grant.created",
        "grant.revoked",
        "grant.expired",
        "interpretation.proposed",
        "interpretation.amended",
        "interpretation.confirmed",
        "interpretation.cancelled",
        "interpretation.expired",
        "reply.accepted",
        "reply.rejected",
    }
)


# -- actors ------------------------------------------------------------------


@dataclass(frozen=True)
class Actor:
    """Who a control message is from.

    ``kind`` drives takeover precedence and nothing else; it is a *claim* the
    client makes, recorded verbatim in the audit trail. Transport-level
    authentication is deliberately out of scope here -- serve speaks over a
    pipe whose peer the OS already established. A networked adapter (item B8)
    maps its authenticated principal onto this record; the semantics below
    hold whatever that mapping is.
    """

    id: str
    kind: str = AUTOMATION
    display: str = ""

    @property
    def precedence(self) -> int:
        return _PRECEDENCE.get(self.kind, 0)

    def as_dict(self) -> dict[str, Any]:
        record: dict[str, Any] = {"id": self.id, "kind": self.kind}
        if self.display:
            record["display"] = self.display
        return record

    @classmethod
    def parse(cls, raw: Any) -> Actor | None:
        """Normalize an ``actor`` field: a bare string id, or a mapping.

        Returns ``None`` when the field is absent or carries no usable id, so
        callers can distinguish "unattributed" from "attributed as X".
        """
        if isinstance(raw, str):
            ident = raw.strip()
            return cls(id=ident) if ident else None
        if isinstance(raw, dict):
            ident = str(raw.get("id", "")).strip()
            if not ident:
                return None
            kind = str(raw.get("kind", AUTOMATION)).strip().lower()
            if kind not in _PRECEDENCE:
                kind = UNKNOWN
            return cls(id=ident, kind=kind, display=str(raw.get("display", "")))
        return None

    @classmethod
    def from_dict(cls, raw: Any) -> Actor:
        return cls.parse(raw) or cls(id=ANONYMOUS_ID, kind=UNKNOWN)


ANONYMOUS = Actor(id=ANONYMOUS_ID, kind=UNKNOWN)


# -- durable value objects ---------------------------------------------------


@dataclass(frozen=True)
class Lease:
    """The single write token. At most one is active per session."""

    lease_id: str
    actor: Actor
    granted_at: float
    expires_at: float
    epoch: int
    heartbeat_at: float

    def expired(self, now: float) -> bool:
        return now >= self.expires_at

    def as_dict(self) -> dict[str, Any]:
        return {
            "lease_id": self.lease_id,
            "actor": self.actor.as_dict(),
            "granted_at": self.granted_at,
            "expires_at": self.expires_at,
            "heartbeat_at": self.heartbeat_at,
            "epoch": self.epoch,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Lease:
        return cls(
            lease_id=str(raw.get("lease_id", "")),
            actor=Actor.from_dict(raw.get("actor")),
            granted_at=float(raw.get("granted_at", 0.0)),
            expires_at=float(raw.get("expires_at", 0.0)),
            epoch=int(raw.get("epoch", 0)),
            heartbeat_at=float(raw.get("heartbeat_at", 0.0)),
        )


def attach_ref(session_id: str, handoff_id: str | None = None) -> str:
    """The durable reference a client stores, mails, or pastes.

    ``amplifier-session:<session_id>`` re-opens/attaches the session;
    ``...#<handoff_id>`` additionally claims that escalation on attach.
    """
    base = f"{ATTACH_SCHEME}:{session_id}"
    return f"{base}#{handoff_id}" if handoff_id else base


def parse_attach_ref(ref: str) -> tuple[str, str | None]:
    """Split an attach ref into ``(session_id, handoff_id | None)``.

    Tolerant on purpose: a bare session id (what a human copies out of
    ``amplifier-tui sessions``) parses as ``(session_id, None)``.
    """
    text = ref.strip()
    if text.startswith(f"{ATTACH_SCHEME}:"):
        text = text[len(ATTACH_SCHEME) + 1 :]
    session_id, _, handoff = text.partition("#")
    return session_id.strip(), (handoff.strip() or None)


def attach_command(session_id: str, handoff_id: str | None = None) -> str:
    """A runnable command a human can be handed verbatim to take over."""
    return f"amplifier-tui serve --attach {attach_ref(session_id, handoff_id)}"


@dataclass(frozen=True)
class Handoff:
    """A durable escalation reference: "a human should take this over".

    Minted by ``session.pause``. Claiming one attaches the claimer to the SAME
    session and grants them the write lease -- the AC2 round trip.
    """

    handoff_id: str
    session_id: str
    reason: str
    note: str
    created_by: Actor
    created_at: float
    claimed_by: Actor | None = None
    claimed_at: float | None = None

    @property
    def claimed(self) -> bool:
        return self.claimed_by is not None

    def as_dict(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "handoff_id": self.handoff_id,
            "session_id": self.session_id,
            "reason": self.reason,
            "note": self.note,
            "created_by": self.created_by.as_dict(),
            "created_at": self.created_at,
            "claimed": self.claimed,
            "ref": attach_ref(self.session_id, self.handoff_id),
            "attach_command": attach_command(self.session_id, self.handoff_id),
        }
        if self.claimed_by is not None:
            record["claimed_by"] = self.claimed_by.as_dict()
            record["claimed_at"] = self.claimed_at
        return record

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Handoff:
        claimed_at = raw.get("claimed_at")
        return cls(
            handoff_id=str(raw.get("handoff_id", "")),
            session_id=str(raw.get("session_id", "")),
            reason=str(raw.get("reason", "")),
            note=str(raw.get("note", "")),
            created_by=Actor.from_dict(raw.get("created_by")),
            created_at=float(raw.get("created_at", 0.0)),
            claimed_by=Actor.parse(raw.get("claimed_by")),
            claimed_at=float(claimed_at) if claimed_at is not None else None,
        )


@dataclass(frozen=True)
class SessionHandle:
    """Stable cross-process identity for one live session."""

    handle_id: str
    session_id: str
    created_at: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "handle_id": self.handle_id,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "ref": attach_ref(self.session_id),
            "attach_command": attach_command(self.session_id),
        }


# -- durable state -----------------------------------------------------------


@dataclass
class _State:
    """The whole control file, in memory. Mutated only inside a transaction."""

    handle_id: str = ""
    session_id: str = ""
    created_at: float = 0.0
    epoch: int = 0
    lease: Lease | None = None
    paused: bool = False
    paused_by: Actor | None = None
    paused_at: float | None = None
    handoffs: list[Handoff] = field(default_factory=list)
    idempotency: list[dict[str, Any]] = field(default_factory=list)
    audit_seq: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "handle_id": self.handle_id,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "epoch": self.epoch,
            "lease": self.lease.as_dict() if self.lease else None,
            "paused": self.paused,
            "paused_by": self.paused_by.as_dict() if self.paused_by else None,
            "paused_at": self.paused_at,
            "handoffs": [h.as_dict() for h in self.handoffs],
            "idempotency": self.idempotency,
            "audit_seq": self.audit_seq,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> _State:
        lease_raw = raw.get("lease")
        paused_at = raw.get("paused_at")
        return cls(
            handle_id=str(raw.get("handle_id", "")),
            session_id=str(raw.get("session_id", "")),
            created_at=float(raw.get("created_at", 0.0)),
            epoch=int(raw.get("epoch", 0)),
            lease=Lease.from_dict(lease_raw) if isinstance(lease_raw, dict) else None,
            paused=bool(raw.get("paused", False)),
            paused_by=Actor.parse(raw.get("paused_by")),
            paused_at=float(paused_at) if paused_at is not None else None,
            handoffs=[Handoff.from_dict(h) for h in raw.get("handoffs", []) if isinstance(h, dict)],
            idempotency=[e for e in raw.get("idempotency", []) if isinstance(e, dict)],
            audit_seq=int(raw.get("audit_seq", 0)),
        )


@dataclass(frozen=True)
class WriteDecision:
    """Outcome of gating one write op (submit / steer / approve / interrupt)."""

    allowed: bool
    actor: Actor
    records: list[dict[str, Any]]
    reason: str = ""


class SessionControl:
    """The control plane for ONE session directory.

    Every mutating method returns the list of protocol records an adapter
    should emit (``lease.state`` / ``control.conflict`` / ``control.audit``
    ...), so the wire shape lives in exactly one home and a non-serve adapter
    gets the same records for free.
    """

    def __init__(
        self,
        session_dir: Path,
        session_id: str,
        *,
        now: Callable[[], float] | None = None,
        default_ttl: float = DEFAULT_LEASE_TTL,
        default_actor: Actor = ANONYMOUS,
    ) -> None:
        self.session_dir = session_dir
        self.session_id = session_id
        self._now = now or time.time
        self.default_ttl = default_ttl
        self.default_actor = default_actor
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self._path = session_dir / CONTROL_FILENAME
        self._audit_path = session_dir / AUDIT_FILENAME
        self._ensure_handle()

    # -- persistence -------------------------------------------------------

    def _read(self) -> _State:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return _State(session_id=self.session_id)
        if not isinstance(raw, dict):
            return _State(session_id=self.session_id)
        return _State.from_dict(raw)

    def _write(self, state: _State) -> None:
        tmp = self._path.with_name(f"{self._path.name}.tmp{os.getpid()}")
        tmp.write_text(json.dumps(state.as_dict(), default=str), encoding="utf-8")
        os.replace(tmp, self._path)

    @contextmanager
    def _transaction(self) -> Iterator[_State]:
        """Read -> mutate -> atomically write, inside the inter-process lock."""
        with _file_lock(self._path):
            state = self._read()
            yield state
            self._write(state)

    def _ensure_handle(self) -> None:
        """Mint the durable handle once; a reattach re-reads the same one."""
        with _file_lock(self._path):
            state = self._read()
            if state.handle_id:
                return
            state.handle_id = f"h-{uuid.uuid4().hex[:16]}"
            state.session_id = self.session_id
            state.created_at = self._now()
            self._write(state)

    # -- reads -------------------------------------------------------------

    @property
    def handle(self) -> SessionHandle:
        state = self._read()
        return SessionHandle(
            handle_id=state.handle_id,
            session_id=self.session_id,
            created_at=state.created_at,
        )

    def active_lease(self) -> Lease | None:
        """The lease if one is held AND unexpired (a pure read; no eviction)."""
        state = self._read()
        if state.lease is not None and not state.lease.expired(self._now()):
            return state.lease
        return None

    def paused(self) -> bool:
        return self._read().paused

    def handoffs(self) -> list[Handoff]:
        return list(self._read().handoffs)

    def audit_entries(self, limit: int = 50) -> list[dict[str, Any]]:
        """The newest *limit* audit entries, oldest-first within the window."""
        limit = max(1, min(int(limit), MAX_AUDIT_REPLY))
        entries: list[dict[str, Any]] = []
        try:
            with self._audit_path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    text = line.strip()
                    if not text:
                        continue
                    try:
                        parsed = json.loads(text)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(parsed, dict):
                        entries.append(parsed)
        except OSError:
            return []
        return entries[-limit:]

    # -- records -----------------------------------------------------------

    def handle_record(self) -> dict[str, Any]:
        state = self._read()
        return {
            "schema_version": SCHEMA_VERSION,
            "type": "session.handle",
            "ok": True,
            "session_id": self.session_id,
            "handle": SessionHandle(
                handle_id=state.handle_id,
                session_id=self.session_id,
                created_at=state.created_at,
            ).as_dict(),
            "epoch": state.epoch,
            "paused": state.paused,
            "lease": state.lease.as_dict() if state.lease else None,
        }

    def _lease_record(self, state: _State, *, ok: bool = True, detail: str = "") -> dict[str, Any]:
        now = self._now()
        lease = state.lease
        record: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "type": "lease.state",
            "ok": ok,
            "session_id": self.session_id,
            "handle_id": state.handle_id,
            "epoch": state.epoch,
            "paused": state.paused,
            "now": now,
            "lease": None if lease is None or lease.expired(now) else lease.as_dict(),
        }
        if detail:
            record["detail"] = detail
        return record

    def status_record(self) -> dict[str, Any]:
        """Read-only ``lease.status`` reply (never mutates, never audits)."""
        return self._lease_record(self._read())

    def _conflict(
        self, *, op: str, reason: str, detail: str, state: _State, actor: Actor
    ) -> dict[str, Any]:
        lease = state.lease
        return {
            "schema_version": SCHEMA_VERSION,
            "type": "control.conflict",
            "ok": False,
            "op": op,
            "reason": reason,
            "detail": detail,
            "session_id": self.session_id,
            "actor": actor.as_dict(),
            "epoch": state.epoch,
            "paused": state.paused,
            "holder": lease.actor.as_dict() if lease else None,
            "lease_id": lease.lease_id if lease else None,
        }

    # -- audit -------------------------------------------------------------

    def _audit(self, state: _State, action: str, actor: Actor, **detail: Any) -> dict[str, Any]:
        """Append one attribution entry and return its wire record.

        Called INSIDE a transaction so ``audit_seq`` advances with the state it
        describes. The file append is best-effort: an unwritable audit log must
        not break the control plane (the decision still reaches the client on
        the wire).
        """
        state.audit_seq += 1
        entry: dict[str, Any] = {
            "seq": state.audit_seq,
            "at": self._now(),
            "action": action,
            "actor": actor.as_dict(),
            "session_id": self.session_id,
            "handle_id": state.handle_id,
            "epoch": state.epoch,
        }
        if state.lease is not None:
            entry["lease_id"] = state.lease.lease_id
        if detail:
            entry["detail"] = detail
        try:
            self._audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self._audit_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, default=str) + "\n")
        except OSError:
            pass
        return {"schema_version": SCHEMA_VERSION, "type": "control.audit", "entry": entry}

    # -- idempotency -------------------------------------------------------

    def replay(self, key: str) -> list[dict[str, Any]] | None:
        """The remembered response for *key*, marked ``replay``, or ``None``.

        Durable, so a controller that reconnects into a NEW process still
        recognizes its own retry and does not double-submit.
        """
        if not key:
            return None
        for entry in self._read().idempotency:
            if entry.get("key") == key:
                records = [dict(r) for r in entry.get("records", []) if isinstance(r, dict)]
                for record in records:
                    record["replay"] = True
                return records
        return None

    def remember(self, key: str, records: list[dict[str, Any]]) -> None:
        """Bind *key* to the records that answered it (oldest evicted first)."""
        if not key:
            return
        with self._transaction() as state:
            state.idempotency = [e for e in state.idempotency if e.get("key") != key]
            state.idempotency.append({"key": key, "at": self._now(), "records": records})
            if len(state.idempotency) > MAX_IDEMPOTENCY_ENTRIES:
                state.idempotency = state.idempotency[-MAX_IDEMPOTENCY_ENTRIES:]

    # -- lease lifecycle ---------------------------------------------------

    def _expire_if_due(self, state: _State) -> dict[str, Any] | None:
        """Drop an expired lease, auditing the expiry.

        The AC5 backstop: a controller that vanished mid-turn never leaves the
        session permanently locked -- the next control op reaps its lease.
        """
        lease = state.lease
        if lease is None or not lease.expired(self._now()):
            return None
        state.lease = None
        return self._audit(
            state,
            "lease.expired",
            lease.actor,
            lease_id=lease.lease_id,
            expires_at=lease.expires_at,
        )

    def _grant(self, state: _State, actor: Actor, ttl: float, action: str) -> dict[str, Any]:
        state.epoch += 1
        now = self._now()
        state.lease = Lease(
            lease_id=f"l-{uuid.uuid4().hex[:16]}",
            actor=actor,
            granted_at=now,
            expires_at=now + ttl,
            epoch=state.epoch,
            heartbeat_at=now,
        )
        return self._audit(state, action, actor, ttl=ttl)

    def _ttl(self, raw: Any) -> float:
        if raw is None:
            return self.default_ttl
        try:
            ttl = float(raw)
        except (TypeError, ValueError):
            return self.default_ttl
        return max(MIN_LEASE_TTL, min(ttl, MAX_LEASE_TTL))

    def acquire(self, actor: Actor | None, *, ttl: Any = None) -> list[dict[str, Any]]:
        """Take the write lease if it is free, expired, or already ours.

        Re-acquiring while still holding it is a **renew**, not a conflict --
        the forgiving path for a controller that lost track of its lease id. A
        different actor holding a live lease is a hard conflict: use
        :meth:`takeover`.
        """
        seconds = self._ttl(ttl)
        records: list[dict[str, Any]] = []
        with self._transaction() as state:
            if actor is None:
                records.append(
                    self._conflict(
                        op="lease.acquire",
                        reason=REASON_NO_ACTOR,
                        detail="lease.acquire requires an actor identity",
                        state=state,
                        actor=ANONYMOUS,
                    )
                )
                records.append(self._audit(state, "lease.denied", ANONYMOUS, why=REASON_NO_ACTOR))
                return records
            expired = self._expire_if_due(state)
            if expired is not None:
                records.append(expired)
            held = state.lease
            if held is not None and held.actor.id != actor.id:
                records.append(
                    self._conflict(
                        op="lease.acquire",
                        reason=REASON_LEASE_HELD,
                        detail=f"write lease held by {held.actor.id} until {held.expires_at}",
                        state=state,
                        actor=actor,
                    )
                )
                records.append(self._audit(state, "lease.denied", actor, why=REASON_LEASE_HELD))
                return records
            action = "lease.renewed" if held is not None else "lease.granted"
            records.append(self._grant(state, actor, seconds, action))
            records.append(self._lease_record(state, detail=action))
        return records

    def heartbeat(self, lease_id: str, *, ttl: Any = None) -> list[dict[str, Any]]:
        """Extend the holder's lease.

        Only the current, unexpired lease renews; a heartbeat for a superseded
        lease is a conflict, never a resurrection -- otherwise a paused-then-
        taken-over controller could steal the pen back by waking up.
        """
        seconds = self._ttl(ttl)
        records: list[dict[str, Any]] = []
        with self._transaction() as state:
            expired = self._expire_if_due(state)
            if expired is not None:
                records.append(expired)
            held = state.lease
            if held is None or held.lease_id != lease_id:
                records.append(
                    self._conflict(
                        op="lease.heartbeat",
                        reason=REASON_LEASE_EXPIRED if held is None else REASON_NOT_HOLDER,
                        detail="lease is no longer current; re-acquire before writing",
                        state=state,
                        actor=held.actor if held else ANONYMOUS,
                    )
                )
                return records
            now = self._now()
            state.lease = replace(held, heartbeat_at=now, expires_at=now + seconds)
            records.append(self._lease_record(state, detail="lease.renewed"))
        return records

    def release(self, lease_id: str, *, actor: Actor | None = None) -> list[dict[str, Any]]:
        """Explicitly give up the lease.

        Releasing a lease that is already gone is a no-op success -- a
        reconnecting client may safely retry it.
        """
        records: list[dict[str, Any]] = []
        with self._transaction() as state:
            held = state.lease
            if held is None or (lease_id and held.lease_id != lease_id):
                records.append(self._lease_record(state, detail="lease.absent"))
                return records
            released = self._audit(
                state, "lease.released", actor or held.actor, lease_id=held.lease_id
            )
            state.lease = None
            records.append(released)
            records.append(self._lease_record(state, detail="lease.released"))
        return records

    def takeover(
        self, actor: Actor | None, *, reason: str = "", force: bool = False, ttl: Any = None
    ) -> list[dict[str, Any]]:
        """Deterministically seize the lease.

        The whole rule, in precedence order -- no timing, no negotiation:

        * no lease (or an expired one) -> granted;
        * requester outranks the holder (``human`` > ``automation`` >
          ``unknown``) -> granted, the holder's lease invalidated;
        * equal precedence AND ``force`` AND the requester is human -> granted;
        * anything else -> ``control.conflict`` (``takeover_denied``).

        So a person can always break in on a bot, and a bot can never break in
        on a person.
        """
        seconds = self._ttl(ttl)
        records: list[dict[str, Any]] = []
        with self._transaction() as state:
            if actor is None:
                records.append(
                    self._conflict(
                        op="lease.takeover",
                        reason=REASON_NO_ACTOR,
                        detail="lease.takeover requires an actor identity",
                        state=state,
                        actor=ANONYMOUS,
                    )
                )
                return records
            expired = self._expire_if_due(state)
            if expired is not None:
                records.append(expired)
            held = state.lease
            if held is not None and not (
                actor.precedence > held.actor.precedence
                or (force and actor.kind == HUMAN and actor.precedence == held.actor.precedence)
            ):
                records.append(
                    self._conflict(
                        op="lease.takeover",
                        reason=REASON_TAKEOVER_DENIED,
                        detail=(
                            f"{actor.kind} '{actor.id}' may not take the lease from "
                            f"{held.actor.kind} '{held.actor.id}'"
                        ),
                        state=state,
                        actor=actor,
                    )
                )
                records.append(
                    self._audit(state, "lease.denied", actor, why=REASON_TAKEOVER_DENIED)
                )
                return records
            if held is not None:
                records.append(
                    self._audit(
                        state,
                        "lease.revoked",
                        actor,
                        from_actor=held.actor.as_dict(),
                        from_lease=held.lease_id,
                        why=reason or "takeover",
                    )
                )
            records.append(self._grant(state, actor, seconds, "lease.takeover"))
            records.append(self._lease_record(state, detail="lease.takeover"))
        return records

    # -- pause / handoff ---------------------------------------------------

    def note_ambient(self, action: str, actor: Actor | None, **detail: Any) -> dict[str, Any]:
        """Append one **ambient-layer** attribution entry (item B8).

        The ambient delegation layer records cross-context access
        (``source.*`` / ``grant.*``), the interpretation loop
        (``interpretation.*``) and authenticated replies (``reply.*``) into
        this session's existing ``control-audit.jsonl``, rather than starting
        a second trail. One trail is the point: "which grant authorized this
        read", "which interpretation the human agreed to", and "who answered
        the notification" are all answerable in the same place, in ``seq``
        order, as the lease decisions they interleave with.

        *action* MUST be one of :data:`AMBIENT_ACTIONS`. The vocabulary is
        closed and separate from the control actions on purpose: an ambient
        caller can add to the account of what happened, but cannot forge a
        ``lease.granted`` or a ``handoff.claimed``.
        """
        if action not in AMBIENT_ACTIONS:
            raise ValueError(
                f"{action!r} is not an ambient audit action (known: {sorted(AMBIENT_ACTIONS)})"
            )
        with self._transaction() as state:
            return self._audit(state, action, actor or self.default_actor, **detail)

    def pause(
        self, actor: Actor | None, *, reason: str = "", note: str = "", lease_id: str = ""
    ) -> list[dict[str, Any]]:
        """Park the write lane and mint a durable handoff reference.

        Pausing revokes the active lease (nobody holds the pen while the
        session waits for a human) and blocks every write until a
        :meth:`claim_handoff` or :meth:`resume`. The returned
        ``handoff.created`` record carries the ref and a runnable attach
        command -- the escalation payload a controller hands to a person.

        Pausing does NOT cancel a running turn; the caller decides whether to
        also interrupt (``serve`` exposes that as ``"interrupt": true``).
        """
        who = actor or self.default_actor
        records: list[dict[str, Any]] = []
        with self._transaction() as state:
            self._expire_if_due(state)
            held = state.lease
            if held is not None and lease_id and held.lease_id != lease_id:
                records.append(
                    self._conflict(
                        op="session.pause",
                        reason=REASON_NOT_HOLDER,
                        detail="pause must present the current lease id (or none)",
                        state=state,
                        actor=who,
                    )
                )
                return records
            handoff = Handoff(
                handoff_id=f"ho-{uuid.uuid4().hex[:12]}",
                session_id=self.session_id,
                reason=reason,
                note=note,
                created_by=who,
                created_at=self._now(),
            )
            state.handoffs.append(handoff)
            state.paused = True
            state.paused_by = who
            state.paused_at = self._now()
            if held is not None:
                records.append(
                    self._audit(state, "lease.released", who, lease_id=held.lease_id, why="pause")
                )
                state.lease = None
            records.append(
                self._audit(state, "session.paused", who, handoff_id=handoff.handoff_id, why=reason)
            )
            records.append(
                self._audit(state, "handoff.created", who, handoff_id=handoff.handoff_id)
            )
            records.append(
                {
                    "schema_version": SCHEMA_VERSION,
                    "type": "handoff.created",
                    "ok": True,
                    "session_id": self.session_id,
                    "handle_id": state.handle_id,
                    "handoff": handoff.as_dict(),
                    "paused": True,
                }
            )
            records.append(self._lease_record(state, detail="session.paused"))
        return records

    def claim_handoff(
        self, handoff_id: str, actor: Actor | None, *, ttl: Any = None
    ) -> list[dict[str, Any]]:
        """Attach to the escalation: clear the pause and grant the lease.

        Unknown or already-claimed refs conflict rather than silently granting
        -- a handoff is a one-shot escalation token, so two people racing on
        the same link cannot both believe they own the session.
        """
        seconds = self._ttl(ttl)
        records: list[dict[str, Any]] = []
        with self._transaction() as state:
            who = actor or self.default_actor
            match = next((h for h in state.handoffs if h.handoff_id == handoff_id), None)
            if match is None:
                records.append(
                    self._conflict(
                        op="handoff.claim",
                        reason=REASON_UNKNOWN_HANDOFF,
                        detail=f"no handoff {handoff_id!r} on this session",
                        state=state,
                        actor=who,
                    )
                )
                return records
            if match.claimed:
                holder = match.claimed_by.id if match.claimed_by else "another actor"
                records.append(
                    self._conflict(
                        op="handoff.claim",
                        reason=REASON_HANDOFF_CLAIMED,
                        detail=f"handoff already claimed by {holder}",
                        state=state,
                        actor=who,
                    )
                )
                return records
            expired = self._expire_if_due(state)
            if expired is not None:
                records.append(expired)
            held = state.lease
            if held is not None and who.precedence < held.actor.precedence:
                records.append(
                    self._conflict(
                        op="handoff.claim",
                        reason=REASON_TAKEOVER_DENIED,
                        detail=f"lease held by {held.actor.kind} '{held.actor.id}'",
                        state=state,
                        actor=who,
                    )
                )
                return records
            claimed = replace(match, claimed_by=who, claimed_at=self._now())
            state.handoffs = [claimed if h.handoff_id == handoff_id else h for h in state.handoffs]
            state.paused = False
            state.paused_by = None
            state.paused_at = None
            records.append(self._grant(state, who, seconds, "handoff.claimed"))
            records.append(
                {
                    "schema_version": SCHEMA_VERSION,
                    "type": "handoff.claimed",
                    "ok": True,
                    "session_id": self.session_id,
                    "handle_id": state.handle_id,
                    "handoff": claimed.as_dict(),
                    "paused": False,
                }
            )
            records.append(self._lease_record(state, detail="handoff.claimed"))
        return records

    def resume(self, actor: Actor | None) -> list[dict[str, Any]]:
        """Lift a pause without claiming a handoff (the controller's own undo)."""
        who = actor or self.default_actor
        records: list[dict[str, Any]] = []
        with self._transaction() as state:
            if not state.paused:
                records.append(self._lease_record(state, detail="session.running"))
                return records
            state.paused = False
            state.paused_by = None
            state.paused_at = None
            records.append(self._audit(state, "session.resumed", who))
            records.append(self._lease_record(state, detail="session.resumed"))
        return records

    def handoff_list_record(self) -> dict[str, Any]:
        state = self._read()
        return {
            "schema_version": SCHEMA_VERSION,
            "type": "handoff.list",
            "ok": True,
            "session_id": self.session_id,
            "handoffs": [h.as_dict() for h in state.handoffs],
            "paused": state.paused,
        }

    def audit_record(self, limit: Any = 50) -> dict[str, Any]:
        try:
            count = int(limit)
        except (TypeError, ValueError):
            count = 50
        return {
            "schema_version": SCHEMA_VERSION,
            "type": "audit.list",
            "ok": True,
            "session_id": self.session_id,
            "entries": self.audit_entries(count),
        }

    # -- write gating ------------------------------------------------------

    def authorize(self, op_kind: str, op: dict[str, Any]) -> WriteDecision:
        """Decide whether one write op may proceed, and attribute it.

        The rule, stated once:

        * the session is paused -> denied (``session_paused``);
        * the op presents a ``lease`` id -> it must BE the current, unexpired
          lease, else denied (``lease_expired`` / ``not_holder``);
        * the op presents no lease -> allowed only while no lease is active,
          else denied (``lease_held``).

        That is the single-writer guarantee: two clients can never both land in
        the allowed branch, so conflicting input is refused at the door instead
        of interleaved into the transcript.
        """
        presented = str(op.get("lease", "") or "")
        supplied = Actor.parse(op.get("actor"))
        records: list[dict[str, Any]] = []
        with self._transaction() as state:
            expired = self._expire_if_due(state)
            if expired is not None:
                records.append(expired)
            held = state.lease
            actor = supplied or (
                held.actor
                if held is not None and presented == held.lease_id
                else self.default_actor
            )
            denial: tuple[str, str] | None = None
            if state.paused:
                denial = (
                    REASON_SESSION_PAUSED,
                    "session is paused pending human handoff; claim it or resume first",
                )
            elif presented and held is None:
                denial = (
                    REASON_LEASE_EXPIRED,
                    "the presented lease is no longer active; re-acquire before writing",
                )
            elif presented and held is not None and held.lease_id != presented:
                denial = (
                    REASON_NOT_HOLDER,
                    f"write lease is held by {held.actor.id} (epoch {held.epoch})",
                )
            elif not presented and held is not None:
                denial = (
                    REASON_LEASE_HELD,
                    f"write lease is held by {held.actor.id}; acquire or take it over first",
                )
            if denial is not None:
                reason, detail = denial
                records.append(
                    self._conflict(
                        op=op_kind, reason=reason, detail=detail, state=state, actor=actor
                    )
                )
                records.append(self._audit(state, "write.rejected", actor, op=op_kind, why=reason))
                return WriteDecision(False, actor, records, reason)
            records.append(self._audit(state, "write.accepted", actor, op=op_kind))
        return WriteDecision(True, actor, records)
