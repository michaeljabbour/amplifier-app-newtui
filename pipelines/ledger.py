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
  update <issue> <st>   set a row's state
  stats                 counts by state
  sort                  rewrite file: new first, then implemented, then acknowledged
  add <issue> <slug>    append a new row (idempotent on issue)
"""

from __future__ import annotations

import sys
from pathlib import Path

LEDGER = Path(__file__).with_name("ledger.tsv")
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

    if cmd == "update" and len(argv) == 3:
        issue, state = argv[1], argv[2]
        for r in rows:
            if r[0] == issue:
                r[2] = state
        _write(rows)
        print(f"{issue} -> {state}")
        return 0

    if cmd == "add" and len(argv) == 3:
        issue, slug = argv[1], argv[2]
        if not any(r[0] == issue for r in rows):
            rows.append([issue, slug, "new"])
            _write(rows)
        print(f"added {issue}")
        return 0

    if cmd == "sort":
        rows.sort(key=lambda r: (ORDER.get(r[2], 9), int(r[0])))
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
