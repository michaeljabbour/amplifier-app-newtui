"""/improve proposal mining: allowlist candidates + trust-slot suggestions."""

from __future__ import annotations

import pytest

from amplifier_app_newtui.commands.improve import (
    ApprovalJournal,
    ApprovalTally,
    OverriddenDenial,
    allowlist_proposals,
    build_improve_block,
    improve_proposals,
    trust_slot_proposals,
)
from amplifier_app_newtui.model.trust import CapabilityClass, DenialLog


def test_allowlist_requires_every_ask_approved() -> None:
    tallies = (
        ApprovalTally(action="uv run pytest", approved=22, asked=22),
        ApprovalTally(action="git push", approved=9, asked=10),  # one deny → out
        ApprovalTally(action="rare thing", approved=2, asked=2),  # below min → out
    )
    proposals = allowlist_proposals(tallies)
    # Mockup row: dim 'allowlist: ' title + the action named once in green.
    assert [(p.title, p.action) for p in proposals] == [("allowlist:", "uv run pytest")]
    assert proposals[0].rationale == "approved 22/22 times · add to auto"


def test_allowlist_orders_by_ask_volume() -> None:
    tallies = (
        ApprovalTally(action="a", approved=5, asked=5),
        ApprovalTally(action="b", approved=9, asked=9),
    )
    proposals = allowlist_proposals(tallies)
    assert [p.action for p in proposals] == ["b", "a"]


def test_trust_slot_requires_all_denials_overridden() -> None:
    overrides = (
        OverriddenDenial(action="push-to-fork", denied=3, overridden=3),
        OverriddenDenial(action="net fetch", denied=4, overridden=2),  # not all → out
        OverriddenDenial(action="once", denied=1, overridden=1),  # below min → out
    )
    proposals = trust_slot_proposals(overrides)
    # Trust-slot rows name the action once, inside the rationale.
    assert [p.title for p in proposals] == ["trust slot:"]
    assert proposals[0].action == ""
    assert (
        proposals[0].rationale
        == "3 denials on push-to-fork all overridden · add fork remote to boundary"
    )


def test_improve_proposals_combines_both_kinds_allowlist_first() -> None:
    proposals = improve_proposals(
        tallies=(ApprovalTally(action="uv run pytest", approved=3, asked=3),),
        overrides=(OverriddenDenial(action="push-to-fork", denied=2, overridden=2),),
    )
    assert [p.title for p in proposals] == ["allowlist:", "trust slot:"]
    assert [p.action for p in proposals] == ["uv run pytest", ""]


def test_no_evidence_no_proposals() -> None:
    assert improve_proposals() == ()


def test_build_improve_block() -> None:
    proposals = improve_proposals(
        tallies=(ApprovalTally(action="uv run pytest", approved=3, asked=3),)
    )
    block = build_improve_block("b9", proposals)
    assert block.kind == "improve"
    assert block.id == "b9"
    assert len(block.proposals) == 1


def test_journal_tallies_and_capabilities() -> None:
    journal = ApprovalJournal()
    for _ in range(3):
        journal.record_ask("uv run pytest", approved=True, capability="test")
    journal.record_ask("git push", approved=False, capability="net")
    tallies = {t.action: t for t in journal.tallies()}
    assert tallies["uv run pytest"].asked == 3
    assert tallies["uv run pytest"].always_approved
    assert tallies["uv run pytest"].capability == "test"
    assert not tallies["git push"].always_approved


def test_journal_overrides_use_denial_log_counts() -> None:
    journal = ApprovalJournal()
    log = DenialLog()
    for _ in range(3):
        log.record_denial(
            capability=CapabilityClass.NET,
            action="push-to-fork",
            reason="net has real downside",
        )
        journal.record_override("push-to-fork")
    (row,) = journal.overrides(log)
    assert row == OverriddenDenial(action="push-to-fork", denied=3, overridden=3)
    assert row.all_overridden


def test_journal_rejects_empty_action() -> None:
    journal = ApprovalJournal()
    with pytest.raises(ValueError):
        journal.record_ask("   ", approved=True)
    with pytest.raises(ValueError):
        journal.record_override("")


def test_journal_normalizes_whitespace() -> None:
    journal = ApprovalJournal()
    journal.record_ask("uv  run   pytest", approved=True)
    journal.record_ask("uv run pytest", approved=True)
    (tally,) = journal.tallies()
    assert tally.action == "uv run pytest"
    assert tally.asked == 2
