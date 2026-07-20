# AGENTS.md — amplifier-app-newtui

Guidance for AI coding agents working in this repo (the Amplifier full-screen
Textual TUI). The authoritative development guide is
[docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) — read it before making changes.

## Quick commands

```sh
uv sync                  # install / update dependencies
uv run pytest -q         # full suite (offline, no credentials)
uv run ruff check .      # lint
uv run pyright src/      # types
```

CI runs exactly: `uv sync --frozen` → `ruff check .` → `pyright src/` → `pytest -q`.

## Non-negotiables

- **Layering** — `ui/` → `model/` → `kernel/`. Only `kernel/` touches
  amplifier-core/foundation; `kernel/` never imports Textual (details:
  [docs/ARCHITECTURE.md §1](docs/ARCHITECTURE.md)).
- **Bundle byte-identity** — `bundle.md` (repo root) and
  `src/amplifier_app_newtui/data/bundles/newtui.md` must stay byte-identical;
  after editing one, copy it over the other.
- **Never mount printing hooks** (`hooks-streaming-ui` and friends) in the
  bundle — they write ANSI to stdout and corrupt the Textual screen.
- **Golden files** — presentation changes to the transcript renderer must
  regenerate `tests/goldens/` in the same commit
  (`uv run python tests/goldens/regen.py`).

See [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) for the full rules, the test
suite map, and the pre-PR checklist.
