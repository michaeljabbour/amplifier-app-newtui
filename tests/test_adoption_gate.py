"""Adoption gate (governance item B5) — the ledger checker that can say no.

The shipped ledger under ``docs/adoption/`` is asserted as-is; every rule is then
exercised against synthetic ledgers written to ``tmp_path``. Nothing here touches the
real files, and nothing here needs network or credentials (the house rule).
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 8, 10)


def _load_gate() -> ModuleType:
    """Load the governance script by path (scripts/ is not a package)."""
    path = REPO_ROOT / "scripts" / "adoption_gate.py"
    spec = importlib.util.spec_from_file_location("adoption_gate", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # dataclasses resolve their own module during class creation.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


gate = _load_gate()


# -- synthetic ledger construction -------------------------------------------

STAGE_DEFAULTS = ["-", "-", "-", "-", "-", "pending"]


def _stage_row(number: int, **over: str) -> list[str]:
    row = {
        "stage": str(number),
        "owner": f"owner-{number}",
        "min_window_days": "0" if number == 5 else "1",
        "entry_criteria": f"S{number}-entry",
        "exit_criteria": f"S{number}-exit",
        "tested_commit": "-",
        "start_date": "-",
        "end_date": "-",
        "entry_evidence": "-",
        "exit_evidence": "-",
        "decision": "pending",
    }
    row.update(over)
    return list(row.values())


def _promoted(number: int) -> list[str]:
    return _stage_row(
        number,
        tested_commit="abc1234",
        start_date="2026-08-01",
        end_date="2026-08-02",
        entry_evidence="smoke green",
        exit_evidence="shipped 3 tasks",
        decision="promoted",
    )


def _seat(name: str, **over: str) -> list[str]:
    row = {
        "seat": name,
        "participant": "TBD",
        "stage": "3",
        "tested_commit": "-",
        "date": "-",
        "completion_evidence": "-",
        "friction": "-",
        "disposition": "untriaged",
        "disposition_ref": "-",
    }
    row.update(over)
    return list(row.values())


def _write(
    tmp_path: Path,
    stages: list[list[str]],
    blockers: list[list[str]] | None = None,
    feedback: list[list[str]] | None = None,
) -> Path:
    directory = tmp_path / "adoption"
    directory.mkdir(exist_ok=True)
    for name, rows in (
        ("stages.tsv", stages),
        ("blockers.tsv", blockers or []),
        ("feedback.tsv", feedback or [_seat(f"seat-{i}") for i in (1, 2, 3)]),
    ):
        body = "# header\n" + "".join("\t".join(r) + "\n" for r in rows)
        (directory / name).write_text(body)
    return directory


def _reasons(directory: Path, stage: int) -> list[str]:
    return gate.promote_reasons(gate.load(directory), stage, TODAY)


# -- the shipped ledger ------------------------------------------------------


def test_shipped_ledger_validates() -> None:
    problems = gate.validate(gate.load())
    assert problems == []


def test_shipped_ledger_has_five_stages_with_owners_and_windows() -> None:
    stages = {s.stage: s for s in gate.load().stages}
    assert sorted(stages) == [1, 2, 3, 4, 5]
    assert stages[1].owner == "MJ Jabbour"
    assert stages[2].owner == "Brian Krabach"
    # AC1: every stage that can still run carries a >= 1 day window.
    assert [stages[n].min_window_days for n in (1, 2, 3, 4)] == [1, 1, 1, 1]
    assert all(s.decision == "pending" for s in stages.values())


def test_shipped_ledger_reserves_three_named_but_empty_stage_3_seats() -> None:
    seats = [f for f in gate.load().feedback if f.stage == "3"]
    assert len(seats) == 3
    # The people are genuinely unknown; the seats are not.
    assert {s.participant for s in seats} == {"TBD"}


def test_shipped_ledger_promotes_nothing_yet() -> None:
    ledger = gate.load()
    for stage in (1, 2, 3, 4, 5):
        assert gate.promote_reasons(ledger, stage, TODAY), f"stage {stage} should be blocked"


# -- AC1: window, evidence, recorded decision --------------------------------


def test_stage_without_start_date_is_blocked(tmp_path: Path) -> None:
    directory = _write(tmp_path, [_stage_row(n) for n in (1, 2, 3, 4, 5)])
    assert "stage has not started (no start_date)" in _reasons(directory, 1)


def test_window_shorter_than_one_day_is_blocked(tmp_path: Path) -> None:
    rows = [_stage_row(n) for n in (2, 3, 4, 5)]
    rows.insert(
        0,
        _stage_row(
            1,
            tested_commit="abc1234",
            start_date=TODAY.isoformat(),
            entry_evidence="smoke green",
            exit_evidence="shipped 2 tasks",
        ),
    )
    directory = _write(tmp_path, rows)
    assert "usage window not met: 0 of 1 day(s) elapsed" in _reasons(directory, 1)


def test_one_full_day_with_evidence_clears_the_gate(tmp_path: Path) -> None:
    rows = [_stage_row(n) for n in (2, 3, 4, 5)]
    rows.insert(
        0,
        _stage_row(
            1,
            tested_commit="abc1234",
            start_date="2026-08-09",
            entry_evidence="smoke green",
            exit_evidence="shipped 2 tasks",
        ),
    )
    directory = _write(tmp_path, rows)
    assert _reasons(directory, 1) == []


@pytest.mark.parametrize(
    ("field", "reason"),
    [
        ("tested_commit", "no tested_commit recorded"),
        ("entry_evidence", "no entry evidence recorded for S1-entry"),
        ("exit_evidence", "no exit evidence recorded for S1-exit"),
    ],
)
def test_missing_evidence_blocks_promotion(tmp_path: Path, field: str, reason: str) -> None:
    over = {
        "tested_commit": "abc1234",
        "start_date": "2026-08-01",
        "entry_evidence": "smoke green",
        "exit_evidence": "shipped 2 tasks",
    }
    over[field] = "-"
    rows = [_stage_row(n) for n in (2, 3, 4, 5)]
    rows.insert(0, _stage_row(1, **over))
    directory = _write(tmp_path, rows)
    assert reason in _reasons(directory, 1)


def test_out_of_order_promotion_is_blocked(tmp_path: Path) -> None:
    rows = [_stage_row(n) for n in (1, 3, 4, 5)]
    rows.insert(
        1,
        _stage_row(
            2,
            tested_commit="abc1234",
            start_date="2026-08-01",
            entry_evidence="smoke green",
            exit_evidence="shipped 2 tasks",
        ),
    )
    assert "stage 1 is not promoted (decision=pending)" in _reasons(_write(tmp_path, rows), 2)


def test_promoted_stage_missing_evidence_is_a_validation_error(tmp_path: Path) -> None:
    bad = _promoted(1)
    bad[8] = "-"  # entry_evidence
    rows = [bad] + [_stage_row(n) for n in (2, 3, 4, 5)]
    problems = gate.validate(gate.load(_write(tmp_path, rows)))
    assert any("promoted but entry_evidence is not recorded" in p for p in problems)


# -- AC2: a release-blocking defect outranks the clock -----------------------


def _stage_1_ready() -> list[list[str]]:
    rows = [_stage_row(n) for n in (2, 3, 4, 5)]
    rows.insert(
        0,
        _stage_row(
            1,
            tested_commit="abc1234",
            start_date="2026-08-01",
            entry_evidence="smoke green",
            exit_evidence="shipped 2 tasks",
        ),
    )
    return rows


def test_open_release_blocker_blocks_promotion_after_the_window_elapsed(tmp_path: Path) -> None:
    blocker = ["BL-1", "1", "release-blocking", "open", "2026-08-02", "-", "resume loses cost"]
    directory = _write(tmp_path, _stage_1_ready(), blockers=[blocker])
    reasons = _reasons(directory, 1)
    # The clock is satisfied — 9 days on a 1-day window — and it does not help.
    assert not any("usage window" in r for r in reasons)
    assert "open release-blocking defect BL-1 (stage 1)" in reasons


def test_resolving_the_blocker_unblocks_promotion(tmp_path: Path) -> None:
    blocker = ["BL-1", "1", "release-blocking", "resolved", "2026-08-02", "#210", "fixed"]
    assert _reasons(_write(tmp_path, _stage_1_ready(), blockers=[blocker]), 1) == []


def test_friction_severity_is_tracked_but_does_not_gate(tmp_path: Path) -> None:
    blocker = ["BL-2", "1", "friction", "open", "2026-08-02", "-", "footer is noisy"]
    assert _reasons(_write(tmp_path, _stage_1_ready(), blockers=[blocker]), 1) == []


def test_a_blocker_filed_on_another_stage_still_blocks(tmp_path: Path) -> None:
    blocker = ["BL-3", "3", "release-blocking", "open", "2026-08-02", "-", "crash on resume"]
    reasons = _reasons(_write(tmp_path, _stage_1_ready(), blockers=[blocker]), 1)
    assert "open release-blocking defect BL-3 (stage 3)" in reasons


# -- AC3: three named daily drivers with tracked dispositions ----------------


def _through_stage_2() -> list[list[str]]:
    rows = [_promoted(1), _promoted(2)]
    rows.append(
        _stage_row(
            3,
            tested_commit="abc1234",
            start_date="2026-08-01",
            entry_evidence="three seats filled",
            exit_evidence="feedback consolidated",
        )
    )
    rows.extend(_stage_row(n) for n in (4, 5))
    return rows


def test_stage_3_blocked_until_three_seats_are_named(tmp_path: Path) -> None:
    seats = [
        _seat("seat-1", participant="ann", tested_commit="abc1234", disposition="fixed"),
        _seat("seat-2", participant="bob", tested_commit="abc1234", disposition="deferred"),
        _seat("seat-3"),
    ]
    reasons = _reasons(_write(tmp_path, _through_stage_2(), feedback=seats), 3)
    assert any("needs 3 named daily-driver participants, 2 named" in r for r in reasons)


def test_stage_3_blocked_while_a_seat_is_untriaged(tmp_path: Path) -> None:
    seats = [
        _seat("seat-1", participant="ann", tested_commit="abc1234", disposition="fixed"),
        _seat("seat-2", participant="bob", tested_commit="abc1234", disposition="deferred"),
        _seat("seat-3", participant="cy", tested_commit="abc1234"),
    ]
    reasons = _reasons(_write(tmp_path, _through_stage_2(), feedback=seats), 3)
    assert "seat-3 (cy) feedback is still untriaged" in reasons


def test_stage_3_blocked_when_a_seat_has_no_tested_commit(tmp_path: Path) -> None:
    seats = [
        _seat("seat-1", participant="ann", tested_commit="abc1234", disposition="fixed"),
        _seat("seat-2", participant="bob", tested_commit="abc1234", disposition="deferred"),
        _seat("seat-3", participant="cy", disposition="wont-fix"),
    ]
    reasons = _reasons(_write(tmp_path, _through_stage_2(), feedback=seats), 3)
    assert "seat-3 (cy) has no tested_commit recorded" in reasons


def test_stage_3_clears_with_three_dispositioned_seats(tmp_path: Path) -> None:
    seats = [
        _seat("seat-1", participant="ann", tested_commit="abc1234", disposition="fixed"),
        _seat("seat-2", participant="bob", tested_commit="abc1234", disposition="deferred"),
        _seat("seat-3", participant="cy", tested_commit="def5678", disposition="wont-fix"),
    ]
    assert _reasons(_write(tmp_path, _through_stage_2(), feedback=seats), 3) == []


def test_fewer_than_three_seats_is_a_validation_error(tmp_path: Path) -> None:
    directory = _write(
        tmp_path,
        [_stage_row(n) for n in (1, 2, 3, 4, 5)],
        feedback=[_seat("seat-1"), _seat("seat-2")],
    )
    problems = gate.validate(gate.load(directory))
    assert any("stage 3 needs at least 3 seats" in p for p in problems)


# -- AC5: `promote 4` is the replacement gate --------------------------------


def _through_stage_3() -> list[list[str]]:
    rows = [_promoted(1), _promoted(2), _promoted(3)]
    rows.append(
        _stage_row(
            4,
            tested_commit="abc1234",
            start_date="2026-08-01",
            entry_evidence="rollback drill walked 2026-08-01",
            exit_evidence="team default 1 day; amplifier still installed",
        )
    )
    rows.append(_stage_row(5))
    return rows


def _named_seats() -> list[list[str]]:
    return [
        _seat("seat-1", participant="ann", tested_commit="abc1234", disposition="fixed"),
        _seat("seat-2", participant="bob", tested_commit="abc1234", disposition="deferred"),
        _seat("seat-3", participant="cy", tested_commit="def5678", disposition="wont-fix"),
    ]


def test_replacement_gate_clears_when_the_record_is_clean(tmp_path: Path) -> None:
    directory = _write(tmp_path, _through_stage_3(), feedback=_named_seats())
    assert _reasons(directory, 4) == []


def test_replacement_gate_refuses_while_any_release_blocker_is_open(tmp_path: Path) -> None:
    blocker = ["BL-9", "3", "release-blocking", "open", "2026-08-02", "-", "hangs on resume"]
    directory = _write(tmp_path, _through_stage_3(), blockers=[blocker], feedback=_named_seats())
    assert "open release-blocking defect BL-9 (stage 3)" in _reasons(directory, 4)


# -- the tool refuses to guess ------------------------------------------------


def test_malformed_ledger_blocks_every_promotion(tmp_path: Path) -> None:
    directory = tmp_path / "adoption"
    directory.mkdir()
    (directory / "stages.tsv").write_text("1\ttoo\tfew\tcolumns\n")
    (directory / "blockers.tsv").write_text("")
    (directory / "feedback.tsv").write_text("")
    reasons = _reasons(directory, 1)
    assert len(reasons) == 1
    assert reasons[0].startswith("ledger does not validate")


def test_missing_directory_is_reported_not_raised(tmp_path: Path) -> None:
    problems = gate.validate(gate.load(tmp_path / "nope"))
    assert any("stages.tsv: missing" in p for p in problems)


def test_already_promoted_stage_says_so(tmp_path: Path) -> None:
    rows = [_promoted(1)] + [_stage_row(n) for n in (2, 3, 4, 5)]
    assert _reasons(_write(tmp_path, rows), 1) == ["already promoted"]


def test_unknown_stage_is_reported(tmp_path: Path) -> None:
    rows = [_stage_row(n) for n in (1, 2, 3, 4, 5)]
    assert _reasons(_write(tmp_path, rows), 9) == ["no stage 9 in the ledger"]


# -- CLI surface --------------------------------------------------------------


def test_cli_check_and_status_pass_on_the_shipped_ledger() -> None:
    assert gate.main(["check"]) == 0
    assert gate.main(["status"]) == 0


def test_cli_promote_returns_one_when_blocked() -> None:
    assert gate.main(["promote", "1"]) == 1


def test_cli_rejects_unknown_commands_without_raising() -> None:
    assert gate.main(["frobnicate"]) == 2
    assert gate.main(["promote", "banana"]) == 2


def test_cli_honours_dir_and_today_flags(tmp_path: Path) -> None:
    directory = _write(tmp_path, _stage_1_ready())
    assert gate.main(["promote", "1", "--dir", str(directory), "--today", "2026-08-10"]) == 0
    assert gate.main(["promote", "1", "--dir", str(directory), "--today", "2026-08-01"]) == 1
