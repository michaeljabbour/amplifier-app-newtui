"""ApprovalBroker: the app's ApprovalSystem implementation (ADR-0007 §Approvals).

A request broker with a FIFO of :class:`ApprovalTicket`\\ s. The kernel-facing
contract is exactly the 4-point-boundary signature::

    async request_approval(prompt, options, timeout, default) -> str

We own both ends of the approval path (the governance hook stages a
structured :class:`ApprovalDetail` on this broker; the kernel then calls
``request_approval`` with the same prompt), so rich detail travels through
the broker itself — no module-global keyed-by-prompt smuggling. Staging is
instance-scoped and consumed FIFO per prompt, so concurrent identical
prompts pair with their details in request order.

Fail-closed invariants (Rust string-matches "Allow"-family options):

- Presented options ALWAYS contain the verbatim strings ``Allow once`` /
  ``Allow always`` / ``Deny``.
- Timeouts resolve to the ticket's default (deny unless stated otherwise).
- ``defer(ticket_id)`` parks the head ticket into the NeedsYouQueue
  (deny-and-continue, ADR-0007 resolution 5): the future keeps waiting; at
  timeout it resolves to the default (deny), the denial lands in the
  DenialLog AND the needs-you item stays pending — retro-answerable, so a
  later answer becomes a next-turn instruction ("Applying decision: …").

"Allow always" persistence is NOT handled here (user directive:
permissions are managed natively) — the asker (hooks-approval) receives
the choice string back and owns remember/allow-always bookkeeping.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from time import monotonic
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..model.queues import NeedsYouItem, NeedsYouQueue
from ..model.trust import CapabilityClass, DenialLog

ALLOW_ONCE = "Allow once"
ALLOW_ALWAYS = "Allow always"
DENY = "Deny"
STANDARD_OPTIONS: tuple[str, str, str] = (ALLOW_ONCE, ALLOW_ALWAYS, DENY)

_ALLOW_FAMILY = frozenset({"allow", "allow once", "allow always"})

ApprovalDefault = Literal["allow", "deny"]

Listener = Callable[[], None]


class ApprovalDetail(BaseModel):
    """Structured payload behind one approval prompt (ctrl-a detail view).

    Fields mirror the mockup's detail rows: command, cwd, the trust rule
    that fired, and the capability class.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: str = ""
    cwd: str = ""
    rule: str = ""
    capability: str = ""
    tool_name: str = ""
    tool_input: dict[str, Any] = Field(default_factory=dict)


def is_allow(choice: str) -> bool:
    """True when *choice* is in the fail-closed Allow family."""
    return choice.strip().casefold() in _ALLOW_FAMILY


@dataclass(slots=True)
class ApprovalTicket:
    """One in-flight approval request (FIFO position = arrival order)."""

    ticket_id: str
    prompt: str
    options: tuple[str, ...]
    detail: ApprovalDetail
    future: asyncio.Future[str]
    timeout: float
    default: ApprovalDefault
    created_at: float
    deferred: bool = False
    decision_id: str = ""
    """NeedsYouQueue decision id once deferred; empty otherwise."""


class ApprovalBroker:
    """FIFO approval request broker (kernel ApprovalSystem implementation).

    The inline approval bar answers :attr:`head`; ctrl-y calls
    :meth:`defer` on it. UI listeners fire on every queue change.
    """

    def __init__(
        self,
        *,
        needs_you: NeedsYouQueue | None = None,
        denial_log: DenialLog | None = None,
        clock: Callable[[], float] = monotonic,
        min_timeout: float = 0.0,
    ) -> None:
        self._needs_you = needs_you
        self._denial_log = denial_log
        self._clock = clock
        self._min_timeout = min_timeout
        """Floor for ticket timeouts. An interactive app sets this HIGH:
        the kernel's default (300s) silently timed approvals out to deny
        while the supervisor was still reading the plan (found live —
        every file write of a run 'came back denied' untouched)."""
        self._next_id = 1
        self._tickets: list[ApprovalTicket] = []
        self._staged: dict[str, deque[ApprovalDetail]] = {}
        self._listeners: list[Listener] = []

    # -- introspection ------------------------------------------------------

    @property
    def pending(self) -> tuple[ApprovalTicket, ...]:
        """All unresolved tickets in FIFO order."""
        return tuple(self._tickets)

    @property
    def head(self) -> ApprovalTicket | None:
        """The ticket the inline approval bar is answering (first
        non-deferred pending ticket)."""
        for ticket in self._tickets:
            if not ticket.deferred:
                return ticket
        return None

    def add_listener(self, listener: Listener) -> Callable[[], None]:
        self._listeners.append(listener)

        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    # -- detail staging (governance hook side) -------------------------------

    def stage_detail(self, prompt: str, detail: ApprovalDetail) -> None:
        """Attach structured detail to the next ``request_approval`` call
        bearing *prompt*. Instance-scoped, FIFO per prompt."""
        self._staged.setdefault(prompt, deque()).append(detail)

    # -- kernel-facing contract ----------------------------------------------

    async def request_approval(
        self,
        prompt: str,
        options: list[str] | None = None,
        timeout: float = 300.0,
        default: ApprovalDefault = "deny",
    ) -> str:
        """Ask the human; resolves via :meth:`answer`, :meth:`defer` +
        needs-you, or timeout-to-default. Never raises to the kernel."""
        timeout = max(timeout, self._min_timeout)
        detail = self._pop_staged(prompt)
        # NO local "Allow always" bookkeeping (user directive): the asker
        # (natively, hooks-approval) owns allow-always persistence — it
        # receives the choice string back and stops asking. A second
        # remember table here would shadow the native one.
        ticket = ApprovalTicket(
            ticket_id=f"approval-{self._next_id}",
            prompt=prompt,
            options=presented_options(options or ()),
            detail=detail,
            future=asyncio.get_running_loop().create_future(),
            timeout=timeout,
            default=default,
            created_at=self._clock(),
        )
        self._next_id += 1
        self._tickets.append(ticket)
        self._notify()

        try:
            async with asyncio.timeout(timeout):
                choice = await ticket.future
        except TimeoutError:
            choice = ALLOW_ONCE if default == "allow" else DENY
            self._record_timeout(ticket, choice)
        finally:
            if ticket in self._tickets:
                self._tickets.remove(ticket)
            self._notify()

        return choice

    # -- UI-facing actions ---------------------------------------------------

    def answer(self, ticket_id: str, choice: str) -> None:
        """Resolve one pending ticket with the human's *choice*.

        A deferred ticket answered before its timeout is applied live, so
        its needs-you item is dismissed (nothing left to retro-apply).
        Raises ``KeyError`` for unknown/already-resolved tickets and
        ``ValueError`` for a choice not among the presented options.
        """
        ticket = self._find(ticket_id)
        if choice not in ticket.options:
            raise ValueError(f"choice {choice!r} is not one of {ticket.options}")
        if not ticket.future.done():
            ticket.future.set_result(choice)
        if ticket.deferred and ticket.decision_id and self._needs_you is not None:
            try:
                self._needs_you.dismiss(ticket.decision_id)
            except (KeyError, ValueError):
                pass

    def defer(self, ticket_id: str) -> NeedsYouItem:
        """Park a pending ticket into the NeedsYouQueue (ctrl-y).

        The turn is NOT held: the ticket keeps its future and times out to
        its default (deny) — landing in the DenialLog — while the
        needs-you item stays pending and retro-answerable.
        """
        if self._needs_you is None:
            raise RuntimeError("broker has no NeedsYouQueue to defer into")
        ticket = self._find(ticket_id)
        if ticket.deferred:
            raise ValueError(f"ticket {ticket_id} is already deferred")
        item = self._needs_you.defer(
            ticket.prompt,
            ticket.detail.rule or "deferred approval",
            choices=ticket.options,
            highlight=deferral_highlight(
                ticket.prompt, ticket.detail.cwd, ticket.detail.command
            ),
            # MUST equal _record_timeout's DenialLog key: a retro-answer's
            # override joins the timeout denial for /improve trust slots.
            action=ticket.detail.command or ticket.prompt,
        )
        ticket.deferred = True
        ticket.decision_id = item.decision_id
        self._notify()
        return item

    # -- internals -----------------------------------------------------------

    def _pop_staged(self, prompt: str) -> ApprovalDetail:
        queue = self._staged.get(prompt)
        if not queue:
            return ApprovalDetail()
        detail = queue.popleft()
        if not queue:
            del self._staged[prompt]
        return detail

    def _record_timeout(self, ticket: ApprovalTicket, choice: str) -> None:
        if choice != DENY or self._denial_log is None:
            return
        capability = _capability_or_exec(ticket.detail.capability)
        self._denial_log.record_denial(
            capability=capability,
            action=ticket.detail.command or ticket.prompt,
            reason="approval timed out · denied by default",
        )

    def _find(self, ticket_id: str) -> ApprovalTicket:
        for ticket in self._tickets:
            if ticket.ticket_id == ticket_id:
                return ticket
        raise KeyError(f"unknown approval ticket: {ticket_id}")

    def _notify(self) -> None:
        for listener in tuple(self._listeners):
            listener()


def deferral_highlight(question: str, *candidates: str) -> str:
    """First candidate appearing verbatim in *question* — the teal accent
    substring of a needs-you row (DESIGN-SPEC §7). Candidates come from
    the native approval payload (target/cwd before command); anything
    absent from the question, empty, or beyond the queue's 200-char
    highlight bound yields no accent rather than a broken one."""
    for candidate in candidates:
        clean = " ".join(str(candidate or "").split())
        if clean and len(clean) <= 200 and clean in question:
            return clean
    return ""


def presented_options(options: Iterable[str]) -> tuple[str, ...]:
    """The options the approval bar shows: the verbatim standard triple,
    plus any caller-provided options outside the standard/allow/deny set."""
    extras = tuple(
        option
        for option in options
        if option not in STANDARD_OPTIONS
        and option.strip().casefold() not in {"allow", "deny"}
    )
    return STANDARD_OPTIONS + extras


def _capability_or_exec(value: str) -> CapabilityClass:
    try:
        return CapabilityClass(value)
    except ValueError:
        return CapabilityClass.EXEC


__all__ = [
    "ALLOW_ALWAYS",
    "ALLOW_ONCE",
    "ApprovalBroker",
    "ApprovalDetail",
    "ApprovalTicket",
    "DENY",
    "STANDARD_OPTIONS",
    "deferral_highlight",
    "is_allow",
    "presented_options",
]
