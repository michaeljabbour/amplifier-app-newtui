"""ApprovalBroker tests: FIFO, options, remember keys, defer/timeout.

Pure asyncio — no Textual, no network, no real kernel.
"""

from __future__ import annotations

import asyncio

import pytest

from amplifier_app_newtui.kernel.approval import (
    ALLOW_ALWAYS,
    ALLOW_ONCE,
    DENY,
    STANDARD_OPTIONS,
    ApprovalBroker,
    ApprovalDetail,
    is_allow,
    presented_options,
    remember_key,
)
from amplifier_app_newtui.model.queues import NeedsYouQueue
from amplifier_app_newtui.model.trust import DenialLog


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


# -- remember keys --------------------------------------------------------------


def test_remember_key_file_tool_scopes_by_parent_dir() -> None:
    key = remember_key("write_file", {"file_path": "/repo/src/app.py"})
    assert key == "write_file:/repo/src/"
    assert key == remember_key("write_file", {"file_path": "/repo/src/other.py"})
    assert key != remember_key("write_file", {"file_path": "/repo/docs/readme.md"})


def test_remember_key_bash_scopes_by_two_token_prefix() -> None:
    key = remember_key("bash", {"command": "git push origin main"})
    assert key == "bash:git push"
    assert key == remember_key("bash", {"command": "git push --force"})
    assert remember_key("bash", {"command": "ls"}) == "bash:ls"


def test_remember_key_other_tools_fall_back_to_name() -> None:
    assert remember_key("web_fetch", {"url": "https://x"}) == "web_fetch"


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


# -- allow-always remember --------------------------------------------------------


@pytest.mark.asyncio
async def test_allow_always_short_circuits_same_scope() -> None:
    broker, _, _ = make_broker()
    detail = ApprovalDetail(
        command="git push origin main",
        tool_name="bash",
        tool_input={"command": "git push origin main"},
    )
    broker.stage_detail("Allow git push origin main?", detail)
    task = asyncio.ensure_future(
        broker.request_approval("Allow git push origin main?", [])
    )
    await _settle()
    broker.answer(broker.head.ticket_id, ALLOW_ALWAYS)  # type: ignore[union-attr]
    assert await task == ALLOW_ALWAYS

    # Same 2-token scope resolves instantly without a ticket.
    broker.stage_detail(
        "Allow git push --tags?",
        ApprovalDetail(tool_name="bash", tool_input={"command": "git push --tags"}),
    )
    assert await broker.request_approval("Allow git push --tags?", []) == ALLOW_ALWAYS
    assert broker.pending == ()

    # Different scope still asks.
    broker.stage_detail(
        "Allow rm x?",
        ApprovalDetail(tool_name="bash", tool_input={"command": "rm x"}),
    )
    other = asyncio.ensure_future(broker.request_approval("Allow rm x?", []))
    await _settle()
    assert broker.head is not None
    broker.answer(broker.head.ticket_id, DENY)
    assert await other == DENY


# -- defer / timeout ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_defer_parks_into_needs_you_then_denies_on_timeout() -> None:
    broker, needs_you, denial_log = make_broker()
    broker.stage_detail(
        "Allow push?",
        ApprovalDetail(command="git push", rule="ask net", capability="net"),
    )
    task = asyncio.ensure_future(
        broker.request_approval("Allow push?", [], timeout=0.05)
    )
    await _settle()
    head = broker.head
    assert head is not None
    item = broker.defer(head.ticket_id)

    # Deferred: no longer the approval-bar head, parked in needs-you.
    assert broker.head is None
    assert needs_you.pending_count == 1
    assert item.question == "Allow push?"
    assert item.choices == STANDARD_OPTIONS

    # Timeout resolves to the default deny…
    assert await task == DENY
    # …lands in the DenialLog…
    assert denial_log.total_count == 1
    assert denial_log.records[-1].reason == "approval timed out · denied by default"
    # …and stays retro-answerable in needs-you.
    assert needs_you.pending_count == 1
    answered = needs_you.answer(item.decision_id, ALLOW_ONCE)
    assert answered.status == "answered"


@pytest.mark.asyncio
async def test_deferred_ticket_answered_before_timeout_dismisses_item() -> None:
    broker, needs_you, denial_log = make_broker()
    task = asyncio.ensure_future(
        broker.request_approval("Allow thing?", [], timeout=5.0)
    )
    await _settle()
    ticket = broker.pending[0]
    item = broker.defer(ticket.ticket_id)
    broker.answer(ticket.ticket_id, ALLOW_ONCE)
    assert await task == ALLOW_ONCE
    # Applied live: nothing left to retro-answer, no denial recorded.
    assert needs_you.pending_count == 0
    assert needs_you.items[0].decision_id == item.decision_id
    assert needs_you.items[0].status == "dismissed"
    assert denial_log.total_count == 0


@pytest.mark.asyncio
async def test_timeout_with_allow_default_returns_allow_once() -> None:
    broker, _, denial_log = make_broker()
    choice = await broker.request_approval(
        "Allow read?", [], timeout=0.01, default="allow"
    )
    assert choice == ALLOW_ONCE
    assert denial_log.total_count == 0


@pytest.mark.asyncio
async def test_defer_requires_needs_you_queue() -> None:
    broker = ApprovalBroker()
    task = asyncio.ensure_future(broker.request_approval("Allow x?", [], timeout=1.0))
    await _settle()
    with pytest.raises(RuntimeError):
        broker.defer(broker.pending[0].ticket_id)
    broker.answer(broker.pending[0].ticket_id, DENY)
    assert await task == DENY


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
