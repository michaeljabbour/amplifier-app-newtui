# Decision: lane / subagent todo surfacing

**Issue:** #36 — "Decide + implement lane/subagent todo surfacing (root-only today)"
(labels: `enhancement`, `runtime-parity`)
**Status:** Decision recorded (gated deliverable). Implementation plan below is ready
for a follow-up PR.
**Evidence base:** clean `origin/main` read-only checkout at
`/Users/michaeljabbour/dev/newtui-wt/base` @ `ac854ef`; donor `amplifier-app-cli`
read-only at `/Users/michaeljabbour/dev/amplifier-app-cli`.

---

## Problem

Amplifier's `todo` tool emits a live checklist (`[{content, activeForm, status}]`).
For the **root** session that checklist is surfaced ambiently: the `PlanPanel`
bottom strip on wide terminals, with a `Plan N/M` footer fallback on narrow ones
(shipped, PR #13). See "Evidence" for the exact wiring.

For a **subagent / lane** (a background `delegate`) the same checklist surfaces
**nowhere as a checklist**. The runtime deliberately attaches the event-queue bridge
to child sessions so their usage can feed lane telemetry, but the reducer diverts
every child tool/stream event out of the root turn *before* dispatch. The `todo`
tool is only ever interpreted on the root path, so a delegate's plan never reaches
the plan renderer. The one place child work is already reconstructed — the
focused-lane transcript (PR #17) — currently renders a child `todo` call as a
meaningless generic line ("using todo"), throwing the structured items away.

This issue asks a single, bounded question: **do lane todos surface at all, and if
so where** — a lane-row badge, the focused-lane transcript, or nothing. This doc
decides that and hands over a concrete, low-risk implementation plan. It does **not**
touch the already-decided-and-shipped root surfacing.

---

## Evidence (verified file:line)

Root surfacing (the shipped baseline this decision must not disturb):

- `src/amplifier_app_newtui/ui/reducer.py:1385-1392` — `_tool_pre` routes
  `tool_name == "todo"` to `_update_todo` (root dispatch path only).
- `src/amplifier_app_newtui/ui/reducer.py:1559-1588` — `_update_todo` builds
  `TodoItem`s and calls `self._host.plan_changed(items)`. Its docstring is the
  literal source the issue cites: *"Root-session only: child ToolPre events are
  diverted before dispatch (see `_is_foreign_turn_event`)."* (lines 1566-1567).
- `src/amplifier_app_newtui/ui/reducer.py:419-422` + `app.py:682-686` —
  `plan_changed` → `app_support.apply_plan_change` → `sync_plan_surfaces` (the D2
  responsive ladder).
- `src/amplifier_app_newtui/ui/plan_panel.py:46-95` — `plan_counts`,
  `format_plan_lines`, `PLAN_MAX_ROWS`: the pure renderer for the root plan strip
  and the `Plan N/M` header.
- `src/amplifier_app_newtui/ui/app_support.py:627-660` — the wide→narrow ladder:
  panel yields below a width threshold, footer carries `plan_counts`.

The diversion (why child todos never reach the plan renderer):

- `src/amplifier_app_newtui/ui/reducer.py:703-707` — `handle()`: if
  `_is_foreign_turn_event(event)` → `_track_child_activity(event)` and **return**
  (never reaches the root `match`).
- `src/amplifier_app_newtui/ui/reducer.py:770-801` — `_is_foreign_turn_event`:
  any non-root `session_id` carrying `ToolPre/ToolPost/Stream*/OrchestratorComplete`
  is "foreign" and diverted.
- `src/amplifier_app_newtui/ui/reducer.py:817-825` — child `ToolPre` handler:
  labels the op via `_live_op_label(event.tool_name, ...)`; `"todo"` is **not** in
  `_LIVE_TOOL_VERBS` (`reducer.py:151-167`), so it degrades to the generic
  "using todo".
- `src/amplifier_app_newtui/ui/reducer.py:839-847` — child `ToolPost` appends a
  generic `ToolLine` (summary = `_live_op_label(...)`) into the lane transcript —
  i.e. today a delegate's `todo` shows up as a content-free "using todo" row.

The focused-lane transcript surface already exists (PR #17, commit `b62da3e`):

- `src/amplifier_app_newtui/ui/reducer.py:892-965` — `_seed_lane_transcript`,
  `_append_lane_block`, `lane_transcript(key)`, plus `_LANE_TRANSCRIPT_MAX_BLOCKS`
  bounding. Per-lane block lists keyed by session id, rendered when a lane is focused.
- `src/amplifier_app_newtui/model/blocks.py:189-213` — `PlanItem`
  (`pending/active/done`) and `PlanBlock` (`kind="plan"`) are first-class
  `TranscriptBlock`s.
- `src/amplifier_app_newtui/ui/transcript.py:300` + `:881` — `_render_plan` is a
  pure `(PlanBlock, width) → Line[]` renderer, already registered in the block
  dispatch table. **A `PlanBlock` dropped into a lane transcript renders as a real
  checklist today, for free.**

The lane row (the "badge" candidate surface) and its cost:

- `src/amplifier_app_newtui/model/lanes.py:57-113` — `LaneState` is a **frozen**
  pydantic model (`name/glyph/color_token/activity/elapsed/tokens/cost/state`);
  `LaneRecord` wraps it. A badge means a new field here + `for_state` + every
  construction site.
- `src/amplifier_app_newtui/ui/lanes_panel.py:70-124` — `format_lane_lines` already
  pads five columns and **elides `activity` then drops the tokens column** under
  width pressure (`_MIN_ACTIVITY_WIDTH`, lines 116-124). The row is width-starved
  already; a sixth column is the first thing to fall off.

Prior stated decision this issue re-opens:

- `docs/plans/2026-07-21-ambient-progress-design.md:87` — D1: *"Only root-session
  todos feed the panel; sub-agent todo lists are out of scope (v2)."*
- `docs/plans/2026-07-21-ambient-progress-design.md:162` — Non-goals: *"Sub-agent
  todo merging into the plan panel."*
- `docs/BACKLOG.md` §2 (lines 80-81) — the open "revisit whether lane todos surface
  at all" item this issue resolves.

Architecture rules the plan must honor:

- `docs/decisions/ADR-0007-newtui-ground-up-architecture.md:17-18` — layering
  `ui/ → model/ → kernel/`; `model/` imports neither Textual nor amplifier-core.
- `docs/decisions/ADR-0007...:114-115` and `docs/BACKLOG.md:7-9` — pure renderer
  transforms; golden/width-matrix tests in the **same** commit.

app-cli parity (donor, read-only):

- `amplifier-app-cli/amplifier_app_cli/ui/task_status.py:295-300` — app-cli
  **explicitly drops** subagent todos: `if tool_name == "todo" and
  emitting_session_id and emitting_session_id != root_session_id: return`.
- `amplifier-app-cli/amplifier_app_cli/ui/task_pane.py:13-21` —
  `format_task_pane_text` renders *"root todos and delegated sessions"* — root todos
  plus a delegated-agent **tree** (agent + short session id + status), never a
  per-subagent checklist. newtui's lanes panel is already the equivalent of that tree.

---

## Options considered

### Option A — Do nothing (keep root-only; leave lane todos unsurfaced)

Ship the status quo as an explicit decision.

- **Pro:** zero code churn; exactly matches app-cli's deliberate drop
  (`task_status.py:295-300`); respects the D1/D3 restraint call; the lanes panel +
  focused-lane transcript already communicate *what* a delegate is doing.
- **Con:** the current behavior isn't actually "nothing" — a child `todo` call
  renders as a content-free **"using todo"** line in the focused-lane transcript
  (`reducer.py:839-847`). That's a small latent bug we'd be endorsing. A delegate's
  plan is also genuinely useful when you drill into a long-running lane, and it's
  discarded.

### Option B — Ambient lane-row badge (`✓ k/n`)

Add a todo counter to each lane row in the ctrl-t lanes panel.

- **Pro:** glanceable without drilling in; ambient, matching the root `Plan N/M`
  spirit.
- **Con:** highest structural cost for the least information. `LaneState` is frozen
  (`lanes.py:57-101`) → new field + `for_state` + registry plumbing + a special-case
  exception carved into the `_is_foreign_turn_event` divert so child `todo` payloads
  reach the registry. The lane row is **already** width-starved and elides/drops
  columns (`lanes_panel.py:116-124`), so the badge is the first casualty on real
  fan-outs. A bare `k/n` with no item text is low-signal; the panel already shows a
  live `activity` string that carries more. Churns lane goldens for marginal value.

### Option C — Focused-lane transcript checklist (recommended)

When a lane is focused, render that delegate's `todo` as the **same** `PlanBlock`
checklist newtui already renders for the root — but into the lane's own focus
transcript, in place. Nothing enters the global plan strip, the root transcript, or
the lane row.

- **Pro:** shows the full checklist (content + status), not a bare count; on-demand
  (only when the user drills into that delegate), so zero ambient clutter; reuses the
  existing `PlanItem/PlanBlock` model and the `_render_plan` renderer
  (`transcript.py:300`) and the PR-#17 lane-transcript infra (`reducer.py:892-965`) —
  **no new model fields, no new renderer, no new block kind**; replaces the misleading
  "using todo" line with something meaningful; honors "the ambient strip belongs to
  the root plan" (D1/D3) while still answering the issue's "where."
- **Con:** not visible until a lane is focused (acceptable — a background delegate's
  micro-plan is drill-in detail, not headline status); needs replace-in-place per
  lane (one plan block id per lane) so repeated `todo` updates don't stack.

---

## Decision / Recommendation

**Adopt Option C, and explicitly reject B and "nothing."**

- **Do lane todos surface at all?** Yes — but only in the **focused-lane
  transcript**, as a proper checklist, and only when the user drills into that lane.
- **Where do they NOT surface?** Not in the ambient `PlanPanel`/footer (that strip
  stays the root plan's, per D1/D3), not as a lane-row badge (fragile, low-signal,
  frozen-model churn), and not in the root transcript (the whole point of the
  diversion at `reducer.py:770-801`).

Rationale in one line: reuse the surface the user *already opened to inspect this
delegate* and the renderer we *already have*, instead of minting a new ambient
channel or a new model field. This also fixes the latent "using todo" bug for free
and keeps full app-cli parity (app-cli likewise keeps subagent todos out of its
ambient pane).

> ★ **Insight:** the cheapest correct surface was already on screen. The
> focused-lane transcript exists and `_render_plan` already turns a `PlanBlock` into
> a checklist — so "surface lane todos" collapses from "new panel + new model field"
> to "route the child `todo` payload into a block type the renderer already knows."
> Principle: add a route, not a subsystem.

---

## Implementation plan (phased, concrete file paths)

All changes live in `ui/` and reuse existing `model/` types — no new model fields,
no layering violations.

### Phase 1 — Route the child `todo` payload into a per-lane `PlanBlock`

`src/amplifier_app_newtui/ui/reducer.py`

1. In `_track_child_activity` (`reducer.py:803-890`), add a `todo` guard at the top
   of the `ev.ToolPre()` case (mirroring how `_tool_pre` special-cases `todo` at
   `:1390`): if `event.tool_name == "todo"`, call a new
   `self._update_lane_todo(record, event)` and `return` (do **not** fall through to
   the generic `_live_op_label` activity / `ToolLine` path — this is what removes the
   "using todo" line).
2. Add `_update_lane_todo(self, record: LaneRecord, event: ev.ToolPre)`:
   - Parse `event.tool_input["todos"]` with the existing `_todo_status` coercion
     (`reducer.py:106`); skip empty/`list`-op payloads (same guard as
     `_update_todo:1571`).
   - Map each item to a `PlanItem` (`model/blocks.py:192`): `in_progress → "active"`,
     `completed → "done"`, else `"pending"`. (Reusing `PlanItem` means the existing
     `_render_plan` handles it with no renderer change.)
   - Build a `PlanBlock(kind="plan", items=...)` with a stable per-lane id.
3. Add per-lane plan-block tracking: a `dict[str, str]`
   `self._lane_plan_ids: dict[session_id, block_id]` initialized alongside
   `_lane_transcripts`/`_pending_briefs` (`reducer.py:581-582`). On first todo for a
   lane, append the
   `PlanBlock` via a new `_replace_or_append_lane_block(record, block, block_id)`;
   on subsequent updates, replace the existing block in that lane's list
   (scan-and-swap by id — the lists in `_lane_transcripts` are small and bounded by
   `_LANE_TRANSCRIPT_MAX_BLOCKS`).

### Phase 2 — In-place replace helper for lane transcripts

`src/amplifier_app_newtui/ui/reducer.py`

4. Generalize `_append_lane_block` (`reducer.py:925-947`) or add a sibling
   `_replace_or_append_lane_block(record, block, block_id)` that, if a block with
   `block_id` already exists in the lane's list, swaps it in place (so a plan updates,
   not stacks), else appends and re-applies the existing bound trimming. Seed the
   banner-only lane the same way `_append_lane_block` already does for
   spawn-less lanes.

### Phase 3 — No view change required; verify focus render

`src/amplifier_app_newtui/ui/transcript.py` / `app.py`

5. Confirm the focused-lane render path (the app swapping in `lane_transcript(key)`)
   already dispatches `PlanBlock` through `_render_plan` — it does
   (`transcript.py:881`), so **no view edit is expected**. If the lane-focus render
   uses a restricted block whitelist, add `"plan"` to it (grep `lane_transcript`
   consumers in `app.py`). This is the only place a view touch might be needed.

### Explicitly NOT in scope (documented non-goals for this decision)

- No change to `PlanPanel`, the footer `Plan N/M` ladder, or `plan_changed`
  (root surfacing is untouched).
- No new `LaneState` field and no lane-row badge.
- No merging of lane todos into the root plan (still a non-goal, per design doc:162).

---

## Test & validation strategy

Per ADR-0007 (pure transforms, golden tests in the same commit):

1. **Reducer unit test** — new `tests/` case: feed a child-session `ToolPre`
   `todo` event (non-root `session_id`, `create` then `update` ops) after an
   `AgentSpawned`; assert (a) `lane_transcript(child)` contains exactly one
   `PlanBlock` whose `PlanItem`s reflect the mapped statuses, (b) a second `update`
   **replaces** rather than appends (still one `PlanBlock`), (c) **no** "using todo"
   `ToolLine` is emitted for that lane, and (d) the root plan surface
   (`plan_changed`) is **never** called for the child payload (root strip untouched).
2. **Diversion guard test** — assert the child `todo` still does not mutate the root
   turn (extends the existing `_is_foreign_turn_event` coverage).
3. **Golden render** — a width-matrix golden (40/80/97/120, per
   `ADR-0007:114-115`) of a focused lane transcript containing the checklist, reusing
   the existing `_render_plan` goldens' harness so the checklist glyphs/wrap are
   pinned.
4. **Regression** — run the existing plan-panel and lanes-panel suites unchanged to
   prove root surfacing and lane-row rendering did not move.
5. **Pilot/snapshot** — a Textual Pilot beat: fan out, `ctrl-t`, focus a lane, assert
   the delegate's checklist is visible; unfocus, assert it's gone from the main view.

---

## Risks & mitigations

| Risk | Mitigation |
| --- | --- |
| Carving a `todo` exception into the child path re-introduces child noise into the root transcript. | The exception lives entirely inside `_track_child_activity` and writes only to `_lane_transcripts[child]`; it never touches `_append_content`/root turn. Test #2 guards this. |
| `PlanBlock` update stacks N copies as the delegate ticks its plan. | Per-lane `block_id` + `_replace_or_append_lane_block` replace-in-place; test #1(b) asserts a single block. |
| `PlanItem` vs `TodoItem` status vocab mismatch (`active/done` vs `in_progress/completed`). | Explicit mapping in `_update_lane_todo`; reusing `PlanItem` avoids adding a `TodoItem` transcript renderer. Covered by test #1(a). |
| Lane-focus render uses a block whitelist that omits `plan`. | Phase 3 verifies/extends it; low risk since `_render_plan` is already registered globally (`transcript.py:881`). |
| Redacted/rekeyed child session ids route the plan to the wrong lane. | Reuse the existing `lanes.get`/alias resolution already used by `_track_child_activity` (`reducer.py:811`); no new id logic. |
| Scope creep back toward an ambient badge. | Recorded as an explicit non-goal here; revisit only with real user demand. |

---

## Acceptance mapping

The issue has no explicit "Acceptance" block; the contract is derived from its
"What to do" (decide whether lane todos surface, and where) plus the `runtime-parity`
label and the "record the decision here" instruction.

| Derived acceptance | How this doc satisfies it |
| --- | --- |
| **Decide whether lane/subagent todos surface at all.** | Yes — decided in **Decision / Recommendation**: they surface, but only in the focused-lane transcript. |
| **Decide *where* (lane row badge? focused-lane transcript? nothing).** | All three candidates evaluated in **Options A/B/C** with honest trade-offs; chosen = focused-lane transcript; badge and "nothing" explicitly rejected with reasons. |
| **Ground the decision in the referenced code** (`_update_todo` root-only diversion, PR #13 root surfacing, lane surfaces). | **Evidence** cites the exact lines: diversion `reducer.py:770-801`/`703-707`, `_update_todo` docstring `reducer.py:1559-1588`, root strip `plan_panel.py`/`app_support.py:627-660`, lane surfaces `lanes.py`/`lanes_panel.py`, and the pre-existing focus transcript `reducer.py:892-965`. |
| **Record the decision (deliverable) with an implementation path** ("Decide + implement"). | Decision recorded here (gated deliverable); a phased, file-path-level **Implementation plan** is ready to lift into a follow-up PR. |
| **Do not disturb the shipped root surfacing** (PlanPanel + footer, PR #13). | Plan changes only `ui/reducer.py` (child path) + optional view whitelist; **Not in scope** and **Risks** guard `plan_changed`/`PlanPanel`/footer; regression test #4. |
| **Honor architecture rules** (ADR-0007 pure renderer, goldens same commit, layering). | Reuses `model/` `PlanItem/PlanBlock` and the pure `_render_plan`; adds no `model/` fields; **Test strategy** mandates width-matrix goldens in-commit (`ADR-0007:114-115`, `:17-18`). |
| **`runtime-parity` — is app-cli parity relevant, and preserved?** | Addressed in **Evidence** (app-cli) + **Options A/C**: app-cli deliberately drops subagent todos from its ambient task pane (`task_status.py:295-300`) and shows only root todos + a delegate tree (`task_pane.py:13-21`). Option C keeps subagent todos out of the ambient strip → parity preserved; the focus-transcript checklist is an additive newtui affordance, not a parity divergence. |

---

## Appendix — one-paragraph summary for the issue thread

Decision: **surface lane/subagent todos only in the focused-lane transcript, as a
reused `PlanBlock` checklist — not in the ambient plan strip, not as a lane-row
badge, not in the root transcript.** It reuses existing model types and the
`_render_plan` renderer (no new model fields/renderers), fixes the current
content-free "using todo" lane line, keeps the shipped root surfacing untouched, and
preserves parity with app-cli (which likewise keeps subagent todos out of its ambient
pane). Implementation is a contained `ui/reducer.py` change routing the diverted
child `todo` payload into that lane's focus transcript, with width-matrix goldens in
the same commit.
