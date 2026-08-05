#!/usr/bin/env bash
# Run the forge-driven capability tier (issue #49).
#
# The tier drives the shipped `amplifier-tui` binary through a real PTY via
# the amplifier-skill-forge daemon. It is opt-in and excluded from the default
# gate (`addopts = -m "not forge"`), so this wrapper re-selects it with
# `-m forge` after a `forge doctor` health check.
#
# Demo lane runs whenever the PTY substrate is available. Pass --require (or
# AMPLIFIER_FORGE_REQUIRED=1) to make missing Forge infrastructure a hard
# failure; adoption/release gates always do this. The real lane still skips
# unless credentials are configured AND AMPLIFIER_FORGE_REAL=1 is set because
# it drives a real, paid session.
set -euo pipefail

cd "$(dirname "$0")/.."

# Parse the wrapper-only option while forwarding every other argument to pytest.
REQUIRED=0
case "${AMPLIFIER_FORGE_REQUIRED:-}" in
  1|true|TRUE|True|yes|YES|Yes|on|ON|On) REQUIRED=1 ;;
esac
PYTEST_ARGS=()
for arg in "$@"; do
  case "$arg" in
    --require) REQUIRED=1 ;;
    *) PYTEST_ARGS+=("$arg") ;;
  esac
done

# Resolve the forge helper: $FORGE, then the known skill install dirs.
FORGE="${FORGE:-}"
if [[ -z "$FORGE" ]]; then
  for candidate in \
    "$HOME/.codex/skills/amplifier-skill-forge/tools/forge.py" \
    "$HOME/.claude/skills/amplifier-skill-forge/tools/forge.py" \
    "$HOME/.amplifier/skills/amplifier-skill-forge/tools/forge.py" \
    "$HOME/dev/amplifier-skill-forge/tools/forge.py"; do
    if [[ -f "$candidate" ]]; then FORGE="$candidate"; break; fi
  done
fi

if [[ -n "$FORGE" && -f "$FORGE" ]]; then
  echo "forge doctor ($FORGE)…"
  if ! python3 "$FORGE" doctor; then
    if [[ "$REQUIRED" == "1" ]]; then
      echo "error: forge doctor failed during a required capability run" >&2
      exit 1
    fi
    echo "warning: forge doctor failed — the tier will skip"
  fi
else
  if [[ "$REQUIRED" == "1" ]]; then
    echo "error: forge.py not found during a required capability run (set \$FORGE)" >&2
    exit 1
  fi
  echo "warning: forge.py not found — the tier will skip (set \$FORGE)"
fi

export FORGE
if [[ "$REQUIRED" == "1" ]]; then
  export AMPLIFIER_FORGE_REQUIRED=1
fi

if [[ "${#PYTEST_ARGS[@]}" -eq 0 ]]; then
  exec uv run pytest -q -m forge tests/forge/
fi
exec uv run pytest -q -m forge tests/forge/ "${PYTEST_ARGS[@]}"
