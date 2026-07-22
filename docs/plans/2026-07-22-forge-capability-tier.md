# Design: Forge-driven capability test tier

**Issue:** [#49](https://github.com/michaeljabbour/amplifier-app-newtui/issues/49) — "Forge-driven
capability test tier: validate the real TUI through a real terminal"
**Status:** proposed · **Author:** backlog-attractor worker · **Date:** 2026-07-22
**Slug:** `forge-capability-tier`

> This is a design/decision doc only. No code lands with it. All line citations were verified
> against the read-only `origin/main` checkout at `/Users/michaeljabbour/dev/newtui-wt/base` and
> the forge helper at `~/.claude/skills/amplifier-skill-forge`.

---

## Problem

Every existing test in this repo drives the app through a *substituted* seam. That is a deliberate
design choice — and it is also the exact blind spot this issue targets.

- The whole suite is **offline by construction**: `DEVELOPMENT.md:88` states "Everything runs
  offline. If your test needs credentials or network, it's designed wrong." An autouse fixture in
  `tests/conftest.py:172-189` stubs live pricing and redirects the cache so nothing ever touches the
  network.
- End-to-end flow tests drive the **`DemoRuntime`**, a scripted producer with "no bundle, no network,
  no credentials" (`docs/ARCHITECTURE.md:225`). ADR-0007 makes the demo's fidelity a *contract*:
  "the UI cannot tell the difference" (`docs/decisions/ADR-0007-newtui-ground-up-architecture.md:87`;
  `DEVELOPMENT.md:45-46`).
- Widget tests use Textual **Pilot headless driving** and a `FakeCommandContext`
  (`tests/conftest.py:21-169`; `DEVELOPMENT.md:81-83`). Pilot renders to an in-process virtual
  screen, never a real PTY.

The value of that design is speed and determinism. Its cost is that **the seams that break in
production are precisely the ones the tests fake**: the real amplifier-core event stream, the real
governance hook firing on a real tool call, and the real terminal (PTY sizing, key encoding, alt-
screen, resize). The issue's motivating lesson (2026-07-22) is that both live-found bugs that round
surfaced *only* because real agents ran real commands through a real terminal:

1. **Event drift** — the app consumes a hardcoded `CONSUMED_EVENTS` list; an upstream rename/addition
   "used to silently disappear" (`tests/test_kernel_event_canary.py:1-13`). The canary that catches
   this only fires against the *installed core's* published `ALL_EVENTS` plus module contributions
   (`test_kernel_event_canary.py:108-129`) — a real session surface, not a fixture.
2. **Governance deny-and-continue** — the auto-mode trust boundary denies a real tool call and parks
   a deferred needs-you ticket rather than halting (`kernel/governance_hook.py:8-23,191,316-330`).
   That path is only exercised when a *real orchestrator* actually attempts a governed tool.

There is currently **no scoreable, end-to-end capability check** that boots the shipped binary in a
real terminal and asserts user-visible behavior. Issue #49 asks for that tier. Its companion (#50,
self-improvement loop) needs it as the **evaluation substrate**: without an end-to-end fitness signal
you cannot run rollout→reflect→validate over skills/harness.

### The contract (issue #49 Acceptance + the enumerated capability list)

> A forge-driven suite exercising the list below, green on a dev machine, documented in
> DEVELOPMENT.md; flake-resistant (bounded waits on ledger/screen state, not sleeps). Runnable
> locally (`uv run pytest -m forge` or a script), optional in CI (needs a PTY + forge daemon; keep
> out of the default gate initially).

Enumerated capabilities to assert end-to-end:

- **A.** boot to composer (real bundle prepare, no credentials → demo; with credentials → real)
- **B.** `/status`, `/model`, palette open
- **C.** a full `--demo` turn: streaming, plan panel, footer cost
- **D.** fan-out: lanes appear, tail focus (ctrl+o), delegate summary
- **E.** resume: transcript + cost re-seed match pre-exit state

---

## Evidence (verified file:line)

**What forge actually provides** (`~/.claude/skills/amplifier-skill-forge`):

- Subcommands (verified via `forge.py --help`): `new type key submit run screen read grep wait exec
  codex-exec list close close-tag spawn-* delegate history doctor`.
- `new` opens a persistent PTY with an explicit program and a fixed terminal size:
  `--program`, `--arg`/`--args`, **`--cols`, `--rows`** (verified `forge.py new -h`). Fixed cols is
  the lever for deterministic layout.
- `wait <id> <regex> --timeout` — "regex; exit 1 on timeout"; **capped ~30 s server-side**, "loop
  `wait` calls for longer" (`SKILL.md` core-loop + Rules). This is the bounded-wait primitive.
- `screen <id>` = rendered viewport (for TUIs); `read` = incremental stream; `grep <id> <regex>
  --max` with the caveat **"single words only: ANSI can split phrases in the buffer"** (`SKILL.md`).
- `key <id> <key>` accepts a **fixed list** ("enter, escape, ctrl+c, up, down, tab…"), and "other
  control chars via `type` raw bytes: `\"$(printf '\\x11')\"`" (`SKILL.md` Rules; `forge.py:14`).
- `exec` = one-shot with a **real exit code** and auto-cleanup (`SKILL.md`).
- `doctor` auto-starts the daemon if down and repairs the node-pty spawn-helper bit
  (`SKILL.md` "Before the first Forge operation"; `README.md` Requirements).
- Fan-out lifecycle: `--tag` every session, reap with `close-tag` (`SKILL.md` Rules).

**What the app exposes to drive** (`/Users/michaeljabbour/dev/newtui-wt/base`):

- Console entry `amplifier-newtui = amplifier_app_newtui.main:main` (`pyproject.toml:20-21`).
- `--demo` swaps `DemoRuntimeAdapter` for `RealRuntimeAdapter`; that adapter choice "is the *only*
  place demo and real diverge" (`main.py:41-49`; `docs/ARCHITECTURE.md:185-187`).
- Real boot = foundation 7-step lifecycle, `prepare()` once, then `create_initialized_session`;
  **zero providers = fatal `ProviderMountError` with a doctor pointer** (`docs/ARCHITECTURE.md:189-217`;
  ADR-0007:81-83). This is why "no credentials" cannot boot the *real* runtime to a composer.
- `resume ID` subcommand launches the TUI on the real runtime resuming a stored session
  (`main.py:264-271`).
- Keymap is data: `open_palette` = `/` (`ui/keymap.py:157`), `cycle_permission` = ctrl+p,
  `toggle_lanes` = ctrl+t, **`cycle_tail` = ctrl+o** (`ui/keymap.py:116-119`). `/status` and
  `/model` are live commands over the coordinator (`docs/BACKLOG.md:20` on `origin/main`).
- Composer/footer are the boot target: `Container #composer-slot`, `FooterBar #footer-bar` showing
  `mode · trust · bundle · $cost` (`docs/ARCHITECTURE.md:318-325`). Lane focus swaps the transcript
  to the subagent's blocks (`docs/ARCHITECTURE.md:378-386`).
- Demo turns are scripted and named `seed build auto plan brainstorm agents` with fixed costs
  `$0.06/$0.03/$0.52` for plan/brainstorm/agents (`kernel/demo.py:5-7,25,117`). The `agents` turn
  feeds lanes + the delegate summary; end notice "agents 3 done · click a lane to inspect its
  transcript" (`kernel/demo.py:271`). Plan turn title "Proposed plan · durable session history"
  (`kernel/demo.py:242`).

**The durable ledger the acceptance means by "ledger state"**:

- Sessions persist an **append-only `ui-events.jsonl`** of normalized UIEvents under
  `~/.amplifier/projects/<slug>/sessions/<id>/` (`kernel/persistence.py:1-29,47`,
  `SessionStore.append_event` :272-292, `read_events` :294-312). ADR-0007 §9 makes this log the
  source for cost re-seed, evidence, replay, and contract tests (ADR-0007:68-69).
- Resume cost re-seed is `sum_prior_cost(events_path)` replayed through the same pricing math
  (`kernel/cost.py:305-407`); the adapter learns the baseline during boot and stamps
  `session_cost_start` (`ui/runtime_adapter.py:60,285`; `kernel/runtime.py:467`).
- **Critical constraint:** the `DemoRuntime` does **not** persist — no `SessionStore`/`append_event`
  call exists on the demo path (verified by grep of `kernel/demo.py` + `ui/demo_wiring.py`). So a
  ledger file exists only for the *real* runtime; the demo lane must observe via `screen`/`wait`.

**CI shape** (what "keep out of the default gate" must respect):

- CI is `ubuntu-latest`: `uv sync --frozen` → `ruff check .` → `pyright src/` → `pytest -q`
  (coverage floor 85), then perf/snapshot uninstrumented (`.github/workflows/ci.yml:9-24`;
  `DEVELOPMENT.md:22-24`). No PTY, no forge daemon, no credentials in that job.
- `pyproject.toml [tool.pytest.ini_options]` has `testpaths`, `--import-mode=importlib`,
  `asyncio_mode=strict` — **no marker registry yet** (`pyproject.toml:52-56`). A new `forge` marker
  must be registered or `-m forge` warns/errors under strict config.

---

## Options considered

### Option 1 — Pure in-process PTY harness (pexpect / `pty.openpty`), no forge

Spawn `amplifier-newtui` under a Python PTY directly from pytest; scrape the fd.

- **+** No external daemon; self-contained; nothing new to install in CI.
- **−** Re-implements exactly what forge already gives: ANSI stripping, rendered-viewport reads,
  bounded regex waits, key encoding, fixed cols/rows, teardown. `forge.py` is stdlib-only and already
  battle-tested for "drive a TUI a normal subprocess cannot exercise" (`README.md` capability table).
- **−** Duplicates the #50 substrate. #50's evaluation loop is meant to run *through forge*
  (`SKILL.md` describes supervising attractor/evaluation jobs). A bespoke harness forks the substrate.
- **Verdict:** rejected — rebuilds the wheel and diverges from the companion issue's runner.

### Option 2 — Forge-driven suite, screen-scrape only

Use forge but assert purely on `screen`/`grep` text.

- **+** Uniform observation surface; works for demo and real alike.
- **−** Directly fights forge's own warning: `grep` is "single words only: ANSI can split phrases in
  the buffer" (`SKILL.md`). Multi-word assertions (`"Proposed plan"`, `"agents 3 done"`) become
  flaky. Screen state is also *ephemeral* — a fast turn can paint and repaint before a poll lands.
- **−** For resume, screen text cannot distinguish "re-seeded from ledger" from "coincidental total";
  the acceptance explicitly says re-seed must *match pre-exit state*, which is a ledger fact.
- **Verdict:** rejected as the *sole* surface; screen waits are necessary but not sufficient.

### Option 3 — Forge-driven suite, **two lanes**, ledger-primary + screen-secondary  ✅ recommended

A pytest tier marked `@pytest.mark.forge` that shells out to `forge.py` via a thin Python client, with
two lanes selected by credential presence:

- **Demo lane (always on):** launch `amplifier-newtui --demo` in a forge PTY at fixed cols. Assert
  boot-to-composer, `/status` + `/model` + palette, and a full demo turn (streaming, plan panel,
  footer cost), and the agents fan-out (lanes, ctrl+o tail focus, delegate summary) via bounded
  `wait` on *single-token anchors* + `screen` structural checks. Deterministic (virtual clock, fixed
  costs) so it is green on any dev machine or PTY-capable CI.
- **Real lane (credential-gated, `skipif` when absent):** launch the *real* runtime, assert real
  bundle prepare boots to the composer, drive one governed action, and — the payoff — assert on the
  **`ui-events.jsonl` ledger** (`SessionStore.read_events`) for semantic truth (event kinds present,
  governance ticket parked), then exit and `resume` and assert transcript rebuild + `sum_prior_cost`
  re-seed *equals* the pre-exit total read from the same ledger.

- **+** Ledger assertions are ANSI-free, race-free, and exactly the "ledger state" the acceptance
  names; screen waits cover UI-presence. Belt and suspenders, each on its strength.
- **+** Credential gating makes "green on a dev machine" honest in both worlds: no creds → demo lane
  only (still green); creds → both lanes.
- **+** Reuses forge = aligns with #50's runner; nothing bespoke to maintain.
- **−** Two observation surfaces = more harness code than Option 2. Mitigated by one shared helper
  module (below). Accepted: the asymmetry is inherent (demo doesn't persist).
- **Verdict:** **chosen.** It is the only option that satisfies the ledger-vs-screen flake-resistance
  clause *and* the credential-adaptive boot clause without faking the seams the issue exists to test.

---

## Decision / Recommendation

**Adopt Option 3.** Build a new opt-in pytest tier, `tests/forge/`, marked `@pytest.mark.forge` and
**excluded from the default gate**, that drives the shipped `amplifier-newtui` binary through a real
PTY via `amplifier-skill-forge`'s `forge.py`. Observation is **ledger-primary** (assert on the
session's append-only `ui-events.jsonl` where a real session exists) and **screen-secondary** (bounded
`forge wait`/`screen` on single-token anchors for UI-presence and for the non-persisting demo lane).
The tier is **credential-adaptive**: a demo lane always runs; a real lane runs only when provider
credentials are present, else `skip`.

Guiding principles, each traceable to evidence:

1. **No sleeps.** Every synchronization is a bounded `forge wait <regex> --timeout` (looped past the
   ~30 s server cap) or a bounded poll of `SessionStore.read_events` — never `time.sleep`
   (`SKILL.md` Rules; acceptance clause).
2. **Assert on tokens ANSI can't split.** Anchor screen waits on single words / stable glyphs; push
   multi-word/semantic assertions to the ledger (`SKILL.md` grep caveat).
3. **Fixed cols/rows** so rendered layout is deterministic and matches the golden width family
   (40/80/97/120 — `DEVELOPMENT.md:50-51`); default the tier to `--cols 120 --rows 40`.
4. **Tag + `close-tag` teardown** in a fixture finalizer so a crashed test never leaks PTYs
   (`SKILL.md` Rules).
5. **Demo divergence is the only seam** — reuse the exact same driver for both lanes; only the
   launch args and the observation surface differ (`ARCHITECTURE.md:185-187`).

---

## Implementation plan (phased, concrete file paths)

All paths under the writable repo `/Users/michaeljabbour/dev/amplifier-app-newtui`.

### Phase 0 — Scaffolding & marker (no assertions yet)

- `pyproject.toml` — register the marker under `[tool.pytest.ini_options]` (`markers = ["forge: real-PTY
  capability tier via amplifier-skill-forge; opt-in, needs a PTY + forge daemon"]`), and add an
  `addopts` default of `-m "not forge"` so the existing gate and CI keep excluding it with zero CI
  edits. (`pyproject.toml:52-56` is the anchor.)
- `tests/forge/__init__.py`, `tests/forge/conftest.py` — a `forge` session fixture that: resolves
  `$FORGE` = `<skill-dir>/tools/forge.py`, runs `forge doctor` once per session (skip the whole tier
  with a clear reason if the daemon can't start or `forge.py` is absent), `new`s a tagged PTY at
  `--cols 120 --rows 40`, and `close-tag`s on teardown.
- `tests/forge/_forge.py` — a ~100-line stdlib client wrapping `forge.py` subprocess calls:
  `new/type/key/submit/screen/wait/close`, plus `press_ctrl(id, letter)` that tries `key <id>
  ctrl+<x>` and falls back to `type <id> "$(printf '\xNN')" --no-newline` for control chars outside
  forge's fixed key list (ctrl+o = `\x0f`, ctrl+t = `\x14`) — grounded in `SKILL.md`'s raw-byte rule.
- `tests/forge/_ledger.py` — a bounded poller over `SessionStore.read_events(session_id)`
  (`kernel/persistence.py:294-312`) with a deadline, returning kinds/records; plus a helper to locate
  the newest session dir for the project slug (`SessionStore.list_sessions` :316-332).

### Phase 1 — Demo lane (Capabilities A-demo, B, C, D)

`tests/forge/test_capability_demo.py`, launching `.venv/bin/amplifier-newtui --demo`:

- **A (boot to composer):** `wait` for a stable composer/footer anchor token (e.g. the bundle name in
  `#footer-bar`, `ARCHITECTURE.md:325`); assert the composer prompt is present on `screen`.
- **B (`/status`, `/model`, palette):** `type "/"` → assert palette strip visible (`keymap.py:157`);
  run `/status` and `/model`, `wait` for each command's distinctive single-token output.
- **C (full demo turn):** drive the `build` turn; `wait` for streamed tokens to appear (streaming),
  drive the `plan` turn and anchor on `"Proposed"` / plan glyphs (plan panel — `demo.py:242`), and
  assert the footer shows a `$` cost figure after the turn (fixed `$0.06/$0.03/$0.52` — `demo.py:25`).
- **D (fan-out):** drive the `agents` turn; ctrl+t to open lanes, assert ≥1 lane row; **ctrl+o** to
  cycle tail focus (`keymap.py:119`); anchor on `"agents"` + a lane count for the delegate summary
  (`demo.py:271`). Multi-word phrases are avoided per the ANSI caveat — anchor on `3` / `done`
  separately or via the lane count glyph.

### Phase 2 — Real lane (Capabilities A-real, E, + the two lesson seams)

`tests/forge/test_capability_real.py`, gated by `pytestmark = pytest.mark.skipif(no provider creds)`:

- **A (real boot):** launch the real runtime (no `--demo`); `wait` for the composer with the *real*
  bundle name; a missing provider surfaces as `ProviderMountError` (`ARCHITECTURE.md:215`), which the
  skip guard prevents by requiring creds first.
- **Lesson seams (bonus fitness signals, cheap once real):** drive one governed tool action and poll
  the ledger for the governance/needs-you event kinds (`governance_hook.py:316-330`); assert no
  `event-canary` "unbridged event kind" notice appears for the exercised turn
  (`test_kernel_event_canary.py:88-93`) — i.e. the installed core's events are all bridged.
- **E (resume):** capture pre-exit `session_cost` by summing the ledger (`sum_prior_cost`,
  `cost.py:312`); exit the PTY cleanly; `resume <id>` in a fresh PTY; assert (a) the transcript
  rebuilds (user/assistant lines re-appear — `runtime_adapter.py:64-67`,
  `test_app_boot.py:251`) and (b) the footer total equals the pre-exit ledger sum (re-seed match).

### Phase 3 — Runner + docs

- `scripts/forge_capability.sh` (or `make forge-cap`) — thin wrapper: `uv run pytest -m forge -q`,
  after a `forge doctor`. Satisfies the "or a script" clause.
- `docs/DEVELOPMENT.md` — add a **"Forge capability tier"** subsection near the Daily commands / Test
  suite map (`DEVELOPMENT.md:8-20,75-89`): how to install forge, `uv run pytest -m forge`, the
  credential-gating behavior, and the explicit statement that it is **out of the default gate and CI**
  by design (needs a PTY + daemon). Add a row to the Test suite map table.
- Optional (do **not** wire into `ci.yml` now): a *separate, non-required* GitHub Actions workflow
  `.github/workflows/forge-capability.yml` (manual `workflow_dispatch`) that installs forge and runs
  the demo lane on a self-hosted/PTY-capable runner. Left as a follow-up so the default gate
  (`ci.yml:9-24`) is untouched — honoring "keep out of the default gate initially."

---

## Test & validation strategy

- **Layer placement:** this is a new top tier *above* the existing map (`DEVELOPMENT.md:75-89`) —
  it does not replace pure-logic, Pilot, golden, or contract-replay tests; it complements them by
  exercising the one thing they all fake (the real PTY + real event stream).
- **Determinism budget:** the demo lane inherits the virtual clock / seeded RNG / fixed costs of
  `DemoRuntime` (`ARCHITECTURE.md:227`), so its assertions are exact, not fuzzy.
- **Flake-resistance proof:** grep the new tier for `sleep(` in review — it must be zero; every wait
  is `forge wait … --timeout` or a deadlined ledger poll. Run the demo lane ~20× locally to confirm
  no intermittent failures before merge.
- **Self-containment:** the tier must `skip` (never `fail`) when forge/daemon is unavailable or creds
  are absent, so `uv run pytest` (default `-m "not forge"`) and CI are wholly unaffected.
- **Cost containment (real lane):** use the smallest/cheapest model role and a single trivial governed
  action; the real lane's purpose is *capability presence*, not workload.

---

## Risks & mitigations

| Risk | Evidence | Mitigation |
|---|---|---|
| `forge key` lacks `ctrl+o`/`ctrl+t` (fixed key list) | `SKILL.md` Rules; `forge.py:14` | `press_ctrl` helper falls back to raw-byte `type "$(printf '\x0f')"` — documented forge pattern. Probe once in Phase 0. |
| Multi-word screen assertions flake (ANSI splits phrases) | `SKILL.md` grep caveat | Anchor screen `wait`s on single tokens/glyphs; push semantic checks to the ledger. |
| Demo lane has no ledger to assert on | no `append_event` on demo path (grep) | Demo lane is screen-only by design; ledger assertions live in the real lane. Stated as an accepted asymmetry. |
| Real boot hard-fails without a provider | `ARCHITECTURE.md:215` `ProviderMountError` | Credential `skipif` guard runs the real lane only when creds exist; else demo lane keeps the suite green. |
| `forge wait` 30 s cap too short for a real turn | `SKILL.md` Rules | Loop `wait` past the cap; keep the real action trivial. |
| CI accidentally runs the tier (no PTY/daemon) | `ci.yml:9-24` has none | `addopts = -m "not forge"` excludes by default; no `ci.yml` edit; any opt-in workflow is separate + non-required. |
| Strict marker config rejects `-m forge` | `pyproject.toml:52-56` (no markers) | Register the `forge` marker in Phase 0. |
| PTY leak on crash | — | Tagged sessions + `close-tag` in fixture finalizer (`SKILL.md` fan-out rule). |
| Doc references "BACKLOG.md §7" but `origin/main`'s §7 is "Smaller wins" | `docs/BACKLOG.md:115` (base) | Grounded the lesson in the *mechanisms* that exist on main (event canary + governance deny-and-continue) rather than a §number that differs between the issue's BACKLOG snapshot and `origin/main`. Noted for the author. |
| Skill install path assumption | `README.md` (install copies to `~/.amplifier/skills`) | Resolve `$FORGE` at runtime (env or known skill dirs); skip tier with a clear message if unresolved. |

---

## Acceptance mapping

| Acceptance clause (issue #49) | How this plan satisfies it |
|---|---|
| **A. boot to composer (no creds → demo; creds → real)** | Phase 1 demo lane asserts boot-to-composer on `--demo` (always); Phase 2 real lane asserts real bundle-prepare boot, `skipif` when no creds. Credential-adaptive by the `RealRuntimeAdapter`/`DemoRuntimeAdapter` split (`main.py:41-49`). |
| **B. `/status`, `/model`, palette open** | Phase 1: `type "/"` opens the palette strip (`keymap.py:157`); run `/status` and `/model`, bounded-`wait` on each command's distinctive token. |
| **C. full `--demo` turn: streaming, plan panel, footer cost** | Phase 1 drives `build` (streaming tokens), `plan` (panel anchor "Proposed", `demo.py:242`), and asserts the footer `$` figure (`demo.py:25`; `ARCHITECTURE.md:325`). |
| **D. fan-out: lanes appear, tail focus (ctrl+o), delegate summary** | Phase 1 drives the `agents` turn; ctrl+t lanes, **ctrl+o** tail focus (`keymap.py:118-119`), delegate-summary anchor (`demo.py:271`). |
| **E. resume: transcript + cost re-seed match pre-exit state** | Phase 2 real lane: sum ledger pre-exit (`sum_prior_cost`, `cost.py:312`), `resume <id>` (`main.py:264-271`), assert transcript rebuild (`runtime_adapter.py:64-67`) and footer total == pre-exit ledger sum. |
| **green on a dev machine** | Demo lane deterministic (virtual clock/fixed cost, `ARCHITECTURE.md:227`); real lane `skip`s without creds — so the tier is green whether or not creds/forge exist. |
| **documented in DEVELOPMENT.md** | Phase 3 adds a "Forge capability tier" subsection + a Test-suite-map row (`DEVELOPMENT.md:75-89`). |
| **flake-resistant: bounded waits on ledger/screen state, not sleeps** | Every sync is `forge wait … --timeout` (looped) or a deadlined `SessionStore.read_events` poll (`persistence.py:294-312`); zero-`sleep` review gate + 20× local stability run. |
| **Runnable locally (`uv run pytest -m forge` or a script)** | `forge` marker registered (Phase 0); `scripts/forge_capability.sh` wrapper (Phase 3). |
| **optional in CI; out of default gate initially (needs PTY + forge daemon)** | `addopts = -m "not forge"` excludes by default with no `ci.yml` change (`ci.yml:9-24`); any opt-in workflow is separate and non-required. |
| **(companion #50) evaluation substrate** | Each capability assertion is a discrete pass/fail fitness signal; observing via the durable ledger yields machine-readable outcomes #50's rollout→reflect→validate loop can score. |

---

## Open questions for the author

1. Confirm the intended reading of "no credentials → demo": run `--demo` as the fallback lane (this
   design), vs. attempt a real credential-less boot (which hard-fails at `ProviderMountError`).
2. Real lane: acceptable to require a provider key in the dev/CI environment, or should the real lane
   stay a documented manual step only?
3. Should the lesson-seam assertions (event canary / governance) be in-scope for v1 of this tier, or
   deferred to #50 once the substrate exists?
