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
                        record the product owner's disposition for one gap.
                        A real disposition needs a REAL owner: `TBD`, `owner`,
                        `team`, `?`, `unknown`, blank and the rest of
                        PLACEHOLDER_OWNERS are refused, unwritten.
  validate              audit the gate file for decisions nobody signed;
                        last line VALID (exit 0) or INVALID (exit 1)
  awaiting              print gaps still owed a real, attributed decision
  passes                print the pass record
  gaps [<disposition>]  print gate rows, optionally filtered
  stats                 counts for both artifacts

A DECISION NOBODY SIGNED IS NOT A DECISION. An owner field naming a
placeholder (see PLACEHOLDER_OWNERS — one list, one home) cannot record a
disposition, and a hand-edited row that claims one reads back `unattributed`
and BLOCKS at the gate exactly like `pending` does.
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

#: Dispositions that assert a human actually ruled on the gap. `pending` is the
#: ABSENCE of a ruling, so it is the one disposition allowed to carry no owner.
DECIDED_DISPOSITIONS = tuple(d for d in DISPOSITIONS if d != "pending")

#: What `disposition_of` reports for a row that claims a decision but attributes
#: it to a placeholder owner. A decision nobody signed is not a decision, so it
#: blocks exactly like `pending` does.
UNATTRIBUTED = "unattributed"

#: THE placeholder-owner list — one home, deliberately. `decide`, `disposition_of`
#: and `validate` all consult this and nothing else, so a hand-edited gate file
#: cannot smuggle an unattributed decision past a check that knows a shorter list.
#: Compared against the owner NORMALIZED by `_normalize_owner` (see there), so an
#: empty / whitespace-only / bracketed / @-prefixed placeholder is caught too.
PLACEHOLDER_OWNERS = frozenset(
    {
        "",  # empty or whitespace-only
        "-",  # the TSV's own "no value" filler
        "?",
        "??",
        "???",
        "n/a",
        "na",
        "none",
        "null",
        "nil",
        "tbd",
        "tba",
        "todo",
        "pending",
        "unknown",
        "unassigned",
        "nobody",
        "anyone",
        "someone",
        "somebody",
        "placeholder",
        "example",
        "sample",
        "test",
        "xxx",
        "yyy",
        "zzz",
        "foo",
        "bar",
        "baz",
        "me",
        "you",
        "us",
        "self",
        "agent",
        "ai",
        "bot",
        "assistant",
        # Role names, not people. A role cannot be held accountable for a call:
        # "the team accepted it" names no one who can be asked why.
        "owner",
        "owners",
        "product owner",
        "product-owner",
        "productowner",
        "po",
        "team",
        "the team",
        "teams",
        "maintainer",
        "maintainers",
        "reviewer",
        "reviewers",
        "approver",
        "approvers",
        "lead",
        "leads",
        "tech lead",
        "dev",
        "devs",
        "developer",
        "developers",
        "eng",
        "engineer",
        "engineering",
        "admin",
        "user",
        "human",
        "author",
        "someone else",
    }
)

PASS_WIDTH = 7
GATE_WIDTH = 6

PASSES_HEADER = "# pass\tdate\tcommit\toutcome\tgaps_found\tgap_ids\tnote\n"
GATES_HEADER = "# gap_id\tslug\tdisposition\towner\tdate\tnote\n"

# New owner-ended rows use an explicit separator so a human's full name is not
# confused with the beginning of the reason.  `_end_run_owner` still accepts
# the original `owner=<single-token> <reason>` rows already in an audit record.
END_OWNER_PREFIX = "owner="
END_OWNER_SEPARATOR = " | "


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


# -------------------------------------------------------------- owner identity


def _normalize_owner(owner: str) -> str:
    """Fold an owner field into the form `PLACEHOLDER_OWNERS` is written in.

    Collapses whitespace, lowercases, drops a leading `@`, and strips wrapping
    punctuation — so `"  <TBD>  "`, `"@TBD"` and `"tbd."` all normalize to `tbd`
    and are caught by the one enumerated list.
    """
    text = " ".join(owner.replace("\t", " ").split()).lower()
    text = text.strip("\"'`<>[](){}*_~!?.,;:")
    return text.lstrip("@").strip()


def is_placeholder_owner(owner: str) -> bool:
    """True when the owner field names nobody a human could actually go ask.

    Two rules, both deliberately blunt:

    1. its normalized form is in `PLACEHOLDER_OWNERS` (empty, whitespace, `-`,
       `?`, `TBD`, `unknown`, `owner`, `team`, and the rest of that one list); or
    2. it carries fewer than two letters — punctuation, digits and lone initials
       identify no one either.

    A decision attributed to such an owner is not a decision: `decide` refuses to
    write it and `disposition_of` refuses to read it back as one.
    """
    normalized = _normalize_owner(owner)
    if normalized in PLACEHOLDER_OWNERS:
        return True
    return sum(1 for ch in normalized if ch.isalpha()) < 2


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
    """The note from a *real owner's* end-run row, or '' while open.

    Owner identity is enforced at read time as well as write time.  A hand-edited
    `owner-ended` row attributed to `TBD`, `team`, or another placeholder cannot
    stop the loop.
    """
    rows = read_passes() if rows is None else rows
    for row in reversed(rows):
        if outcome_of(row) == OUTCOME_OWNER_ENDED and not is_placeholder_owner(
            _end_run_owner(row[6])
        ):
            return row[6]
    return ""


def _end_run_owner(note: str) -> str:
    """Extract the attributed owner from an owner-ended note.

    Current rows are `owner=<full name> | <reason>`.  For backward compatibility,
    the original `owner=<single-token> <reason>` form reads the first token as the
    owner.  A malformed note returns an empty owner and therefore fails closed as
    a placeholder.
    """
    if not note.startswith(END_OWNER_PREFIX):
        return ""
    payload = note[len(END_OWNER_PREFIX) :]
    owner, separator, _reason = payload.partition(END_OWNER_SEPARATOR)
    if separator:
        return owner.strip()
    return payload.split(maxsplit=1)[0] if payload.strip() else ""


def unattributed_end_rows(rows: list[list[str]] | None = None) -> list[list[str]]:
    """Owner-ended rows that no identifiable human signed."""
    rows = read_passes() if rows is None else rows
    return [
        row
        for row in rows
        if outcome_of(row) == OUTCOME_OWNER_ENDED and is_placeholder_owner(_end_run_owner(row[6]))
    ]


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


def end_run(owner: str, reason: str = "") -> list[str] | None:
    """Record a real owner's decision to end the run (the other AC4 exit).

    Returns ``None`` without writing when the supplied owner is a placeholder.
    """
    if is_placeholder_owner(owner):
        return None
    rows = read_passes()
    row = [
        str(_next_pass_number(rows)),
        _date.today().isoformat(),
        "-",
        OUTCOME_OWNER_ENDED,
        "0",
        "-",
        _field(f"{END_OWNER_PREFIX}{_field(owner)}{END_OWNER_SEPARATOR}{_field(reason)}"),
    ]
    rows.append(row)
    _write(passes_file(), rows, PASSES_HEADER)
    return row


# ----------------------------------------------------------------- owner gate


def read_gates() -> list[list[str]]:
    return _read(gates_file(), GATE_WIDTH)


def disposition_of(gap_id: str, rows: list[list[str]] | None = None) -> str:
    """The disposition that COUNTS for `gap_id` — not merely the word stored.

    Enforced at READ time as well as write time, so a hand-edited gate file that
    claims `accepted` against owner `TBD` reads back `unattributed` and blocks
    just like `pending` does.
    """
    rows = read_gates() if rows is None else rows
    for row in rows:
        if row[0] == gap_id:
            if row[2] in DECIDED_DISPOSITIONS and is_placeholder_owner(row[3]):
                return UNATTRIBUTED
            return row[2]
    return UNDECIDED


def stored_disposition_of(gap_id: str, rows: list[list[str]] | None = None) -> str:
    """The raw word on the row, placeholder owner or not (for reporting only)."""
    rows = read_gates() if rows is None else rows
    for row in rows:
        if row[0] == gap_id:
            return row[2]
    return UNDECIDED


def unattributed_rows(rows: list[list[str]] | None = None) -> list[list[str]]:
    """Gate rows claiming a decision that no identifiable human signed."""
    rows = read_gates() if rows is None else rows
    return [r for r in rows if r[2] in DECIDED_DISPOSITIONS and is_placeholder_owner(r[3])]


def awaiting_rows(rows: list[list[str]] | None = None) -> list[list[str]]:
    """Gate rows still waiting on a real, attributed product-owner decision."""
    rows = read_gates() if rows is None else rows
    return [r for r in rows if disposition_of(r[0], rows) not in DECIDED_DISPOSITIONS]


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


def decide(gap_id: str, disposition: str, owner: str = "-", note: str = "") -> list[str] | None:
    """Record the product owner's disposition for one gap (upsert).

    Returns None WITHOUT writing anything when a decided disposition is
    attributed to a placeholder owner — an unsigned ruling is not a ruling, and
    silently storing one would let the gate be opened by nobody.
    """
    if disposition in DECIDED_DISPOSITIONS and is_placeholder_owner(owner):
        return None
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
    "decide <gap_id> <disposition> [<owner>] [<note>...] | validate | awaiting | "
    "passes | gaps [<disposition>] | stats"
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
        if row is None:
            print(
                f"placeholder owner refused: {args[0]!r} names nobody who can end "
                "the parity run; nothing was written.",
                file=sys.stderr,
            )
            return 1
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
        owner = args[2] if len(args) > 2 else "-"
        row = decide(gap_id, disposition, owner=owner, note=" ".join(args[3:]))
        if row is None:
            print(
                f"placeholder owner refused: {owner!r} names nobody who can be asked why. "
                f"A `{disposition}` disposition needs a real person; nothing was written.",
                file=sys.stderr,
            )
            return 1
        print(f"{row[0]} -> {row[2]} (owner={row[3]})")
        return 0

    if cmd == "validate":
        rows = read_gates()
        bad_gates = unattributed_rows(rows)
        bad_ends = unattributed_end_rows()
        for row in bad_gates:
            print(f"{row[0]}\t{row[2]}\towner={row[3]!r}\tplaceholder owner — not a decision")
        for row in bad_ends:
            print(
                f"pass={row[0]}\towner-ended\towner={_end_run_owner(row[6])!r}"
                "\tplaceholder owner — run remains open"
            )
        if bad_gates or bad_ends:
            print(
                f"INVALID gates={len(rows)} unattributed={len(bad_gates)} "
                f"owner_ends_unattributed={len(bad_ends)}"
            )
            return 1
        print(f"VALID gates={len(rows)} unattributed=0")
        return 0

    if cmd == "awaiting":
        rows = read_gates()
        waiting = awaiting_rows(rows)
        for row in waiting:
            print("\t".join([*row, f"effective={disposition_of(row[0], rows)}"]))
        print(f"awaiting={len(waiting)}/{len(rows)}")
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
