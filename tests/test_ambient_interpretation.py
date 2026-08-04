"""E3 -- the structured, editable interpretation payload (AC1).

Tested the way B6's own state machine is: against a ``tmp_path`` session
directory with an INJECTED clock, so expiry is something the test *causes*
rather than waits out.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from amplifier_app_tui.kernel.ambient.interpretation import (
    EDITABLE_FIELDS,
    InterpretationDesk,
    InterpretationError,
)
from amplifier_app_tui.kernel.ambient.principal import LocalPrincipal
from amplifier_app_tui.kernel.session_control import (
    AUDIT_FILENAME,
    HUMAN,
    REASON_HANDOFF_CLAIMED,
    REASON_SESSION_PAUSED,
    Actor,
    SessionControl,
)


class _Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


MJ = LocalPrincipal("mj", kind=HUMAN, verified=True)
SAM = LocalPrincipal("sam", kind=HUMAN, verified=True)


@pytest.fixture
def clock() -> _Clock:
    return _Clock()


@pytest.fixture
def control(tmp_path: Path, clock: _Clock) -> SessionControl:
    return SessionControl(tmp_path / "s-1", "s-1", now=clock)


@pytest.fixture
def desk(tmp_path: Path, control: SessionControl, clock: _Clock) -> InterpretationDesk:
    return InterpretationDesk(tmp_path / "s-1", control, now=clock, ttl=600.0)


def _propose(desk: InterpretationDesk, **kwargs: object) -> object:
    defaults: dict[str, object] = {
        "summary": "Reply to Dana's thread confirming Thursday",
        "targets": ("amplifier-session:s-1",),
        "grants": ("your Outlook inbox, read only, Dana's thread",),
        "reversibility": "externally_visible",
        "negative_scope": ("send to anyone else", "touch other threads"),
    }
    defaults.update(kwargs)
    return desk.propose(MJ, **defaults)  # type: ignore[arg-type]


def _actions(control: SessionControl) -> list[str]:
    lines = (control.session_dir / AUDIT_FILENAME).read_text(encoding="utf-8").splitlines()
    return [json.loads(line)["action"] for line in lines if line.strip()]


# -- propose ------------------------------------------------------------------


def test_proposing_parks_the_session_behind_a_b6_handoff(
    desk: InterpretationDesk, control: SessionControl
) -> None:
    outcome = _propose(desk)
    assert control.paused()
    assert outcome.interpretation.handoff_id  # type: ignore[attr-defined]
    assert any(h.handoff_id for h in control.handoffs())


def test_no_write_can_slip_through_while_the_human_decides(
    desk: InterpretationDesk, control: SessionControl
) -> None:
    """The gate is B6's, unchanged -- this proves the reuse actually holds."""
    _propose(desk)
    decision = control.authorize("submit", {"actor": {"id": "bot-1"}, "text": "go"})
    assert not decision.allowed
    assert decision.reason == REASON_SESSION_PAUSED


def test_the_echo_is_ordered_for_speech_and_names_its_negative_scope(
    desk: InterpretationDesk,
) -> None:
    spoken = _propose(desk).interpretation.spoken()  # type: ignore[attr-defined]
    assert spoken.startswith("Reply to Dana's thread confirming Thursday.")
    assert "amplifier-session:s-1" in spoken
    assert "Outlook inbox" in spoken
    assert "cannot be un-sent" in spoken
    assert "I will not send to anyone else" in spoken


def test_the_echo_enumerates_the_editable_fields(desk: InterpretationDesk) -> None:
    """ "change the ..." needs a closed vocabulary to hit."""
    row = _propose(desk).interpretation  # type: ignore[attr-defined]
    assert row.editable_fields == EDITABLE_FIELDS
    for name in EDITABLE_FIELDS:
        assert name in row.spoken()


def test_an_empty_summary_is_refused(desk: InterpretationDesk) -> None:
    with pytest.raises(InterpretationError):
        desk.propose(MJ, summary="   ")


# -- amend --------------------------------------------------------------------


def test_amend_mints_a_new_id_and_never_mutates(desk: InterpretationDesk) -> None:
    original = _propose(desk).interpretation  # type: ignore[attr-defined]

    amended = desk.amend(original.interpretation_id, "summary", "Reply confirming Friday", MJ)

    assert amended.interpretation.interpretation_id != original.interpretation_id
    assert amended.interpretation.summary == "Reply confirming Friday"
    stored_original = desk.get(original.interpretation_id)
    assert stored_original is not None
    assert stored_original.summary == original.summary  # untouched
    assert stored_original.state == "superseded"
    assert stored_original.superseded_by == amended.interpretation.interpretation_id
    assert amended.interpretation.supersedes == original.interpretation_id


def test_amend_keeps_the_same_handoff_so_the_gate_never_lifts(
    desk: InterpretationDesk, control: SessionControl
) -> None:
    original = _propose(desk).interpretation  # type: ignore[attr-defined]
    amended = desk.amend(original.interpretation_id, "summary", "Something else", MJ)
    assert amended.interpretation.handoff_id == original.handoff_id
    assert control.paused()


def test_a_field_outside_the_closed_vocabulary_cannot_be_amended(
    desk: InterpretationDesk,
) -> None:
    row = _propose(desk).interpretation  # type: ignore[attr-defined]
    with pytest.raises(InterpretationError, match="not editable"):
        desk.amend(row.interpretation_id, "reversibility", "reversible", MJ)


def test_list_valued_fields_amend_to_tuples(desk: InterpretationDesk) -> None:
    row = _propose(desk).interpretation  # type: ignore[attr-defined]
    amended = desk.amend(row.interpretation_id, "targets", ["a", "b"], MJ)
    assert amended.interpretation.targets == ("a", "b")


def test_a_superseded_interpretation_cannot_be_confirmed(desk: InterpretationDesk) -> None:
    row = _propose(desk).interpretation  # type: ignore[attr-defined]
    desk.amend(row.interpretation_id, "summary", "changed", MJ)
    with pytest.raises(InterpretationError, match="superseded"):
        desk.confirm(row.interpretation_id, MJ)


# -- confirm / cancel / expire ------------------------------------------------


def test_confirm_claims_the_handoff_and_grants_the_lease(
    desk: InterpretationDesk, control: SessionControl
) -> None:
    row = _propose(desk).interpretation  # type: ignore[attr-defined]

    outcome = desk.confirm(row.interpretation_id, MJ)

    assert outcome.interpretation.state == "confirmed"
    assert not control.paused()
    lease = control.active_lease()
    assert lease is not None and lease.actor.id == "mj"


def test_a_second_confirm_conflicts_rather_than_double_executing(
    desk: InterpretationDesk, control: SessionControl
) -> None:
    row = _propose(desk).interpretation  # type: ignore[attr-defined]
    desk.confirm(row.interpretation_id, MJ)

    # A different person answering the same parked question.
    outcome = control.claim_handoff(row.handoff_id, Actor(id="sam", kind=HUMAN))

    conflict = next(r for r in outcome if r.get("type") == "control.conflict")
    assert conflict["reason"] == REASON_HANDOFF_CLAIMED


def test_cancel_resumes_the_session_so_nothing_stays_wedged(
    desk: InterpretationDesk, control: SessionControl
) -> None:
    row = _propose(desk).interpretation  # type: ignore[attr-defined]

    outcome = desk.cancel(row.interpretation_id, MJ)

    assert outcome.interpretation.state == "cancelled"
    assert not control.paused()


def test_expiry_is_cancel_and_resumes_the_session(
    desk: InterpretationDesk, control: SessionControl, clock: _Clock
) -> None:
    row = _propose(desk).interpretation  # type: ignore[attr-defined]
    clock.advance(601.0)

    expired = desk.expire_due(MJ)

    assert [r.interpretation_id for r in expired] == [row.interpretation_id]
    assert not control.paused()
    assert desk.pending() == []


def test_an_expired_interpretation_cannot_be_confirmed(
    desk: InterpretationDesk, clock: _Clock
) -> None:
    row = _propose(desk).interpretation  # type: ignore[attr-defined]
    clock.advance(601.0)
    with pytest.raises(InterpretationError, match="expired"):
        desk.confirm(row.interpretation_id, MJ)


# -- the audit account --------------------------------------------------------


def test_confirm_cancel_and_expiry_are_all_audited(tmp_path: Path, clock: _Clock) -> None:
    """What the assistant DIDN'T do on your behalf is part of the account."""
    control = SessionControl(tmp_path / "s-2", "s-2", now=clock)
    desk = InterpretationDesk(tmp_path / "s-2", control, now=clock, ttl=100.0)

    confirmed = desk.propose(MJ, summary="one")
    desk.confirm(confirmed.interpretation.interpretation_id, MJ)
    cancelled = desk.propose(MJ, summary="two")
    desk.cancel(cancelled.interpretation.interpretation_id, MJ)
    desk.propose(MJ, summary="three")
    clock.advance(101.0)
    desk.expire_due(MJ)

    actions = _actions(control)
    assert "interpretation.proposed" in actions
    assert "interpretation.confirmed" in actions
    assert "interpretation.cancelled" in actions
    assert "interpretation.expired" in actions


def test_the_audit_entry_carries_the_interpretation_and_handoff_ids(
    desk: InterpretationDesk, control: SessionControl
) -> None:
    row = _propose(desk).interpretation  # type: ignore[attr-defined]
    entry = next(
        e for e in control.audit_entries(limit=20) if e["action"] == "interpretation.proposed"
    )
    assert entry["detail"]["interpretation_id"] == row.interpretation_id
    assert entry["detail"]["handoff_id"] == row.handoff_id
    assert entry["detail"]["auth"]["verified"] is True


def test_the_record_survives_a_restart(
    tmp_path: Path, control: SessionControl, clock: _Clock
) -> None:
    """Durable, like control.json -- a second process sees the same proposal."""
    desk = InterpretationDesk(tmp_path / "s-1", control, now=clock)
    row = desk.propose(MJ, summary="durable").interpretation

    reopened = InterpretationDesk(tmp_path / "s-1", control, now=clock)

    assert [r.interpretation_id for r in reopened.pending()] == [row.interpretation_id]


def test_the_payload_is_typed_not_stuffed_into_the_handoff_note(
    desk: InterpretationDesk, control: SessionControl
) -> None:
    """The doc's explicit rejection: never JSON-stuff a human-readable field."""
    _propose(desk)
    handoff = control.handoffs()[-1]
    assert handoff.note == ""
    assert "{" not in handoff.reason
