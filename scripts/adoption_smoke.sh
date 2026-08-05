#!/usr/bin/env bash
# Compatibility smoke — the single named entry point run at EVERY adoption promotion
# gate (governance item B5; see docs/adoption/README.md).
#
# This deliberately adds NO new test suite. It composes the gates this repo already
# has, in the order CI runs them, and appends the adoption ledger's own validation:
#
#   1. ruff check .                    lint          (docs/DEVELOPMENT.md "Daily commands")
#   2. ruff format --check .           formatting
#   3. pyright src/                    types
#   4. pytest -q                       the offline unit/flow/golden suite
#   5. forge capability tier           required real-PTY boot of the shipped binary via
#                                      scripts/forge_capability.sh --require
#   6. adoption_gate.py check          the stage ledger parses and every row is legal
#   7. adoption_gate.py rollback       the MECHANICAL half of the documented rollback
#                                      path still holds (command shapes, the pinned
#                                      commit, side-by-side installability). It prints
#                                      the half only a human can walk.
#
# Steps 1-4 are the same gates a PR must pass, so a red smoke is never a smoke-only
# problem. Step 5 is the acceptance oracle for "does the thing a daily driver actually
# launches still boot" (docs/DEVELOPMENT.md "Forge capability tier").
#
# Usage:
#   scripts/adoption_smoke.sh                   # full smoke
#
# Record the exit status and the tested commit in docs/adoption/stages.tsv. A red smoke
# is entry/exit evidence of failure, not a formality to re-run until green.
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ "$#" -ne 0 ]]; then
  echo "adoption smoke accepts no options; the required Forge tier cannot be bypassed" >&2
  exit 2
fi

COMMIT="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
echo "adoption smoke @ ${COMMIT}"
echo

step() { echo "--- $* ---"; }

step "ruff check"
uv run ruff check .

step "ruff format --check"
uv run ruff format --check .

step "pyright src/"
uv run pyright src/

step "pytest -q"
uv run pytest -q

step "forge capability tier (required demo PTY; real provider remains opt-in)"
scripts/forge_capability.sh --require

step "adoption ledger check"
python3 scripts/adoption_gate.py check

step "adoption rollback mechanics"
python3 scripts/adoption_gate.py rollback

echo
echo "adoption smoke PASS @ ${COMMIT}"
echo "record this commit in docs/adoption/stages.tsv as the tested_commit for the stage."
