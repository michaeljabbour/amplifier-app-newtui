"""Deferred-decision UX (user report, auto mode "asks if risky").

Four failures, one contract (mirrored 1:1 by the Rust client's tests of
the same names):

1. Blocked lines rendered ``⊘ blocked · <ENTIRE raw heredoc>`` — now a
   verb-noun digest with the raw command behind the click-to-expand body.
2. Needs-you rows dumped the same raw command — now the compact digest.
3. Nothing said WHY — the governance escalation reason renders as its own
   dim ``why · …`` line, and the blocked line points at the next step
   (``needs your ok — ctrl+y to review``).
4. The answer path: chips act in-process (pinned elsewhere); the protocol
   client answers through the additive ``decision`` serve op
   (test_serve_offline.py::test_serve_decision_op_answers_deferred_decision).
"""

from __future__ import annotations

from amplifier_app_tui.kernel import events as ev
from amplifier_app_tui.model.blocks import (
    BlockIdAllocator,
    Blocked,
    NeedsYouBlock,
    NeedsYouEntry,
)
from amplifier_app_tui.model.formatting import DIGEST_MAX_CHARS, command_digest
from amplifier_app_tui.model.lanes import LaneRegistry
from amplifier_app_tui.model.turn import OutcomeLedger
from amplifier_app_tui.ui.app_support import needs_you_block, needs_you_display_question
from amplifier_app_tui.ui.needs_you import decision_why_line
from amplifier_app_tui.ui.plan_panel import (
    PLAN_DRILL_EXTRA,
    PLAN_MAX_ROWS,
    format_plan_lines,
    plan_drill_notice,
)
from amplifier_app_tui.ui.reducer import TranscriptReducer
from amplifier_app_tui.ui.transcript_render import render_block
from tests.test_ui_reducer_outcomes import FakeHost

HEREDOC = (
    "cat > /tmp/diag/build2.py <<'PY'\n" + "\n".join(f"print({n})" for n in range(1, 15)) + "\nPY"
)
"""The user-reported raw sprawl shape: a 14-line heredoc write."""

REASON = "outside configured project boundary without explicit authorization"


def _reducer() -> tuple[TranscriptReducer, FakeHost]:
    host = FakeHost("auto")
    reducer = TranscriptReducer(
        host,
        allocator=BlockIdAllocator(),
        ledger=OutcomeLedger(),
        lanes=LaneRegistry(),
    )
    return reducer, host


def _plain(lines) -> list[str]:
    return ["".join(seg.text for seg in line) for line in lines]


# ---------------------------------------------------------------------------
# command_digest — the shared verb-noun summarization (byte-identical Rust)
# ---------------------------------------------------------------------------


def test_command_digest_shapes() -> None:
    # The task's canonical example, verbatim.
    assert command_digest(HEREDOC) == "write /tmp/diag/build2.py (heredoc, 14 lines)"
    # Heredoc without a redirect target: the head word stands in.
    assert command_digest("python3 <<'EOF'\nx\nEOF") == "python3 (heredoc, 1 line)"
    # Whitespace-collapsed heredoc (queue-sanitized actions lose their
    # newlines): the body length is unknowable — say so honestly.
    assert command_digest("cat > /tmp/x.py <<'PY' print(1) PY") == "write /tmp/x.py (heredoc)"
    # Plain multi-line: first line + a (+N lines) tail.
    assert command_digest("echo one\necho two\necho three") == "echo one (+2 lines)"
    # Single-line redirect is a write of its target.
    assert command_digest("echo hi > /etc/motd") == "write /etc/motd"
    assert command_digest("echo hi >>/var/log/x.log") == "write /var/log/x.log"
    # Short commands pass through unchanged (existing ⊘ pins keep holding).
    assert command_digest("git push --force origin main") == "git push --force origin main"
    assert command_digest("uv run pytest") == "uv run pytest"
    # Hard cap at the digest measure.
    long = "x" * 200
    assert len(command_digest(long)) == DIGEST_MAX_CHARS
    assert command_digest(long).endswith("…")
    assert command_digest("") == "(command)"


# ---------------------------------------------------------------------------
# a. Friendly blocked line + expand body (reducer + renderer)
# ---------------------------------------------------------------------------


def test_blocked_line_digest_and_expand_body_keeps_raw_command() -> None:
    reducer, host = _reducer()
    reducer.handle(ev.PromptSubmit(session_id="root", prompt="run diagnostics", ts=1.0))
    # Real governance ordering: the deferral notification lands BEFORE the
    # deny renders its ⊘ line (governance defers, then deny-and-continues).
    reducer.handle(
        ev.Notification(
            session_id="root",
            message=f"decision deferred to queue · Allow {HEREDOC}?",
            level="decision",
            source="needs_you",
            decision_id="decision-1",
            question=f"Allow {HEREDOC}?",
            reason=REASON,
            choices=("Allow once", "Allow always", "Deny"),
            action=HEREDOC,
            ts=2.0,
        )
    )
    reducer.handle(
        ev.ApprovalDenied(
            session_id="root",
            prompt=f"Allow {HEREDOC}?",
            command=HEREDOC,
            reason=f"Denied by trust policy: {REASON}.",
            continuation="continuing without the write",
            ts=3.0,
        )
    )
    blocked = [b for b in host.blocks if isinstance(b, Blocked)]
    assert len(blocked) == 1
    block = blocked[0]
    # The row carries the digest — never the raw heredoc sprawl.
    assert block.cmd == "write /tmp/diag/build2.py (heredoc, 14 lines)"
    assert block.deferred is True
    # The raw command survives verbatim in the expand body, why first.
    assert block.body[0] == f"why · Denied by trust policy: {REASON}."
    assert list(block.body[1:]) == [ln for ln in HEREDOC.splitlines() if ln.strip()]

    head = _plain(render_block(block, 200))[0]
    assert head == (
        "  ⊘ blocked · write /tmp/diag/build2.py (heredoc, 14 lines)"
        " · needs your ok — ctrl+y to review · click to expand"
    )
    assert HEREDOC.splitlines()[0] not in head  # raw never sprawls the row
    expanded = block.model_copy(update={"expanded": True})
    body_lines = _plain(render_block(expanded, 200))[1:]
    assert f"      $ {HEREDOC}".splitlines()[0] not in body_lines  # no $ prefix contract
    assert "      cat > /tmp/diag/build2.py <<'PY'" in body_lines
    assert "      print(14)" in body_lines
    assert "      PY" in body_lines


def test_blocked_line_upgrades_when_denial_renders_first() -> None:
    """Demo/mockup ordering (deny → deferral): the already-rendered ⊘ line
    is replaced in place with the deferred form."""
    reducer, host = _reducer()
    reducer.handle(ev.PromptSubmit(session_id="root", prompt="push it", ts=1.0))
    reducer.handle(
        ev.ApprovalDenied(
            session_id="root",
            prompt="git push --force origin main",
            reason="outside user authorization",
            continuation="finding safer path",
            ts=2.0,
        )
    )
    before = [b for b in host.blocks if isinstance(b, Blocked)][0]
    assert before.deferred is False
    head = _plain(render_block(before, 200))[0]
    assert head == (
        "  ⊘ blocked · git push --force origin main"
        " · outside user authorization · finding safer path"
    )
    reducer.handle(
        ev.Notification(
            session_id="root",
            message="decision deferred to queue · run continues",
            level="decision",
            source="needs_you",
            ts=3.0,
        )
    )
    after = [b for b in host.blocks if isinstance(b, Blocked)][0]
    assert after.id == before.id  # replaced in place
    assert after.deferred is True
    head = _plain(render_block(after, 200))[0]
    assert head == (
        "  ⊘ blocked · git push --force origin main"
        " · needs your ok — ctrl+y to review · click to expand"
    )
    # The reason moved into the expand body (the WHY line).
    assert after.body[0] == "why · outside user authorization"
    assert after.body[1] == "git push --force origin main"


def test_plain_denial_line_is_unchanged_and_not_expandable() -> None:
    """A deny with no deferral and nothing hidden keeps its original
    single-line form — no expand hint, no ctrl+y tail."""
    reducer, host = _reducer()
    reducer.handle(ev.PromptSubmit(session_id="root", prompt="test it", ts=1.0))
    reducer.handle(
        ev.ApprovalDenied(
            session_id="root",
            prompt="uv run pytest",
            reason="denied by user",
            continuation="continuing without test run",
            ts=2.0,
        )
    )
    block = [b for b in host.blocks if isinstance(b, Blocked)][0]
    assert block.body == () and block.deferred is False
    assert _plain(render_block(block, 200))[0] == (
        "  ⊘ blocked · uv run pytest · denied by user · continuing without test run"
    )


# ---------------------------------------------------------------------------
# b. Needs-you compact rows + why line
# ---------------------------------------------------------------------------


def test_needs_you_compact_rows_with_why_line() -> None:
    # Park through the REAL queue: its sanitizer collapses the multi-line
    # action to one line (exactly what governance-parked items look like).
    from amplifier_app_tui.model.queues import NeedsYouQueue

    queue = NeedsYouQueue()
    item = queue.defer(
        f"Allow {HEREDOC}?",
        REASON,
        choices=("Allow once", "Allow always", "Deny"),
        action=HEREDOC,
    )
    # Display-only compaction: the Allow-shape question gets the digest
    # (the collapsed action can no longer count its body lines — honest).
    assert needs_you_display_question(item) == "Allow write /tmp/diag/build2.py (heredoc)?"
    # …while prose questions (escalations, demo) pass through verbatim.
    prose = item.model_copy(update={"question": "Review the run's denial pattern?"})
    assert needs_you_display_question(prose) == "Review the run's denial pattern?"

    block = needs_you_block((item,), BlockIdAllocator())
    assert block is not None
    lines = _plain(render_block(block, 200))
    assert lines[0] == "· Needs you  1 deferred decision"
    assert lines[1] == (
        "  1 Allow write /tmp/diag/build2.py (heredoc)?  [Allow once]  [Allow always]  [Deny]"
    )
    # The WHY is its own dim line — the row never inlines the reason.
    assert lines[2] == decision_why_line(REASON)
    assert "print(1)" not in lines[1]  # the raw body never sprawls the row


def test_needs_you_row_without_reason_has_no_why_line() -> None:
    block = NeedsYouBlock(id="b1", items=(NeedsYouEntry(decision_id="d1", question="Allow x?"),))
    lines = _plain(render_block(block, 200))
    assert lines == ["· Needs you  1 deferred decision", "  1 Allow x?"]


# ---------------------------------------------------------------------------
# c. Deferral detail on the wire (additive Notification fields)
# ---------------------------------------------------------------------------


def test_decision_notification_normalizes_deferral_detail() -> None:
    """The additive question/reason/choices/highlight/action fields survive
    the hook-payload normalization path (old payloads default empty)."""
    event = ev.normalize(
        "user:notification",
        {
            "session_id": "root",
            "message": "decision deferred to queue · Allow x?",
            "level": "decision",
            "source": "needs_you",
            "decision_id": "decision-3",
            "question": "Allow x?",
            "reason": REASON,
            "choices": ["Allow once", "Deny"],
            "highlight": "x",
            "action": "x",
        },
    )
    assert isinstance(event, ev.Notification)
    assert event.question == "Allow x?"
    assert event.reason == REASON
    assert event.choices == ("Allow once", "Deny")
    assert event.highlight == "x"
    assert event.action == "x"
    legacy = ev.normalize(
        "user:notification", {"session_id": "root", "message": "hi", "level": "info"}
    )
    assert isinstance(legacy, ev.Notification)
    assert legacy.question == "" and legacy.choices == ()


# ---------------------------------------------------------------------------
# d. Plan drilldown (+2/+3) — the data model is FLAT (TodoItem has content
#    + status only), so drilling honestly widens the row window.
# ---------------------------------------------------------------------------


def test_plan_drilldown_cycles_rows() -> None:
    from amplifier_app_tui.model.blocks import TodoItem
    from amplifier_app_tui.ui.plan_panel import PlanPanel

    panel = PlanPanel()
    items = tuple(
        TodoItem(content=f"step {i}", status="in_progress" if i == 0 else "pending")
        for i in range(10)
    )
    panel.update_plan(items)
    assert PLAN_DRILL_EXTRA == (0, 2, 3)
    assert panel.max_rows == PLAN_MAX_ROWS
    assert panel.plan_lines[-1] == "  ⋮ +5 more"
    assert len(panel.plan_lines) == 1 + 5 + 1  # header + rows + more

    assert panel.cycle_drill() == 2
    assert panel.max_rows == PLAN_MAX_ROWS + 2
    assert len(panel.plan_lines) == 1 + 7 + 1
    assert panel.plan_lines[-1] == "  ⋮ +3 more"

    assert panel.cycle_drill() == 3
    assert panel.max_rows == PLAN_MAX_ROWS + 3
    assert len(panel.plan_lines) == 1 + 8 + 1
    assert panel.plan_lines[-1] == "  ⋮ +2 more"

    assert panel.cycle_drill() == 0  # back to default
    assert panel.max_rows == PLAN_MAX_ROWS

    # The notice strings both apps show, verbatim.
    assert plan_drill_notice(0) == "plan · default rows"
    assert plan_drill_notice(2) == "plan · +2 rows"
    assert plan_drill_notice(3) == "plan · +3 rows"

    # format_plan_lines honors the widened cap directly.
    assert len(format_plan_lines(items, max_rows=8)) == 1 + 8 + 1
