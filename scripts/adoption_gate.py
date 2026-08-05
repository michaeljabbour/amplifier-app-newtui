#!/usr/bin/env python3
"""Adoption gate - read-only checker over the `docs/adoption/` stage ledger.

Governance item B5: amplifier-app-tui may only replace amplifier-app-cli after five
staged gates clear. The ledger (`docs/adoption/*.tsv`) is the record; this tool is the
only thing that reads it mechanically, so the negative rules cannot be waved through:

  * a stage cannot be promoted while ANY release-blocking defect is open, no matter how
    much of its usage window has elapsed (AC2);
  * amplifier-app-cli cannot be replaced until stage 5 promotes, which by the same rule
    requires zero unresolved release-blockers (AC5);
  * a placeholder is not a person. `TBD`, `?`, `unknown`, an empty cell and friends are
    refused by name - they can never fill a stage-3 seat, own a stage, or stand in for
    evidence (see `PLACEHOLDERS`);
  * hand-editing `decision = promoted` does not bypass the gate. `check` re-derives every
    promotion condition against every row that claims to be promoted.

Deliberately **read-only**: it never edits a ledger file, and the only external commands
it runs are read-only git plumbing (`rev-parse`, `cat-file -e`) used to prove that a
recorded `tested_commit` is a real commit object in this clone.

Modeled on `pipelines/ledger.py`: stdlib only, TSV rows, never raises (a crash returns a
non-zero exit code and a message, so it is safe to wire into a shell gate).

Commands:
  check                validate every ledger row; exit 0 when clean
  status               one line per stage: owner, decision, window progress, blockers
  promote <stage>      may stage <stage> be promoted? exit 0 = yes, 1 = blocked
                       `promote 5` IS the replacement gate (AC5): stage 4 authorizes the
                       final observation window; stage 5 authorizes deprecation.
  rollback             verify the MECHANICAL half of the documented rollback path:
                       command shapes, the pinned-commit target, and the side-by-side
                       coexistence claim. The walk-through stays human - it says so.

Options:
  --today YYYY-MM-DD   evaluate windows against this date (default: today)
  --dir PATH           ledger directory (default: $ADOPTION_DIR or docs/adoption)
  --no-git             skip git commit resolution (shape checks still apply)
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, fields
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

# --- what counts as a placeholder -------------------------------------------
#
# ONE list, used everywhere a cell is supposed to hold a real person or a real piece of
# evidence. A ledger is only worth anything if the names in it are names, so a cell that
# merely LOOKS filled must be refused as loudly as an empty one - otherwise `TBD` quietly
# counts as a daily driver. `-` (NOT_RECORDED) is in here on purpose: "not recorded" and
# "recorded as nothing" are the same fact, and splitting them just invites a second,
# weaker code path.
PLACEHOLDERS = frozenset(
    {
        "",
        "-",
        "--",
        "---",
        ".",
        "?",
        "??",
        "???",
        "n/a",
        "na",
        "nil",
        "none",
        "null",
        "tba",
        "tbc",
        "tbd",
        "todo",
        "to do",
        "to-do",
        "unassigned",
        "unfilled",
        "unknown",
        "xx",
        "xxx",
        "fixme",
        "pending",
        "placeholder",
        "someone",
        "somebody",
        "anyone",
        "name",
        "name here",
        "your name",
        "participant",
        "owner",
    }
)

# Wrappers a hand-editor reaches for when writing a blank: <name>, [TBD], (unknown), `?`.
_PLACEHOLDER_WRAPPERS = "<>[](){}\"'`*_ \t"

# A value can be non-empty and still name a role rather than a person. Keep this
# person-specific: ``is_recorded`` is also used for evidence such as "team-wide smoke",
# where role words are perfectly legitimate. Parenthetical instructions are removed before
# comparison, so ``stage-3 seats (see feedback.tsv)`` cannot masquerade as an owner.
PERSON_ROLE_PLACEHOLDERS = frozenset(
    {
        "daily driver",
        "daily drivers",
        "maintainer",
        "participants",
        "repo maintainer",
        "stage seats",
        "team",
        "team wide",
        "team-wide",
        "three additional daily drivers",
        "users",
    }
)
_STAGE_SEATS = re.compile(r"\Astage[- ]?\d+ seats?\Z")


def is_placeholder(value: str) -> bool:
    """True when `value` is a stand-in rather than a real name / commit / piece of evidence.

    Normalizes the way a human actually types a blank before comparing: non-breaking
    spaces become spaces, wrapping punctuation is stripped, internal whitespace is
    collapsed, and the comparison is case-folded. `"  TBD "`, `"<name>"`, `"[ ? ]"` and
    `"N/A"` are all the same answer: nobody.
    """
    token = value.replace("\u00a0", " ").strip(_PLACEHOLDER_WRAPPERS)
    token = " ".join(token.split()).casefold()
    return token in PLACEHOLDERS


def is_recorded(value: str) -> bool:
    """Inverse of :func:`is_placeholder`, for the many `if actually filled in` reads."""
    return not is_placeholder(value)


def _person_token(value: str) -> str:
    """Normalize a would-be person's name while discarding trailing instructions."""
    token = value.replace("\u00a0", " ").strip()
    token = re.sub(r"\s*\([^)]*\)\s*$", "", token)
    token = token.strip(_PLACEHOLDER_WRAPPERS)
    return " ".join(token.split()).casefold()


def is_named_person(value: str) -> bool:
    """True only when ``value`` is neither a blank nor a role label."""
    if is_placeholder(value):
        return False
    token = _person_token(value)
    return token not in PERSON_ROLE_PLACEHOLDERS and not bool(_STAGE_SEATS.fullmatch(token))


def _person_refusal(value: str) -> str:
    if is_placeholder(value):
        return f"{value!r} is a placeholder, not a named person"
    return f"{value!r} is a role label, not a named person"


# --- commit shape and resolution --------------------------------------------

COMMIT_MIN_LENGTH = 7
COMMIT_MAX_LENGTH = 40
_HEX = re.compile(r"\A[0-9a-fA-F]+\Z")

CommitResolver = Callable[[str], bool]


def is_commit_shaped(value: str) -> bool:
    """True when `value` could be a git object name: 7-40 hex characters.

    Shape is checked even when git cannot be consulted, because it is the half that
    catches prose ("latest main", "the build MJ ran") with no repository at all.
    """
    return COMMIT_MIN_LENGTH <= len(value) <= COMMIT_MAX_LENGTH and bool(_HEX.match(value))


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _git(repo: Path, *args: str) -> tuple[int, str]:
    """Run read-only git plumbing. Never raises: a missing binary is just a failure code."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # no git, no repo, hung call
        return 127, f"{type(exc).__name__}: {exc}"
    return proc.returncode, proc.stdout.strip()


def commit_resolver(repo: Path | None = None) -> tuple[CommitResolver | None, str]:
    """Return `(resolver, note)`; `resolver is None` when git cannot answer honestly.

    Refusing to answer is not the same as answering "no". In a shallow clone the object
    for a real, correctly-recorded commit is genuinely absent, so reporting it as
    fabricated would be a false accusation - the tool says it cannot tell instead.
    """
    root = repo or repo_root()
    code, _ = _git(root, "rev-parse", "--is-inside-work-tree")
    if code != 0:
        return None, "git cannot be consulted here (no git binary, or not a work tree)"
    code, out = _git(root, "rev-parse", "--is-shallow-repository")
    if code == 0 and out == "true":
        return None, "shallow clone: commit history is incomplete, resolution would lie"

    def resolves(sha: str) -> bool:
        found, _ = _git(root, "cat-file", "-e", f"{sha}^{{commit}}")
        return found == 0

    return resolves, ""


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

    def seats(self) -> list[Feedback]:
        return [f for f in self.feedback if f.stage == str(FEEDBACK_STAGE)]

    def open_release_blockers(self) -> list[Blocker]:
        return [b for b in self.blockers if b.severity == "release-blocking" and b.status == "open"]


def default_dir() -> Path:
    """Ledger directory: $ADOPTION_DIR, else `<repo>/docs/adoption`."""
    env = os.environ.get("ADOPTION_DIR")
    if env:
        return Path(env)
    return repo_root() / "docs" / "adoption"


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


# --- shared predicates ------------------------------------------------------
#
# `validate` and `promote_reasons` ask the same questions from two directions - "is this
# promoted row legitimate?" and "may this row be promoted?" - so the questions live here
# once. Duplicating them is how a hand-edit finds the weaker copy.


def _commit_problem(label: str, sha: str, resolve: CommitResolver | None) -> str | None:
    """Why `sha` is not an acceptable tested_commit, or None. Assumes it is recorded."""
    if not is_commit_shaped(sha):
        return f"{label} {sha!r} is not a git object name (expect 7-40 hex characters)"
    if resolve is not None and not resolve(sha):
        return f"{label} {sha!r} is not a commit in this repository"
    return None


def _elapsed_days(row: Stage, today: date) -> int | None:
    """Days the stage has been running, or None when it has not started."""
    if not is_recorded(row.start_date) or not _valid_date(row.start_date):
        return None
    if is_recorded(row.end_date) and _valid_date(row.end_date):
        end = date.fromisoformat(row.end_date)
    else:
        end = today
    return (end - date.fromisoformat(row.start_date)).days


def _evidence_reasons(row: Stage, resolve: CommitResolver | None) -> list[str]:
    """AC1: a stage carries a real tested build and real entry/exit evidence."""
    reasons: list[str] = []
    if not is_recorded(row.tested_commit):
        reasons.append("no tested_commit recorded")
    else:
        problem = _commit_problem("tested_commit", row.tested_commit, resolve)
        if problem:
            reasons.append(problem)
    if not is_recorded(row.entry_evidence):
        reasons.append(f"no entry evidence recorded for {row.entry_criteria}")
    if not is_recorded(row.exit_evidence):
        reasons.append(f"no exit evidence recorded for {row.exit_criteria}")
    return reasons


def _stage_3_reasons(ledger: Ledger, resolve: CommitResolver | None) -> list[str]:
    """AC3: three named drivers with reproducible, dispositioned feedback records."""
    reasons: list[str] = []
    seats = ledger.seats()
    named = [s for s in seats if is_named_person(s.participant)]

    # Name the unfilled seats individually. "2 of 3 named" tells you the shortfall;
    # "seat-3 is unfilled" tells you what to go do about it.
    for seat in seats:
        if not is_named_person(seat.participant):
            reasons.append(
                f"{seat.seat} is unfilled: participant {_person_refusal(seat.participant)}"
            )
    if len(named) < STAGE_3_MIN_SEATS:
        reasons.append(
            f"stage 3 needs {STAGE_3_MIN_SEATS} named daily-driver participants, "
            f"{len(named)} named ({len(seats)} seats reserved)"
        )
    for seat in named:
        if not is_recorded(seat.tested_commit):
            reasons.append(f"{seat.seat} ({seat.participant}) has no tested_commit recorded")
        else:
            problem = _commit_problem(
                f"{seat.seat} ({seat.participant}) tested_commit", seat.tested_commit, resolve
            )
            if problem:
                reasons.append(problem)
        if seat.disposition == "untriaged":
            reasons.append(f"{seat.seat} ({seat.participant}) feedback is still untriaged")
        if not is_recorded(seat.date):
            reasons.append(f"{seat.seat} ({seat.participant}) has no feedback date recorded")
        if not is_recorded(seat.completion_evidence):
            reasons.append(f"{seat.seat} ({seat.participant}) has no completion evidence")
        if not is_recorded(seat.friction):
            reasons.append(f"{seat.seat} ({seat.participant}) has no friction report recorded")
        if not is_recorded(seat.disposition_ref):
            reasons.append(f"{seat.seat} ({seat.participant}) has no disposition reference")
    return reasons


# --- validation -------------------------------------------------------------


def _validate_stages(ledger: Ledger, problems: list[str], resolve: CommitResolver | None) -> None:
    seen = [s.stage for s in ledger.stages]
    for expected in STAGES:
        if seen.count(expected) != 1:
            problems.append(f"stages.tsv: stage {expected} must appear exactly once")

    for row in ledger.stages:
        where = f"stages.tsv stage {row.stage}"
        if not is_named_person(row.owner):
            problems.append(
                f"{where}: owner {_person_refusal(row.owner)} - "
                "every stage needs someone accountable for the record"
            )
        if row.min_window_days < 1:
            problems.append(
                f"{where}: min_window_days is {row.min_window_days}; every stage requires at "
                "least one day"
            )
        if row.decision not in DECISIONS:
            problems.append(f"{where}: decision {row.decision!r} not in {list(DECISIONS)}")
        for field, value in (("start_date", row.start_date), ("end_date", row.end_date)):
            if not _valid_date(value):
                problems.append(f"{where}: {field} {value!r} is not YYYY-MM-DD or '-'")
        if is_recorded(row.end_date) and not is_recorded(row.start_date):
            problems.append(f"{where}: end_date recorded without a start_date")
        if _valid_date(row.start_date) and _valid_date(row.end_date):
            if is_recorded(row.start_date) and is_recorded(row.end_date):
                if date.fromisoformat(row.end_date) < date.fromisoformat(row.start_date):
                    problems.append(f"{where}: end_date is before start_date")
        if is_recorded(row.tested_commit):
            problem = _commit_problem("tested_commit", row.tested_commit, resolve)
            if problem:
                problems.append(f"{where}: {problem}")

    _validate_stage_order(ledger, problems)
    _validate_promoted_rows(ledger, problems, resolve)


def _validate_stage_order(ledger: Ledger, problems: list[str]) -> None:
    """Stages run in sequence, so their recorded dates must too.

    Every stage's entry criterion names the previous stage as `promoted` (README,
    "Entry and exit criteria"), so a stage that starts before its predecessor ended
    contradicts the record it is part of - which is what back-dating looks like.
    """
    ordered = sorted(ledger.stages, key=lambda s: s.stage)
    for previous, current in zip(ordered, ordered[1:], strict=False):
        if not (is_recorded(previous.end_date) and is_recorded(current.start_date)):
            continue
        if not (_valid_date(previous.end_date) and _valid_date(current.start_date)):
            continue
        if date.fromisoformat(current.start_date) < date.fromisoformat(previous.end_date):
            problems.append(
                f"stages.tsv stage {current.stage}: start_date {current.start_date} is before "
                f"stage {previous.stage} ended ({previous.end_date}); stages run in sequence"
            )


def _validate_promoted_rows(
    ledger: Ledger, problems: list[str], resolve: CommitResolver | None
) -> None:
    """Re-derive the gate for every row that CLAIMS to be promoted.

    `promote` is advisory - a human still hand-edits `decision = promoted` in a PR. If
    validation only checked that the evidence columns were non-empty, editing the decision
    column would be the whole bypass. So a promoted row must satisfy the same conditions
    `promote` would have demanded, forever, every time `check` runs.
    """
    for row in ledger.stages:
        if row.decision != "promoted":
            continue
        where = f"stages.tsv stage {row.stage}"
        for reason in _evidence_reasons(row, resolve):
            problems.append(f"{where}: promoted but {reason}")
        for field, value in (("start_date", row.start_date), ("end_date", row.end_date)):
            if not is_recorded(value):
                problems.append(f"{where}: promoted but {field} is not recorded")

        elapsed = _elapsed_days(row, date.today())
        if is_recorded(row.end_date) and elapsed is not None and elapsed < row.min_window_days:
            problems.append(
                f"{where}: promoted on a {elapsed}-day window, minimum is {row.min_window_days}"
            )

        earlier = [s for s in ledger.stages if s.stage < row.stage and s.decision != "promoted"]
        for stage in sorted(earlier, key=lambda s: s.stage):
            problems.append(
                f"{where}: promoted while stage {stage.stage} is {stage.decision}; "
                "stages promote in order"
            )

        if row.stage == FEEDBACK_STAGE:
            for reason in _stage_3_reasons(ledger, resolve):
                problems.append(f"{where}: promoted but {reason}")

        # A hand-edited ``promoted`` decision must not erase an already-known blocker.
        # A blocker opened later does not retroactively invalidate a prior promotion, but
        # it still blocks every future promotion through ``promote_reasons``. Unknown
        # blocker dates are conservative: the record cannot prove they post-date the gate.
        if is_recorded(row.end_date) and _valid_date(row.end_date):
            promoted_on = date.fromisoformat(row.end_date)
            for blocker in ledger.open_release_blockers():
                if not is_recorded(blocker.opened) or not _valid_date(blocker.opened):
                    problems.append(
                        f"{where}: promoted while open release-blocking defect {blocker.id} "
                        "has no valid opened date, so the record cannot prove it came later"
                    )
                    continue
                if date.fromisoformat(blocker.opened) <= promoted_on:
                    problems.append(
                        f"{where}: promoted while release-blocking defect {blocker.id} was "
                        f"open (opened {blocker.opened} on or before {row.end_date})"
                    )


def _validate_blockers(ledger: Ledger, problems: list[str]) -> None:
    for row in ledger.blockers:
        where = f"blockers.tsv {row.id}"
        if row.severity not in SEVERITIES:
            problems.append(f"{where}: severity {row.severity!r} not in {list(SEVERITIES)}")
        if row.status not in BLOCKER_STATUSES:
            problems.append(f"{where}: status {row.status!r} not in {list(BLOCKER_STATUSES)}")
        if not _valid_date(row.opened):
            problems.append(f"{where}: opened {row.opened!r} is not YYYY-MM-DD or '-'")
        if row.status == "resolved" and not is_recorded(row.resolution):
            problems.append(f"{where}: resolved but no resolution recorded")


def _placeholder_seat_problem(row: Feedback) -> str | None:
    """A seat may be reserved and empty; it may not be anonymous and full.

    A reserved, untouched seat is legitimate - the people genuinely have not been chosen.
    A seat that carries a build, a date or a report is CLAIMING somebody sat in it, and an
    anonymous claim is not evidence. That is the smuggling route, so it is a hard
    validation error rather than a promote-time refusal: it cannot ride along inside a
    ledger nobody happened to re-gate.
    """
    if is_named_person(row.participant):
        return None
    filled = [
        name
        for name, value in (
            ("tested_commit", row.tested_commit),
            ("date", row.date),
            ("completion_evidence", row.completion_evidence),
            ("friction", row.friction),
            ("disposition_ref", row.disposition_ref),
        )
        if is_recorded(value)
    ]
    if row.disposition != "untriaged":
        filled.append("disposition")
    if not filled:
        return None
    return (
        f"participant {row.participant!r} is a placeholder but the seat records "
        f"{', '.join(filled)} - evidence needs a name attached to it"
    )


def _validate_feedback(ledger: Ledger, problems: list[str], resolve: CommitResolver | None) -> None:
    for row in ledger.feedback:
        where = f"feedback.tsv {row.seat}"
        if row.disposition not in DISPOSITIONS:
            problems.append(f"{where}: disposition {row.disposition!r} not in {list(DISPOSITIONS)}")
        if not _valid_date(row.date):
            problems.append(f"{where}: date {row.date!r} is not YYYY-MM-DD or '-'")
        if is_recorded(row.tested_commit):
            problem = _commit_problem("tested_commit", row.tested_commit, resolve)
            if problem:
                problems.append(f"{where}: {problem}")
        anonymous = _placeholder_seat_problem(row)
        if anonymous:
            problems.append(f"{where}: {anonymous}")

    seats = ledger.seats()
    if len(seats) < STAGE_3_MIN_SEATS:
        problems.append(
            f"feedback.tsv: stage 3 needs at least {STAGE_3_MIN_SEATS} seats, found {len(seats)}"
        )
    identifiers = [s.seat for s in seats]
    for seat_id in sorted({s for s in identifiers if identifiers.count(s) > 1}):
        problems.append(f"feedback.tsv: seat {seat_id!r} appears more than once")


def validate(ledger: Ledger, resolve: CommitResolver | None = None) -> list[str]:
    """Every structural problem in the ledger, worst-first is not needed - all of them.

    `resolve` is the git commit oracle. Omitted (the default) means shape checks only,
    which is what keeps this function pure for tests and usable in a clone where git
    cannot answer; the CLI supplies a real one.
    """
    problems = list(ledger.errors)
    _validate_stages(ledger, problems, resolve)
    _validate_blockers(ledger, problems)
    _validate_feedback(ledger, problems, resolve)
    return problems


def promote_reasons(
    ledger: Ledger,
    number: int,
    today: date,
    resolve: CommitResolver | None = None,
) -> list[str]:
    """Why stage `number` may NOT be promoted. Empty list == the gate is clear."""
    problems = validate(ledger, resolve)
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

    elapsed = _elapsed_days(row, today)
    if elapsed is None:
        reasons.append("stage has not started (no start_date)")
    elif elapsed < row.min_window_days:
        reasons.append(f"usage window not met: {elapsed} of {row.min_window_days} day(s) elapsed")

    reasons.extend(_evidence_reasons(row, resolve))

    # AC2 - independent of elapsed time, and deliberately repo-wide: a release-blocking
    # defect open anywhere stops the whole train, including the stage-5 replacement gate.
    for blocker in ledger.open_release_blockers():
        reasons.append(f"open release-blocking defect {blocker.id} (stage {blocker.stage})")

    if number == FEEDBACK_STAGE:
        reasons.extend(_stage_3_reasons(ledger, resolve))

    return reasons


# --- rollback: the mechanically checkable half ------------------------------
#
# The rollback path is documented in docs/adoption/README.md, and the honest check for
# "is it real" is walking it - a human running the commands on a real machine. That half
# stays human, and `rollback` prints exactly which half it did not do. But the half that
# is a claim about THIS repo's files is checkable, and leaving it to prose means a
# `pyproject.toml` edit could quietly falsify the coexistence story with nothing to catch it.

CLI_REPO_URL = "https://github.com/microsoft/amplifier"
TUI_SCRIPT_NAME = "amplifier-tui"
CLI_SCRIPT_NAME = "amplifier"
COMMIT_PLACEHOLDER = "<tested_commit>"
ROLLBACK_HEADING = "## Rollback path"
ADR_PATH = Path("docs") / "decisions" / "ADR-0008-console-script-name.md"
INSTALL_CONTRACT_PATH = Path("src") / "amplifier_app_tui" / "install_contract.py"

_CLI_RESTORE_RE = re.compile(r"^uv tool install (git\+\S+?)\s*$", re.MULTILINE)
_PINNED_RESTORE_RE = re.compile(
    r'^bash -o pipefail -c "curl .*? -fsSL (\S+) \| bash -s -- --ref (\S+)"\s*$',
    re.MULTILINE,
)
_APP_REPO_URL_RE = re.compile(r'^APP_REPO_URL\s*=\s*"([^"]+)"', re.MULTILINE)

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


@dataclass(frozen=True)
class Check:
    status: str
    label: str
    detail: str


def _read(path: Path) -> str:
    try:
        return path.read_text()
    except OSError:
        return ""


def _check_documented(readme: str) -> Check:
    label = "the rollback path is documented"
    if ROLLBACK_HEADING not in readme:
        return Check(FAIL, label, f"docs/adoption/README.md has no `{ROLLBACK_HEADING}` section")
    return Check(PASS, label, "docs/adoption/README.md#rollback-path")


def _check_cli_restore(readme: str) -> Check:
    label = "amplifier-app-cli restore command is well-formed"
    targets = _CLI_RESTORE_RE.findall(readme)
    if not targets:
        return Check(FAIL, label, "no `uv tool install git+...` line in the rollback section")
    if f"git+{CLI_REPO_URL}" not in targets:
        return Check(FAIL, label, f"documented target {targets} is not git+{CLI_REPO_URL}")
    return Check(PASS, label, f"uv tool install git+{CLI_REPO_URL}")


def _check_pinned_restore(readme: str, repo: Path) -> Check:
    label = "pinned-build rollback command is well-formed"
    matches = _PINNED_RESTORE_RE.findall(readme)
    if not matches:
        return Check(
            FAIL, label, "no fail-closed source installer with `--ref <commit>` documented"
        )
    url, pin = matches[0]
    canonical = _APP_REPO_URL_RE.findall(_read(repo / INSTALL_CONTRACT_PATH))
    if not canonical:
        return Check(FAIL, label, f"cannot read APP_REPO_URL from {INSTALL_CONTRACT_PATH}")
    expected_url = (
        canonical[0].replace("https://github.com/", "https://raw.githubusercontent.com/")
        + "/main/scripts/install.sh"
    )
    if url != expected_url:
        return Check(FAIL, label, f"documented {url} is not this app's installer {expected_url}")
    if pin != COMMIT_PLACEHOLDER:
        return Check(FAIL, label, f"pin placeholder is {pin!r}, expected {COMMIT_PLACEHOLDER!r}")
    if "tested_commit" not in {f.name for f in fields(Stage)}:
        return Check(FAIL, label, "the ledger has no tested_commit column to pin from")
    return Check(
        PASS,
        label,
        f"{url} --ref {COMMIT_PLACEHOLDER} pins the ledger's tested_commit column",
    )


def _check_no_console_script_collision(repo: Path) -> Check:
    label = "both executables can be installed side by side"
    try:
        data = tomllib.loads(_read(repo / "pyproject.toml"))
    except tomllib.TOMLDecodeError as exc:
        return Check(FAIL, label, f"pyproject.toml does not parse: {exc}")
    project = data.get("project")
    scripts = project.get("scripts") if isinstance(project, dict) else None
    if not isinstance(scripts, dict):
        return Check(FAIL, label, "pyproject.toml declares no [project.scripts]")
    names = sorted(str(name) for name in scripts)  # pyright: ignore[reportUnknownVariableType]
    if CLI_SCRIPT_NAME in names:
        return Check(
            FAIL,
            label,
            f"this repo declares {CLI_SCRIPT_NAME!r}, which amplifier-app-cli already owns - "
            "uv refuses the second install (ADR-0008)",
        )
    if names != [TUI_SCRIPT_NAME]:
        return Check(FAIL, label, f"expected exactly [{TUI_SCRIPT_NAME!r}], found {names}")
    return Check(
        PASS, label, f"{TUI_SCRIPT_NAME!r} only; nothing here contends for {CLI_SCRIPT_NAME!r}"
    )


def _check_adr(repo: Path) -> Check:
    label = "the coexistence decision is recorded (ADR-0008)"
    text = _read(repo / ADR_PATH)
    if not text:
        return Check(FAIL, label, f"{ADR_PATH} is missing")
    if f"Keep `{TUI_SCRIPT_NAME}`" not in text:
        return Check(FAIL, label, f"{ADR_PATH} no longer records keeping `{TUI_SCRIPT_NAME}`")
    return Check(PASS, label, f"{ADR_PATH} keeps `{TUI_SCRIPT_NAME}`, measured against uv 0.10.2")


def _uv_argvs(source: str) -> list[list[str]]:
    """Every `["uv", ...]` argv literal in a Python module.

    AST, not grep. `kernel/reset.py` has a docstring explaining that it deliberately does
    NOT port `uv tool uninstall`, and a substring scan cannot tell a sentence saying "we
    don't do this" from a subprocess call doing it. Argv literals are what actually runs.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    argvs: list[list[str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.List | ast.Tuple) or not node.elts:
            continue
        head = node.elts[0]
        if not (isinstance(head, ast.Constant) and head.value == "uv"):
            continue
        argvs.append(
            [e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        )
    return argvs


def _uv_shell_argvs(source: str) -> list[list[str]]:
    """`uv ...` invocations in a shell script, with `#` comments stripped."""
    argvs: list[list[str]] = []
    for raw in source.splitlines():
        line = raw.split("#", 1)[0].strip()
        tokens = line.split()
        if "uv" in tokens:
            argvs.append(tokens[tokens.index("uv") :])
    return argvs


def _tool_mutation_problem(argv: list[str]) -> str | None:
    """Why this `uv` invocation would disturb another installed tool, or None."""
    if "uninstall" in argv:
        return f"removes a tool: {' '.join(argv)}"
    # `amplifier` as a bare argv token is how you would name amplifier-app-cli's tool
    # install. This repo's own installs name `...amplifier-app-tui`, which does not match.
    if any(token == CLI_SCRIPT_NAME for token in argv[1:]):
        return f"targets {CLI_SCRIPT_NAME!r}: {' '.join(argv)}"
    return None


def _check_never_mutates_the_cli(repo: Path) -> Check:
    label = "nothing here installs, upgrades, or removes amplifier-app-cli"
    offenders: list[str] = []
    for directory in ("src", "scripts"):
        for path in sorted((repo / directory).rglob("*")):
            if not path.is_file() or path.suffix not in (".py", ".sh"):
                continue
            body = _read(path)
            reader = _uv_argvs if path.suffix == ".py" else _uv_shell_argvs
            for argv in reader(body):
                problem = _tool_mutation_problem(argv)
                if problem:
                    offenders.append(f"{path.relative_to(repo)} {problem}")
    if offenders:
        return Check(FAIL, label, "; ".join(offenders))
    return Check(PASS, label, "no executed `uv` argv in src/ or scripts/ touches the CLI's install")


def _requirement_name(requirement: str) -> str:
    return re.split(r"[\[<>=!~;\s]", requirement.strip(), maxsplit=1)[0].strip().lower()


def _check_no_dependency_tie(repo: Path) -> Check:
    label = "no dependency tie between the two apps"
    try:
        data = tomllib.loads(_read(repo / "pyproject.toml"))
    except tomllib.TOMLDecodeError as exc:
        return Check(FAIL, label, f"pyproject.toml does not parse: {exc}")
    project = data.get("project")
    declared = project.get("dependencies") if isinstance(project, dict) else None
    requirements = [str(r) for r in declared] if isinstance(declared, list) else []
    tied = sorted(
        {
            n
            for n in map(_requirement_name, requirements)
            if n in (CLI_SCRIPT_NAME, "amplifier-app-cli")
        }
    )
    if tied:
        return Check(
            FAIL,
            label,
            f"this app depends on {', '.join(tied)} - rolling one back would drag the other",
        )
    return Check(PASS, label, f"{len(requirements)} dependencies, none of them amplifier-app-cli")


def _check_pins_resolve(ledger: Ledger, resolve: CommitResolver | None, note: str) -> Check:
    label = "every recorded tested_commit is a real build to roll back to"
    pins = [(f"stage {s.stage}", s.tested_commit) for s in ledger.stages]
    pins += [(s.seat, s.tested_commit) for s in ledger.seats()]
    pins = [(where, sha) for where, sha in pins if is_recorded(sha)]
    if not pins:
        return Check(SKIP, label, "no tested_commit recorded yet - no stage has started")
    if resolve is None:
        return Check(SKIP, label, note or "commit resolution unavailable")
    bad = [f"{where}={sha}" for where, sha in pins if _commit_problem(where, sha, resolve)]
    if bad:
        return Check(FAIL, label, f"unresolvable: {', '.join(bad)}")
    return Check(PASS, label, f"{len(pins)} pin(s) resolve in this clone")


def rollback_checks(
    ledger: Ledger,
    repo: Path | None = None,
    resolve: CommitResolver | None = None,
    note: str = "",
) -> list[Check]:
    """The mechanical half of the rollback path, as a list of named checks."""
    root = repo or repo_root()
    readme = _read(root / "docs" / "adoption" / "README.md")
    return [
        _check_documented(readme),
        _check_cli_restore(readme),
        _check_pinned_restore(readme, root),
        _check_no_console_script_collision(root),
        _check_adr(root),
        _check_no_dependency_tie(root),
        _check_never_mutates_the_cli(root),
        _check_pins_resolve(ledger, resolve, note),
    ]


# What no file check can establish, stated so nobody mistakes a green run for a drill.
HUMAN_ONLY = (
    "run `amplifier` after `amplifier-tui` on a real machine and confirm both launch",
    "confirm ~/.amplifier/ settings and keys carry over in both directions",
    "confirm amplifier-app-cli stayed installed and usable for the WHOLE stage-4 window",
    "then cite the drill in stages.tsv entry_evidence (S4-entry) - a green run is not a drill",
)


# --- commands ---------------------------------------------------------------


def _window(row: Stage, today: date) -> str:
    elapsed = _elapsed_days(row, today)
    return f"{0 if elapsed is None else elapsed}/{row.min_window_days}d"


def _cmd_status(ledger: Ledger, today: date) -> int:
    open_blockers = ledger.open_release_blockers()
    print("stage\towner\tdecision\twindow\ttested_commit")
    for row in sorted(ledger.stages, key=lambda s: s.stage):
        print(
            f"{row.stage}\t{row.owner}\t{row.decision}\t{_window(row, today)}\t{row.tested_commit}"
        )
    unfilled = [s.seat for s in ledger.seats() if not is_named_person(s.participant)]
    print(f"stage-3 seats unfilled: {', '.join(unfilled) if unfilled else 'none'}")
    label = ", ".join(b.id for b in open_blockers) if open_blockers else "none"
    print(f"open release-blockers: {label}")
    return 0


def _cmd_check(ledger: Ledger, resolve: CommitResolver | None) -> int:
    problems = validate(ledger, resolve)
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


def _cmd_promote(ledger: Ledger, argument: str, today: date, resolve: CommitResolver | None) -> int:
    if not argument.isdigit():
        print(f"usage: adoption_gate.py promote <stage>; got {argument!r}", file=sys.stderr)
        return 2
    number = int(argument)
    reasons = promote_reasons(ledger, number, today, resolve)
    if not reasons:
        print(f"PROMOTE stage {number}: gate clear")
        return 0
    print(f"BLOCKED stage {number}")
    for reason in reasons:
        print(f"  - {reason}")
    return 1


def _cmd_rollback(ledger: Ledger, resolve: CommitResolver | None, note: str) -> int:
    checks = rollback_checks(ledger, repo_root(), resolve, note)
    for check in checks:
        print(f"{check.status} {check.label}: {check.detail}")
    print("\nNOT machine-checked - a human still has to:")
    for line in HUMAN_ONLY:
        print(f"  - {line}")
    failed = [c for c in checks if c.status == FAIL]
    if failed:
        print(f"\nROLLBACK MECHANICS FAILED: {len(failed)} check(s)")
        return 1
    print("\nrollback mechanics OK")
    return 0


def main(
    argv: list[str],
    *,
    resolver_factory: Callable[[], tuple[CommitResolver | None, str]] = commit_resolver,
) -> int:
    """CLI entry point. `resolver_factory` defaults to the real `commit_resolver` (this
    repo, ambient git); tests inject a fake so their outcome does not depend on whatever
    the ambient clone (shallow or full) can answer - see tests/test_adoption_gate.py.
    """
    args = list(argv)
    today = date.today()
    directory: Path | None = None
    use_git = True

    if "--no-git" in args:
        use_git = False
        args = [a for a in args if a != "--no-git"]
    while len(args) >= 2 and args[-2] in ("--today", "--dir"):
        flag, value = args[-2], args[-1]
        args = args[:-2]
        if flag == "--today":
            today = date.fromisoformat(value)
        else:
            directory = Path(value)

    command = args[0] if args else "status"
    ledger = load(directory)

    resolve: CommitResolver | None = None
    note = "commit resolution disabled (--no-git); shape checks still apply"
    if use_git:
        resolve, note = resolver_factory()
        if resolve is None:
            print(
                f"note: could not verify commits against real git history ({note})",
                file=sys.stderr,
            )

    if command == "check":
        return _cmd_check(ledger, resolve)
    if command == "status":
        return _cmd_status(ledger, today)
    if command == "rollback":
        return _cmd_rollback(ledger, resolve, note)
    if command == "promote" and len(args) == 2:
        return _cmd_promote(ledger, args[1], today, resolve)

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
