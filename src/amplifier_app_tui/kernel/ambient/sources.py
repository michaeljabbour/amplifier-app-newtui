"""E8 -- the external-source **port**, and a local implementation of it.

-- What cannot be built here, stated plainly --------------------------------

**No real Teams or Outlook connector is shipped, and none can be built or
verified in this environment.** A working connector needs a Microsoft Graph
application registration, delegated permission scopes granted by a real
tenant, an interactive consent flow, and live network access to
``graph.microsoft.com``. None of those exist offline, and every one of them is
a place where a guess would be silently wrong: the exact scope names, whether
tenant admin consent is required, the pagination and throttling behaviour, and
the shape of the returned resources. Writing a plausible-looking ``TeamsSource``
against unverified API surfaces would produce code that *appears* finished and
fails at first contact with a real tenant -- the worst outcome available.

So this module ships the two things that CAN be built and verified offline:

1. **The port** (:class:`SourcePort`) -- the interface a real connector must
   implement, constrained by the permission model E2 already enforces. Its
   shape is the design work: every method takes a *selector* (the same
   narrowing dict a grant carries), returns *previews* rather than bodies, and
   has no notion of ambient policy of its own.
2. **A working local implementation** (:class:`LocalFileSource`) -- reads
   JSONL from a directory. It is a genuine implementation of the port, used by
   the tests and usable by hand; it is not a mock of Teams.

When a real connector is built, the work is the Graph client and its consent
flow. The permission check, the audit trail, the confirmation echo and the
redaction policy are already done and do not move.

-- Why previews, not bodies -------------------------------------------------

:class:`SourceItem` carries a short ``preview`` and deliberately has no
``body`` field. Reading someone's mail is itself a privacy act; an ambient
assistant that pulls full bodies into a notification path -- where they can
reach a phone tray, a push topic, or a log -- has widened the blast radius of
every downstream bug. Full content is fetched only on an authenticated
surface, by an explicit call the user is present for.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..session_control import SessionControl
from .grants import SEND, GrantDecision, GrantStore, consume_grant
from .principal import PrincipalLike

MAX_PREVIEW_CHARS = 160
"""Previews are capped at the source boundary, not at the notification
boundary, so a long body cannot reach a destination that forgot to truncate."""


@dataclass(frozen=True)
class SourceItem:
    """One item from an external source. **Preview only -- never a body.**"""

    item_id: str
    sender: str = ""
    subject: str = ""
    received_at: float = 0.0
    preview: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "sender": self.sender,
            "subject": self.subject,
            "received_at": self.received_at,
            "preview": self.preview[:MAX_PREVIEW_CHARS],
        }


@dataclass(frozen=True)
class SourceSendResult:
    """The outcome of an externally-visible send."""

    ok: bool
    item_id: str = ""
    detail: str = ""


@runtime_checkable
class SourcePort(Protocol):
    """What a real Teams/Outlook connector must implement.

    Three deliberate constraints, each of which exists so the connector stays
    a *transport* and cannot grow its own policy:

    - Every call takes a ``selector`` -- the same narrowing dict the grant
      carries -- so a connector physically cannot be asked for "everything".
    - :meth:`fetch` returns previews (see the module docstring).
    - Nothing here consults grants, writes audit entries, or knows about
      sessions. :class:`GrantedSource` owns all of that, once, for every
      connector that will ever exist.
    """

    @property
    def scope(self) -> str:
        """The grant scope this source answers for, e.g. ``source:outlook``."""
        ...

    def fetch(self, selector: Mapping[str, str], *, limit: int = 20) -> Sequence[SourceItem]: ...

    def send(self, selector: Mapping[str, str], body: str) -> SourceSendResult: ...


@dataclass
class LocalFileSource:
    """A working :class:`SourcePort` over a directory of JSONL files.

    Real, not a stub: it reads actual files off disk, applies the selector,
    and can send (appending to an outbox). It exists so the permission model,
    the confirmation echo and the audit trail are exercised end-to-end offline
    -- and so the port has at least one proven implementation before a Graph
    connector is attempted against it.

    Layout::

        <root>/items.jsonl     # one JSON object per item
        <root>/outbox.jsonl    # appended by send()
    """

    root: Path
    scope_name: str = "source:local"
    _sent: int = field(default=0, init=False, repr=False)

    @property
    def scope(self) -> str:
        return self.scope_name

    def fetch(self, selector: Mapping[str, str], *, limit: int = 20) -> Sequence[SourceItem]:
        path = Path(self.root) / "items.jsonl"
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        items: list[SourceItem] = []
        for line in lines:
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(raw, Mapping):
                continue
            if not all(str(raw.get(k, "")) == v for k, v in selector.items()):
                continue
            items.append(
                SourceItem(
                    item_id=str(raw.get("item_id", "")),
                    sender=str(raw.get("sender", "")),
                    subject=str(raw.get("subject", "")),
                    received_at=float(raw.get("received_at") or 0.0),
                    preview=str(raw.get("preview", ""))[:MAX_PREVIEW_CHARS],
                )
            )
            if len(items) >= limit:
                break
        return items

    def send(self, selector: Mapping[str, str], body: str) -> SourceSendResult:
        path = Path(self.root) / "outbox.jsonl"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"selector": dict(selector), "body": body}) + "\n")
        except OSError as error:
            return SourceSendResult(False, detail=str(error))
        self._sent += 1
        return SourceSendResult(True, item_id=f"local-{self._sent}")


class GrantedSource:
    """The enforcement wrapper every source is reached through.

    This is where E2 and E8 meet, and it is deliberately the *only* place:
    a connector never sees a grant, and the grant store never sees a
    connector. Each call re-consults the grant store **at use** and attributes
    the outcome (``source.read`` / ``source.send`` / ``source.denied``) into
    the consuming session's own ``control-audit.jsonl``.

    A denial returns empty/failed with the decision attached -- it never
    raises and never silently returns partial data, so the caller can *say*
    "I can't see your mail" rather than quietly omitting it.
    """

    def __init__(
        self,
        source: SourcePort,
        grants: GrantStore,
        principal: PrincipalLike,
        *,
        control: SessionControl | None = None,
    ) -> None:
        self._source = source
        self._grants = grants
        self._principal = principal
        self._control = control

    @property
    def scope(self) -> str:
        return self._source.scope

    def fetch(
        self, selector: Mapping[str, str], *, limit: int = 20
    ) -> tuple[Sequence[SourceItem], GrantDecision]:
        decision = consume_grant(
            self._grants,
            self._control,
            self._principal,
            scope=self._source.scope,
            verb="read",
            selector=selector,
        )
        if not decision.allowed:
            return (), decision
        return self._source.fetch(selector, limit=limit), decision

    def send(
        self, selector: Mapping[str, str], body: str
    ) -> tuple[SourceSendResult, GrantDecision]:
        decision = consume_grant(
            self._grants,
            self._control,
            self._principal,
            scope=self._source.scope,
            verb=SEND,
            selector=selector,
        )
        if not decision.allowed:
            return SourceSendResult(False, detail=decision.reason), decision
        return self._source.send(selector, body), decision


__all__ = [
    "MAX_PREVIEW_CHARS",
    "GrantedSource",
    "LocalFileSource",
    "SourceItem",
    "SourcePort",
    "SourceSendResult",
]
