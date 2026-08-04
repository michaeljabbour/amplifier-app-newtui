"""E1 consumption seam: an authenticated principal mapped onto a B6 ``Actor``.

**E1 is not built here.** ``kernel/session_authz.py`` owns the authorization
policy (who the connecting party is, and which of ``read``/``write``/
``control`` they hold); this module is the ambient layer's *consumer* of it.
The split matters: a channel that mints its own credential is not a
credential, so the ambient layer deliberately holds no policy -- it accepts a
principal someone else established and maps it onto the attribution record B6
already writes.

Two behaviours, both of them load-bearing:

**1. Consume if present, degrade cleanly if absent.** ``session_authz`` is
imported lazily and optionally. When it exists, a real ``Principal`` flows
through unchanged. When it does not, :class:`LocalPrincipal` stands in with
``verified=False`` and ``method="local-pipe"`` -- the honest description of
what a local pipe actually establishes -- rather than pretending to an
authentication that did not happen.

**2. An unverified networked principal may not claim to be human.** B6's
takeover rule (``human`` 2 > ``automation`` 1 > ``unknown`` 0,
``kernel/session_control.py``) is a courtesy over a local pipe whose peer the
OS established. Over a channel it is a privilege boundary: an adapter that
can assert ``kind:"human"`` can seize the write lease out from under a real
person's automation. :func:`actor_for` therefore downgrades an unverified
``human`` claim to ``unknown`` unless it arrived over a trusted local
transport -- and says so in the returned provenance, so the downgrade is
visible in the audit trail rather than silent.

The provenance dict :func:`auth_provenance` returns is carried in the audit
``detail`` of every ambient entry. Once ``session_authz`` lands, ``Actor``
gains a native ``auth`` block and this becomes redundant *there* -- but the
detail is additive either way, so nothing has to be un-built.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..session_control import AUTOMATION, HUMAN, UNKNOWN, Actor

READ = "read"
WRITE = "write"
CONTROL = "control"
ALL_PERMISSIONS = frozenset({READ, WRITE, CONTROL})

METHOD_LOCAL = "local-pipe"
"""The method name ``session_authz.TrustedLocalPolicy`` uses; matched here so
a principal minted by either side describes itself identically."""

TRUSTED_METHODS: tuple[str, ...] = (METHOD_LOCAL,)
"""Transports whose peer the OS already established, so an unverified
``human`` claim over them is a courtesy rather than an escalation."""


@runtime_checkable
class PrincipalLike(Protocol):
    """The subset of ``session_authz.Principal`` the ambient layer reads.

    Structural on purpose: this package must compile, type-check and test
    with ``kernel/session_authz.py`` absent (it lands separately), and must
    consume the real class unchanged once it is present.
    """

    @property
    def principal_id(self) -> str: ...

    @property
    def kind(self) -> str: ...

    @property
    def method(self) -> str: ...

    @property
    def verified(self) -> bool: ...

    @property
    def display(self) -> str: ...

    def permits(self, permission: str) -> bool: ...


@dataclass(frozen=True)
class LocalPrincipal:
    """The degraded stand-in used when ``session_authz`` is unavailable.

    Describes exactly what a local pipe establishes and no more:
    ``verified=False``, ``method="local-pipe"``. It holds all three
    permissions because the OS peer is the user themselves -- the same
    posture ``TrustedLocalPolicy`` takes -- but it never claims to have
    *authenticated* anyone.
    """

    principal_id: str
    kind: str = AUTOMATION
    method: str = METHOD_LOCAL
    verified: bool = False
    display: str = ""
    permissions: frozenset[str] = field(default=ALL_PERMISSIONS)

    def permits(self, permission: str) -> bool:
        return permission in self.permissions


def session_authz_available() -> bool:
    """Whether E1's ``kernel/session_authz`` module is importable here.

    Probed by name rather than imported, deliberately: this package must
    type-check and run in a tree where that module does not exist yet, and a
    real ``import`` would make its absence a static error rather than the
    runtime fact it actually is.
    """
    try:
        return importlib.util.find_spec("amplifier_app_tui.kernel.session_authz") is not None
    except (ImportError, ValueError):
        return False


def actor_for(
    principal: PrincipalLike,
    *,
    trusted_methods: tuple[str, ...] = TRUSTED_METHODS,
) -> Actor:
    """Map an authenticated principal onto B6's :class:`Actor`.

    The one rule that makes this more than a field copy: an **unverified**
    ``human`` claim arriving over an untrusted method is recorded as
    ``unknown`` (precedence 0), so it cannot outrank -- and therefore cannot
    seize the lease from -- a real actor. A verified human stays human; an
    unverified human on a trusted local transport stays human, because the OS
    established that peer.
    """
    kind = principal.kind if principal.kind in (HUMAN, AUTOMATION, UNKNOWN) else UNKNOWN
    if kind == HUMAN and not principal.verified and principal.method not in trusted_methods:
        kind = UNKNOWN
    return Actor(id=principal.principal_id, kind=kind, display=principal.display)


def auth_provenance(
    principal: PrincipalLike,
    *,
    trusted_methods: tuple[str, ...] = TRUSTED_METHODS,
) -> dict[str, Any]:
    """The provenance block carried in every ambient audit entry's ``detail``.

    Without it the trail cannot distinguish an authenticated human on a phone
    from a process that typed ``kind:"human"`` -- which makes the trail
    non-probative exactly where AC4 says it must be attributable. ``claimed``
    is present only when :func:`actor_for` downgraded the claim, so a
    downgrade is legible after the fact instead of silent.
    """
    record: dict[str, Any] = {
        "method": principal.method,
        "verified": bool(principal.verified),
        "principal": principal.principal_id,
    }
    effective = actor_for(principal, trusted_methods=trusted_methods)
    if effective.kind != principal.kind:
        record["claimed"] = principal.kind
        record["downgraded"] = True
    return record


__all__ = [
    "ALL_PERMISSIONS",
    "CONTROL",
    "METHOD_LOCAL",
    "READ",
    "TRUSTED_METHODS",
    "WRITE",
    "LocalPrincipal",
    "PrincipalLike",
    "actor_for",
    "auth_provenance",
    "session_authz_available",
]
