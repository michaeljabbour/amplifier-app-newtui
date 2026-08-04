"""Authorization for the session control plane: principals and permissions.

:mod:`kernel.session_control` answers *who may drive a session* -- but before
this module it only ever knew a **claim**. ``Actor.kind`` was an unverified
string, so any client could send ``{"kind": "human"}`` and, because a human
always outranks automation, seize the write lease out from under a real
person's controller. Over a local pipe that was a defensible courtesy (the OS
already established the peer). Over anything else it is a privilege-escalation
path, and the downstream ambient-delegation track (item B8) names closing it as
its hard security prerequisite ("E1": no networked adapter may ship without it).

Two ideas close it, and deliberately no more:

**Principal** -- who the connecting party actually *is*, established by an
:class:`AuthorizationPolicy` from a credential the client presents. It carries
the identity, the **verified** ``kind``, the permissions it holds, and its own
provenance (``method`` / ``verified``) so the trail can tell an authenticated
human apart from a process that merely typed ``kind:"human"``.

**Permission** -- what that principal may do. Exactly three verbs, matching the
``session:<sid>`` scope vocabulary B8 specifies, so a grant minted there maps
across with no translation:

===========  ============================================================
``read``     status, handle, history replay, audit query, handoff list
``write``    ops that mutate the session: submit / steer / approve /
             decision / interrupt / tags / effort / context clear ...
``control``  the ownership surface: lease acquire / heartbeat / release /
             **takeover**, pause / resume, handoff claim. Driving a
             session and *seizing* it are both separate from writing into
             it -- an observer bot can hold ``read`` alone, an assistant
             ``read+write``, and only a trusted operator ``control``.
===========  ============================================================

Three policies ship:

* :data:`TRUSTED_LOCAL` -- the default and the back-compat path. It trusts the
  OS-established pipe peer, mints an *unverified* principal mirroring the
  claimed identity, and grants all three permissions. A project with no token
  store behaves byte-identically to before this module existed.
* :class:`TokenPolicy` -- real authentication. Capability tokens are minted on
  a first-party surface (``amplifier-tui control-token issue``) and stored
  **hashed**, so the plaintext never touches disk; each carries its own kind,
  permission set and expiry. A client presents one as ``{"auth": {"token":
  "..."}}``. Once a store exists, an op with no credential is refused --
  default deny, and the refusal is surfaced rather than silently downgraded.
* :class:`StaticPolicy` -- the networked-adapter shape. An adapter that already
  authenticated its peer (OIDC, mTLS, a device token, platform SSO) builds the
  :class:`Principal` from its own claims and wraps it in this. It authenticates
  and maps; it holds no policy and invents no lease semantics.

Layering (ADR-0007): pure ``kernel/`` logic over the filesystem and stdlib --
no Textual, no amplifier-core, no runtime import.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .file_lock import locked

AUTHZ_FILENAME = "control-authz.json"
"""Durable token store: hashed capability tokens, one file per project."""

SCHEMA_VERSION = 1

HUMAN = "human"
AUTOMATION = "automation"
UNKNOWN = "unknown"

_PRECEDENCE = {UNKNOWN: 0, AUTOMATION: 1, HUMAN: 2}

READ = "read"
WRITE = "write"
CONTROL = "control"

PERMISSIONS: frozenset[str] = frozenset({READ, WRITE, CONTROL})
"""The whole permission vocabulary. Closed on purpose -- a fourth verb is a
design decision with its own review, not a config knob."""

ALL_PERMISSIONS: frozenset[str] = PERMISSIONS

METHOD_LOCAL = "local-pipe"
"""Provenance of the default policy: the OS established the peer, nothing more."""

METHOD_TOKEN = "token"

TOKEN_BYTES = 32
"""Entropy per issued token (256 bits): a capability must not be guessable."""

TOKEN_PREFIX = "amp-ctl-"


def normalize_permissions(raw: Any) -> frozenset[str]:
    """Coerce *raw* into a valid permission set, dropping anything unknown.

    Unknown verbs are dropped rather than rejected, so a token written by a
    NEWER build (carrying a verb this one has never heard of) still
    authenticates for the verbs this build does understand: fail closed on the
    unknown verb, not shut on the whole token.
    """
    if raw is None:
        return frozenset()
    candidates: list[str]
    if isinstance(raw, str):
        candidates = [raw]
    else:
        try:
            candidates = [str(item) for item in raw]
        except TypeError:
            return frozenset()
    return frozenset(item.strip().lower() for item in candidates) & PERMISSIONS


def normalize_kind(raw: Any) -> str:
    """Any unrecognized kind collapses to ``unknown`` -- the lowest precedence."""
    kind = str(raw or "").strip().lower()
    return kind if kind in _PRECEDENCE else UNKNOWN


@dataclass(frozen=True)
class AuthProvenance:
    """How an identity was established: the additive ``actor.auth`` record.

    Without it the audit trail cannot distinguish an authenticated human from
    a process that asserted ``kind:"human"`` -- which makes attribution
    non-probative exactly where it has to hold up.
    """

    method: str = METHOD_LOCAL
    verified: bool = False
    principal: str = ""

    def as_dict(self) -> dict[str, Any]:
        record: dict[str, Any] = {"method": self.method, "verified": self.verified}
        if self.principal:
            record["principal"] = self.principal
        return record

    @classmethod
    def parse(cls, raw: Any) -> AuthProvenance | None:
        if not isinstance(raw, dict):
            return None
        return cls(
            method=str(raw.get("method", METHOD_LOCAL)),
            verified=bool(raw.get("verified", False)),
            principal=str(raw.get("principal", "")),
        )


@dataclass(frozen=True)
class Principal:
    """An authenticated (or explicitly unverified) identity behind a connection.

    ``kind`` here is the **established** kind, as against ``Actor.kind``, which
    is what a message *claims*. The control plane refuses any claim that
    outranks this one -- that refusal is the whole point of the module.
    """

    principal_id: str
    kind: str = AUTOMATION
    permissions: frozenset[str] = ALL_PERMISSIONS
    method: str = METHOD_LOCAL
    verified: bool = False
    display: str = ""

    @property
    def precedence(self) -> int:
        return _PRECEDENCE.get(self.kind, 0)

    def permits(self, permission: str) -> bool:
        return permission in self.permissions

    def may_claim(self, kind: str) -> bool:
        """May this principal act as *kind*?

        A principal may always act as itself or *below* itself: a verified
        human driving a bot lane can legitimately present ``automation`` and
        accept the weaker takeover precedence that comes with it. It may never
        act above itself -- that is the escalation this module exists to stop.
        """
        return _PRECEDENCE.get(normalize_kind(kind), 0) <= self.precedence

    def provenance(self) -> AuthProvenance:
        return AuthProvenance(
            method=self.method, verified=self.verified, principal=self.principal_id
        )

    def as_dict(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "principal_id": self.principal_id,
            "kind": self.kind,
            "permissions": sorted(self.permissions),
            "method": self.method,
            "verified": self.verified,
        }
        if self.display:
            record["display"] = self.display
        return record


@runtime_checkable
class AuthorizationPolicy(Protocol):
    """How a credential becomes a :class:`Principal` -- the adapter boundary.

    ``requires_credential`` says whether an op carrying no ``auth`` field is
    refused outright. ``resolve`` returns the principal a credential
    establishes, or ``None`` for "not authenticated".
    """

    @property
    def name(self) -> str:
        """Short label for the scheme, surfaced in ``session.status``."""
        ...

    @property
    def requires_credential(self) -> bool:
        """Is an op with no ``auth`` field refused outright?"""
        ...

    def resolve(
        self, credential: Any, *, claimed_id: str = "", claimed_kind: str = ""
    ) -> Principal | None: ...

    def describe(self) -> dict[str, Any]: ...


@dataclass(frozen=True)
class TrustedLocalPolicy:
    """Trust the OS-established pipe peer; verify nothing (today's behaviour).

    The default, and what keeps this change opt-in: with no token store, every
    actor resolves to an unverified principal mirroring its own claim and
    holding all three permissions, so the control plane behaves exactly as it
    did before authorization existed. What changed is that the shape is now
    explicit -- a ``verified: false`` provenance instead of a silent assumption.
    """

    name: str = METHOD_LOCAL
    requires_credential: bool = False

    def resolve(
        self, credential: Any, *, claimed_id: str = "", claimed_kind: str = ""
    ) -> Principal | None:
        del credential
        return Principal(
            principal_id=claimed_id or "local",
            kind=normalize_kind(claimed_kind) if claimed_kind else AUTOMATION,
            permissions=ALL_PERMISSIONS,
            method=METHOD_LOCAL,
            verified=False,
        )

    def describe(self) -> dict[str, Any]:
        return {"policy": self.name, "requires_credential": False, "verified": False}


TRUSTED_LOCAL = TrustedLocalPolicy()
"""The default policy, named so callers can compare identity, not just type."""


@dataclass(frozen=True)
class StaticPolicy:
    """One pre-authenticated principal -- the networked-adapter shape.

    An adapter that already authenticated its peer constructs the
    :class:`Principal` from its own claims and wraps it in this. Every
    session-control semantic then holds unchanged, with real provenance in the
    trail. Adapters authenticate; they do not hold policy.
    """

    principal: Principal
    name: str = "static"
    requires_credential: bool = False

    def resolve(
        self, credential: Any, *, claimed_id: str = "", claimed_kind: str = ""
    ) -> Principal | None:
        del credential, claimed_id, claimed_kind
        return self.principal

    def describe(self) -> dict[str, Any]:
        return {
            "policy": self.name,
            "requires_credential": False,
            "verified": self.principal.verified,
            "principal": self.principal.principal_id,
        }


# -- durable capability tokens ------------------------------------------------


def _hash_token(token: str) -> str:
    return sha256(token.strip().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TokenGrant:
    """One issued capability token, stored hashed.

    The plaintext is shown once at issue time and never persisted, so a stolen
    store file yields nothing usable.
    """

    token_id: str
    token_hash: str
    principal_id: str
    kind: str = AUTOMATION
    permissions: frozenset[str] = ALL_PERMISSIONS
    display: str = ""
    issued_at: float = 0.0
    expires_at: float | None = None
    revoked_at: float | None = None

    def active(self, now: float) -> bool:
        if self.revoked_at is not None:
            return False
        return self.expires_at is None or now < self.expires_at

    def as_dict(self) -> dict[str, Any]:
        return {
            "token_id": self.token_id,
            "token_hash": self.token_hash,
            "principal_id": self.principal_id,
            "kind": self.kind,
            "permissions": sorted(self.permissions),
            "display": self.display,
            "issued_at": self.issued_at,
            "expires_at": self.expires_at,
            "revoked_at": self.revoked_at,
        }

    def summary(self, now: float) -> dict[str, Any]:
        """The safe-to-print view (no hash) for ``control-token list``."""
        record = self.as_dict()
        record.pop("token_hash", None)
        record["active"] = self.active(now)
        return record

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> TokenGrant:
        expires_at = raw.get("expires_at")
        revoked_at = raw.get("revoked_at")
        return cls(
            token_id=str(raw.get("token_id", "")),
            token_hash=str(raw.get("token_hash", "")),
            principal_id=str(raw.get("principal_id", "")),
            kind=normalize_kind(raw.get("kind")),
            permissions=normalize_permissions(raw.get("permissions")),
            display=str(raw.get("display", "")),
            issued_at=float(raw.get("issued_at", 0.0)),
            expires_at=float(expires_at) if expires_at is not None else None,
            revoked_at=float(revoked_at) if revoked_at is not None else None,
        )

    def to_principal(self) -> Principal:
        return Principal(
            principal_id=self.principal_id,
            kind=self.kind,
            permissions=self.permissions,
            method=METHOD_TOKEN,
            verified=True,
            display=self.display,
        )


class TokenStore:
    """Durable, project-scoped store of hashed capability tokens.

    Mirrors the control plane's own persistence discipline rather than
    inventing a second one: atomic ``tmp`` write + ``os.replace`` inside the
    shared ``O_EXCL`` file lock (:mod:`kernel.file_lock`), so a concurrent
    issue and revoke cannot lose each other.
    """

    def __init__(self, path: Path | str, *, now: Any = time.time) -> None:
        self.path = Path(path)
        self._now = now

    # -- persistence ----------------------------------------------------

    def _read(self) -> list[TokenGrant]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(raw, dict):
            return []
        return [TokenGrant.from_dict(g) for g in raw.get("grants", []) if isinstance(g, dict)]

    def _write(self, grants: list[TokenGrant]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema_version": SCHEMA_VERSION, "grants": [g.as_dict() for g in grants]}
        tmp = self.path.with_name(f"{self.path.name}.tmp{os.getpid()}")
        tmp.write_text(json.dumps(payload, default=str), encoding="utf-8")
        os.replace(tmp, self.path)
        # Defence in depth only -- the tokens are hashed, so a readable file
        # leaks nothing usable. A filesystem that refuses the mode is fine.
        with suppress(OSError):
            os.chmod(self.path, 0o600)

    # -- operations -----------------------------------------------------

    def exists(self) -> bool:
        return self.path.exists()

    def grants(self) -> list[TokenGrant]:
        return self._read()

    def issue(
        self,
        principal_id: str,
        *,
        kind: str = AUTOMATION,
        permissions: Any = None,
        display: str = "",
        ttl: float | None = None,
    ) -> tuple[str, TokenGrant]:
        """Mint a token; returns ``(plaintext, grant)`` -- the plaintext once.

        Minting is a first-party action on purpose (the CLI, on a surface the
        user can see): a channel that can mint its own credential is not a
        credential.
        """
        allowed = normalize_permissions(permissions) if permissions is not None else ALL_PERMISSIONS
        plaintext = TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)
        now = float(self._now())
        grant = TokenGrant(
            token_id=f"tok-{uuid.uuid4().hex[:12]}",
            token_hash=_hash_token(plaintext),
            principal_id=principal_id,
            kind=normalize_kind(kind),
            permissions=allowed,
            display=display,
            issued_at=now,
            expires_at=(now + float(ttl)) if ttl else None,
        )
        with locked(self.path):
            grants = self._read()
            grants.append(grant)
            self._write(grants)
        return plaintext, grant

    def revoke(self, token_id: str) -> TokenGrant | None:
        """Revoke by id. Effective on the very next resolve -- never cached."""
        revoked: TokenGrant | None = None
        with locked(self.path):
            updated: list[TokenGrant] = []
            for grant in self._read():
                if grant.token_id == token_id and grant.revoked_at is None:
                    revoked = TokenGrant.from_dict(
                        {**grant.as_dict(), "revoked_at": float(self._now())}
                    )
                    updated.append(revoked)
                else:
                    updated.append(grant)
            if revoked is not None:
                self._write(updated)
        return revoked

    def resolve(self, token: str) -> TokenGrant | None:
        """The active grant a plaintext token names, or ``None``.

        Compared with :func:`hmac.compare_digest` so a wrong token cannot be
        narrowed down by timing, and re-read from disk on every call so a
        revoke takes effect immediately -- a cached grant is a revoke that
        did not happen.
        """
        if not token:
            return None
        digest = _hash_token(token)
        now = float(self._now())
        for grant in self._read():
            if hmac.compare_digest(grant.token_hash, digest) and grant.active(now):
                return grant
        return None


@dataclass(frozen=True)
class TokenPolicy:
    """Authenticate a presented capability token against a :class:`TokenStore`.

    ``requires_credential`` is ``True``: once a project has a token store, an
    op with no credential is *refused*, never silently downgraded.
    """

    store: TokenStore
    name: str = METHOD_TOKEN
    requires_credential: bool = True

    def resolve(
        self, credential: Any, *, claimed_id: str = "", claimed_kind: str = ""
    ) -> Principal | None:
        del claimed_id, claimed_kind
        token = credential_token(credential)
        if not token:
            return None
        grant = self.store.resolve(token)
        return grant.to_principal() if grant is not None else None

    def describe(self) -> dict[str, Any]:
        return {"policy": self.name, "requires_credential": True, "verified": True}


def credential_token(credential: Any) -> str:
    """Pull the bearer token out of whatever shape the client sent.

    Accepts ``{"token": "..."}`` (the documented form) and a bare string --
    the same tolerance ``Actor.parse`` shows. A client that sends the simplest
    thing that could work should not be punished for it.
    """
    if isinstance(credential, str):
        return credential.strip()
    if isinstance(credential, dict):
        return str(credential.get("token", "") or "").strip()
    return ""


def policy_for(store_path: Path | None, *, now: Any = time.time) -> AuthorizationPolicy:
    """The policy a project's token store implies.

    A project with a store gets :class:`TokenPolicy`; one without gets
    :data:`TRUSTED_LOCAL`. That is the opt-in seam: authorization exists the
    moment an operator issues the first token and not a moment before --
    exactly as the control plane itself materializes only when a client uses it.
    """
    if store_path is not None:
        store = TokenStore(store_path, now=now)
        if store.exists():
            return TokenPolicy(store)
    return TRUSTED_LOCAL


__all__ = [
    "ALL_PERMISSIONS",
    "AUTHZ_FILENAME",
    "AUTOMATION",
    "CONTROL",
    "HUMAN",
    "METHOD_LOCAL",
    "METHOD_TOKEN",
    "PERMISSIONS",
    "READ",
    "TRUSTED_LOCAL",
    "UNKNOWN",
    "WRITE",
    "AuthProvenance",
    "AuthorizationPolicy",
    "Principal",
    "StaticPolicy",
    "TokenGrant",
    "TokenPolicy",
    "TokenStore",
    "TrustedLocalPolicy",
    "credential_token",
    "normalize_kind",
    "normalize_permissions",
    "policy_for",
]
