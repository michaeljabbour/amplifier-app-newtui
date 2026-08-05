"""ApprovalBroker tests: FIFO, options, allow-always pass-through, defer/timeout.

Pure asyncio — no Textual, no network, no real kernel.
"""

from __future__ import annotations

import asyncio

import pytest

from amplifier_app_tui.kernel.approval import (
    ALLOW_ALWAYS,
    ALLOW_ONCE,
    DENY,
    STANDARD_OPTIONS,
    ApprovalBroker,
    ApprovalDetail,
    is_allow,
    presented_options,
)
from amplifier_app_tui.model.queues import NeedsYouQueue
from amplifier_app_tui.model.trust import DenialLog


def make_broker() -> tuple[ApprovalBroker, NeedsYouQueue, DenialLog]:
    needs_you = NeedsYouQueue()
    denial_log = DenialLog()
    broker = ApprovalBroker(needs_you=needs_you, denial_log=denial_log)
    return broker, needs_you, denial_log


async def _settle() -> None:
    for _ in range(3):
        await asyncio.sleep(0)


# -- options ------------------------------------------------------------------


def test_presented_options_always_contain_standard_triple() -> None:
    assert presented_options([]) == STANDARD_OPTIONS
    assert presented_options(["Allow", "Deny"]) == STANDARD_OPTIONS
    assert presented_options(["Allow once", "Deny", "Skip"]) == (
        "Allow once",
        "Allow always",
        "Deny",
        "Skip",
    )


def test_is_allow_family_matching() -> None:
    assert is_allow("Allow once")
    assert is_allow("Allow always")
    assert is_allow("Allow")
    assert not is_allow("Deny")
    assert not is_allow("Skip")


# -- FIFO / answer ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_approval_fifo_and_answer() -> None:
    broker, _, _ = make_broker()
    first = asyncio.ensure_future(broker.request_approval("Allow first?", ["Deny"]))
    second = asyncio.ensure_future(broker.request_approval("Allow second?", ["Deny"]))
    await _settle()

    assert [t.prompt for t in broker.pending] == ["Allow first?", "Allow second?"]
    head = broker.head
    assert head is not None and head.prompt == "Allow first?"
    assert head.options == STANDARD_OPTIONS

    broker.answer(head.ticket_id, ALLOW_ONCE)
    assert await first == ALLOW_ONCE

    head = broker.head
    assert head is not None and head.prompt == "Allow second?"
    broker.answer(head.ticket_id, DENY)
    assert await second == DENY
    assert broker.pending == ()


@pytest.mark.asyncio
async def test_answer_rejects_unknown_ticket_and_invalid_choice() -> None:
    broker, _, _ = make_broker()
    task = asyncio.ensure_future(broker.request_approval("Allow x?", []))
    await _settle()
    head = broker.head
    assert head is not None
    with pytest.raises(ValueError):
        broker.answer(head.ticket_id, "Maybe")
    with pytest.raises(KeyError):
        broker.answer("approval-999", ALLOW_ONCE)
    broker.answer(head.ticket_id, DENY)
    assert await task == DENY


@pytest.mark.asyncio
async def test_listeners_fire_on_queue_changes() -> None:
    broker, _, _ = make_broker()
    calls: list[int] = []
    remove = broker.add_listener(lambda: calls.append(1))
    task = asyncio.ensure_future(broker.request_approval("Allow x?", []))
    await _settle()
    assert calls  # new ticket notified
    broker.answer(broker.head.ticket_id, DENY)  # type: ignore[union-attr]
    await task
    assert len(calls) >= 2
    remove()


# -- defer / deny-and-continue -------------------------------------------------


@pytest.mark.asyncio
async def test_defer_parks_decision_and_resolves_current_call_to_deny() -> None:
    broker, needs_you, denial_log = make_broker()
    broker.stage_detail(
        "Push upstream?",
        ApprovalDetail(
            command="git push origin main",
            cwd="/work/project",
            rule="external write needs approval",
            capability="exec",
        ),
    )
    task = asyncio.create_task(broker.request_approval("Push upstream?", [], timeout=3600))
    await _settle()
    ticket = broker.head
    assert ticket is not None

    item = broker.defer(ticket.ticket_id)

    assert item is not None
    assert item.question == "Push upstream?"
    assert item.reason == "external write needs approval"
    assert item.action == "git push origin main"
    assert item.choices == STANDARD_OPTIONS
    assert item.highlight == ""
    assert needs_you.pending == (item,)
    assert broker.head is None  # the hidden ticket can no longer block the bar
    assert await asyncio.wait_for(task, timeout=1) == DENY
    assert broker.pending == ()
    assert denial_log.total_count == 1
    assert denial_log.records[0].reason == "approval deferred · denied for current attempt"


@pytest.mark.asyncio
async def test_defer_exposes_next_fifo_ticket_before_cleanup() -> None:
    broker, needs_you, _ = make_broker()
    first = asyncio.create_task(broker.request_approval("First?", [], timeout=3600))
    second = asyncio.create_task(broker.request_approval("Second?", [], timeout=3600))
    await _settle()
    first_ticket = broker.head
    assert first_ticket is not None

    broker.defer(first_ticket.ticket_id)
    next_ticket = broker.head
    assert next_ticket is not None and next_ticket.prompt == "Second?"
    with pytest.raises(ValueError, match="already deferred"):
        broker.defer(first_ticket.ticket_id)
    assert len(needs_you.pending) == 1

    broker.answer(next_ticket.ticket_id, ALLOW_ONCE)
    assert await asyncio.wait_for(first, timeout=1) == DENY
    assert await asyncio.wait_for(second, timeout=1) == ALLOW_ONCE


@pytest.mark.asyncio
async def test_defer_still_denies_when_needs_you_queue_is_full() -> None:
    needs_you = NeedsYouQueue()
    for index in range(needs_you._MAX_DECISIONS):  # noqa: SLF001 - boundary test
        needs_you.defer(f"existing-{index}")
    broker = ApprovalBroker(needs_you=needs_you)
    task = asyncio.create_task(broker.request_approval("One more?", [], timeout=3600))
    await _settle()
    ticket = broker.head
    assert ticket is not None

    assert broker.defer(ticket.ticket_id) is None
    assert await asyncio.wait_for(task, timeout=1) == DENY
    assert len(needs_you.pending) == needs_you._MAX_DECISIONS  # noqa: SLF001


# -- allow-always pass-through ------------------------------------------------------


@pytest.mark.asyncio
async def test_allow_always_passes_through_without_local_bookkeeping() -> None:
    """User directive: the asker (natively hooks-approval) owns allow-always
    persistence — the broker must NOT keep a shadow remember table, so an
    identical follow-up ask presents a fresh ticket."""
    broker, _, _ = make_broker()
    task = asyncio.ensure_future(broker.request_approval("Allow git push?", []))
    await _settle()
    broker.answer(broker.head.ticket_id, ALLOW_ALWAYS)  # type: ignore[union-attr]
    assert await task == ALLOW_ALWAYS

    again = asyncio.ensure_future(broker.request_approval("Allow git push?", []))
    await _settle()
    assert broker.head is not None  # asked again — no local short-circuit
    broker.answer(broker.head.ticket_id, ALLOW_ONCE)
    assert await again == ALLOW_ONCE


# -- timeout -----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_with_allow_default_returns_allow_once() -> None:

    broker, _, denial_log = make_broker()
    choice = await broker.request_approval("Allow read?", [], timeout=0.01, default="allow")
    assert choice == ALLOW_ONCE
    assert denial_log.total_count == 0


# -- staged detail -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_staged_details_pair_fifo_per_prompt() -> None:
    broker, _, _ = make_broker()
    broker.stage_detail("Allow x?", ApprovalDetail(command="first"))
    broker.stage_detail("Allow x?", ApprovalDetail(command="second"))
    t1 = asyncio.ensure_future(broker.request_approval("Allow x?", []))
    t2 = asyncio.ensure_future(broker.request_approval("Allow x?", []))
    await _settle()
    commands = [t.detail.command for t in broker.pending]
    assert commands == ["first", "second"]
    for ticket in list(broker.pending):
        broker.answer(ticket.ticket_id, DENY)
    assert await t1 == DENY
    assert await t2 == DENY
