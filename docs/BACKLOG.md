# Backlog

Calibrated against the code on `main` (post lanes A/B/C merge). Each item states
what **already exists** and what **remains**, so nobody re-builds shipped work.
Order is priority order.

Rubric: every item must stay true to the architecture rules (ADR-0007) — pure
renderer transforms, golden-tested in the same commit, kernel never imports
Textual, UI never touches amplifier-core.

---

## 0. Amplifier-native capabilities & CLI parity

Reusing amplifier's own modules/APIs (never importing amplifier-app-cli;
verified no import/dependency ties). Mount set matches the reference `anchors`
default; `tool-mcp` is the one addition beyond anchors, kept by request.

**Shipped (verified on real boots + full suite):**
- [x] **In-session commands** — `/status /model /effort /compact /clear /tools /agents /diff` over the live coordinator; palette + tests updated.
- [x] **Skills** — `tool-skills` mounted (visibility off, anchors-matching); `/skills` + `/skill <name>`.
- [x] **MCP** — `tool-mcp` mounted; `/mcp` list/add/remove over `~/.amplifier/mcp.json` (verified live: an imagegen server's 8 tools mount).
- [x] **Approvals / modes** — `hooks-mode` + `hooks-approval` + `tool-mode` mounted **off by default** (byte-identical config to anchors); shipped `plan`/`brainstorm`/`careful` mode defs + search-path injection; posture bridge. Verified: `active_mode` None on boot, `write_file`/`bash` fire `continue` (not gated).
- [x] **Routing** — spawner threads `provider_preferences`/`model_role` + settings bridge; `hooks-routing` not mounted in base (anchors parity), activates via overlay.
- [x] **bundle CLI** — `list/show/use/clear/current/add/remove/update` over the shared foundation `BundleRegistry` (Rich table); scoped settings writes.
- [x] **init (partial)** — writes a provider key to `keys.env` (interactive + `--yes`).

- [x] **team-pulse** read tools mounted in the base bundle (12 GET-only lens tools; safe when unconfigured).
- [x] **`update`** — `amplifier-newtui update` (`--check-only/--yes/--force`) over foundation `check_bundle_status`/`update_bundle`, scoped to the composed bundles (active + overlays); `--force` runs `uv cache clean`; self-update (app/platform) intentionally out of scope (printed hint). Rich table report.

- [x] **`init`** — authoritative env-var from `provider.get_info().config_fields[secret].env_var`; `config.providers` settings writer; `--model`; `--from-env` auto-init. (Remaining nice-to-have: interactive provider dashboard, provider install, launch first-run gate.)

**Remaining (Bucket B) — all nice-to-have; core parity done:**
- [ ] `source` command group (module-source overrides: add/remove/list/show) — parity with app-cli's `source` group; `bundle remove` already covers bundle un-registration.
- [ ] `routing list/use` CLI; `/config` live editing; session-manager ops (delete/rename/background); notifications; `--output-format json`.

Dropped: ingested-source deletion (the corpus "Delete original" UI) — not a
newtui feature; no amplifier tool exposes a corpus-document delete.

## 1. Accurate pricing (parity with amplifier-app-cli) — SHIPPED

**Already in `kernel/cost.py`:**
- `estimate_cost()` — Decimal port of app-cli's estimator (input/output/cache-read/cache-write rates per model).
- `FALLBACK_PRICING` — offline default table; cold starts and unit tests never touch the network.
- `cost_of()` — a provider-reported `cost_usd` is **authoritative** over the local estimate (same policy as app-cli).
- `CostTracker` — session + per-turn accounting; already feeds the footer total and the turn-rule `$` figure.
- `sum_prior_cost()` — re-seeds the session total from `events.jsonl` on `--resume`.
- `fetch_live_pricing()` — Helicone fetch, stdlib-only, 5 s timeout, never raises.

**Remaining:** *(all shipped)*
- [x] **Wire `fetch_live_pricing()` at startup.** `start_live_pricing()` runs in a daemon background thread behind settings `pricing.live` (default on); the fetched table swaps in atomically and `CostTracker` snapshots it at `start_turn` — new turns only, mid-session totals never jump retroactively.
- [x] **On-disk cache with TTL**: `~/.amplifier/pricing_cache.json`, 24 h. Fresh cache applies at startup with no fetch; stale/missing → fallback now + background fetch writes the cache. Read/write never raise.
- [x] **Never lie in the footer.** `CostTracker.unpriced` counts unpriceable usage; the footer total and turn-rule `$` figures render `~$1.23` when any usage was unpriced (footer + telemetry-label tests updated in the same change).
- [x] **Parity tests**: `tests/test_cost_parity_appcli.py` — fixtures through newtui's `estimate_cost` vs app-cli's estimator (values generated from amplifier-module-hooks-streaming-ui `cost.py`, hard-coded with provenance), matching well past the cent. **Verified live: 10/10 green.**

## 2. Live plan / TODO tracker — adapter shipped, ambient surfacing open

**Already shipped:** `PlanBlock`/`PlanItem` display pipeline **plus** the real
Amplifier `todo` tool adapter (`action` + `todos: [{content, activeForm,
status}]` → `pending → ☐ content`, `in_progress → → activeForm`,
`completed → ✓ content`; one block per turn, `create`/`update` both replace in
place). The demo `plan` shape and the real `todo` shape coexist — same
PlanBlock, two parsers. **Verified live**: a real session rendered the native
checklist with progress bar and in-place status updates.

**Remaining:**
- [ ] **Decide ambient surfacing.** The ctrl-t panel is *lanes* (background delegates), not todos. Minimal: a `3/7 tasks` counter in the footer next to mode; the block in the transcript remains the source of truth.
- [ ] Subagent todo events: currently root-session only; revisit whether lane todos surface at all.

## 3. Streaming parity — kill "the pop"

The live tail streams raw-ish text, then the consolidated Answer reformats at
stream end — the biggest perceived-quality gap. Fix: line-commit progressive
rendering. Run each **completed** line through the same `_render_answer`
pipeline as it streams (the 30 Hz throttle is the tick), keep only the trailing
partial line plain, and track fence state so a half-open ``` never renders as
prose. Golden-testable: streamed-then-committed output must equal one-shot
rendering of the same text.

## 4. Inline emphasis — mostly shipped, italic open

**Verified live** (markdown torture test): `_inline()` in `live_tail.py` parses
`**bold**` → bright+bold, `` `code` `` → teal, and `[text](url)` → teal text +
dim url, stripping markers. **Remaining:** single-asterisk `*italic*` passes
through literally (confirmed on screen). A contained transform: extend
`_ANSWER_SPAN_RE` with a `*…*` alternation that doesn't collide with `**` or
list bullets, map to the Segment `italic` flag, golden in the same commit.

## 5. Reading measure

Cap **prose** wrap at ~100 cells while code and tables keep full width. One
constant in the pure renderer; the w120 golden shows exactly what changed.

## 6. Model-side rendering contract

Inject a one-line surface hint at `provider:request`: *"terminal, ~N cols;
markdown subset: no images, tables ≤4 columns, prefer fenced code with language
tags, short paragraphs."* Zero renderer code; prevents the pathological cases
(wide tables, deep nesting) instead of rendering them badly.

## 7. Smaller wins

- [ ] `[text](url)` → **half done**: teal text + dim url shipped in `_inline()`. Remaining: real OSC 8 hyperlinks and bare-URL collapsing.
- [ ] `- [x]` / `- [ ]` → ✓/☐ glyphs, rhyming with PlanBlock.
- [ ] Blockquotes as a dim left bar.
- [ ] Click-a-fence-to-copy (the ToolLine click precedent exists; `/copy` grabs the whole answer today).

## Non-goals

- **Syntax highlighting in answers.** Doable, but fights the restraint
  aesthetic and churns goldens forever; calm teal verbatim reads better in a
  transcript than rainbow soup.
