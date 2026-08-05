#!/usr/bin/env python3
"""Gene-transfer ledger — one row per amplifier-app-cli capability being ported.

Modeled on the attractor `semport` fixture's ledger contract. Rows are TSV:

    <issue>\t<slug>\t<state>

state ∈ {new, implemented, acknowledged}
  new          — not yet ported
  implemented  — ported, validated (unit + forge), PR opened
  acknowledged — auto-port could not converge; handed back to a human

Commands (stdlib only, never raises for the pipeline's tool nodes):
  earliest              print "<issue> <slug>" of the first `new` row, or NONE
  earliest-transferable same, but fail closed unless a parity-origin row still
                        has an effective `accepted` owner disposition
  gate-transfer <issue> recheck one selected row at the code-changing boundary
  update <issue> <st>   set a row's state
  stats                 counts by state
  sort                  rewrite file: new first, then implemented, then acknowledged
  add <issue> <slug>    append a parity-origin row (idempotent on issue)
  add-non-parity <issue> <slug>
                        explicit escape hatch for a separately-authorized backlog
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

# Ledger file is overridable so one tool can serve several pipelines
# (e.g. the app-cli backlog ledger and the opencode-transfer ledger).
# Back-compatible: unset LEDGER_FILE keeps the original ledger.tsv sibling.
_LEDGER_ENV = os.environ.get("LEDGER_FILE")
LEDGER = Path(_LEDGER_ENV) if _LEDGER_ENV else Path(__file__).with_name("ledger.tsv")
_SOURCES_ENV = os.environ.get("LEDGER_SOURCES_FILE")
SOURCES = Path(_SOURCES_ENV) if _SOURCES_ENV else LEDGER.with_name(f"{LEDGER.stem}-sources.tsv")
# Custom ledgers (for example opencode-ledger.tsv) retain their three-column
# contract unless a caller deliberately supplies a companion source file.
TRACK_SOURCES = _LEDGER_ENV is None or _SOURCES_ENV is not None

SOURCE_PARITY = "parity"
SOURCE_NON_PARITY = "non-parity"
SOURCE_UNKNOWN = "unknown"
SOURCE_VALUES = (SOURCE_PARITY, SOURCE_NON_PARITY)
ORDER = {"new": 0, "implemented": 1, "acknowledged": 2}


def _rows() -> list[list[str]]:
    if not LEDGER.exists():
        return []
    out = []
    for line in LEDGER.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) == 3:
            out.append(parts)
    return out


def _write(rows: list[list[str]]) -> None:
    LEDGER.write_text("".join("\t".join(r) + "\n" for r in rows))


def _sources() -> dict[str, str]:
    """Issue -> provenance, from a companion file that preserves ledger shape."""
    if not TRACK_SOURCES or not SOURCES.exists():
        return {}
    out: dict[str, str] = {}
    for line in SOURCES.read_text().splitlines():
        parts = line.strip().split("\t")
        if len(parts) == 2 and parts[1] in SOURCE_VALUES:
            out[parts[0]] = parts[1]
    return out


def _record_source(issue: str, source: str) -> None:
    if not TRACK_SOURCES:
        return
    sources = _sources()
    sources[issue] = source
    SOURCES.write_text("".join(f"{key}\t{value}\n" for key, value in sources.items()))


def _load_parity_loop():
    """Load the one authoritative owner-gate implementation from its sibling."""
    path = Path(__file__).with_name("parity_loop.py")
    spec = importlib.util.spec_from_file_location("_ledger_parity_loop", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load parity owner gate")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _effective_disposition(issue: str) -> str:
    """Read the parity gate fail-closed, including its attribution checks."""
    try:
        return str(_load_parity_loop().disposition_of(issue))
    except Exception:  # noqa: BLE001 — deterministic pipeline gate must not raise
        return "unknown"


def transfer_decision(issue: str, rows: list[list[str]] | None = None) -> tuple[bool, str, str]:
    """Return (may_transfer, source, effective_disposition) for one queue row.

    A clearly-marked non-parity row is the deliberate backlog escape hatch.
    Every other row is rechecked against the owner gate.  This makes a direct
    ledger edit fail closed while still allowing a legacy row whose real owner
    acceptance already exists in the gate artifact.
    """
    rows = _rows() if rows is None else rows
    if not any(row[0] == issue and row[2] == "new" for row in rows):
        return False, "missing", "undecided"
    disposition = _effective_disposition(issue)
    source = _sources().get(issue, SOURCE_UNKNOWN)
    if source == SOURCE_NON_PARITY and disposition == "undecided":
        return True, source, "not-applicable"
    return disposition == "accepted", source, disposition


def earliest_transferable(
    rows: list[list[str]] | None = None,
) -> tuple[list[str] | None, tuple[bool, str, str] | None]:
    """First new row plus its boundary decision; never skip a blocked head row."""
    rows = _rows() if rows is None else rows
    for row in rows:
        if row[2] == "new":
            return row, transfer_decision(row[0], rows)
    return None, None


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else "stats"
    rows = _rows()

    if cmd == "earliest":
        for issue, slug, state in rows:
            if state == "new":
                print(f"{issue} {slug}")
                return 0
        print("NONE")
        return 0

    if cmd == "earliest-transferable":
        row, decision = earliest_transferable(rows)
        if row is None or decision is None:
            print("NONE")
            return 0
        allowed, source, disposition = decision
        if allowed:
            print(f"{row[0]} {row[1]}")
            return 0
        print(f"BLOCKED issue={row[0]} source={source} disposition={disposition}")
        return 1

    if cmd == "gate-transfer" and len(argv) == 2:
        issue = argv[1]
        allowed, source, disposition = transfer_decision(issue, rows)
        if allowed:
            print(f"PROCEED issue={issue} source={source} disposition={disposition}")
            return 0
        print(f"BLOCKED issue={issue} source={source} disposition={disposition}")
        return 1

    if cmd == "update" and len(argv) == 3:
        issue, state = argv[1], argv[2]
        for r in rows:
            if r[0] == issue:
                r[2] = state
        _write(rows)
        print(f"{issue} -> {state}")
        return 0

    if cmd in ("add", "add-non-parity") and len(argv) == 3:
        issue, slug = argv[1], argv[2]
        if not any(r[0] == issue for r in rows):
            rows.append([issue, slug, "new"])
            _write(rows)
        source = SOURCE_PARITY if cmd == "add" else SOURCE_NON_PARITY
        _record_source(issue, source)
        print(f"added {issue} source={source}")
        return 0

    if cmd == "sort":
        rows.sort(
            key=lambda r: (
                ORDER.get(r[2], 9),
                int(r[0]) if r[0].isdigit() else 0,
                r[0],
            )
        )
        _write(rows)
        print("sorted")
        return 0

    if cmd == "stats":
        counts: dict[str, int] = {}
        for _, _, state in rows:
            counts[state] = counts.get(state, 0) + 1
        print(" ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "empty")
        return 0

    print(f"unknown command: {cmd}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
