"""Adoption gate (governance item B5) — the ledger checker that can say no.

The shipped ledger under ``docs/adoption/`` is asserted as-is; every rule is then
exercised against synthetic ledgers written to ``tmp_path``. Nothing here touches the
real files, and nothing here needs network or credentials (the house rule).
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from datetime import date
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2026, 8, 10)
# A commit that genuinely exists here, for the checks that resolve one for real.
HEAD = subprocess.run(
    ["git", "-C", str(REPO_ROOT), "rev-parse", "--short", "HEAD"],
    capture_output=True,
    text=True,
    check=False,
).stdout.strip()
# Hex-shaped, 40 characters, and certainly not a commit in this repository.
FABRICATED = "f" * 40


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
        "min_window_days": "1",
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
    """A legitimately promoted stage. Dates are sequential because the validator now
    insists on it: stage N cannot start before stage N-1 ended."""
    return _stage_row(
        number,
        tested_commit="abc1234",
        start_date=f"2026-08-{2 * number - 1:02d}",
        end_date=f"2026-08-{2 * number:02d}",
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


def _named_seat(seat: str, participant: str, **over: str) -> list[str]:
    """A seat a real person actually sat in: name, build, and completion evidence."""
    row = {
        "participant": participant,
        "tested_commit": "abc1234",
        "date": "2026-08-05",
        "completion_evidence": f"{participant} shipped two real tasks",
        "friction": "footer felt noisy",
        "disposition": "fixed",
        "disposition_ref": "#210",
    }
    row.update(over)
    return _seat(seat, **row)


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
    assert stages[3].owner == "MJ Jabbour"
    # AC1 is literal: all five stages carry a >= 1 day window.
    assert [stages[n].min_window_days for n in (1, 2, 3, 4, 5)] == [1, 1, 1, 1, 1]
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


def test_zero_day_minimum_is_a_validation_error(tmp_path: Path) -> None:
    rows = [_stage_row(n) for n in (1, 2, 3, 4)]
    rows.append(_stage_row(5, min_window_days="0"))
    problems = gate.validate(gate.load(_write(tmp_path, rows)))
    assert any(
        "stage 5: min_window_days is 0; every stage requires at least one day" in p
        for p in problems
    )


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
    assert any("promoted but no entry evidence recorded for S1-entry" in p for p in problems)


# -- AC2: a release-blocking defect outranks the clock -----------------------


def _stage_1_ready(commit: str = "abc1234") -> list[list[str]]:
    rows = [_stage_row(n) for n in (2, 3, 4, 5)]
    rows.insert(
        0,
        _stage_row(
            1,
            tested_commit=commit,
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
            start_date="2026-08-05",
            entry_evidence="three seats filled",
            exit_evidence="feedback consolidated",
        )
    )
    rows.extend(_stage_row(n) for n in (4, 5))
    return rows


def test_stage_3_blocked_until_three_seats_are_named(tmp_path: Path) -> None:
    seats = [
        _named_seat("seat-1", "ann", disposition="fixed"),
        _named_seat("seat-2", "bob", disposition="deferred"),
        _seat("seat-3"),
    ]
    reasons = _reasons(_write(tmp_path, _through_stage_2(), feedback=seats), 3)
    assert any("needs 3 named daily-driver participants, 2 named" in r for r in reasons)


def test_stage_3_blocked_while_a_seat_is_untriaged(tmp_path: Path) -> None:
    seats = [
        _named_seat("seat-1", "ann", disposition="fixed"),
        _named_seat("seat-2", "bob", disposition="deferred"),
        _named_seat("seat-3", "cy", disposition="untriaged"),
    ]
    reasons = _reasons(_write(tmp_path, _through_stage_2(), feedback=seats), 3)
    assert "seat-3 (cy) feedback is still untriaged" in reasons


def test_stage_3_blocked_when_a_seat_has_no_tested_commit(tmp_path: Path) -> None:
    seats = [
        _named_seat("seat-1", "ann", disposition="fixed"),
        _named_seat("seat-2", "bob", disposition="deferred"),
        _named_seat("seat-3", "cy", tested_commit="-", disposition="wont-fix"),
    ]
    reasons = _reasons(_write(tmp_path, _through_stage_2(), feedback=seats), 3)
    assert "seat-3 (cy) has no tested_commit recorded" in reasons


@pytest.mark.parametrize(
    ("field", "reason"),
    [
        ("date", "seat-3 (cy) has no feedback date recorded"),
        ("friction", "seat-3 (cy) has no friction report recorded"),
        ("disposition_ref", "seat-3 (cy) has no disposition reference"),
    ],
)
def test_stage_3_requires_a_complete_feedback_record(
    tmp_path: Path, field: str, reason: str
) -> None:
    seats = _named_seats()
    seats[2] = _named_seat("seat-3", "cy", **{field: "-"})
    reasons = _reasons(_write(tmp_path, _through_stage_2(), feedback=seats), 3)
    assert reason in reasons


def test_stage_3_clears_with_three_dispositioned_seats(tmp_path: Path) -> None:
    assert _reasons(_write(tmp_path, _through_stage_2(), feedback=_named_seats()), 3) == []


def test_fewer_than_three_seats_is_a_validation_error(tmp_path: Path) -> None:
    directory = _write(
        tmp_path,
        [_stage_row(n) for n in (1, 2, 3, 4, 5)],
        feedback=[_seat("seat-1"), _seat("seat-2")],
    )
    problems = gate.validate(gate.load(directory))
    assert any("stage 3 needs at least 3 seats" in p for p in problems)


# -- AC5: stage 4 opens the final window; `promote 5` is replacement ----------


def _through_stage_3() -> list[list[str]]:
    rows = [_promoted(1), _promoted(2), _promoted(3)]
    rows.append(
        _stage_row(
            4,
            tested_commit="abc1234",
            start_date="2026-08-07",
            entry_evidence="rollback drill walked 2026-08-07",
            exit_evidence="team default 1 day; amplifier still installed",
        )
    )
    rows.append(_stage_row(5))
    return rows


def _named_seats() -> list[list[str]]:
    return [
        _named_seat("seat-1", "ann", disposition="fixed"),
        _named_seat("seat-2", "bob", disposition="deferred"),
        _named_seat("seat-3", "cy", tested_commit="def5678", disposition="wont-fix"),
    ]


def test_stage_4_gate_clears_to_open_the_final_observation_window(tmp_path: Path) -> None:
    directory = _write(tmp_path, _through_stage_3(), feedback=_named_seats())
    assert _reasons(directory, 4) == []


def test_replacement_gate_refuses_while_any_release_blocker_is_open(tmp_path: Path) -> None:
    # Filed after stage 3 ended, so it does not retroactively invalidate the earlier
    # promotions; it still blocks stage 4 and every future promotion.
    blocker = ["BL-9", "3", "release-blocking", "open", "2026-08-07", "-", "hangs on resume"]
    directory = _write(tmp_path, _through_stage_3(), blockers=[blocker], feedback=_named_seats())
    assert "open release-blocking defect BL-9 (stage 3)" in _reasons(directory, 4)


def _through_stage_4() -> list[list[str]]:
    rows = [_promoted(n) for n in (1, 2, 3, 4)]
    rows.append(
        _stage_row(
            5,
            tested_commit="abc1234",
            start_date="2026-08-09",
            entry_evidence="stage 4 clear; both tools retained",
            exit_evidence="one-day replacement observation complete",
        )
    )
    return rows


def test_stage_5_is_the_final_replacement_gate(tmp_path: Path) -> None:
    directory = _write(tmp_path, _through_stage_4(), feedback=_named_seats())
    assert _reasons(directory, 5) == []


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
    # A synthetic commit + an injected resolver that resolves it: --dir/--today flag
    # handling is what this test is about, so nothing here touches real ambient git
    # (see the "CLI: the end-to-end surface" section for the resolver-branch tests).
    directory = _write(tmp_path, _stage_1_ready(commit="abc1234"))
    resolver_factory = _resolves_only("abc1234")
    ready = ["promote", "1", "--dir", str(directory), "--today", "2026-08-10"]
    too_soon = ["promote", "1", "--dir", str(directory), "--today", "2026-08-01"]
    assert gate.main(ready, resolver_factory=lambda: (resolver_factory, "")) == 0
    assert gate.main(too_soon, resolver_factory=lambda: (resolver_factory, "")) == 1


# -- placeholders: a stand-in is refused by name, never merely uncounted ------


@pytest.mark.parametrize(
    "value",
    [
        "",
        " ",
        "\t",
        "\u00a0",
        "-",
        "--",
        ".",
        "?",
        "???",
        "TBD",
        "tbd",
        "  Tbd  ",
        "<name>",
        "[TBD]",
        "(unknown)",
        "`?`",
        "N/A",
        "n/a",
        "none",
        "null",
        "TODO",
        "to  do",
        "unassigned",
        "XXX",
        "placeholder",
        "someone",
        "Your Name",
    ],
)
def test_placeholder_values_are_refused(value: str) -> None:
    assert gate.is_placeholder(value)
    assert not gate.is_recorded(value)


@pytest.mark.parametrize(
    "value", ["MJ Jabbour", "Brian Krabach", "ann", "Jo", "smoke green @ abc1234", "#210"]
)
def test_real_values_are_not_placeholders(value: str) -> None:
    assert not gate.is_placeholder(value)


def test_the_placeholder_list_is_the_single_source_of_truth() -> None:
    # Every token the docs promise is refused lives in exactly one frozenset, so a second
    # weaker copy cannot drift into existence.
    assert isinstance(gate.PLACEHOLDERS, frozenset)
    assert {"tbd", "-", "", "unknown", "?", "n/a"} <= gate.PLACEHOLDERS
    assert gate.NOT_RECORDED in gate.PLACEHOLDERS


def test_shipped_stage_owners_are_all_real_names() -> None:
    for row in gate.load().stages:
        assert gate.is_named_person(row.owner), row


def test_placeholder_owner_is_a_validation_error(tmp_path: Path) -> None:
    rows = [_stage_row(n) for n in (2, 3, 4, 5)]
    rows.insert(0, _stage_row(1, owner="TBD"))
    problems = gate.validate(gate.load(_write(tmp_path, rows)))
    assert any("stage 1: owner 'TBD' is a placeholder" in p for p in problems)


def test_placeholder_owner_blocks_every_promotion(tmp_path: Path) -> None:
    rows = _stage_1_ready()
    rows[0] = _stage_row(
        1,
        owner="?",
        tested_commit="abc1234",
        start_date="2026-08-01",
        entry_evidence="smoke green",
        exit_evidence="shipped 2 tasks",
    )
    reasons = _reasons(_write(tmp_path, rows), 1)
    assert reasons == ["ledger does not validate (1 problem(s)); run `check`"]


def test_empty_owner_is_a_validation_error(tmp_path: Path) -> None:
    rows = [_stage_row(n) for n in (2, 3, 4, 5)]
    rows.insert(0, _stage_row(1, owner="   "))
    problems = gate.validate(gate.load(_write(tmp_path, rows)))
    assert any("owner '' is a placeholder" in p for p in problems)


@pytest.mark.parametrize("role", ["team", "daily drivers", "stage-3 seats (see feedback.tsv)"])
def test_role_label_cannot_own_a_stage(tmp_path: Path, role: str) -> None:
    rows = [_stage_row(n) for n in (2, 3, 4, 5)]
    rows.insert(0, _stage_row(1, owner=role))
    problems = gate.validate(gate.load(_write(tmp_path, rows)))
    assert any(f"owner {role!r} is a role label, not a named person" in p for p in problems)


def test_every_unfilled_seat_is_named_in_the_refusal(tmp_path: Path) -> None:
    reasons = _reasons(_write(tmp_path, _through_stage_2()), 3)
    for seat in ("seat-1", "seat-2", "seat-3"):
        assert f"{seat} is unfilled: participant 'TBD' is a placeholder, not a named person" in (
            reasons
        )


@pytest.mark.parametrize("token", ["TBD", "-", "?", "unknown", "<name>", "n/a", "   "])
def test_no_placeholder_can_fill_a_stage_3_seat(tmp_path: Path, token: str) -> None:
    seats = [
        _named_seat("seat-1", "ann", disposition="fixed"),
        _named_seat("seat-2", "bob", disposition="deferred"),
        _seat("seat-3", participant=token),
    ]
    reasons = _reasons(_write(tmp_path, _through_stage_2(), feedback=seats), 3)
    assert any("seat-3 is unfilled" in r for r in reasons)
    assert any("needs 3 named daily-driver participants, 2 named" in r for r in reasons)


def test_a_reserved_empty_seat_is_legitimate(tmp_path: Path) -> None:
    # Nobody has been asked yet: that is a blank to fill, not a smuggled name.
    directory = _write(tmp_path, [_stage_row(n) for n in (1, 2, 3, 4, 5)])
    assert gate.validate(gate.load(directory)) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("tested_commit", "abc1234"),
        ("date", "2026-08-05"),
        ("completion_evidence", "shipped two tasks"),
        ("friction", "footer was noisy"),
        ("disposition", "fixed"),
        ("disposition_ref", "#210"),
    ],
)
def test_an_anonymous_seat_carrying_evidence_is_a_hard_error(
    tmp_path: Path, field: str, value: str
) -> None:
    # A seat that records anything is CLAIMING somebody sat in it. An anonymous claim is
    # not evidence, and this is the route a hand-edit would use to smuggle one in.
    seats = [_seat("seat-1", **{field: value}), _seat("seat-2"), _seat("seat-3")]
    problems = gate.validate(gate.load(_write(tmp_path, _stage_1_ready(), feedback=seats)))
    assert any(
        f"seat-1: participant 'TBD' is a placeholder but the seat records {field}" in p
        for p in problems
    )


def test_an_anonymous_seat_with_evidence_blocks_every_promotion(tmp_path: Path) -> None:
    seats = [_seat("seat-1", tested_commit="abc1234"), _seat("seat-2"), _seat("seat-3")]
    reasons = _reasons(_write(tmp_path, _stage_1_ready(), feedback=seats), 1)
    assert reasons == ["ledger does not validate (1 problem(s)); run `check`"]


@pytest.mark.parametrize("token", ["TBD", "?", "unknown", "n/a", "   "])
def test_placeholder_evidence_does_not_count_as_evidence(tmp_path: Path, token: str) -> None:
    rows = _stage_1_ready()
    rows[0] = _stage_row(
        1,
        tested_commit="abc1234",
        start_date="2026-08-01",
        entry_evidence=token,
        exit_evidence="shipped 2 tasks",
    )
    assert "no entry evidence recorded for S1-entry" in _reasons(_write(tmp_path, rows), 1)


def test_placeholder_evidence_on_a_promoted_row_is_a_validation_error(tmp_path: Path) -> None:
    bad = _promoted(1)
    bad[9] = "TBD"  # exit_evidence
    rows = [bad] + [_stage_row(n) for n in (2, 3, 4, 5)]
    problems = gate.validate(gate.load(_write(tmp_path, rows)))
    assert any("promoted but no exit evidence recorded for S1-exit" in p for p in problems)


def test_placeholder_blocker_resolution_is_a_validation_error(tmp_path: Path) -> None:
    blocker = ["BL-1", "1", "release-blocking", "resolved", "2026-08-02", "TBD", "was flaky"]
    problems = gate.validate(gate.load(_write(tmp_path, _stage_1_ready(), blockers=[blocker])))
    assert any("BL-1: resolved but no resolution recorded" in p for p in problems)


# -- tested_commit: shape always, resolution when git can answer --------------


@pytest.mark.parametrize("value", ["abc1234", "ABC1234", "a" * 40, "0123456789abcdef"])
def test_commit_shapes_that_are_accepted(value: str) -> None:
    assert gate.is_commit_shaped(value)


@pytest.mark.parametrize(
    "value",
    ["latest main", "abc12", "the build MJ ran", "zzzzzzz", "a" * 41, "HEAD", "v1.2.3", "#231"],
)
def test_commit_shapes_that_are_refused(value: str) -> None:
    assert not gate.is_commit_shaped(value)


@pytest.mark.parametrize("value", ["latest main", "abc12", "zzzzzzz", "HEAD"])
def test_a_tested_commit_that_is_not_a_sha_is_refused_without_git(
    tmp_path: Path, value: str
) -> None:
    # No resolver at all: shape is the half that works with no repository.
    rows = _stage_1_ready(commit=value)
    problems = gate.validate(gate.load(_write(tmp_path, rows)))
    assert any("is not a git object name (expect 7-40 hex characters)" in p for p in problems)


def _resolves_only(*known: str) -> gate.CommitResolver:
    return lambda sha: sha in known


def test_a_hex_shaped_but_unknown_commit_is_refused_when_git_can_answer(tmp_path: Path) -> None:
    # Well-shaped and entirely invented. Refusing it is a validation error, which is
    # strictly stronger than a promote-time refusal: it fails `check`, and therefore the
    # smoke, and therefore the PR - not just the one gate somebody remembered to run.
    ledger = gate.load(_write(tmp_path, _stage_1_ready(commit="abc1234")))
    problems = gate.validate(ledger, _resolves_only("deadbee"))
    assert any("tested_commit 'abc1234' is not a commit in this repository" in p for p in problems)
    assert gate.promote_reasons(ledger, 1, TODAY, _resolves_only("deadbee")) == [
        "ledger does not validate (1 problem(s)); run `check`"
    ]


def test_a_real_commit_clears_the_same_gate(tmp_path: Path) -> None:
    directory = _write(tmp_path, _stage_1_ready(commit="abc1234"))
    assert gate.promote_reasons(gate.load(directory), 1, TODAY, _resolves_only("abc1234")) == []


def test_a_seat_commit_must_resolve_too(tmp_path: Path) -> None:
    directory = _write(tmp_path, _through_stage_2(), feedback=_named_seats())
    problems = gate.validate(gate.load(directory), _resolves_only("abc1234"))
    assert any(
        "seat-3: tested_commit 'def5678' is not a commit in this repository" in p for p in problems
    )


def test_the_real_resolver_knows_head_from_a_fabricated_sha() -> None:
    resolve, note = gate.commit_resolver()
    if resolve is None:  # shallow clone or no git: it must SAY so, not guess
        assert note
        pytest.skip(f"git cannot answer here: {note}")
    assert note == ""
    assert resolve(HEAD)
    assert not resolve(FABRICATED)


def test_a_missing_resolver_is_reported_rather_than_guessed(tmp_path: Path) -> None:
    resolve, note = gate.commit_resolver(tmp_path / "not-a-repo")
    assert resolve is None
    assert "git cannot be consulted here" in note


# -- dates: well-formed and ordered ------------------------------------------


def test_a_stage_cannot_start_before_the_previous_one_ended(tmp_path: Path) -> None:
    rows = [_promoted(1)]  # 2026-08-01 -> 2026-08-02
    rows.append(_stage_row(2, start_date="2026-08-01", end_date="2026-08-03"))
    rows.extend(_stage_row(n) for n in (3, 4, 5))
    problems = gate.validate(gate.load(_write(tmp_path, rows)))
    assert any(
        "stage 2: start_date 2026-08-01 is before stage 1 ended (2026-08-02)" in p for p in problems
    )


def test_sequential_stage_dates_are_accepted(tmp_path: Path) -> None:
    rows = [_promoted(1), _promoted(2), _promoted(3)]
    rows.extend(_stage_row(n) for n in (4, 5))
    assert gate.validate(gate.load(_write(tmp_path, rows, feedback=_named_seats()))) == []


@pytest.mark.parametrize("value", ["2026-8-1", "08/05/2026", "yesterday", "2026-13-01"])
def test_malformed_dates_are_refused(tmp_path: Path, value: str) -> None:
    rows = [_stage_row(n) for n in (2, 3, 4, 5)]
    rows.insert(0, _stage_row(1, start_date=value))
    problems = gate.validate(gate.load(_write(tmp_path, rows)))
    assert any("is not YYYY-MM-DD or '-'" in p for p in problems)


def test_a_malformed_seat_date_is_refused(tmp_path: Path) -> None:
    seats = [_named_seat("seat-1", "ann", date="last tuesday"), _seat("seat-2"), _seat("seat-3")]
    problems = gate.validate(gate.load(_write(tmp_path, _stage_1_ready(), feedback=seats)))
    assert any("seat-1: date 'last tuesday' is not YYYY-MM-DD or '-'" in p for p in problems)


# -- hand-editing `promoted` does not bypass the gate ------------------------


def test_a_promotion_on_a_window_that_never_elapsed_is_an_error(tmp_path: Path) -> None:
    bad = _promoted(1)
    bad[7] = bad[6]  # end_date = start_date -> a zero-day window on a 1-day minimum
    rows = [bad] + [_stage_row(n) for n in (2, 3, 4, 5)]
    problems = gate.validate(gate.load(_write(tmp_path, rows)))
    assert any("promoted on a 0-day window, minimum is 1" in p for p in problems)


def test_an_out_of_order_promotion_recorded_by_hand_is_an_error(tmp_path: Path) -> None:
    rows = [_stage_row(1), _promoted(2)] + [_stage_row(n) for n in (3, 4, 5)]
    problems = gate.validate(gate.load(_write(tmp_path, rows)))
    assert any(
        "stage 2: promoted while stage 1 is pending; stages promote in order" in p for p in problems
    )


def test_a_stage_3_promotion_without_named_seats_is_an_error(tmp_path: Path) -> None:
    rows = [_promoted(1), _promoted(2), _promoted(3)]
    rows.extend(_stage_row(n) for n in (4, 5))
    problems = gate.validate(gate.load(_write(tmp_path, rows)))  # default seats are all TBD
    assert any("stage 3: promoted but seat-1 is unfilled" in p for p in problems)
    assert any("stage 3: promoted but seat-3 is unfilled" in p for p in problems)


def test_hand_edited_promotion_cannot_bypass_an_existing_open_blocker(tmp_path: Path) -> None:
    blocker = ["BL-1", "1", "release-blocking", "open", "2026-08-01", "-", "resume hangs"]
    rows = [_promoted(1)] + [_stage_row(n) for n in (2, 3, 4, 5)]
    problems = gate.validate(gate.load(_write(tmp_path, rows, blockers=[blocker])))
    assert any(
        "stage 1: promoted while release-blocking defect BL-1 was open" in p for p in problems
    )


def test_later_open_blocker_does_not_retroactively_invalidate_promotion(tmp_path: Path) -> None:
    blocker = ["BL-2", "2", "release-blocking", "open", "2026-08-03", "-", "new failure"]
    rows = [_promoted(1)] + [_stage_row(n) for n in (2, 3, 4, 5)]
    ledger = gate.load(_write(tmp_path, rows, blockers=[blocker]))
    assert gate.validate(ledger) == []
    assert "open release-blocking defect BL-2 (stage 2)" in gate.promote_reasons(ledger, 2, TODAY)


def test_a_promotion_with_a_fabricated_commit_is_an_error(tmp_path: Path) -> None:
    bad = _promoted(1)
    bad[5] = "latest main"
    rows = [bad] + [_stage_row(n) for n in (2, 3, 4, 5)]
    problems = gate.validate(gate.load(_write(tmp_path, rows)))
    assert any("is not a git object name" in p for p in problems)


def test_a_hand_edited_promotion_poisons_every_later_gate(tmp_path: Path) -> None:
    bad = _promoted(1)
    bad[7] = bad[6]
    rows = [bad] + [_stage_row(n) for n in (2, 3, 4, 5)]
    directory = _write(tmp_path, rows)
    for stage in (1, 2, 3, 4, 5):
        assert _reasons(directory, stage)[0].startswith("ledger does not validate")


# -- rollback: the mechanical half -------------------------------------------

GOOD_README = """# adoption

## Rollback path

```sh
uv tool install git+https://github.com/microsoft/amplifier
```

```sh
bash -o pipefail -c "curl --proto '=https' --tlsv1.2 -fsSL https://raw.githubusercontent.com/michaeljabbour/amplifier-app-tui/main/scripts/install.sh | bash -s -- --ref <tested_commit>"
```
"""

GOOD_PYPROJECT = """[project]
name = "amplifier-app-tui"
dependencies = ["textual~=8.2", "amplifier-core>=1.6.0", "click>=8.1.0"]

[project.scripts]
amplifier-tui = "amplifier_app_tui.main:main"
"""

GOOD_ADR = "# ADR-0008\n\n**Keep `amplifier-tui` as this repo's console script. Do not rename.**\n"

GOOD_INSTALL_CONTRACT = 'APP_REPO_URL = "https://github.com/michaeljabbour/amplifier-app-tui"\n'


def _fake_repo(
    tmp_path: Path,
    *,
    readme: str = GOOD_README,
    pyproject: str = GOOD_PYPROJECT,
    adr: str | None = GOOD_ADR,
    install_contract: str = GOOD_INSTALL_CONTRACT,
    extra_py: str = "",
    extra_sh: str = "",
) -> Path:
    root = tmp_path / "repo"
    (root / "docs" / "adoption").mkdir(parents=True)
    (root / "docs" / "decisions").mkdir(parents=True)
    (root / "src" / "amplifier_app_tui" / "kernel").mkdir(parents=True)
    (root / "scripts").mkdir(parents=True)
    (root / "docs" / "adoption" / "README.md").write_text(readme)
    (root / "pyproject.toml").write_text(pyproject)
    if adr is not None:
        (root / gate.ADR_PATH).write_text(adr)
    (root / gate.INSTALL_CONTRACT_PATH).write_text(install_contract)
    (root / "src" / "amplifier_app_tui" / "kernel" / "extra.py").write_text(extra_py)
    (root / "scripts" / "extra.sh").write_text(extra_sh)
    return root


def _rollback(root: Path, ledger: object | None = None, **kw: object) -> dict[str, str]:
    checks = gate.rollback_checks(ledger or gate.Ledger([], [], [], []), root, **kw)  # type: ignore[arg-type]
    return {c.label: f"{c.status} {c.detail}" for c in checks}


def test_rollback_mechanics_hold_for_this_repository() -> None:
    resolve, note = gate.commit_resolver()
    checks = gate.rollback_checks(gate.load(), gate.repo_root(), resolve, note)
    failed = [f"{c.label}: {c.detail}" for c in checks if c.status == gate.FAIL]
    assert failed == []
    assert len(checks) == 8


def test_cli_rollback_passes_on_this_repository() -> None:
    assert gate.main(["rollback"]) == 0


def test_rollback_states_the_half_it_cannot_check(capsys: pytest.CaptureFixture[str]) -> None:
    assert gate.main(["rollback"]) == 0
    out = capsys.readouterr().out
    assert "NOT machine-checked" in out
    for line in gate.HUMAN_ONLY:
        assert line in out


def test_a_healthy_synthetic_repo_passes_every_file_check(tmp_path: Path) -> None:
    results = _rollback(_fake_repo(tmp_path))
    assert [v for v in results.values() if v.startswith("FAIL")] == []


def test_rollback_catches_a_console_script_collision(tmp_path: Path) -> None:
    collide = GOOD_PYPROJECT.replace(
        'amplifier-tui = "amplifier_app_tui.main:main"',
        'amplifier = "amplifier_app_tui.main:main"',
    )
    results = _rollback(_fake_repo(tmp_path, pyproject=collide))
    assert results["both executables can be installed side by side"].startswith("FAIL")
    assert (
        "amplifier-app-cli already owns"
        in (results["both executables can be installed side by side"])
    )


def test_rollback_catches_a_second_console_script(tmp_path: Path) -> None:
    two = GOOD_PYPROJECT.replace(
        "[project.scripts]\namplifier-tui",
        '[project.scripts]\namp = "amplifier_app_tui.main:main"\namplifier-tui',
    )
    results = _rollback(_fake_repo(tmp_path, pyproject=two))
    assert results["both executables can be installed side by side"].startswith("FAIL")


def test_rollback_catches_a_dependency_tie(tmp_path: Path) -> None:
    tied = GOOD_PYPROJECT.replace('"click>=8.1.0"', '"amplifier-app-cli>=1.0"')
    results = _rollback(_fake_repo(tmp_path, pyproject=tied))
    assert results["no dependency tie between the two apps"].startswith("FAIL")


def test_rollback_catches_a_missing_adr(tmp_path: Path) -> None:
    results = _rollback(_fake_repo(tmp_path, adr=None))
    assert results["the coexistence decision is recorded (ADR-0008)"].startswith("FAIL")


def test_rollback_catches_a_reversed_adr(tmp_path: Path) -> None:
    results = _rollback(_fake_repo(tmp_path, adr="# ADR-0008\n\nRename it to `amplifier`.\n"))
    assert results["the coexistence decision is recorded (ADR-0008)"].startswith("FAIL")


def test_rollback_catches_a_missing_rollback_section(tmp_path: Path) -> None:
    results = _rollback(_fake_repo(tmp_path, readme="# adoption\n\nno rollback here\n"))
    assert results["the rollback path is documented"].startswith("FAIL")
    assert results["amplifier-app-cli restore command is well-formed"].startswith("FAIL")


def test_rollback_catches_a_pin_that_points_at_the_wrong_repo(tmp_path: Path) -> None:
    wrong = GOOD_README.replace("michaeljabbour/amplifier-app-tui/main", "someone/else/main")
    results = _rollback(_fake_repo(tmp_path, readme=wrong))
    assert results["pinned-build rollback command is well-formed"].startswith("FAIL")


def test_rollback_catches_a_pin_that_names_the_wrong_column(tmp_path: Path) -> None:
    wrong = GOOD_README.replace("<tested_commit>", "<sha>")
    results = _rollback(_fake_repo(tmp_path, readme=wrong))
    assert results["pinned-build rollback command is well-formed"].startswith("FAIL")


def test_rollback_catches_a_cli_restore_pointed_somewhere_else(tmp_path: Path) -> None:
    wrong = GOOD_README.replace(
        "uv tool install git+https://github.com/microsoft/amplifier\n",
        "uv tool install git+https://github.com/someone/fork\n",
    )
    results = _rollback(_fake_repo(tmp_path, readme=wrong))
    assert results["amplifier-app-cli restore command is well-formed"].startswith("FAIL")


def test_rollback_catches_code_that_would_uninstall_a_tool(tmp_path: Path) -> None:
    offender = 'CMD = ["uv", "tool", "uninstall", "amplifier"]\n'
    results = _rollback(_fake_repo(tmp_path, extra_py=offender))
    assert results["nothing here installs, upgrades, or removes amplifier-app-cli"].startswith(
        "FAIL"
    )


def test_rollback_catches_a_shell_script_that_would_uninstall_a_tool(tmp_path: Path) -> None:
    results = _rollback(_fake_repo(tmp_path, extra_sh="uv tool uninstall amplifier\n"))
    assert results["nothing here installs, upgrades, or removes amplifier-app-cli"].startswith(
        "FAIL"
    )


def test_rollback_catches_code_that_would_upgrade_the_cli(tmp_path: Path) -> None:
    offender = 'CMD = ["uv", "tool", "upgrade", "amplifier"]\n'
    results = _rollback(_fake_repo(tmp_path, extra_py=offender))
    assert results["nothing here installs, upgrades, or removes amplifier-app-cli"].startswith(
        "FAIL"
    )


def test_rollback_does_not_mistake_prose_for_a_subprocess_call(tmp_path: Path) -> None:
    # kernel/reset.py's docstring says it deliberately does NOT port `uv tool
    # uninstall/install`. A substring scan flags that sentence; reading argv literals via
    # the AST does not. This is why the check is an AST walk and not a grep.
    prose = '"""Deliberately NOT ported: uv cache clean + uv tool uninstall/install."""\n'
    results = _rollback(_fake_repo(tmp_path, extra_py=prose))
    assert results["nothing here installs, upgrades, or removes amplifier-app-cli"].startswith(
        "PASS"
    )


def test_rollback_allows_this_app_reinstalling_itself(tmp_path: Path) -> None:
    ours = (
        'CMD = ["uv", "tool", "install", "--reinstall", SOURCE]\nOTHER = ["uv", "cache", "clean"]\n'
    )
    results = _rollback(_fake_repo(tmp_path, extra_py=ours))
    assert results["nothing here installs, upgrades, or removes amplifier-app-cli"].startswith(
        "PASS"
    )


def test_rollback_skips_the_pin_check_when_nothing_is_pinned(tmp_path: Path) -> None:
    results = _rollback(_fake_repo(tmp_path))
    assert results["every recorded tested_commit is a real build to roll back to"].startswith(
        "SKIP"
    )


def test_rollback_refuses_an_unresolvable_pin(tmp_path: Path) -> None:
    ledger = gate.load(_write(tmp_path, _stage_1_ready(commit="abc1234")))
    results = _rollback(_fake_repo(tmp_path), ledger, resolve=_resolves_only("deadbee"))
    assert results["every recorded tested_commit is a real build to roll back to"].startswith(
        "FAIL"
    )


def test_rollback_says_it_cannot_check_pins_without_git(tmp_path: Path) -> None:
    ledger = gate.load(_write(tmp_path, _stage_1_ready(commit="abc1234")))
    results = _rollback(_fake_repo(tmp_path), ledger, resolve=None, note="shallow clone")
    verdict = results["every recorded tested_commit is a real build to roll back to"]
    assert verdict.startswith("SKIP")
    assert "shallow clone" in verdict


def test_rollback_never_raises_on_a_broken_repo(tmp_path: Path) -> None:
    root = _fake_repo(tmp_path, pyproject="this is not = toml [[[")
    checks = gate.rollback_checks(gate.Ledger([], [], [], []), root)
    assert any(c.status == gate.FAIL for c in checks)


# -- CLI: the end-to-end surface ---------------------------------------------
#
# `gate.main` resolves commits through an injectable `resolver_factory` (defaulting to
# the real `commit_resolver`), precisely so these end-to-end tests can assert all three
# resolver answers explicitly instead of inheriting whatever THIS clone's ambient git
# happens to be able to do. Without injection, "cannot tell" is exactly what happens on
# GitHub Actions' default checkout (`actions/checkout`, fetch-depth 1 -> shallow): the
# resolver honestly returns None, the commit check is skipped, and a test hard-coding
# `== 1` for a fabricated commit fails there while passing on every full local clone -
# see the shallow-clone repro in the PR description. Three branches, asserted separately:


def test_cli_refuses_a_fabricated_commit_end_to_end(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """resolver says the commit does NOT exist -> promote refuses, naming the commit.

    A fabricated tested_commit is a validation error (stronger than a promote-time
    refusal - it fails `check` too, see test_a_hex_shaped_but_unknown_commit_is_refused_
    when_git_can_answer), so `promote` itself prints the short "run `check`" pointer;
    running `check` with the same injected resolver surfaces the message that actually
    names the fabricated commit.
    """
    directory = _write(tmp_path, _stage_1_ready(commit=FABRICATED))
    promote_argv = ["promote", "1", "--dir", str(directory), "--today", "2026-08-10"]
    check_argv = ["check", "--dir", str(directory)]

    assert gate.main(promote_argv, resolver_factory=lambda: (_resolves_only(), "")) == 1
    assert gate.main(check_argv, resolver_factory=lambda: (_resolves_only(), "")) == 1
    out = capsys.readouterr().out
    assert f"tested_commit {FABRICATED!r} is not a commit in this repository" in out


def test_cli_does_not_accuse_when_git_cannot_tell(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """resolver says it CANNOT TELL (shallow clone / no git) -> promote does not accuse.

    This is the documented degrade-honest behaviour (`commit_resolver`'s docstring):
    refusing to answer is not the same as answering "no". A real, correctly-recorded
    commit must never be reported as fabricated just because this clone is shallow, so
    the commit check is skipped rather than blocking - and the CLI still says plainly,
    on stderr, that it could not verify.
    """
    directory = _write(tmp_path, _stage_1_ready(commit=FABRICATED))
    argv = ["promote", "1", "--dir", str(directory), "--today", "2026-08-10"]
    shallow_note = "shallow clone: commit history is incomplete, resolution would lie"
    code = gate.main(argv, resolver_factory=lambda: (None, shallow_note))
    assert code == 0
    err = capsys.readouterr().err
    assert "could not verify" in err


def test_cli_allows_promotion_when_the_commit_really_resolves(tmp_path: Path) -> None:
    """resolver says the commit EXISTS -> the commit check does not block."""
    directory = _write(tmp_path, _stage_1_ready(commit="abc1234"))
    argv = ["promote", "1", "--dir", str(directory), "--today", "2026-08-10"]
    code = gate.main(argv, resolver_factory=lambda: (_resolves_only("abc1234"), ""))
    assert code == 0


def test_cli_no_git_falls_back_to_shape_only(tmp_path: Path) -> None:
    directory = _write(tmp_path, _stage_1_ready(commit=FABRICATED))
    argv = ["promote", "1", "--no-git", "--dir", str(directory), "--today", "2026-08-10"]
    assert gate.main(argv) == 0


def test_cli_no_git_still_refuses_prose(tmp_path: Path) -> None:
    directory = _write(tmp_path, _stage_1_ready(commit="latest main"))
    argv = ["check", "--no-git", "--dir", str(directory)]
    assert gate.main(argv) == 1


def test_cli_status_reports_which_seats_are_unfilled(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert gate.main(["status"]) == 0
    assert "stage-3 seats unfilled: seat-1, seat-2, seat-3" in capsys.readouterr().out


def test_cli_never_raises_on_a_garbage_ledger(tmp_path: Path) -> None:
    directory = tmp_path / "adoption"
    directory.mkdir()
    for name in ("stages.tsv", "blockers.tsv", "feedback.tsv"):
        (directory / name).write_text("\x00\x01 not a ledger \t\t\t\n1\t2\n")
    assert gate.main(["check", "--dir", str(directory)]) == 1
    assert gate.main(["status", "--dir", str(directory)]) == 0
    assert gate.main(["promote", "1", "--dir", str(directory)]) == 1
    assert gate.main(["rollback", "--dir", str(directory)]) == 0
