# Backlog

Calibrated against the code on `main` (post lanes A/B/C merge). Each item states
what **already exists** and what **remains**, so nobody re-builds shipped work.
Order is priority order.

Rubric: every item must stay true to the architecture rules (ADR-0007) — pure
renderer transforms, golden-tested in the same commit, kernel never imports
Textual, UI never touches amplifier-core.

---

## 1. Accurate pricing (parity with amplifier-app-cli) — mostly shipped

**Already in `kernel/cost.py`:**
- `estimate_cost()` — Decimal port of app-cli's estimator (input/output/cache-read/cache-write rates per model).
- `FALLBACK_PRICING` — offline default table; cold starts and unit tests never touch the network.
- `cost_of()` — a provider-reported `cost_usd` is **authoritative** over the local estimate (same policy as app-cli).
- `CostTracker` — session + per-turn accounting; already feeds the footer total and the turn-rule `$` figure.
- `sum_prior_cost()` — re-seeds the session total from `events.jsonl` on `--resume`.
- `fetch_live_pricing()` — Helicone fetch, stdlib-only, 5 s timeout, never raises.

**Remaining:**
- [ ] **Wire `fetch_live_pricing()` at startup.** It is defined but never called. Run it in a background thread behind a settings key (e.g. `pricing.live`), swap the table in when it lands — mid-session totals must not jump retroactively (apply to new turns only).
- [ ] **On-disk cache with TTL** for the fetched table (like app-cli), so offline sessions still use recent rates instead of the static fallback.
- [ ] **Never lie in the footer.** Models absent from the table with no provider `cost_usd` currently record `$0` silently. Track an `unpriced` count and mark the total (e.g. `~$1.23`) when any usage was unpriceable.
- [ ] **Parity tests**: same usage fixtures through newtui's `estimate_cost` and app-cli's — totals must match to the cent.

## 2. Live plan / TODO tracker — renderer shipped, adapter missing

**Already shipped:** `PlanBlock`/`PlanItem` (model → reducer → transcript) with
checkbox states, in-place updates, live telemetry suffix, and the read-only
plan-mode variant. The full display pipeline works — for the **demo `plan` tool
shape** (`title` / `steps[{step,status}]`).

**Remaining:**
- [ ] **Adapt the reducer to the real Amplifier `todo` tool.** Its payload is `action` + `todos: [{content, activeForm, status}]`. Today `reducer.py` maps `todo` to a one-line `updated plan` ToolLine. Translate instead: `pending → ☐ content`, `in_progress → → activeForm`, `completed → ✓ content`; one PlanBlock per session, updated in place (`update`/`create` both replace).
- [ ] **Keep the two shapes coexisting** — the demo script and goldens use `plan`; real sessions use `todo`. Same PlanBlock, two parsers.
- [ ] **Decide ambient surfacing.** The ctrl-t panel is *lanes* (background delegates), not todos. Minimal: a `3/7 tasks` counter in the footer next to mode; the block in the transcript remains the source of truth.
- [ ] Subagent todo events: scope to root session first; revisit whether lane todos surface at all.

## 3. Streaming parity — kill "the pop"

The live tail streams raw-ish text, then the consolidated Answer reformats at
stream end — the biggest perceived-quality gap. Fix: line-commit progressive
rendering. Run each **completed** line through the same `_render_answer`
pipeline as it streams (the 30 Hz throttle is the tick), keep only the trailing
partial line plain, and track fence state so a half-open ``` never renders as
prose. Golden-testable: streamed-then-committed output must equal one-shot
rendering of the same text.

## 4. Inline emphasis — verify, then finish

**Verify first**: are `**bold**` / `*italic*` / `` `code` `` parsed into Segment
flags, or passed through literally? (The Segment model already carries
bold/italic/style tokens; the goldens contain no literal markers, which may just
be demo content.) If unparsed, it's a contained transform: inline code → teal
(rhyming with fences), bold → bright, strip the markers. Determines whether
this is a gap or already done.

## 5. Reading measure

Cap **prose** wrap at ~100 cells while code and tables keep full width. One
constant in the pure renderer; the w120 golden shows exactly what changed.

## 6. Model-side rendering contract

Inject a one-line surface hint at `provider:request`: *"terminal, ~N cols;
markdown subset: no images, tables ≤4 columns, prefer fenced code with language
tags, short paragraphs."* Zero renderer code; prevents the pathological cases
(wide tables, deep nesting) instead of rendering them badly.

## 7. Smaller wins

- [ ] `[text](url)` → styled text with OSC 8 hyperlinks; bare URLs collapsed.
- [ ] `- [x]` / `- [ ]` → ✓/☐ glyphs, rhyming with PlanBlock.
- [ ] Blockquotes as a dim left bar.
- [ ] Click-a-fence-to-copy (the ToolLine click precedent exists; `/copy` grabs the whole answer today).

## Non-goals

- **Syntax highlighting in answers.** Doable, but fights the restraint
  aesthetic and churns goldens forever; calm teal verbatim reads better in a
  transcript than rainbow soup.
