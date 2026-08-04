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
uv run pytest -q --cov=src/amplifier_app_tui --cov-report=term  # with coverage
uv run ruff check .                  # lint
uv run pyright src/                  # types
(cd sdk/typescript && npm ci && npm test)  # TypeScript SDK build + tests
uv run amplifier-tui --demo       # eyeball changes on the scripted session
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

These are the [ADR-0007](decisions/ADR-0007-tui-ground-up-architecture.md) invariants
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
dot -Tpng docs/diagrams/tui-architecture.dot -o docs/diagrams/tui-architecture.png
dot -Tpng docs/diagrams/tui-dataflow.dot -o docs/diagrams/tui-dataflow.png
dot -Tpng docs/diagrams/tui-amplifier-integration.dot -o docs/diagrams/tui-amplifier-integration.png
dot -Tsvg docs/diagrams/tui-amplifier-integration.dot -o docs/diagrams/tui-amplifier-integration.svg
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
| cross-product parity (self-skipping) | `tests/test_skill_alias_external_cli_resolver.py` | drives the REAL external `amplifier-app-cli` alias resolver (loaded from a sibling checkout via `AMPLIFIER_APP_CLI_PATH` or `~/dev/amplifier-app-cli`) against this repo's own resolver over one shared fixture; runs for real when the sibling is present, skips cleanly (never fails) when it isn't — never a hard dependency of the default gate |

Everything runs offline. If your test needs credentials or network, it's designed wrong —
look at `test_runtime_offline.py` for how to fake the provider side.

## Forge capability tier (opt-in, out of the default gate)

`tests/forge/` drives the **real** shipped `amplifier-tui` binary through a real PTY via
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

- **Demo lane** (`test_capability_demo.py`, always on) — launches `amplifier-tui --demo`
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
  bundle (tracked at `amplifier-foundation@main` — see "Anchors ref lifecycle" below) and
  overlays only a default provider, `tool-mcp`, and `tool-team-pulse`. The packaged copy at
  `src/amplifier_app_tui/data/bundles/tui.md` must stay **byte-identical** (compare
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

## Anchors ref lifecycle

The wrapper composes foundation's `anchors` bundle via an `includes:` entry. That include
tracks **`amplifier-foundation@main` (a floating ref)** — not a static pin. Background and
policy (issue #53):

- **Why not a bare SHA.** A pinned 40-hex SHA was tried and abandoned: GitHub stops serving a
  non-tip SHA once foundation advances, so clean installs failed with "Include Failed
  (skipping): amplifier-foundation" and booted degraded (#96). Foundation's release **tags**
  (`v2.1.x`) do **not** ship `bundles/anchors` — only `@main` carries it — so `@main` is the
  only fetchable source today, and it matches how the shared registry resolves `anchors`.
  **Re-verified 2026-08-04** (compliance B9 gap-closure pass): foundation has published no
  tag since the prior 2026-08-02 check (`v2.1.0`/`v2.1.1`/`v2.1.2` via `git ls-remote --tags`);
  the latest, `v2.1.2`, still 404s on the `bundles/anchors` contents-API path, `@main` still
  200s. The constraint is unchanged and re-pinning a bare SHA was rejected again for the same
  reason #96 reverted it the first time. Re-run `scripts/verify_anchors_constraint.py` to
  redo this check against the live repo — do that before ever touching this include.
- **How updates flow.** Tracking `@main` means composition changes (roster, behaviors) *and*
  anchors' internal module/behavior fixes all arrive on the next fetch. `amplifier-tui
  update` refreshes the runtime cache (`--force` runs `uv cache clean` for a true re-fetch).
  This is the "bump" — there is no static SHA to hand-edit on the happy path.
- **How staleness surfaces (instead of silence).** Anchors is *included*, and foundation's
  per-bundle `check_bundle_status` deliberately skips included-bundle URIs, so its freshness
  was previously invisible. `kernel/updater.py:anchors_status()` checks it directly (an
  offline-safe `git ls-remote` compare against the local cache) and both `amplifier-tui
  update --check-only` and `amplifier-tui doctor` now report `anchors up to date` /
  `anchors is behind upstream …` / `… check unavailable (offline)`. Offline degrades to a
  neutral note — never a false "stale" finding.
- **Three copies, kept in lockstep.** The anchors include ref appears in **three** live files
  (`kernel/updater.py:pin_files`): repo-root `bundle.md`, the byte-identical packaged
  `tui.md`, and the packaged `anchors.md` pointer. Anti-drift is enforced by
  `tests/test_kernel_session_config.py` (byte-identity + a three-way ref-match).
- **Changing the tracked ref.** Use `uv run python scripts/bump_anchors_ref.py <ref>` — it
  rewrites all three copies atomically and re-verifies byte-identity + lockstep before writing
  (defaults to `main`; idempotent). It **refuses a bare SHA** without `--allow-sha`. When
  foundation ships tagged releases that carry `bundles/anchors`, switch to
  `scripts/bump_anchors_ref.py vX.Y.Z` for reproducible boots (issue #53 Option B).
- **Re-checking whether a tag now ships anchors.** `uv run python
  scripts/verify_anchors_constraint.py` re-runs the exact GitHub-API check above (latest
  release tag vs. `bundles/anchors` contents-API 200/404) against the live repo and exits
  non-zero if the answer has flipped, so this doesn't rely on a human remembering to
  re-poke GitHub. It is a manual/maintenance check (network required), not part of the
  default offline test gate.
- **Guarding every OTHER dependency.** `tests/test_no_floating_dependencies.py` fails the
  build if any git dependency in the packaged bundle, `pyproject.toml`'s `[tool.uv.sources]`,
  or a CI workflow's `uses:` step ever floats a branch instead of a tag/commit SHA. The
  anchors include above is the ONE allow-listed, justified exception (see that file's
  `ALLOWED_FLOATING_REFS`) — every other dependency in this repo is pinned.

## Adoption gates (replacing amplifier-app-cli)

amplifier-app-tui replaces amplifier-app-cli through five staged gates, not by
declaration. The record lives in [adoption/](adoption/README.md): one row per stage with
its owner, minimum usage window, tested commit, entry/exit evidence, and decision.

```sh
python3 scripts/adoption_gate.py status      # where the rollout stands
python3 scripts/adoption_gate.py promote 1   # may stage 1 be promoted? exit 0 = yes
scripts/adoption_smoke.sh                    # the compatibility smoke run at every gate
```

The smoke adds no new suite — it composes `ruff` + `pyright` + `pytest` + the forge tier
above, then validates the ledger. Two rules worth knowing before you touch a stage row:
an **open `release-blocking` defect blocks every promotion regardless of elapsed time**,
and `promote 4` is the gate that authorizes retiring amplifier-app-cli.

## Before you open a PR

- [ ] `uv run pytest -q` green, `ruff check .` clean, `pyright src/` clean
- [ ] SDK changed? Python tests pass in the root suite; `sdk/typescript` passes `npm ci && npm test`
- [ ] New behavior has a test at the right layer (see the map above)
- [ ] Layering rules hold (no Textual in `kernel/`/`model/`, no amplifier-core in `model/`/`commands/`)
- [ ] Rendering changed? Goldens regenerated **in the same commit**, diff reviewed
- [ ] Event added/changed? `kernel/events.py` is the only boundary touched, `DemoRuntime` updated, both channels respected
- [ ] Key added? `ui/keymap.py` table only (footer hints follow automatically)
- [ ] `bundle.md` changed? All **three** anchors-ref copies updated in lockstep (`bundle.md`,
      packaged `tui.md` byte-identically, packaged `anchors.md`) — use `scripts/bump_anchors_ref.py`
- [ ] User-visible behavior changed? [USER-GUIDE.md](USER-GUIDE.md) updated; strings match [DESIGN-SPEC.md](DESIGN-SPEC.md)
- [ ] Docs assets stale? Regenerate screenshot/diagrams (commands above)
