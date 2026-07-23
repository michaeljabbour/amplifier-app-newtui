# Anchors pin lifecycle — design / decision doc

**Issue:** #53 — *Anchors pin lifecycle: automate pin bumps and surface staleness (why-pinned documented)*
**Status:** proposed (doc is the deliverable; no code changes here)
**Scope:** how the newtui wrapper's pinned `anchors` include gets refreshed, and how pin staleness stops being silent.

All file:line citations below were verified against the read-only `origin/main` checkout at
`/Users/michaeljabbour/dev/newtui-wt/base`.

---

## Problem

The newtui wrapper bundle composes foundation's `anchors` bundle **pinned to a specific
foundation commit** (`93615d9847ce40313cc0d60583cb886de4337f9e`). The pin buys reproducible
boots, but today:

1. **Nothing ever bumps the pin.** `amplifier-newtui update` walks the composed bundles via
   foundation's `check_bundle_status` / `update_bundle`; pinned refs report *"no update"* and are
   skipped. New anchors *composition* (roster changes, new behaviors) only lands via a **manual
   40-hex SHA edit**, replicated by hand across multiple packaged copies.
2. **Staleness is silent.** Neither `update --check-only` nor `/doctor` ever says "the anchors pin
   is N commits behind upstream." A maintainer has no signal that the pin has drifted from `main`.
3. **The pin is a *partial* pin and that nuance is easy to lose.** Only anchors' own `bundle.md` is
   pinned; its internal `behaviors/*.yaml` includes and every module `source:` still float `@main`
   and *do* refresh on `update`. So module/behavior fixes arrive automatically; only composition
   changes need a bump. Any policy has to say this out loud or it will over-promise reproducibility.

The issue asks us to **pick a pin lifecycle**, document the policy in `DEVELOPMENT.md`, ship a
working bump mechanism, and make staleness visible in `update --check-only` (and `/doctor`) —
while keeping the packaged bundle copies from drifting apart.

---

## Evidence (verified file:line cites)

**The pin and its documented rationale (partial pin):**
- `bundle.md:18-24` — `includes:` block, comment `# PARTIAL PIN: this pins only anchors' own
  bundle.md — its internal includes (behaviors/*.yaml) and module sources still reference @main`,
  and `bundle.md:24` the pinned include URI
  `git+https://github.com/microsoft/amplifier-foundation@93615d98…#subdirectory=bundles/anchors/bundle.md`.
- `docs/plans/2026-07-20-anchors-migration-implementation.md:64` — "The include URI pins only
  **anchors' own `bundle.md`** … still reference `@main` and keep floating … the pin is partial. Do
  not claim full reproducibility anywhere." Also `:49` (how the SHA was resolved:
  `git ls-remote https://github.com/microsoft/amplifier-foundation main`, "re-resolve ONLY if instructed").

**Update tooling skips pinned refs (the core gap):**
- `src/amplifier_app_newtui/kernel/updater.py:8-13` — docstring: `check_bundle_status` "(SHA compare,
  pinned refs skipped)"; `--force` runs `uv cache clean` so `@main` sources genuinely re-fetch.
- `src/amplifier_app_newtui/kernel/updater.py:75-106` — `check_bundles()` produces `BundleUpdate`
  rows; `:88-91` degrades to `[]` when foundation is unavailable (offline-safe pattern to mirror).
- `src/amplifier_app_newtui/main.py:726-778` — `_update(check_only, yes, force)` renders the "Bundle
  updates" table (`:743-749`), computes `stale` (`:751`), and for `--check-only` prints the hint and
  returns 0 (`:756-758`). **This is the exact function to extend with a pin-staleness line.**

**Staleness surface for `/doctor`:**
- `src/amplifier_app_newtui/commands/doctor.py:52-198` — named checks return `CheckResult(name, ok,
  message)`; `:201-211` `run_checks(...)` composes the suite; `:187-208` `run_standalone` returns
  exit 0 (no findings) / 1 (findings). A new `check_anchors_pin(...)` slots straight in here.

**The pin actually lives in THREE live files, not two** (the issue says "two copies"):
- `bundle.md:24` (repo root), `src/amplifier_app_newtui/data/bundles/newtui.md:24` (packaged copy),
  and **`src/amplifier_app_newtui/data/bundles/anchors.md:13`** (packaged parity pointer). Verified
  via `grep -rn amplifier-foundation@93615d98…` → those three files (the 4th hit,
  `docs/plans/2026-07-20-…md`, is a historical record, not a live pin).
- `src/amplifier_app_newtui/data/bundles/anchors.md:11-12` — comment: "Keep this SHA in lockstep
  with the include in newtui.md — a test pins the two together (test_kernel_session_config.py)."

**The anti-drift tests that already exist:**
- `tests/test_kernel_session_config.py:594-601` — `test_packaged_bundle_matches_repo_root_bundle`:
  `packaged.read_bytes() == (root / "bundle.md").read_bytes()` (bundle.md ↔ newtui.md byte-identity).
- `tests/test_kernel_session_config.py:631-643` —
  `test_packaged_anchors_pointer_resolves_and_matches_the_wrapper_pin`: regex-extracts
  `amplifier-foundation@([0-9a-f]{40})` from newtui.md and asserts the same
  `amplifier-foundation@<sha>` string appears in anchors.md (the anchors ↔ newtui lockstep).

**Docs + CI context:**
- `docs/DEVELOPMENT.md:91-110` — "Customizing / swapping the bundle": states the thin-wrapper +
  partial-pin story and the byte-identity rule; `:121` pre-PR checklist item "`bundle.md` changed?
  Packaged copy updated byte-identically." This is where the policy section belongs.
- `docs/DEVELOPMENT.md:22-26` — CI runs `uv sync --frozen → ruff → pyright → pytest -q`.
- `.github/workflows/` — only `ci.yml` and `pr-title.yml` exist today; a scheduled bump workflow
  would be new. `scripts/` holds only `regen_screenshot.py` — the pattern for a repo maintenance
  script already exists.
- `src/amplifier_app_newtui/kernel/config.py:398` — `packaged_bundles_dir()`, the offline-safe way to
  locate the packaged copies from code/tests.

---

## Options considered

The issue frames three (A/B/C). Two orthogonal axes are actually in play: **(1) what the pin tracks**
(SHA vs tag vs floating) and **(2) how a bump is triggered** (manual vs tooling vs CI). Staleness
*surfacing* is required regardless and is not really optional.

### Option A — pin-bump tooling (resolve latest, rewrite copies, test, PR)
Keep pinning a SHA; add a mechanism that resolves the latest upstream anchors commit, rewrites every
pin copy, runs the suite + a real boot, and opens a PR.

- **Pro:** keeps reproducible boots (the reason the pin exists); automates freshness; the human PR
  gate preserves the "someone verified this composition boots" property.
- **Pro:** no dependency on foundation publishing anything new — works today.
- **Con:** we own a small script + a CI job and a SHA→SHA rewrite across ≥3 files.
- **Honest nuance:** the bump is a **maintainer/repo action, not an end-user runtime action.** The
  pin is baked into repo source and the shipped wheel; rewriting files inside an installed user's
  `site-packages` is meaningless (lost on reinstall). So the "mechanism" is correctly a **repo
  script + CI job**, and the *user-facing* surface is staleness **reporting** only.

### Option B — track a release tag once foundation publishes tagged releases
Pin `…@vX.Y.Z` instead of a SHA; updates arrive by tag bump.

- **Pro:** still reproducible; human-legible ("anchors v1.4" beats a 40-hex SHA); a tag is a curated,
  intentional release rather than whatever `main` happened to be.
- **Con:** **foundation does not publish tags today** (the migration plan resolves the pin via
  `git ls-remote … main`, `docs/plans/2026-07-20-…md:49`). Not available now; strictly a future path.
- **Relationship to A:** B is a *drop-in evolution of A* — same rewrite/verify/PR machinery, the
  regex just matches a tag as well as a SHA. Choosing A does not foreclose B.

### Option C — float anchors `@main`
Drop the pin; max freshness.

- **Pro:** zero bump machinery; always current.
- **Con:** gives up reproducible boots — **the entire reason the pin exists** (`bundle.md:18-24`).
- **Con:** `main` can break composition (roster/behavior changes) and every fresh boot inherits it
  with no human gate. The issue itself marks this **"Not recommended."** Rejected.

---

## Decision / Recommendation

**Adopt Option A now, as a repo script + scheduled CI job, and land staleness surfacing in both
`update --check-only` and `/doctor`. Treat Option B (tag tracking) as the drop-in successor the day
foundation ships tagged releases. Reject Option C.**

Concretely:

1. **Staleness is read-only and always on.** A shared `anchors_pin_status()` helper reads the pinned
   SHA from the packaged bundle and compares it to upstream `main`; its result is surfaced by
   `update --check-only` and `/doctor`. Offline → it degrades to a neutral "upstream check
   unavailable" (never a false finding), mirroring `check_bundles()`
   (`updater.py:88-91`).
2. **The bump is a maintainer mechanism**, not a user command: `scripts/bump_anchors_pin.py`
   (rewrite all pin copies + re-verify) driven by a scheduled `.github/workflows/anchors-pin-bump.yml`
   that runs the suite + a real boot and opens a PR. A human merges it — that merge *is* the
   reproducibility gate.
3. **Anti-drift is enforced by tests over a single source-of-truth list of pin files** — including the
   third copy (`anchors.md`) the "two copies" framing misses.

Rationale: A is the only option that keeps the pin's reason-for-existing (reproducible boots) while
killing the manual toil and the silence. B is genuinely better but blocked on an upstream capability
we don't control; building A's machinery is the prerequisite for B anyway. C throws away the pin.

> ★ **Insight:** the pin isn't the problem — *silence* and *manual toil* are. Reproducibility comes
> from a human gate on "does this composition still boot," so the fix keeps the human PR gate and
> automates only the mechanical parts (resolve, rewrite, test). Automating the judgment away (Option
> C) would delete the very property the pin exists to provide.

---

## Implementation plan (phased, concrete file paths)

### Phase 0 — shared, offline-safe pin helpers (foundation for everything)
- `src/amplifier_app_newtui/kernel/updater.py`:
  - Add `PIN_FILES: tuple[Path, ...]` — the single source of truth: repo-root `bundle.md`,
    `data/bundles/newtui.md`, `data/bundles/anchors.md` (resolved relative to
    `packaged_bundles_dir()` / repo root).
  - Add `read_pinned_sha(text: str) -> str | None` — regex `amplifier-foundation@([0-9a-f]{40})`
    (the exact pattern `test_kernel_session_config.py:641` already relies on), tag-tolerant later.
  - Add `@dataclass PinStatus { pinned: str; upstream: str | None; behind_by: int | None;
    is_stale: bool; error: str | None }` and `async def anchors_pin_status(...) -> PinStatus`:
    read `pinned` from the packaged `newtui.md`; resolve `upstream` from
    `amplifier-foundation@main` (prefer any foundation-provided pin/status helper if one exists;
    else `git ls-remote` for the boolean "different?" and the GitHub compare API
    `…/compare/<pinned>...main` → `behind_by` for the count). Any network failure → `error` set,
    `is_stale=False` (degrade, never a false alarm) — same contract as `check_bundles()`.
- Tests: `tests/test_updater_pin.py` — `read_pinned_sha` against the real packaged bundle (offline);
  `anchors_pin_status` with a monkeypatched resolver for fresh / behind / offline.

### Phase 1 — surface staleness (Acceptance: `update --check-only` + the `/doctor` bullet)
- `src/amplifier_app_newtui/main.py:_update` (`726-769`): after the "Bundle updates" table, print one
  line from `anchors_pin_status()` — e.g. `anchors pin: ● 12 commits behind upstream (pinned 93615d9)`
  or `✓ anchors pin current`, or dim `anchors pin: upstream check unavailable (offline)`. In
  `--check-only` (`:756-758`) it reports and changes nothing; keep exit-code semantics as
  reporting-only (staleness is informational, consistent with today's "up to date → 0").
- `src/amplifier_app_newtui/commands/doctor.py`: add `check_anchors_pin(status) -> CheckResult`
  (`ok=True` when current or offline; `ok=False` "anchors pin is N commits behind upstream ·
  run scripts/bump_anchors_pin.py" when stale) and include it in `run_checks` (`:201-211`).
- Tests: `tests/test_commands_doctor.py` — stale status → finding; current → ok; offline → ok (no
  false finding). Assert the `--check-only` line renders (extend `tests/test_*update*`).

### Phase 2 — the bump mechanism (Acceptance: "working mechanism")
- `scripts/bump_anchors_pin.py` (mirrors `scripts/regen_screenshot.py` as a repo maintenance script):
  resolve the new SHA (CLI arg, or `git ls-remote …@main`), string-replace the old 40-hex SHA →
  new across every entry in `updater.PIN_FILES`, then re-assert byte-identity (bundle.md ↔ newtui.md)
  and anchors lockstep before writing. Idempotent (no-op when already current). Prints the diff.
  **Does not commit** and does not touch installed wheels — repo source only.
- `.github/workflows/anchors-pin-bump.yml`: `schedule:` (weekly) + `workflow_dispatch:` → run the
  script → `uv sync --frozen` → `pytest -q` → a real headless boot (`amplifier-newtui --demo` /
  `tests/test_runtime_offline.py`) → open a PR (e.g. `peter-evans/create-pull-request`). Green suite +
  boot is the precondition for the PR; the PR is never auto-merged.

### Phase 3 — anti-drift guard (Acceptance: copies can't drift)
- Keep the two existing tests (`test_kernel_session_config.py:594-601`, `:631-643`).
- Add `test_all_pin_copies_share_one_sha` iterating `updater.PIN_FILES`, asserting `read_pinned_sha`
  returns the **same** value for all three — generalizes the pairwise lockstep and guarantees the bump
  tool touched every copy (closes the "two copies but there are three" gap).

### Phase 4 — documented policy (Acceptance: policy in DEVELOPMENT.md)
- `docs/DEVELOPMENT.md` — add an **"Anchors pin lifecycle"** subsection near lines 91-110:
  why the partial pin exists (reproducible boots; floating internals still refresh on `update`);
  the policy (Option A now → Option B when foundation tags); how to bump
  (`scripts/bump_anchors_pin.py` / the scheduled workflow); how staleness surfaces
  (`update --check-only` + `/doctor`). Update the pre-PR checklist (`:121`) to name the
  **three** pin copies, not "the packaged copy."

---

## Test & validation strategy

- **Unit (offline, the house rule — `DEVELOPMENT.md:88`):** `read_pinned_sha` on the real packaged
  bundle; `anchors_pin_status` fresh/behind/offline via a monkeypatched resolver; `check_anchors_pin`
  finding/ok/offline; `bump_anchors_pin` on a `tmp_path` copy of the three files (rewrites all, no-op
  when current, refuses to write if verification fails).
- **Anti-drift:** the three-way SHA test + the two existing lockstep/byte-identity tests must stay
  green — they are the contract the bump tool is validated against.
- **Existing regression:** `test_packaged_anchors_pointer_resolves_and_matches_the_wrapper_pin`
  (`:631-643`) already fails if the bump misses `anchors.md` — a built-in safety net.
- **Real boot (CI, in the bump workflow):** `uv sync --frozen` → `pytest -q` → headless
  `amplifier-newtui --demo` / `tests/test_runtime_offline.py` before any PR is opened.
- **Manual smoke:** `amplifier-newtui update --check-only` shows the pin line; `amplifier-newtui
  doctor` flags a deliberately-stale pin.

---

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Network needed to compute "N behind"; offline runs must not lie | Degrade to `error`/"upstream check unavailable", `is_stale=False` — never a false finding; mirror `updater.py:88-91`. Boolean "different?" via unauthenticated `git ls-remote`; the count via compare API with a token **only in CI**. |
| Bump tool forgets a pin copy → silent drift | Single `PIN_FILES` source of truth + the new three-way SHA test + existing lockstep test (`:631-643`) fail CI. |
| Auto-bump lands a broken anchors composition | CI runs full suite **+ a real boot** before opening the PR; PR is human-reviewed, never auto-merged — preserves the reproducibility gate. |
| Over-promising reproducibility | Policy text states plainly: bump refreshes only anchors *composition*; internal `behaviors/*.yaml` + module sources still float `@main` and already refresh via `update` (`bundle.md:20-23`, `updater.py:12-13`). |
| Rewriting a pin inside an installed user's wheel is meaningless | Bump is scoped to repo source (script + CI), **not** a user `amplifier-newtui` subcommand; users only get staleness *reporting*. |
| `--check-only` exit code churn breaks CI expectations | Keep staleness informational (exit unchanged); `/doctor` remains the exit-1-on-findings surface. |
| Foundation later ships a native pin/status helper | `anchors_pin_status` is one function — swap its resolver; callers unaffected. |

---

## Acceptance mapping

The issue's **Acceptance** section: *"Documented policy in DEVELOPMENT.md + working mechanism;
`update --check-only` reports pin staleness; the two bundle copies can't drift (a test already
enforces byte-identity)."* Plus the "Whichever lands" clause: *surface staleness in
`update --check-only` and `/doctor` instead of silence.*

| Acceptance bullet | How the plan satisfies it |
|---|---|
| **Documented policy in `DEVELOPMENT.md`** | Phase 4 — new "Anchors pin lifecycle" section (why the partial pin, Option A-now/B-later policy, how to bump, how staleness surfaces) near `docs/DEVELOPMENT.md:91-110`; checklist updated for the three copies. |
| **Working mechanism** (the bump) | Phase 2 — `scripts/bump_anchors_pin.py` (resolve → rewrite all `PIN_FILES` → verify) + scheduled `anchors-pin-bump.yml` that runs the suite + a real boot and opens a PR. Implements Option A's "resolve latest, rewrite both copies, run suite + boot, open PR." |
| **`update --check-only` reports pin staleness** | Phase 0 + 1 — `anchors_pin_status()` and a pin line rendered in `_update` (`main.py:726-769`), reporting-only under `--check-only` (`:756-758`). |
| **`/doctor` reports staleness** ("N commits behind") | Phase 1 — `check_anchors_pin` `CheckResult` added to `doctor.py` `run_checks` (`:201-211`): green when current/offline, numbered finding when stale. |
| **The bundle copies can't drift; a test enforces byte-identity** | Phase 3 — existing `test_packaged_bundle_matches_repo_root_bundle` (`:594-601`, byte-identity) and `test_packaged_anchors_pointer…` (`:631-643`, anchors lockstep) stay green; a new three-way SHA test over `PIN_FILES` extends the guard to **all three** live copies, and the bump tool re-verifies before writing. |

**Deviation flagged for the maintainer (intentional, not a gap):** the issue says *"two bundle
copies."* There are in fact **three** live pin sites — `bundle.md:24`,
`data/bundles/newtui.md:24`, and `data/bundles/anchors.md:13` — the third enforced by
`test_kernel_session_config.py:631-643`. The plan treats all three uniformly; a bump that touched only
two would break that existing test.
