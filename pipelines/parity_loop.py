#!/usr/bin/env python3
"""Owner-gated parity loop — read-only re-audit pass record + per-gap owner gate.

Companion to `ledger.py`, same contract: stdlib only, never raises, TSV rows,
and a LAST printed line a deterministic tool node can route on.

Two artifacts, both greppable, both rerunnable against a later release (AC5):

  parity-passes.tsv — one row per READ-ONLY re-audit pass
      <pass>\t<date>\t<commit>\t<outcome>\t<gaps_found>\t<gap_ids>\t<note>
      outcome ∈ {clean, gaps, owner-ended}

  parity-gates.tsv — one row per discovered gap: the product owner's decision
      <gap_id>\t<slug>\t<disposition>\t<owner>\t<date>\t<note>
      disposition ∈ {pending, accepted, rejected, deferred, already-covered}

THE TWO COUNTERS IN THIS PIPELINE ARE DIFFERENT COUNTERS:

  * FIX RETRIES (pipelines/README.md, gene-transfer.dot's RetryGate) bound how
    many times ONE gap's fix may fail to converge — 3 attempts, then the row
    goes `acknowledged` in ledger.tsv and a human takes it. Scope: one gap.
    Code-changing. Owned by the transfer pipeline.
  * CONSECUTIVE CLEAN PASSES (this file, `streak`) bound the RUN — three
    re-audits IN A ROW that discover no new relevant gap end it. Scope: the
    whole loop. Read-only. Owned by this tool.

  A fix retry never advances or resets the clean-pass streak; a clean pass
  never refunds a fix-retry budget. This tool never reads or writes ledger.tsv
  and never runs a code-changing step.

The streak is derived from the gaps actually recorded on a row, not from the
stored `outcome` word — a clean streak cannot be hand-edited into existence
without also deleting the gap ids that contradict it.

Commands:
  record-pass <commit> [<gap_ids>] [<date>] [<note>...]
                        append a read-only re-audit pass. gap_ids is `-` or a
                        comma list of `<id>` / `<id>:<slug>`. Every discovered
                        gap is auto-registered `pending` in the gate file, so
                        nothing reaches a code-changing step undecided.
  end-run <owner> [<reason>...]
                        the owner ends the run (AC4's second exit condition)
  streak                print `clean_streak=<n>/3`
  should-continue       run exit condition; last line CONTINUE or DONE
  gate <gap_id>         owner gate for one gap; last line PROCEED (exit 0) or
                        BLOCKED (exit 1) — the only route to code-changing work
  decide <gap_id> <disposition> [<owner>] [<note>...]
                        record the product owner's disposition for one gap
  passes                print the pass record
  gaps [<disposition>]  print gate rows, optionally filtered
  stats                 counts for both artifacts
"""

from __future__ import annotations

import os
import sys
from datetime import date as _date
from pathlib import Path

# Both artifacts are overridable so one tool can serve several audits (and so
# tests can point at a scratch dir), mirroring ledger.py's LEDGER_FILE.
PASSES_ENV = "PARITY_PASSES_FILE"
GATES_ENV = "PARITY_GATES_FILE"

#: How many consecutive clean re-audit passes end the run (AC4).
REQUIRED_CLEAN_PASSES = 3

OUTCOME_CLEAN = "clean"
OUTCOME_GAPS = "gaps"
OUTCOME_OWNER_ENDED = "owner-ended"

#: Owner dispositions. Only `accepted` opens a code-changing route (AC3);
#: a freshly-discovered gap is `pending`, which blocks.
DISPOSITIONS = ("pending", "accepted", "rejected", "deferred", "already-covered")
PROCEED_DISPOSITIONS = ("accepted",)
UNDECIDED = "undecided"

PASS_WIDTH = 7
GATE_WIDTH = 6

PASSES_HEADER = "# pass\tdate\tcommit\toutcome\tgaps_found\tgap_ids\tnote\n"
GATES_HEADER = "# gap_id\tslug\tdisposition\towner\tdate\tnote\n"


# ---------------------------------------------------------------------- files


def passes_file() -> Path:
    override = os.environ.get(PASSES_ENV)
    return Path(override) if override else Path(__file__).with_name("parity-passes.tsv")


def gates_file() -> Path:
    override = os.environ.get(GATES_ENV)
    return Path(override) if override else Path(__file__).with_name("parity-gates.tsv")


def _read(path: Path, width: int) -> list[list[str]]:
    """Rows of exactly `width` fields; blanks, comments and malformed rows skipped."""
    if not path.exists():
        return []
    rows: list[list[str]] = []
    for line in path.read_text().splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) == width:
            rows.append([p.strip() for p in parts])
    return rows


def _write(path: Path, rows: list[list[str]], header: str) -> None:
    path.write_text(header + "".join("\t".join(r) + "\n" for r in rows))


def _field(value: str, default: str = "-") -> str:
    """Keep the TSV parseable: no tabs, no newlines, never empty."""
    return " ".join(value.replace("\t", " ").split()) or default


def _int(value: str) -> int:
    try:
        return int(value)
    except ValueError:
        return 0


# ---------------------------------------------------------------- pass record


def read_passes() -> list[list[str]]:
    return _read(passes_file(), PASS_WIDTH)


def parse_gap_ids(raw: str) -> list[tuple[str, str]]:
    """`101,102:notify-cli` -> [('101', '-'), ('102', 'notify-cli')]."""
    out: list[tuple[str, str]] = []
    if raw.strip() in ("", "-"):
        return out
    for chunk in raw.split(","):
        gap_id, _, slug = chunk.strip().partition(":")
        gap_id = _field(gap_id, "")
        if gap_id:
            out.append((gap_id, _field(slug)))
    return out


def outcome_of(row: list[str]) -> str:
    """A pass's outcome, derived from its evidence rather than its stored word."""
    if row[3] == OUTCOME_OWNER_ENDED:
        return OUTCOME_OWNER_ENDED
    discovered = _int(row[4]) or len(parse_gap_ids(row[5]))
    return OUTCOME_GAPS if discovered else OUTCOME_CLEAN


def clean_streak(rows: list[list[str]] | None = None) -> int:
    """Consecutive clean read-only re-audit passes at the tail of the record.

    Deliberately NOT the fix-retry counter: it counts whole-surface re-audits,
    it resets only when a pass discovers a gap, and no code-changing step can
    touch it.
    """
    rows = read_passes() if rows is None else rows
    streak = 0
    for row in reversed(rows):
        if outcome_of(row) != OUTCOME_CLEAN:
            break
        streak += 1
    return streak


def run_ended_by(rows: list[list[str]] | None = None) -> str:
    """The note from the owner's end-run row, or '' while the run is open."""
    rows = read_passes() if rows is None else rows
    for row in reversed(rows):
        if outcome_of(row) == OUTCOME_OWNER_ENDED:
            return row[6]
    return ""


def _next_pass_number(rows: list[list[str]]) -> int:
    return max((_int(r[0]) for r in rows), default=0) + 1


def record_pass(commit: str, raw_ids: str = "-", when: str = "", note: str = "") -> list[str]:
    """Append one read-only re-audit pass; register its gaps as `pending`."""
    rows = read_passes()
    found = parse_gap_ids(raw_ids)
    row = [
        str(_next_pass_number(rows)),
        _field(when or _date.today().isoformat()),
        _field(commit),
        OUTCOME_GAPS if found else OUTCOME_CLEAN,
        str(len(found)),
        ",".join(gap_id for gap_id, _ in found) or "-",
        _field(note),
    ]
    rows.append(row)
    _write(passes_file(), rows, PASSES_HEADER)
    for gap_id, slug in found:
        register_gap(gap_id, slug)
    return row


def end_run(owner: str, reason: str = "") -> list[str]:
    """Record the owner's decision to end the run (the other AC4 exit)."""
    rows = read_passes()
    row = [
        str(_next_pass_number(rows)),
        _date.today().isoformat(),
        "-",
        OUTCOME_OWNER_ENDED,
        "0",
        "-",
        _field(f"owner={_field(owner)} {reason}"),
    ]
    rows.append(row)
    _write(passes_file(), rows, PASSES_HEADER)
    return row


# ----------------------------------------------------------------- owner gate


def read_gates() -> list[list[str]]:
    return _read(gates_file(), GATE_WIDTH)


def disposition_of(gap_id: str, rows: list[list[str]] | None = None) -> str:
    rows = read_gates() if rows is None else rows
    for row in rows:
        if row[0] == gap_id:
            return row[2]
    return UNDECIDED


def may_proceed(gap_id: str) -> bool:
    """AC3: no code-changing step runs without an explicit owner acceptance."""
    return disposition_of(gap_id) in PROCEED_DISPOSITIONS


def register_gap(gap_id: str, slug: str = "-") -> bool:
    """Add a newly-discovered gap as `pending`. Idempotent; never overwrites a decision."""
    rows = read_gates()
    for row in rows:
        if row[0] == gap_id:
            if row[1] == "-" and slug != "-":
                row[1] = _field(slug)
                _write(gates_file(), rows, GATES_HEADER)
            return False
    rows.append(
        [
            _field(gap_id),
            _field(slug),
            "pending",
            "-",
            _date.today().isoformat(),
            "awaiting owner disposition",
        ]
    )
    _write(gates_file(), rows, GATES_HEADER)
    return True


def decide(gap_id: str, disposition: str, owner: str = "-", note: str = "") -> list[str]:
    """Record the product owner's disposition for one gap (upsert)."""
    rows = read_gates()
    today = _date.today().isoformat()
    for row in rows:
        if row[0] == gap_id:
            row[2], row[3], row[4], row[5] = disposition, _field(owner), today, _field(note)
            _write(gates_file(), rows, GATES_HEADER)
            return row
    row = [_field(gap_id), "-", disposition, _field(owner), today, _field(note)]
    rows.append(row)
    _write(gates_file(), rows, GATES_HEADER)
    return row


# ------------------------------------------------------------------------ cli


def _counts(values: list[str]) -> str:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return " ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "empty"


USAGE = (
    "commands: record-pass <commit> [<gap_ids>] [<date>] [<note>...] | "
    "end-run <owner> [<reason>...] | streak | should-continue | gate <gap_id> | "
    "decide <gap_id> <disposition> [<owner>] [<note>...] | passes | gaps [<disposition>] | stats"
)


def _dispatch(argv: list[str]) -> int:
    cmd = argv[0] if argv else "stats"
    args = argv[1:]

    if cmd == "record-pass" and args:
        row = record_pass(
            commit=args[0],
            raw_ids=args[1] if len(args) > 1 else "-",
            when=args[2] if len(args) > 2 else "",
            note=" ".join(args[3:]),
        )
        print(
            f"pass={row[0]} outcome={outcome_of(row)} gaps={row[4]} "
            f"clean_streak={clean_streak()}/{REQUIRED_CLEAN_PASSES}"
        )
        return 0

    if cmd == "end-run" and args:
        row = end_run(args[0], " ".join(args[1:]))
        print(f"pass={row[0]} outcome={OUTCOME_OWNER_ENDED} {row[6]}")
        return 0

    if cmd == "streak":
        print(f"clean_streak={clean_streak()}/{REQUIRED_CLEAN_PASSES}")
        return 0

    if cmd == "should-continue":
        ended = run_ended_by()
        if ended:
            print(f"DONE reason=owner-ended {ended}")
            return 0
        streak = clean_streak()
        if streak >= REQUIRED_CLEAN_PASSES:
            print(f"DONE reason=three-consecutive-clean-passes clean_streak={streak}")
            return 0
        print(f"CONTINUE clean_streak={streak}/{REQUIRED_CLEAN_PASSES}")
        return 0

    if cmd == "gate" and args:
        gap_id = args[0]
        disposition = disposition_of(gap_id)
        if disposition in PROCEED_DISPOSITIONS:
            print(f"PROCEED gap={gap_id} disposition={disposition}")
            return 0
        print(f"BLOCKED gap={gap_id} disposition={disposition}")
        return 1

    if cmd == "decide" and len(args) >= 2:
        gap_id, disposition = args[0], args[1]
        if disposition not in DISPOSITIONS:
            print(
                f"unknown disposition: {disposition} (expected one of {', '.join(DISPOSITIONS)})",
                file=sys.stderr,
            )
            return 1
        row = decide(
            gap_id,
            disposition,
            owner=args[2] if len(args) > 2 else "-",
            note=" ".join(args[3:]),
        )
        print(f"{row[0]} -> {row[2]} (owner={row[3]})")
        return 0

    if cmd == "passes":
        for row in read_passes():
            print("\t".join(row))
        return 0

    if cmd == "gaps":
        wanted = args[0] if args else ""
        for row in read_gates():
            if not wanted or row[2] == wanted:
                print("\t".join(row))
        return 0

    if cmd == "stats":
        rows = read_passes()
        run_state = "ended" if run_ended_by(rows) else "open"
        print(
            f"passes={len(rows)} clean_streak={clean_streak(rows)}/{REQUIRED_CLEAN_PASSES} "
            f"run={run_state}"
        )
        print(f"gaps: {_counts([r[2] for r in read_gates()])}")
        return 0

    print(f"unknown command: {cmd}", file=sys.stderr)
    print(USAGE, file=sys.stderr)
    return 1


def main(argv: list[str]) -> int:
    """Never raises — a tool node must always yield a routable exit code."""
    try:
        return _dispatch(argv)
    except Exception as exc:  # noqa: BLE001 — tool-node contract: never raise
        print(f"parity_loop error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
