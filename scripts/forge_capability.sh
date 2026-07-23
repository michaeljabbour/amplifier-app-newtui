#!/usr/bin/env bash
# Run the forge-driven capability tier (issue #49).
#
# The tier drives the shipped `amplifier-newtui` binary through a real PTY via
# the amplifier-skill-forge daemon. It is opt-in and excluded from the default
# gate (`addopts = -m "not forge"`), so this wrapper re-selects it with
# `-m forge` after a `forge doctor` health check.
#
# Demo lane runs always; the real lane skips unless provider credentials are
# configured AND AMPLIFIER_FORGE_REAL=1 is set (it drives a real, paid session).
set -euo pipefail

cd "$(dirname "$0")/.."

# Resolve the forge helper: $FORGE, then the known skill install dirs.
FORGE="${FORGE:-}"
if [[ -z "$FORGE" ]]; then
  for candidate in \
    "$HOME/.claude/skills/amplifier-skill-forge/tools/forge.py" \
    "$HOME/.amplifier/skills/amplifier-skill-forge/tools/forge.py"; do
    if [[ -f "$candidate" ]]; then FORGE="$candidate"; break; fi
  done
fi

if [[ -n "$FORGE" && -f "$FORGE" ]]; then
  echo "forge doctor ($FORGE)…"
  python3 "$FORGE" doctor || echo "warning: forge doctor failed — the tier will skip"
else
  echo "warning: forge.py not found — the tier will skip (set \$FORGE)"
fi

exec uv run pytest -q -m forge tests/forge/ "$@"
