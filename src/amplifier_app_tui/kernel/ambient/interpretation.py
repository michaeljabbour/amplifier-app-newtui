"""E3 -- the structured, editable interpretation payload (AC1).

> **AC1** -- a voice/conversational request is echoed back as a concise,
> **editable** interpretation before consequential work begins.

The *gating* for this already exists and is reused unchanged: B6's
``session.pause`` parks the write lane and mints a durable one-shot handoff;
while paused, ``authorize()`` denies **every** write with ``session_paused``;
``claim_handoff()`` races safely, so two people answering the same prompt
cannot both believe they own it. Nothing can slip through while the human is
deciding, and none of that had to be rebuilt.

**Only the payload was missing.** B6's handoff carries free-text ``reason`` and
``note``. A structured proposal with enumerated editable fields is not
expressible in a free-text field -- and JSON-stuffing ``note`` would turn a
human-readable field into an untyped side channel and guarantee drift. So this
module adds a typed record, keyed by the handoff id, with its own
``propose`` / ``amend`` / ``confirm`` / ``cancel`` ops.

Three rules the state machine enforces:

- **``amend`` mints a NEW interpretation with a new id; it never mutates.** An
  interpretation you can mutate is an interpretation you cannot audit -- the
  record the user agreed to must be exactly the record that executes. The
  superseded record stays on disk, marked, so the amendment chain is legible.
- **Only enumerated fields are editable.** ``EDITABLE_FIELDS`` is a closed
  vocabulary, so "change the ..." has a fixed set to hit and an ASR slip
  cannot invent a field.
- **Interpretations expire, and expiry is ``cancel``.** A forgotten voice
  request must not wedge a paused session, so expiry resolves it -- and
  ``cancel`` and expiry are **both** audited, not just ``confirm``. What the
  assistant *didn't* do on your behalf is part of the account.

Durable state lives in ``interpretations.json`` beside ``control.json``, using
the same atomic-write-under-``O_EXCL``-lock idiom as
:mod:`kernel.session_control` and :mod:`kernel.attention_store` -- one idiom,
not a third invention.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from ..file_lock import locked as _file_lock
from ..session_control import Actor, SessionControl
from .principal import PrincipalLike, actor_for, auth_provenance

logger = logging.getLogger(__name__)

INTERPRETATIONS_FILENAME = "interpretations.json"
SCHEMA_VERSION = 1

DEFAULT_TTL = 600.0
"""10 minutes -- the doc's proposal, deliberately much shorter than B6's
128-entry idempotency ring turns over, so a confirm cannot outlive the replay
protection that makes it exactly-once."""

ReversibilityClass = Literal["reversible", "externally_visible", "irreversible"]
"""Ordered by blast radius. ``externally_visible`` is its own class because it
cannot be un-sent and reaches people who never consented to the delegation."""

InterpretationState = Literal["pending", "confirmed", "cancelled", "expired", "superseded"]

EDITABLE_FIELDS: tuple[str, ...] = ("summary", "targets", "grants", "negative_scope")
"""The closed vocabulary an ``amend`` may name. Deliberately excludes
``reversibility``: the blast-radius class is *derived* from what the request
does, not something the speaker may talk down."""

_TUPLE_FIELDS = frozenset({"targets", "grants", "negative_scope"})


class InterpretationError(ValueError):
    """An interpretation op was invalid (unknown id, closed state, bad field)."""


@dataclass(frozen=True)
class Interpretation:
    """One echo of what the assistant believes it was asked to do.

    Field order mirrors the spoken order: most decision-relevant first, so
    :meth:`spoken` can be read aloud in one breath and a listener hears the
    thing most likely to be wrong (the verb, then the target, then the grants
    it will burn) before their attention wanders.
    """

    interpretation_id: str
    session_id: str
    handoff_id: str
    summary: str
    targets: tuple[str, ...] = ()
    grants: tuple[str, ...] = ()
    reversibility: ReversibilityClass = "reversible"
    negative_scope: tuple[str, ...] = ()
    created_at: float = 0.0
    expires_at: float = 0.0
    state: InterpretationState = "pending"
    supersedes: str = ""
    superseded_by: str = ""
    principal: str = ""

    @property
    def editable_fields(self) -> tuple[str, ...]:
        return EDITABLE_FIELDS

    def expired(self, now: float) -> bool:
        return self.expires_at > 0.0 and now >= self.expires_at

    def spoken(self) -> str:
        """The one-breath echo, ordered for speech.

        Kept to short clauses because this is read aloud on an eyes-free
        channel: the listener cannot scroll back.
        """
        parts = [self.summary.rstrip(".") + "."]
        if self.targets:
            parts.append("In " + ", ".join(self.targets) + ".")
        if self.grants:
            parts.append("Using " + ", ".join(self.grants) + ".")
        parts.append(_REVERSIBILITY_PHRASE[self.reversibility])
        if self.negative_scope:
            parts.append("I will not " + "; ".join(self.negative_scope) + ".")
        parts.append("Say confirm, cancel, or change " + ", ".join(EDITABLE_FIELDS) + ".")
        return " ".join(parts)

    def as_dict(self) -> dict[str, Any]:
        return {
            "interpretation_id": self.interpretation_id,
            "session_id": self.session_id,
            "handoff_id": self.handoff_id,
            "summary": self.summary,
            "targets": list(self.targets),
            "grants": list(self.grants),
            "reversibility": self.reversibility,
            "negative_scope": list(self.negative_scope),
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "state": self.state,
            "supersedes": self.supersedes,
            "superseded_by": self.superseded_by,
            "principal": self.principal,
            "editable_fields": list(EDITABLE_FIELDS),
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Interpretation:
        return cls(
            interpretation_id=str(raw.get("interpretation_id", "")),
            session_id=str(raw.get("session_id", "")),
            handoff_id=str(raw.get("handoff_id", "")),
            summary=str(raw.get("summary", "")),
            targets=tuple(str(v) for v in (raw.get("targets") or [])),
            grants=tuple(str(v) for v in (raw.get("grants") or [])),
            reversibility=_as_reversibility(raw.get("reversibility")),
            negative_scope=tuple(str(v) for v in (raw.get("negative_scope") or [])),
            created_at=float(raw.get("created_at") or 0.0),
            expires_at=float(raw.get("expires_at") or 0.0),
            state=_as_state(raw.get("state")),
            supersedes=str(raw.get("supersedes", "")),
            superseded_by=str(raw.get("superseded_by", "")),
            principal=str(raw.get("principal", "")),
        )


_REVERSIBILITY_PHRASE: dict[str, str] = {
    "reversible": "This is reversible.",
    "externally_visible": "This is externally visible and cannot be un-sent.",
    "irreversible": "This is irreversible.",
}


def _as_reversibility(raw: Any) -> ReversibilityClass:
    value = str(raw or "reversible")
    if value in ("reversible", "externally_visible", "irreversible"):
        return value  # pyright: ignore[reportReturnType]
    return "reversible"


def _as_state(raw: Any) -> InterpretationState:
    value = str(raw or "pending")
    if value in ("pending", "confirmed", "cancelled", "expired", "superseded"):
        return value  # pyright: ignore[reportReturnType]
    return "pending"


@dataclass(frozen=True)
class InterpretationOutcome:
    """What a ``confirm``/``cancel``/``amend`` produced."""

    interpretation: Interpretation
    records: tuple[dict[str, Any], ...] = field(default=())
    """B6 control records produced as a side effect (e.g. ``handoff.claimed``
    and the resulting ``lease.state`` on a confirm) -- returned rather than
    hidden, so the caller can present the same conflict the control plane saw."""


class InterpretationDesk:
    """The ``propose -> amend -> confirm | cancel | expire`` state machine.

    Durable per session, clock-injected, and deliberately *not* a second
    gate: every state transition that matters to safety leans on the B6
    :class:`~amplifier_app_tui.kernel.session_control.SessionControl` handed
    in, so there is exactly one place that decides who may write.
    """

    def __init__(
        self,
        session_dir: Path,
        control: SessionControl,
        *,
        now: Callable[[], float] = time.time,
        ttl: float = DEFAULT_TTL,
    ) -> None:
        self._path = Path(session_dir) / INTERPRETATIONS_FILENAME
        self._control = control
        self._now = now
        self._ttl = ttl

    # -- persistence -------------------------------------------------------

    def _load(self) -> list[Interpretation]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        rows = raw.get("interpretations") if isinstance(raw, dict) else None
        if not isinstance(rows, list):
            return []
        return [Interpretation.from_dict(r) for r in rows if isinstance(r, Mapping)]

    def _save(self, rows: Sequence[Interpretation]) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "interpretations": [row.as_dict() for row in rows],
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(f"{self._path.name}.tmp{os.getpid()}")
            tmp.write_text(json.dumps(payload, default=str), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError:
            logger.debug("interpretation persist failed (non-fatal)", exc_info=True)

    def _replace_row(self, updated: Interpretation) -> None:
        with _file_lock(self._path):
            rows = self._load()
            self._save(
                [updated if r.interpretation_id == updated.interpretation_id else r for r in rows]
            )

    # -- reads -------------------------------------------------------------

    def get(self, interpretation_id: str) -> Interpretation | None:
        return next((r for r in self._load() if r.interpretation_id == interpretation_id), None)

    def pending(self) -> list[Interpretation]:
        """Live, unexpired proposals -- what a reply-on-open surface offers."""
        now = self._now()
        return [r for r in self._load() if r.state == "pending" and not r.expired(now)]

    def all_rows(self) -> list[Interpretation]:
        return self._load()

    # -- ops ---------------------------------------------------------------

    def propose(
        self,
        principal: PrincipalLike,
        *,
        summary: str,
        targets: Sequence[str] = (),
        grants: Sequence[str] = (),
        reversibility: ReversibilityClass = "reversible",
        negative_scope: Sequence[str] = (),
        pause_reason: str = "awaiting interpretation confirmation",
    ) -> InterpretationOutcome:
        """Echo an interpretation and park the session behind a B6 handoff.

        The pause is what makes the echo *safe* rather than advisory: from
        this moment ``authorize()`` denies every write on the session, so no
        consequential work can begin while the human is still deciding.
        """
        if not summary.strip():
            raise InterpretationError("an interpretation must summarize the request")
        actor = actor_for(principal)
        records = tuple(self._control.pause(actor, reason=pause_reason))
        handoff_id = _handoff_id_from(records)
        if not handoff_id:
            # The control plane refused to pause (a conflict). Surface it
            # rather than proposing an interpretation nothing is gating.
            raise InterpretationError(
                "session could not be parked for confirmation: "
                + str(records[0].get("reason", "unknown") if records else "unknown")
            )
        now = self._now()
        row = Interpretation(
            interpretation_id=f"i-{uuid.uuid4().hex[:12]}",
            session_id=self._control.session_id,
            handoff_id=handoff_id,
            summary=summary.strip(),
            targets=tuple(targets),
            grants=tuple(grants),
            reversibility=reversibility,
            negative_scope=tuple(negative_scope),
            created_at=now,
            expires_at=now + self._ttl,
            principal=principal.principal_id,
        )
        with _file_lock(self._path):
            rows = self._load()
            rows.append(row)
            self._save(rows)
        self._audit("interpretation.proposed", principal, row)
        return InterpretationOutcome(row, records)

    def amend(
        self, interpretation_id: str, field_name: str, value: Any, principal: PrincipalLike
    ) -> InterpretationOutcome:
        """Mint a NEW interpretation with one field changed. Never mutates.

        The original is marked ``superseded`` and keeps a pointer to its
        successor, so the audit trail shows the whole amendment chain and the
        record that ultimately executed is unambiguous.
        """
        current = self._require_pending(interpretation_id)
        if field_name not in EDITABLE_FIELDS:
            raise InterpretationError(
                f"{field_name!r} is not editable (editable: {list(EDITABLE_FIELDS)})"
            )
        coerced: Any
        if field_name in _TUPLE_FIELDS:
            coerced = (
                tuple(str(v) for v in value) if isinstance(value, (list, tuple)) else (str(value),)
            )
        else:
            coerced = str(value)
        now = self._now()
        successor = replace(
            current,
            interpretation_id=f"i-{uuid.uuid4().hex[:12]}",
            created_at=now,
            expires_at=now + self._ttl,
            state="pending",
            supersedes=current.interpretation_id,
            superseded_by="",
            **{field_name: coerced},
        )
        retired = replace(current, state="superseded", superseded_by=successor.interpretation_id)
        with _file_lock(self._path):
            rows = self._load()
            rows = [
                retired if r.interpretation_id == current.interpretation_id else r for r in rows
            ]
            rows.append(successor)
            self._save(rows)
        self._audit(
            "interpretation.amended",
            principal,
            successor,
            amended_field=field_name,
            supersedes=current.interpretation_id,
        )
        return InterpretationOutcome(successor)

    def confirm(self, interpretation_id: str, principal: PrincipalLike) -> InterpretationOutcome:
        """Accept the echo: claim the B6 handoff, which grants the lease.

        Returns the control records verbatim -- including a ``control.conflict``
        if someone else already claimed the handoff -- so a second "yes" over a
        flaky link conflicts rather than double-executing.
        """
        current = self._require_pending(interpretation_id)
        actor = actor_for(principal)
        records = tuple(self._control.claim_handoff(current.handoff_id, actor))
        if any(r.get("type") == "control.conflict" for r in records):
            return InterpretationOutcome(current, records)
        confirmed = replace(current, state="confirmed")
        self._replace_row(confirmed)
        self._audit("interpretation.confirmed", principal, confirmed)
        return InterpretationOutcome(confirmed, records)

    def cancel(
        self, interpretation_id: str, principal: PrincipalLike, *, why: str = "cancelled"
    ) -> InterpretationOutcome:
        """Decline the echo and resume the session.

        Resuming matters: a cancel that left the session paused would turn
        "no thanks" into a wedged session.
        """
        current = self._require_pending(interpretation_id)
        cancelled = replace(current, state="cancelled")
        self._replace_row(cancelled)
        records = tuple(self._control.resume(actor_for(principal)))
        self._audit("interpretation.cancelled", principal, cancelled, why=why)
        return InterpretationOutcome(cancelled, records)

    def expire_due(self, principal: PrincipalLike) -> list[Interpretation]:
        """Expire every overdue proposal, resuming the sessions they parked.

        Expiry *is* cancel (the doc's rule), and it is audited like one -- a
        request the assistant silently dropped would be the one thing missing
        from the account.
        """
        now = self._now()
        expired: list[Interpretation] = []
        with _file_lock(self._path):
            rows = self._load()
            updated: list[Interpretation] = []
            for row in rows:
                if row.state == "pending" and row.expired(now):
                    retired = replace(row, state="expired")
                    expired.append(retired)
                    updated.append(retired)
                else:
                    updated.append(row)
            if expired:
                self._save(updated)
        for row in expired:
            self._control.resume(actor_for(principal))
            self._audit("interpretation.expired", principal, row)
        return expired

    # -- internals ---------------------------------------------------------

    def _require_pending(self, interpretation_id: str) -> Interpretation:
        row = self.get(interpretation_id)
        if row is None:
            raise InterpretationError(f"no interpretation {interpretation_id!r}")
        if row.state != "pending":
            raise InterpretationError(
                f"interpretation {interpretation_id!r} is {row.state}, not pending"
            )
        if row.expired(self._now()):
            raise InterpretationError(f"interpretation {interpretation_id!r} has expired")
        return row

    def _audit(
        self,
        action: str,
        principal: PrincipalLike,
        row: Interpretation,
        **detail: Any,
    ) -> None:
        self._control.note_ambient(
            action,
            actor_for(principal),
            interpretation_id=row.interpretation_id,
            handoff_id=row.handoff_id,
            reversibility=row.reversibility,
            grants=list(row.grants),
            auth=auth_provenance(principal),
            **detail,
        )


def _handoff_id_from(records: Sequence[Mapping[str, Any]]) -> str:
    for record in records:
        if record.get("type") == "handoff.created":
            handoff = record.get("handoff")
            if isinstance(handoff, Mapping):
                return str(handoff.get("handoff_id", ""))
    return ""


def interpretation_actor(principal: PrincipalLike) -> Actor:
    """Public alias for the principal -> actor mapping used by this module."""
    return actor_for(principal)


__all__ = [
    "DEFAULT_TTL",
    "EDITABLE_FIELDS",
    "INTERPRETATIONS_FILENAME",
    "Interpretation",
    "InterpretationDesk",
    "InterpretationError",
    "InterpretationOutcome",
    "InterpretationState",
    "ReversibilityClass",
    "interpretation_actor",
]
