"""Real-runtime needs-you deferrals carry the native approval data.

The kernel parks every deferral (broker ctrl-y park, auto-classifier
deny, escalation) in the shared NeedsYouQueue with the fields the ctrl-y
UI and the /improve override join need — question, reason, choices,
highlight, action — sourced from the native approval payload
(ApprovalTicket / staged ApprovalDetail), never re-parsed from message
strings. A ``level="decision"`` Notification carrying the item's
``decision_id`` surfaces each deferral; the app resolves that item
instead of parking a duplicate.

Offline: bare kernel objects, an unbooted ``RealRuntimeAdapter``, and one
Pilot flow over the base adapter feeding the real event path.
"""

from __future__ import annotations

from typing import Any


import pytest

from amplifier_app_newtui.kernel import events as ev
from amplifier_app_newtui.kernel.approval import (
    ALLOW_ONCE,
    STANDARD_OPTIONS,
    deferral_highlight,
)

from amplifier_app_newtui.kernel.governance_hook import GovernanceHook
from amplifier_app_newtui.kernel.runtime import RealRuntime
from amplifier_app_newtui.model.blocks import BlockIdAllocator
from amplifier_app_newtui.model.lanes import LaneRegistry
from amplifier_app_newtui.model.queues import NeedsYouItem, NeedsYouQueue
from amplifier_app_newtui.model.trust import DenialLog
from amplifier_app_newtui.ui.reducer import TranscriptReducer
from amplifier_app_newtui.model.turn import OutcomeLedger
from amplifier_app_newtui.ui.runtime_adapter import RealRuntimeAdapter

ROOT = "sess-root"
PUSH = "git push origin main"


# ---------------------------------------------------------------------------
# deferral_highlight — the teal accent substring
# ---------------------------------------------------------------------------


def test_deferral_highlight_prefers_first_matching_candidate() -> None:
    question = f"Allow {PUSH}?"
    # Target first (the mockup accents the target, e.g. ``mj/waypoint``).
    assert deferral_highlight(question, "origin main", PUSH) == "origin main"
    # Absent target falls through to the command.
    assert deferral_highlight(question, "/tmp/elsewhere", PUSH) == PUSH
    assert deferral_highlight(question, "", PUSH) == PUSH


def test_deferral_highlight_degrades_to_empty() -> None:
    assert deferral_highlight("Allow x?", "", "") == ""
    assert deferral_highlight("Allow x?", "y", "z") == ""
    # Beyond the queue's 200-char highlight bound: no accent, not a broken one.
    long_cmd = "x" * 201
    assert deferral_highlight(f"Allow {long_cmd}?", long_cmd) == ""


# ---------------------------------------------------------------------------
# Governance classifier deferral — highlight from action/target
# ---------------------------------------------------------------------------



@pytest.mark.asyncio
async def test_classifier_deferral_carries_highlight_and_action() -> None:
    class AlwaysDeny:
        async def classify(self, **kwargs: Any) -> tuple[bool, str]:
            return (False, "not authorized")

    needs_you = NeedsYouQueue()
    hook = GovernanceHook(
        ROOT,
        mode=lambda: "auto",
        denial_log=DenialLog(),
        needs_you=needs_you,
        classifier=AlwaysDeny(),
    )
    result = await hook.handle_event(
        "tool:pre",
        {"session_id": ROOT, "tool_name": "bash", "tool_input": {"command": PUSH}},
    )
    assert result.action == "deny"
    item = needs_you.pending[0]
    assert item.question == f"Allow {PUSH}?"
    assert item.reason == "not authorized"
    assert item.choices == STANDARD_OPTIONS
    assert item.highlight == PUSH
    assert item.action == PUSH


# ---------------------------------------------------------------------------
# NeedsYouQueue defer listeners — per-item deferral callbacks
# ---------------------------------------------------------------------------


def test_defer_listener_fires_per_item_and_unregisters() -> None:
    queue = NeedsYouQueue()
    seen: list[NeedsYouItem] = []
    remove = queue.add_defer_listener(seen.append)
    item = queue.defer("q?", "r", action="a")
    assert seen == [item]
    queue.answer(item.decision_id, "yes")  # answer/dismiss never re-fire it
    assert seen == [item]
    remove()
    queue.defer("q2?", "r")
    assert seen == [item]


# ---------------------------------------------------------------------------
# RealRuntime — kernel-side deferral emits ONE decision Notification
# ---------------------------------------------------------------------------


def test_real_runtime_emits_decision_notification_on_defer() -> None:
    runtime = RealRuntime()  # never started: queue/bridge/broker exist
    item = runtime.needs_you.defer(
        f"Allow {PUSH}?", "not authorized", choices=STANDARD_OPTIONS, action=PUSH
    )
    event = runtime.queue.get_nowait()
    assert isinstance(event, ev.Notification)
    assert event.level == "decision"
    assert event.source == "needs_you"
    assert event.decision_id == item.decision_id
    assert item.question in event.message


# ---------------------------------------------------------------------------
# RealRuntimeAdapter — resolves the parked item by decision_id
# ---------------------------------------------------------------------------


def test_real_adapter_resolves_deferred_decision_by_id() -> None:
    adapter = RealRuntimeAdapter(bundle="x")  # unbooted: shared queue only
    item = adapter.needs_you.defer(
        f"Allow {PUSH}?",
        "not authorized",
        choices=STANDARD_OPTIONS,
        highlight=PUSH,
        action=PUSH,
    )
    assert adapter.deferred_decision("ignored", item.decision_id) == (
        f"Allow {PUSH}?",
        "not authorized",
        STANDARD_OPTIONS,
        PUSH,
        PUSH,
    )
    # Unknown / missing id degrades to the message-only base stub.
    assert adapter.deferred_decision("msg", "decision-999") == ("msg", "", (), "", "")
    assert adapter.deferred_decision("msg") == ("msg", "", (), "", "")


def test_real_adapter_narration_names_the_action() -> None:
    adapter = RealRuntimeAdapter(bundle="x")
    assert (
        adapter.decision_narration(ALLOW_ONCE, PUSH)
        == f"Applying decision: {ALLOW_ONCE} · {PUSH}"
    )
    assert adapter.decision_narration(ALLOW_ONCE) == f"Applying decision: {ALLOW_ONCE}"


# ---------------------------------------------------------------------------
# Reducer — decision notifications route the decision_id to the host
# ---------------------------------------------------------------------------


class _RecordingHost:
    """Minimal ReducerHost recording decision_deferred calls."""

    mode_id = "auto"

    def __init__(self) -> None:
        self.deferred: list[tuple[str, str]] = []
        self.notices: list[str] = []

    def append_block(self, block: Any) -> None:
        pass

    def replace_block(self, block: Any) -> None:
        pass

    def remove_block(self, block_id: str) -> None:
        pass

    def show_notice(self, text: str) -> None:
        self.notices.append(text)

    def set_mode_by_id(self, mode_id: str, *, notify: bool = True) -> None:
        pass

    def turn_started(self) -> None:
        pass

    def turn_finished(self) -> None:
        pass

    def lanes_changed(self) -> None:
        pass

    def plan_changed(self, items: Any) -> None:
        pass

    def approval_opened(self, prompt: str, options: tuple[str, ...]) -> None:
        pass

    def decision_deferred(self, message: str, decision_id: str = "") -> None:
        self.deferred.append((message, decision_id))

    def stream_opened(self, block_type: str) -> None:
        pass

    def stream_delta(self, text: str) -> None:
        pass

    def stream_closed(self) -> None:
        pass

    def lane_tail_updated(self, text: str) -> None:
        pass

    def lane_tail_cleared(self) -> None:
        pass


def test_reducer_routes_decision_id_to_host() -> None:
    host = _RecordingHost()
    reducer = TranscriptReducer(
        host,
        allocator=BlockIdAllocator(),
        ledger=OutcomeLedger(),
        lanes=LaneRegistry(),
    )
    reducer.handle(
        ev.Notification(
            session_id=ROOT,
            message="decision deferred to queue · Allow x?",
            level="decision",
            source="needs_you",
            decision_id="decision-7",
        )
    )
    assert host.deferred == [("decision deferred to queue · Allow x?", "decision-7")]
    assert host.notices == ["decision deferred to queue · Allow x?"]
