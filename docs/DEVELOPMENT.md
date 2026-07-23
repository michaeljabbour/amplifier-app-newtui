# Development Guide

Working on the code: the daily commands, the rules the codebase holds itself to, and the
checklist to run before a PR. Architecture background is in
[ARCHITECTURE.md](ARCHITECTURE.md); what the app must *do* is in
[DESIGN-SPEC.md](DESIGN-SPEC.md).

## Daily commands

```sh
uv sync                              # install / update dependencies
uv run pytest -q                     # full suite (offline, no credentials, ~90 files)
uv run pytest tests/test_ui_reducer_outcomes.py   # one file
uv run pytest -q -k "steer"                       # by keyword
uv run pytest -q --cov=src/amplifier_app_newtui --cov-report=term  # with coverage
uv run ruff check .                  # lint
uv run pyright src/                  # types
(cd sdk/typescript && npm ci && npm test)  # TypeScript SDK build + tests
uv run amplifier-newtui --demo       # eyeball changes on the scripted session
```

CI (`.github/workflows/ci.yml`) runs exactly: `uv sync --frozen` → `ruff check .` →
`pyright src/` → `pytest -q` with coverage (floor: 85%, actual ~89%), then the perf and
snapshot tests uninstrumented — coverage tracing blows the frame budget on CI runners.
If those pass locally, CI passes. PR titles are linted for Conventional Commits format
(`.github/workflows/pr-title.yml`) — squash-merge titles become the permanent history.

## Type checking

`pyright src/` runs in **`basic`** mode (`[tool.pyright]` in `pyproject.toml`) and is a hard
gate at **0 errors**. Strict mode has been trialed and rejected — and re-verified here.

**Strict trial (2026-07, current tree).** A throwaway strict config over `src/`
(`typeCheckingMode = "strict"`, deleted right after the run so the shipped config stays
`basic`) reports **798 errors across 99 files, 0 warnings**. The distribution is the verdict:

| count | rule | what it is |
| ----: | ---- | ---------- |
| 270 | `reportUnknownMemberType` | attribute access on an untyped third-party value |
| 252 | `reportUnknownVariableType` | value inferred from an untyped return |
| 173 | `reportUnknownArgumentType` | an untyped value passed onward |
| 48 | `reportArgumentType` | a genuine arg-type mismatch worth a look |
| 17 | `reportMissingTypeStubs` | a dependency ships no stubs |
| 38 | *(all other rules)* | parameter / lambda / private-usage / unnecessary-cast … |

**Verdict: stay `basic`.** ~695 of 798 (≈87%) are the `Unknown*` trio — they originate at
the untyped boundaries of `amplifier-core`, Textual, and rich, then propagate through
otherwise well-annotated code. Adopting strict would mean ~700 boundary casts/annotations
whose only job is to launder third-party `Unknown`s, for almost no defect-catching upside;
`basic` already flags the real mismatches (`reportArgumentType`, 48) without that noise. This
re-verifies the earlier trial (~666 on an older tree) — the number tracks tree growth, not
new type debt.

**What would flip the verdict:** when the hot dependencies ship complete type stubs (or we
wrap them behind a thin typed boundary layer), the `Unknown*` trio collapses and the residue
(~100 real findings) becomes a tractable, worthwhile strict adoption. Re-run the throwaway
trial then — don't flip `typeCheckingMode` until that number is small.

## The rules the code holds itself to

These are the [ADR-0007](decisions/ADR-0007-newtui-ground-up-architecture.md) invariants
reviewers will hold your PR to (details in [ARCHITECTURE.md §1](ARCHITECTURE.md)):

1. **Layering** — `ui/` → `model/` → `kernel/`. `kernel/` never imports Textual; `model/`
   imports neither Textual nor amplifier-core; `commands/` imports only `model/` + stdlib.
2. **One normalization boundary** — raw hook payloads become `UIEvent`s in
   `kernel/events.py` and nowhere else.
3. **Reducer never touches widgets** — it acts through the `ReducerHost` protocol; widgets
   talk back only via Textual messages.
4. **Colors are theme-token names** — hex values live only in `ui/themes.py`.
5. **Keymap is data** — new keys go in `ui/keymap.py`'s table (which also drives the
   footer hints); `validate()` rejects conflicting claims.
6. **`ui/app.py` stays a composition root** — ADR-0007 prescribes a <500-line budget; the
   file currently exceeds it, so the direction for new work is extraction into
   `app_support.py`/widgets, never growth.
7. **The demo is a contract** — `DemoRuntime` must emit the same typed events as
   `RealRuntime`; if you add an event, teach both.

## Golden files (transcript renderer)

Presentation changes to transcript rendering are locked by plain-text goldens at widths
**40 / 80 / 97 / 120** (`tests/goldens/`, asserted by `tests/test_golden_widths.py`).

```sh
uv run python tests/goldens/regen.py     # regenerate after an intentional visual change
git diff tests/goldens/                  # review what changed — this diff IS the review
```

**Rule (from [tui-v3-cohesive.md](tui-v3-cohesive.md)):** a presentation change and its
golden update land **in the same commit**. A golden diff you can't explain is a regression,
not noise.

## Regenerating docs assets

```sh
# README screenshot — boots the app headlessly on the demo runtime (deterministic output)
uv run python scripts/regen_screenshot.py

# Architecture diagrams (requires graphviz)
dot -Tpng docs/diagrams/newtui-architecture.dot -o docs/diagrams/newtui-architecture.png
dot -Tpng docs/diagrams/newtui-dataflow.dot -o docs/diagrams/newtui-dataflow.png
dot -Tpng docs/diagrams/newtui-amplifier-integration.dot -o docs/diagrams/newtui-amplifier-integration.png
dot -Tsvg docs/diagrams/newtui-amplifier-integration.dot -o docs/diagrams/newtui-amplifier-integration.svg
```

## Test suite map

| Area | Where | Pattern |
|---|---|---|
| kernel logic | `tests/test_*` (events, approval, governance, cost, persistence, rewind, steering, spawner…) | pure-logic, events consumed directly |
| model | `tests/test_model_*.py` | pure dataclass/enum tests |
| commands | `tests/test_commands_*.py` | `FakeCommandContext` protocol fake — no Textual |
| widgets & reducer | `tests/test_ui_*.py` | per-widget + Textual Pilot headless driving |
| end-to-end flows | `tests/test_flow_*.py` | scripted turns via `DemoRuntime` (approval, interrupt, lanes, rewind, steer/queue…) |
| real lifecycle | `tests/test_runtime_offline.py` | genuine foundation lifecycle with fake modules mounted via `file://` bundles |
| renderer | `tests/test_golden_widths.py` | golden width matrix |
| performance | `tests/test_perf_spike.py` | renderer + live-tail budgets and the hybrid infinite-history 5k frame budget are enforced |
| real-PTY capability (opt-in) | `tests/forge/test_capability_*.py` (`-m forge`) | drives the shipped binary through a real PTY via the forge daemon — demo lane always-on, real lane credential-gated (see below) |

Everything runs offline. If your test needs credentials or network, it's designed wrong —
look at `test_runtime_offline.py` for how to fake the provider side.

## Forge capability tier (opt-in, out of the default gate)

`tests/forge/` drives the **real** shipped `amplifier-newtui` binary through a real PTY via
the `amplifier-skill-forge` terminal daemon — the one seam every other test fakes (real
event stream, real governance hook, real terminal). It is marked `@pytest.mark.forge` and
**excluded from the default gate** (`addopts = -m "not forge"` in `pyproject.toml`), so
`uv run pytest -q` and CI are wholly unaffected: only this tier needs a PTY + the forge
daemon.

```sh
uv run pytest -q -m forge tests/forge/     # run the tier (-m forge overrides the default filter)
scripts/forge_capability.sh                # same, after a `forge doctor` health check
```

Two credential-adaptive lanes:

- **Demo lane** (`test_capability_demo.py`, always on) — launches `amplifier-newtui --demo`
  at a fixed 120×40 and asserts boot→composer, `/status` + `/model` + palette, a full demo
  turn (streaming, plan panel, footer cost), and the agents fan-out (lanes, ctrl+o tail
  focus, delegate summary). Deterministic (virtual clock, fixed costs); screen-observed.
- **Real lane** (`test_capability_real.py`, credential-gated) — boots the real runtime and
  asserts real bundle-prepare boot + resume cost re-seed against the durable
  `ui-events.jsonl` ledger (ADR-0007 §9). It **skips cleanly** when no provider credentials
  are configured, and — because it drives a real, paid session — also skips unless you opt
  in with `AMPLIFIER_FORGE_REAL=1`.

The forge helper is resolved from `$FORGE` or `~/.claude/skills/amplifier-skill-forge`; the
whole tier **skips** (never fails) when forge or its daemon is unavailable. Every wait is a
bounded `forge wait` / ledger poll — **no `sleep`s** — so the tier is flake-resistant.

## Customizing / swapping the bundle

The app's capabilities (orchestrator, provider, tools, agents) come from its **bundle**,
not from code:

- `bundle.md` at the repo root is a **thin wrapper**: it `includes:` foundation's `anchors`
  bundle at a pinned `amplifier-foundation` SHA (partial pin — only anchors' own `bundle.md`
  is pinned; its internal includes and module sources still float `@main`) and overlays only
  a default provider, `tool-mcp`, and `tool-team-pulse`. The packaged copy at
  `src/amplifier_app_newtui/data/bundles/newtui.md` must stay **byte-identical** (compare
  with `diff` after editing).
- Users can point `--bundle` at any bundle file/URI, drop bundles into
  `.amplifier/bundles/` (project) or `~/.amplifier/bundles/` (global), or overlay modules
  via settings — see [SETTINGS.md](SETTINGS.md).
- **Never mount printing hooks** (`hooks-streaming-ui` and friends): they write ANSI to
  stdout and corrupt the Textual screen. The runtime strips them defensively
  (`_apply_hook_suppression`; extend via the `hooks.suppress` setting), but don't add them
  to the bundle in the first place.
- Bundle authoring itself is an Amplifier-ecosystem topic — see the
  [foundation Bundle Guide](https://github.com/microsoft/amplifier-foundation/blob/main/docs/BUNDLE_GUIDE.md).

## Before you open a PR

- [ ] `uv run pytest -q` green, `ruff check .` clean, `pyright src/` clean
- [ ] SDK changed? Python tests pass in the root suite; `sdk/typescript` passes `npm ci && npm test`
- [ ] New behavior has a test at the right layer (see the map above)
- [ ] Layering rules hold (no Textual in `kernel/`/`model/`, no amplifier-core in `model/`/`commands/`)
- [ ] Rendering changed? Goldens regenerated **in the same commit**, diff reviewed
- [ ] Event added/changed? `kernel/events.py` is the only boundary touched, `DemoRuntime` updated, both channels respected
- [ ] Key added? `ui/keymap.py` table only (footer hints follow automatically)
- [ ] `bundle.md` changed? Packaged copy updated byte-identically
- [ ] User-visible behavior changed? [USER-GUIDE.md](USER-GUIDE.md) updated; strings match [DESIGN-SPEC.md](DESIGN-SPEC.md)
- [ ] Docs assets stale? Regenerate screenshot/diagrams (commands above)
