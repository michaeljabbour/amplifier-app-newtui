#!/usr/bin/env python3
"""Adoption gate - read-only checker over the `docs/adoption/` stage ledger.

Governance item B5: amplifier-app-tui may only replace amplifier-app-cli after five
staged gates clear. The ledger (`docs/adoption/*.tsv`) is the record; this tool is the
only thing that reads it mechanically, so the two negative rules cannot be waved through:

  * a stage cannot be promoted while ANY release-blocking defect is open, no matter how
    much of its usage window has elapsed (AC2);
  * amplifier-app-cli cannot be replaced until stage 4 promotes, which by the same rule
    requires zero unresolved release-blockers (AC5).

Deliberately **read-only**: it never edits a ledger file. Promotions are hand-edited in a
reviewed PR - the git history is the audit trail - and this tool only agrees or refuses.

Modeled on `pipelines/ledger.py`: stdlib only, TSV rows, never raises (a crash returns a
non-zero exit code and a message, so it is safe to wire into a shell gate).

Commands:
  check                validate every ledger row; exit 0 when clean
  status               one line per stage: owner, decision, window progress, blockers
  promote <stage>      may stage <stage> be promoted? exit 0 = yes, 1 = blocked
                       `promote 4` IS the replacement gate (AC5): clearing it is what
                       authorizes stage 5, the retirement of amplifier-app-cli.

Options:
  --today YYYY-MM-DD   evaluate windows against this date (default: today)
  --dir PATH           ledger directory (default: $ADOPTION_DIR or docs/adoption)
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

NOT_RECORDED = "-"
DECISIONS = ("pending", "promoted", "held", "rolled-back")
SEVERITIES = ("release-blocking", "friction")
BLOCKER_STATUSES = ("open", "resolved")
DISPOSITIONS = ("untriaged", "fixed", "deferred", "wont-fix", "duplicate")
STAGES = (1, 2, 3, 4, 5)
FEEDBACK_STAGE = 3
STAGE_3_MIN_SEATS = 3
STAGE_COLUMNS = 11
BLOCKER_COLUMNS = 7
FEEDBACK_COLUMNS = 9


def default_dir() -> Path:
    """Ledger directory: $ADOPTION_DIR, else `<repo>/docs/adoption`."""
    env = os.environ.get("ADOPTION_DIR")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "docs" / "adoption"


@dataclass(frozen=True)
class Stage:
    stage: int
    owner: str
    min_window_days: int
    entry_criteria: str
    exit_criteria: str
    tested_commit: str
    start_date: str
    end_date: str
    entry_evidence: str
    exit_evidence: str
    decision: str


@dataclass(frozen=True)
class Blocker:
    id: str
    stage: str
    severity: str
    status: str
    opened: str
    resolution: str
    summary: str


@dataclass(frozen=True)
class Feedback:
    seat: str
    participant: str
    stage: str
    tested_commit: str
    date: str
    completion_evidence: str
    friction: str
    disposition: str
    disposition_ref: str


@dataclass
class Ledger:
    stages: list[Stage]
    blockers: list[Blocker]
    feedback: list[Feedback]
    errors: list[str]

    def stage(self, number: int) -> Stage | None:
        for row in self.stages:
            if row.stage == number:
                return row
        return None

    def open_release_blockers(self) -> list[Blocker]:
        return [b for b in self.blockers if b.severity == "release-blocking" and b.status == "open"]


def _rows(path: Path, columns: int, errors: list[str]) -> list[list[str]]:
    """Read a TSV, skipping blanks and `#` comments. Bad rows become errors, not raises."""
    if not path.exists():
        errors.append(f"{path.name}: missing")
        return []
    out: list[list[str]] = []
    for number, raw in enumerate(path.read_text().splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = raw.split("\t")
        if len(parts) != columns:
            errors.append(f"{path.name}:{number}: expected {columns} columns, got {len(parts)}")
            continue
        out.append([p.strip() for p in parts])
    return out


def _valid_date(value: str) -> bool:
    if value == NOT_RECORDED:
        return True
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def load(directory: Path | None = None) -> Ledger:
    """Parse the three ledger files. Never raises: parse failures land in `errors`."""
    root = directory or default_dir()
    errors: list[str] = []

    stages: list[Stage] = []
    for row in _rows(root / "stages.tsv", STAGE_COLUMNS, errors):
        if not row[0].isdigit() or not row[2].isdigit():
            errors.append(f"stages.tsv: stage and min_window_days must be integers: {row[0]!r}")
            continue
        stages.append(
            Stage(
                stage=int(row[0]),
                owner=row[1],
                min_window_days=int(row[2]),
                entry_criteria=row[3],
                exit_criteria=row[4],
                tested_commit=row[5],
                start_date=row[6],
                end_date=row[7],
                entry_evidence=row[8],
                exit_evidence=row[9],
                decision=row[10],
            )
        )

    blockers = [Blocker(*row) for row in _rows(root / "blockers.tsv", BLOCKER_COLUMNS, errors)]
    feedback = [Feedback(*row) for row in _rows(root / "feedback.tsv", FEEDBACK_COLUMNS, errors)]
    return Ledger(stages=stages, blockers=blockers, feedback=feedback, errors=errors)


def _validate_stages(ledger: Ledger, problems: list[str]) -> None:
    seen = [s.stage for s in ledger.stages]
    for expected in STAGES:
        if seen.count(expected) != 1:
            problems.append(f"stages.tsv: stage {expected} must appear exactly once")
    for row in ledger.stages:
        where = f"stages.tsv stage {row.stage}"
        if row.decision not in DECISIONS:
            problems.append(f"{where}: decision {row.decision!r} not in {list(DECISIONS)}")
        for field, value in (("start_date", row.start_date), ("end_date", row.end_date)):
            if not _valid_date(value):
                problems.append(f"{where}: {field} {value!r} is not YYYY-MM-DD or '-'")
        if row.end_date != NOT_RECORDED and row.start_date == NOT_RECORDED:
            problems.append(f"{where}: end_date recorded without a start_date")
        if _valid_date(row.start_date) and _valid_date(row.end_date):
            if row.start_date != NOT_RECORDED and row.end_date != NOT_RECORDED:
                if date.fromisoformat(row.end_date) < date.fromisoformat(row.start_date):
                    problems.append(f"{where}: end_date is before start_date")
        if row.decision == "promoted":
            for field, value in (
                ("tested_commit", row.tested_commit),
                ("start_date", row.start_date),
                ("end_date", row.end_date),
                ("entry_evidence", row.entry_evidence),
                ("exit_evidence", row.exit_evidence),
            ):
                if value == NOT_RECORDED:
                    problems.append(f"{where}: promoted but {field} is not recorded")


def _validate_blockers(ledger: Ledger, problems: list[str]) -> None:
    for row in ledger.blockers:
        where = f"blockers.tsv {row.id}"
        if row.severity not in SEVERITIES:
            problems.append(f"{where}: severity {row.severity!r} not in {list(SEVERITIES)}")
        if row.status not in BLOCKER_STATUSES:
            problems.append(f"{where}: status {row.status!r} not in {list(BLOCKER_STATUSES)}")
        if not _valid_date(row.opened):
            problems.append(f"{where}: opened {row.opened!r} is not YYYY-MM-DD or '-'")
        if row.status == "resolved" and row.resolution == NOT_RECORDED:
            problems.append(f"{where}: resolved but no resolution recorded")


def _validate_feedback(ledger: Ledger, problems: list[str]) -> None:
    for row in ledger.feedback:
        where = f"feedback.tsv {row.seat}"
        if row.disposition not in DISPOSITIONS:
            problems.append(f"{where}: disposition {row.disposition!r} not in {list(DISPOSITIONS)}")
        if not _valid_date(row.date):
            problems.append(f"{where}: date {row.date!r} is not YYYY-MM-DD or '-'")
    seats = [f for f in ledger.feedback if f.stage == str(FEEDBACK_STAGE)]
    if len(seats) < STAGE_3_MIN_SEATS:
        problems.append(
            f"feedback.tsv: stage 3 needs at least {STAGE_3_MIN_SEATS} seats, found {len(seats)}"
        )


def validate(ledger: Ledger) -> list[str]:
    """Every structural problem in the ledger, worst-first is not needed - all of them."""
    problems = list(ledger.errors)
    _validate_stages(ledger, problems)
    _validate_blockers(ledger, problems)
    _validate_feedback(ledger, problems)
    return problems


def _stage_3_reasons(ledger: Ledger) -> list[str]:
    """AC3: three named daily drivers, each on a known build, each dispositioned."""
    reasons: list[str] = []
    seats = [f for f in ledger.feedback if f.stage == str(FEEDBACK_STAGE)]
    named = [f for f in seats if f.participant not in (NOT_RECORDED, "TBD", "")]
    if len(named) < STAGE_3_MIN_SEATS:
        reasons.append(
            f"stage 3 needs {STAGE_3_MIN_SEATS} named daily-driver participants, "
            f"{len(named)} named ({len(seats)} seats reserved)"
        )
    for seat in named:
        if seat.tested_commit == NOT_RECORDED:
            reasons.append(f"{seat.seat} ({seat.participant}) has no tested_commit recorded")
        if seat.disposition == "untriaged":
            reasons.append(f"{seat.seat} ({seat.participant}) feedback is still untriaged")
    return reasons


def promote_reasons(ledger: Ledger, number: int, today: date) -> list[str]:
    """Why stage `number` may NOT be promoted. Empty list == the gate is clear."""
    problems = validate(ledger)
    if problems:
        return [f"ledger does not validate ({len(problems)} problem(s)); run `check`"]

    row = ledger.stage(number)
    if row is None:
        return [f"no stage {number} in the ledger"]

    reasons: list[str] = []
    if row.decision == "promoted":
        return ["already promoted"]

    earlier_stages = sorted((s for s in ledger.stages if s.stage < number), key=lambda s: s.stage)
    for earlier in earlier_stages:
        if earlier.decision != "promoted":
            reasons.append(f"stage {earlier.stage} is not promoted (decision={earlier.decision})")

    if row.start_date == NOT_RECORDED:
        reasons.append("stage has not started (no start_date)")
    else:
        end = date.fromisoformat(row.end_date) if row.end_date != NOT_RECORDED else today
        elapsed = (end - date.fromisoformat(row.start_date)).days
        if elapsed < row.min_window_days:
            reasons.append(
                f"usage window not met: {elapsed} of {row.min_window_days} day(s) elapsed"
            )

    if row.tested_commit == NOT_RECORDED:
        reasons.append("no tested_commit recorded")
    if row.entry_evidence == NOT_RECORDED:
        reasons.append(f"no entry evidence recorded for {row.entry_criteria}")
    if row.exit_evidence == NOT_RECORDED:
        reasons.append(f"no exit evidence recorded for {row.exit_criteria}")

    # AC2 - independent of elapsed time, and deliberately repo-wide: a release-blocking
    # defect open anywhere stops the whole train, including the stage-4 replacement gate.
    for blocker in ledger.open_release_blockers():
        reasons.append(f"open release-blocking defect {blocker.id} (stage {blocker.stage})")

    if number == FEEDBACK_STAGE:
        reasons.extend(_stage_3_reasons(ledger))

    return reasons


def _window(row: Stage, today: date) -> str:
    if row.start_date == NOT_RECORDED:
        return f"0/{row.min_window_days}d"
    end = date.fromisoformat(row.end_date) if row.end_date != NOT_RECORDED else today
    elapsed = (end - date.fromisoformat(row.start_date)).days
    return f"{elapsed}/{row.min_window_days}d"


def _cmd_status(ledger: Ledger, today: date) -> int:
    open_blockers = ledger.open_release_blockers()
    print("stage\towner\tdecision\twindow\ttested_commit")
    for row in sorted(ledger.stages, key=lambda s: s.stage):
        print(
            f"{row.stage}\t{row.owner}\t{row.decision}\t{_window(row, today)}\t{row.tested_commit}"
        )
    label = ", ".join(b.id for b in open_blockers) if open_blockers else "none"
    print(f"open release-blockers: {label}")
    return 0


def _cmd_check(ledger: Ledger) -> int:
    problems = validate(ledger)
    if not problems:
        print(
            f"ledger OK: {len(ledger.stages)} stages, {len(ledger.blockers)} blockers, "
            f"{len(ledger.feedback)} feedback rows"
        )
        return 0
    for problem in problems:
        print(f"PROBLEM {problem}")
    print(f"{len(problems)} problem(s)")
    return 1


def _cmd_promote(ledger: Ledger, argument: str, today: date) -> int:
    if not argument.isdigit():
        print(f"usage: adoption_gate.py promote <stage>; got {argument!r}", file=sys.stderr)
        return 2
    number = int(argument)
    reasons = promote_reasons(ledger, number, today)
    if not reasons:
        print(f"PROMOTE stage {number}: gate clear")
        return 0
    print(f"BLOCKED stage {number}")
    for reason in reasons:
        print(f"  - {reason}")
    return 1


def main(argv: list[str]) -> int:
    args = list(argv)
    today = date.today()
    directory: Path | None = None

    while len(args) >= 2 and args[-2] in ("--today", "--dir"):
        flag, value = args[-2], args[-1]
        args = args[:-2]
        if flag == "--today":
            today = date.fromisoformat(value)
        else:
            directory = Path(value)

    command = args[0] if args else "status"
    ledger = load(directory)

    if command == "check":
        return _cmd_check(ledger)
    if command == "status":
        return _cmd_status(ledger, today)
    if command == "promote" and len(args) == 2:
        return _cmd_promote(ledger, args[1], today)

    print(f"unknown command: {command}", file=sys.stderr)
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - a governance gate must fail loud, not crash
        print(f"adoption_gate: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
