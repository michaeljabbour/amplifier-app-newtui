"""E2 -- the permission-grant store, and the ``source.*`` / ``grant.*`` audit.

> **AC4** -- "Cross-context access is explicit, permissioned, attributable, and
> limited to the sources the user enabled."

A grant is a **record**, not a setting. It names one principal, one scope, one
verb and one narrowing selector, and it is the only thing that authorizes
cross-context access. Four rules carry the whole design:

1. **Default deny.** No grant, no access -- and the refusal is *surfaced*
   (:class:`GrantDecision` always carries a reason), never silently skipped.
   An ambient assistant that quietly omits a source the user believed was
   connected is worse than one that says "I can't see your mail."
2. **Consulted at use, never cached.** :meth:`GrantStore.authorize` re-reads
   ``grants.json`` from disk on **every** call. This class deliberately holds
   no in-memory grant cache, because a cached grant is a revoke that didn't
   happen: a revoke must fail the very next read, including mid-turn and
   including one issued by a different process.
3. **No wildcards.** A ``source:*`` grant with no selector is invalid at
   creation, not treated as "everything". ``read`` never implies ``send``.
4. **First-party minting only.** A voice channel may *request* a grant; it may
   never create one (:data:`FIRST_PARTY_SURFACES`). A permission escalation is
   the one action where a channel's own weakness -- lossy ASR, an unattended
   room, a replayed recording -- is exactly the attack.

Storage mirrors B6's proven pattern rather than inventing one: an atomic
snapshot plus an append-only trail, written under the shared
:func:`kernel.file_lock.locked` ``O_EXCL`` discipline::

    ~/.amplifier/ambient/
        grants.json          # current grants (atomic write under the lock)
        grants-audit.jsonl   # append-only: created / revoked / expired / denied

Grants are **per-user, not per-session** (a mail grant naturally spans
sessions), but every *use* is additionally attributed into the consuming
session's own ``control-audit.jsonl`` via
:func:`amplifier_app_tui.kernel.session_control.SessionControl.note_ambient`,
so a session's trail stays a complete account of what was done on its behalf.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..file_lock import locked as _file_lock
from ..session_control import Actor, SessionControl
from .principal import PrincipalLike, actor_for, auth_provenance

logger = logging.getLogger(__name__)

GRANTS_FILENAME = "grants.json"
GRANTS_AUDIT_FILENAME = "grants-audit.jsonl"
SCHEMA_VERSION = 1

READ = "read"
SEND = "send"
WRITE = "write"
CONTROL = "control"

SCOPE_VERBS: dict[str, frozenset[str]] = {
    "session": frozenset({READ, WRITE, CONTROL}),
    "project": frozenset({READ}),
    "source": frozenset({READ, SEND}),
}
"""The four scope families of the design doc, deliberately small.

``session:<sid>`` matches ``kernel.session_authz``'s permission vocabulary
exactly, so a grant minted here maps across with no translation; ``control``
is separate from ``write`` on purpose -- driving a session and *seizing* it
are different powers.
"""

SELECTOR_REQUIRED = ("source",)
"""Families where a selector-less grant is invalid rather than "everything"."""

EXPIRY_REQUIRED = ("source",)
"""Families where an expiry is mandatory. Session/project grants may be
open-ended: they grant nothing the user's own filesystem permissions do not
already allow."""

DEFAULT_SOURCE_TTL = 30.0 * 24 * 3600
"""30 days -- the doc's starting proposal for ``source:*`` expiry, not a
measured number. Callers pass their own ``expires_at``; this is only the
default when they don't."""

FIRST_PARTY_SURFACES = frozenset({"tui", "cli"})
"""Surfaces allowed to MINT a grant: visually-confirmed and first-party."""

# Decision reasons. Stable strings -- callers and the audit trail branch on them.
REASON_GRANTED = "granted"
REASON_NO_GRANT = "no_grant"
REASON_EXPIRED = "expired"
REASON_REVOKED = "revoked"
REASON_SELECTOR_MISMATCH = "selector_mismatch"
REASON_NO_SELECTOR = "no_selector"


class GrantError(ValueError):
    """A grant could not be minted (invalid scope/verb/selector/surface)."""


def parse_scope(scope: str) -> tuple[str, str]:
    """Split ``family:target`` -- e.g. ``("source", "outlook")``.

    Raises :class:`GrantError` for an unknown family or a missing target, so
    a typo becomes a refusal at creation rather than a grant that silently
    matches nothing (or, worse, everything).
    """
    family, _, target = str(scope).partition(":")
    if family not in SCOPE_VERBS:
        raise GrantError(f"unknown scope family {family!r} (known: {sorted(SCOPE_VERBS)})")
    if not target.strip():
        raise GrantError(f"scope {scope!r} names no target")
    return family, target.strip()


@dataclass(frozen=True)
class Grant:
    """One durable authorization record."""

    grant_id: str
    principal: str
    scope: str
    verb: str
    selector: Mapping[str, str] = field(default_factory=dict)
    granted_by: Actor = field(default_factory=lambda: Actor(id="unknown", kind="unknown"))
    granted_at: float = 0.0
    expires_at: float | None = None
    revoked_at: float | None = None
    surface: str = "tui"

    def active(self, now: float) -> bool:
        if self.revoked_at is not None:
            return False
        return self.expires_at is None or now < self.expires_at

    def as_dict(self) -> dict[str, Any]:
        return {
            "grant_id": self.grant_id,
            "principal": self.principal,
            "scope": self.scope,
            "verb": self.verb,
            "selector": dict(self.selector),
            "granted_by": self.granted_by.as_dict(),
            "granted_at": self.granted_at,
            "expires_at": self.expires_at,
            "revoked_at": self.revoked_at,
            "surface": self.surface,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Grant:
        selector = raw.get("selector")
        return cls(
            grant_id=str(raw.get("grant_id", "")),
            principal=str(raw.get("principal", "")),
            scope=str(raw.get("scope", "")),
            verb=str(raw.get("verb", "")),
            selector={str(k): str(v) for k, v in (selector or {}).items()}
            if isinstance(selector, Mapping)
            else {},
            granted_by=Actor.from_dict(raw.get("granted_by")),
            granted_at=float(raw.get("granted_at") or 0.0),
            expires_at=None if raw.get("expires_at") is None else float(raw["expires_at"]),
            revoked_at=None if raw.get("revoked_at") is None else float(raw["revoked_at"]),
            surface=str(raw.get("surface", "tui")),
        )


@dataclass(frozen=True)
class GrantDecision:
    """The answer to "may this principal do this, right now?".

    ``reason`` is always populated -- on an allow it is
    :data:`REASON_GRANTED`, on a deny it names *why*, so the refusal can be
    surfaced to the user instead of the source being silently skipped.
    """

    allowed: bool
    reason: str
    grant_id: str = ""
    scope: str = ""
    verb: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "grant_id": self.grant_id,
            "scope": self.scope,
            "verb": self.verb,
        }


def _selector_matches(grant_selector: Mapping[str, str], requested: Mapping[str, str]) -> bool:
    """Every narrowing key on the grant must be matched exactly by the request.

    A request may be *narrower* than its grant (extra keys are fine); it may
    never be broader. Comparison is exact-string on purpose: a prefix or glob
    match is the kind of convenience that turns "Dana's thread" into "the
    inbox" the first time someone gets a selector slightly wrong.
    """
    return all(requested.get(key) == value for key, value in grant_selector.items())


def authorize_source(
    grants: Sequence[Grant],
    principal: str,
    scope: str,
    verb: str,
    selector: Mapping[str, str],
    now: float,
) -> GrantDecision:
    """Pure deny-by-default authorization over a snapshot of grants.

    Pure by design: no I/O, no clock of its own, table-driven and trivially
    testable (the design doc's own test strategy asks for exactly this --
    an empty-grants case proving deny-by-default, an expired case and a
    revoked-mid-turn case).

    The returned reason is the *most specific* refusal encountered: a grant
    that matched principal/scope/verb but was revoked reports ``revoked``,
    not ``no_grant``, because "you had this and it was taken away" and "you
    never had this" are different things to tell a user.
    """
    family, _ = parse_scope(scope)
    if family in SELECTOR_REQUIRED and not selector:
        return GrantDecision(False, REASON_NO_SELECTOR, scope=scope, verb=verb)

    best = REASON_NO_GRANT
    best_id = ""
    for grant in grants:
        if grant.principal != principal or grant.scope != scope or grant.verb != verb:
            continue
        if grant.revoked_at is not None:
            best, best_id = REASON_REVOKED, grant.grant_id
            continue
        if grant.expires_at is not None and now >= grant.expires_at:
            if best != REASON_REVOKED:
                best, best_id = REASON_EXPIRED, grant.grant_id
            continue
        if not _selector_matches(grant.selector, selector):
            if best not in (REASON_REVOKED, REASON_EXPIRED):
                best, best_id = REASON_SELECTOR_MISMATCH, grant.grant_id
            continue
        return GrantDecision(True, REASON_GRANTED, grant.grant_id, scope, verb)
    return GrantDecision(False, best, best_id, scope, verb)


def default_ambient_root() -> Path:
    """``~/.amplifier/ambient`` -- per-user, beside the project session tree."""
    return Path.home() / ".amplifier" / "ambient"


class GrantStore:
    """Durable ``grants.json`` + ``grants-audit.jsonl`` for one user.

    Holds **no** in-memory grant cache. Every :meth:`authorize` is a fresh
    read; see rule 2 in the module docstring.
    """

    def __init__(
        self,
        root: Path | None = None,
        *,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.root = Path(root) if root is not None else default_ambient_root()
        self._path = self.root / GRANTS_FILENAME
        self._audit_path = self.root / GRANTS_AUDIT_FILENAME
        self._now = now

    # -- persistence -------------------------------------------------------

    def _load(self) -> list[Grant]:
        """Every grant on disk, or ``[]`` on any problem.

        Tolerant exactly like ``SessionControl._read``: a missing file, a torn
        write from a crashed process, or a permissions problem degrade to
        "nothing granted yet" -- which, for a deny-by-default store, is the
        safe direction to fail.
        """
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(raw, dict):
            return []
        rows = raw.get("grants")
        if not isinstance(rows, list):
            return []
        return [Grant.from_dict(row) for row in rows if isinstance(row, Mapping)]

    def _save(self, grants: Sequence[Grant]) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "grants": [grant.as_dict() for grant in grants],
        }
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(f"{self._path.name}.tmp{os.getpid()}")
        tmp.write_text(json.dumps(payload, default=str), encoding="utf-8")
        os.replace(tmp, self._path)

    def _audit(self, action: str, **detail: Any) -> dict[str, Any]:
        """Append one line to ``grants-audit.jsonl`` (best-effort).

        An unwritable audit file must not break the permission check -- the
        same posture ``SessionControl._audit`` takes -- but note the asymmetry:
        a *lost audit line* degrades the record, while a lost grant check would
        degrade security, so only the former is ever swallowed.
        """
        entry: dict[str, Any] = {"at": self._now(), "action": action, **detail}
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with self._audit_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, default=str) + "\n")
        except OSError:
            logger.debug("grant audit append failed (non-fatal)", exc_info=True)
        return entry

    def audit_entries(self, limit: int = 50) -> list[dict[str, Any]]:
        """The tail of the grant trail, oldest first."""
        try:
            lines = self._audit_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        entries: list[dict[str, Any]] = []
        for line in lines[-max(0, limit) :] if limit else lines:
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                entries.append(parsed)
        return entries

    # -- minting / revoking ------------------------------------------------

    def create(
        self,
        *,
        principal: str,
        scope: str,
        verb: str,
        selector: Mapping[str, str] | None = None,
        granted_by: Actor,
        surface: str = "tui",
        expires_at: float | None = None,
    ) -> Grant:
        """Mint a grant on a first-party surface. Validates, never widens.

        Raises :class:`GrantError` -- rather than storing something permissive
        -- for an unknown scope family, a verb that family does not offer, a
        selector-less ``source:*`` grant, or a non-first-party surface.
        """
        family, _ = parse_scope(scope)
        if surface not in FIRST_PARTY_SURFACES:
            raise GrantError(
                f"grants may only be minted on a first-party surface "
                f"{sorted(FIRST_PARTY_SURFACES)}, not {surface!r}"
            )
        if not principal.strip():
            raise GrantError("a grant must name a principal")
        if verb not in SCOPE_VERBS[family]:
            raise GrantError(
                f"scope family {family!r} does not offer verb {verb!r} "
                f"(offers {sorted(SCOPE_VERBS[family])})"
            )
        narrowing = {str(k): str(v) for k, v in (selector or {}).items()}
        if family in SELECTOR_REQUIRED and not narrowing:
            raise GrantError(
                f"a {family}:* grant with no selector is invalid, not 'everything' -- "
                "there is no wildcard grant in this design"
            )
        now = self._now()
        if family in EXPIRY_REQUIRED and expires_at is None:
            expires_at = now + DEFAULT_SOURCE_TTL
        grant = Grant(
            grant_id=f"g-{uuid.uuid4().hex[:12]}",
            principal=principal.strip(),
            scope=scope,
            verb=verb,
            selector=narrowing,
            granted_by=granted_by,
            granted_at=now,
            expires_at=expires_at,
            surface=surface,
        )
        with _file_lock(self._path):
            grants = self._load()
            grants.append(grant)
            self._save(grants)
        self._audit(
            "grant.created",
            grant_id=grant.grant_id,
            principal=grant.principal,
            scope=grant.scope,
            verb=grant.verb,
            selector=dict(grant.selector),
            granted_by=granted_by.as_dict(),
            surface=surface,
            expires_at=expires_at,
        )
        return grant

    def revoke(self, grant_id: str, *, actor: Actor) -> Grant | None:
        """Revoke immediately and lease-independently.

        Returns the revoked grant, or ``None`` if the id is unknown or was
        already revoked (idempotent -- a repeated revoke is not an error).
        """
        revoked: Grant | None = None
        with _file_lock(self._path):
            grants = self._load()
            updated: list[Grant] = []
            for grant in grants:
                if grant.grant_id == grant_id and grant.revoked_at is None:
                    revoked = Grant(
                        grant_id=grant.grant_id,
                        principal=grant.principal,
                        scope=grant.scope,
                        verb=grant.verb,
                        selector=grant.selector,
                        granted_by=grant.granted_by,
                        granted_at=grant.granted_at,
                        expires_at=grant.expires_at,
                        revoked_at=self._now(),
                        surface=grant.surface,
                    )
                    updated.append(revoked)
                else:
                    updated.append(grant)
            if revoked is not None:
                self._save(updated)
        if revoked is not None:
            self._audit(
                "grant.revoked",
                grant_id=grant_id,
                principal=revoked.principal,
                scope=revoked.scope,
                verb=revoked.verb,
                actor=actor.as_dict(),
            )
        return revoked

    def list_grants(
        self, *, principal: str | None = None, active_only: bool = False
    ) -> list[Grant]:
        """Grants on disk, optionally narrowed to one principal / to live ones."""
        now = self._now()
        rows = self._load()
        if principal is not None:
            rows = [g for g in rows if g.principal == principal]
        if active_only:
            rows = [g for g in rows if g.active(now)]
        return rows

    # -- the use-time check ------------------------------------------------

    def authorize(
        self,
        principal: str,
        scope: str,
        verb: str,
        selector: Mapping[str, str] | None = None,
    ) -> GrantDecision:
        """Consult the store **at use**. Re-reads from disk every single call.

        This is the method that makes revocation real: a revoke written by any
        process -- the TUI, the CLI, another daemon -- lands on the very next
        call here, mid-turn included, because there is nothing in between to
        be stale.
        """
        narrowing = {str(k): str(v) for k, v in (selector or {}).items()}
        decision = authorize_source(self._load(), principal, scope, verb, narrowing, self._now())
        if not decision.allowed:
            self._audit(
                "grant.denied",
                principal=principal,
                scope=scope,
                verb=verb,
                selector=narrowing,
                why=decision.reason,
                grant_id=decision.grant_id,
            )
        return decision


def consume_grant(
    store: GrantStore,
    control: SessionControl | None,
    principal: PrincipalLike,
    *,
    scope: str,
    verb: str,
    selector: Mapping[str, str] | None = None,
) -> GrantDecision:
    """Check a grant at use AND attribute the outcome into the session trail.

    The pairing is the point of E2: the grant store answers "may this happen",
    and the consuming session's ``control-audit.jsonl`` records that it did
    (``source.read`` / ``source.send``) or did not (``source.denied``), with
    ``grant_id`` in the detail. That is what makes AC4's "attributable" real
    rather than aspirational: "which grant authorized this" is answerable
    after the fact, from the same trail that already holds session control.

    *control* may be ``None`` (an ambient check with no session yet); the
    grant-side audit still happens, only the session-side attribution is
    skipped.
    """
    decision = store.authorize(principal.principal_id, scope, verb, selector)
    if control is None:
        return decision
    action = "source.denied"
    if decision.allowed:
        action = "source.send" if verb == SEND else "source.read"
    control.note_ambient(
        action,
        actor_for(principal),
        grant_id=decision.grant_id,
        scope=scope,
        verb=verb,
        selector=dict(selector or {}),
        why=decision.reason,
        auth=auth_provenance(principal),
    )
    return decision


__all__ = [
    "DEFAULT_SOURCE_TTL",
    "FIRST_PARTY_SURFACES",
    "GRANTS_AUDIT_FILENAME",
    "GRANTS_FILENAME",
    "REASON_EXPIRED",
    "REASON_GRANTED",
    "REASON_NO_GRANT",
    "REASON_NO_SELECTOR",
    "REASON_REVOKED",
    "REASON_SELECTOR_MISMATCH",
    "SCOPE_VERBS",
    "Grant",
    "GrantDecision",
    "GrantError",
    "GrantStore",
    "authorize_source",
    "consume_grant",
    "default_ambient_root",
    "parse_scope",
]
