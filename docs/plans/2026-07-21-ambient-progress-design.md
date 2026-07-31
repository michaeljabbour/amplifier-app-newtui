# Feature: "Ambient Progress" — plan panel, lane live tail, collapsible delegate summary

**Date:** 2026-07-21
**Status:** ✅ Implemented — all three phases landed in PR #13 (`476c493`); lane
transcripts deepened in PR #17. Historical record.
**Branch context:** `agent/anchors-migration`

## Problem

During multi-agent turns the transcript shows a preamble ("I'll dispatch a few agents…") and
then goes quiet until the synthesis lands. The user cannot see the plan progressing or the
agents working. The CLI's stdout hooks that would provide this feedback (`hooks-todo-display`,
`hooks-streaming-ui`) are deliberately suppressed in the TUI — they write raw ANSI to stdout
and corrupt the Textual full-screen display — so the TUI must render equivalents natively from
the normalized `UIEvent` stream it already receives.

## Approved UX

Separation of concerns: **transcript = durable history, bottom chrome = live state.** Nothing
is duplicated between them.

### 1. Full-screen layout — turn in flight

Transcript shows a compact `● 3 delegates running…` line plus a dim `┆` live tail of the
focused agent's stream. The bottom strip splits horizontally: LanesPanel (left) | Plan panel
(right).

```
┌─ Amplifier · tui ──────────────────────────────────── main · $0.42 · ⏱ 1:12 ─┐
│                                                                                  │
│  ❯ [auto] Spin up a few different agents to figure out what this repo does.      │
│                                                                                  │
│  I'll dispatch a few agents in parallel to explore different aspects of          │
│  this repo.                                                                      │
│  ● 3 delegates running…                                                          │
│                                                                                  │
│  ┆ …the queue bridge normalizes delegate lifecycle events at a single            │
│  ┆ boundary, so the lanes are fed from the same UIEvent union as the             │
│  ┆ transcript — checking trackers/task_status.py next▌                           │
│                                                                                  │
├────────────────────────────────────────────┬─────────────────────────────────────┤
│ Lanes                                      │ Plan                            2/4 │
│ ✔ explorer          done 38s               │ ✔ survey purpose                    │
│ ◐ explorer          kernel/queue_bridge.py │ ✔ map source                        │
│ ◐ zen-architect ▸   ADR-0007.md      00:09 │ ▶ assess architecture               │
│                                            │ ○ synthesize                        │
├────────────────────────────────────────────┴─────────────────────────────────────┤
│ ›                                                                                │
└─ auto · anthropic ───────────────────────────────────────────────────────────────┘
```

### 2. Post-turn — collapsed summary, expandable

At turn end the delegate activity collapses into a single durable transcript line:

```
│  ● Used 3 delegates · Plan 4/4 · 1m 42s ▸                                        │
```

Click or Enter expands it (chevron flips to `▾`) to per-agent rows plus the final plan line:

```
│  ● Used 3 delegates · Plan 4/4 · 1m 42s ▾                                        │
│    ├─ ✔ explorer        38s · "full-screen Textual TUI front-end for Amplifier…" │
│    ├─ ✔ explorer        51s · "four strictly-layered packages under src/…"       │
│    └─ ✔ zen-architect   42s · "unusually disciplined, event-driven hexagonal…"   │
│    Plan  ✔ survey purpose  ✔ map source  ✔ assess architecture  ✔ synthesize     │
```

Interaction rules:

| Action | Binding |
|---|---|
| Toggle summary block | click the `●` line, or Enter when focused; `▸`/`▾` chevron signals state |
| Drill into a lane's full transcript | Enter/click on an expanded row |
| Collapse again | Enter on the header line, or Esc |
| Old turns | **every** past delegate-summary block in scrollback stays expandable, not just the latest |

## Design decisions

### D1 — Todo source: reuse the existing reducer intercept, add `plan_changed()`

The reducer already intercepts `tool_name == "todo"` ToolPre events carrying
`tool_input["todos"]` (`ui/reducer.py:971-972`, `1133-1162`), building a `TodoBlock` via
replace-in-place (`turn.todo_id`). We reuse the existing `TodoItem` / `TodoStatus` models
(`model/blocks.py:208-227`) unchanged. **New:** `ReducerHost` gains a `plan_changed()`
callback, mirroring the existing `lanes_changed()` pattern (`reducer.py:297-314`). Only
root-session todos feed the panel; sub-agent todo lists are out of scope (v2).

### D2 — Plan panel: new widget sharing a horizontal bottom strip with LanesPanel

New `ui/plan_panel.py` defining `PlanPanel` (`#plan-panel`), sharing a horizontal bottom strip
with the existing `LanesPanel` (`#lanes-panel`), which is currently full-width, docked above
the composer, auto-opens at fan-out, and toggles with ctrl-t (`ui/lanes_panel.py`,
`app.py:619-631`). Responsive ladder as width shrinks:

1. **≥ 90 cols:** full plan panel (right half of the strip).
2. **Narrower:** collapse to a `Plan 2/4` count in the strip header.
3. **Narrowest:** count only, in the `FooterBar`.

### D3 — Transcript TodoBlock retired from live appending

The live `TodoBlock` no longer appends to the transcript during a turn; the plan lives in the
panel while in flight. The **final** plan state folds into the new `DelegateSummaryBlock`
(D5), which is durable: `events.jsonl` already logs every ToolPre / AgentSpawned /
AgentCompleted (`persistence.py:239-257`), so the block is fully reconstructable on resume.

### D4 — Lane live tail: reuse `LiveTail`, feed it the focused lane when root is idle

Reuse the existing `LiveTail` widget (`#live-tail`). Child stream deltas already reach the
reducer — child `Stream*` events are diverted at `reducer.py:520-550` into lane activity, and
`StreamStatusTracker` is root-only ("child streams stay dark by design",
`stream_status.py:10-11`). **New behavior:** when the root stream is idle and lanes are
active, feed the *focused* lane's deltas to `LiveTail` in dim `┆` style — max 3 lines,
throttled at ~0.05s. The root stream always preempts. Tail content is ephemeral and discarded
on consolidate; durable content arrives via Channel B `ContentBlockEnd` (`app.py:646-660`).

### D5 — `DelegateSummaryBlock`: one durable, expandable block per fan-out

New `DelegateSummaryBlock` kind added to the `TranscriptBlock` union
(`model/blocks.py:470-492`, currently 20 kinds). It **replaces** the current per-agent
tree-line blocks appended by `_agent_spawned` / `_agent_completed` (`reducer.py:1310-1359`).
One block per fan-out, updated replace-in-place. Fields:

- `entries`: list of `(agent, state ∈ {running, done, error, cancelled}, elapsed, result snippet)`
- `plan_final`: final plan items/statuses
- `duration`: wall time for the fan-out
- `expanded: bool` — toggled by click/Enter; `▸`/`▾` chevron

Rendered as a pure `(block, width)` function registered in `ui/transcript.py` `_RENDERERS`,
so goldens cover both collapsed and expanded states.

### D6 — Invariants preserved

- **4-layer import rule** (`ui → model → kernel`): `PlanPanel` and `DelegateSummaryBlock`
  rendering live in `ui/`; block models in `model/`; no new kernel↔Textual coupling.
- **Events normalized only in `kernel/events.py`** — no new event ingestion paths; everything
  here consumes the existing `UIEvent` union.
- **Reducer acts only through `ReducerHost`** — `plan_changed()` follows the established
  callback pattern; the reducer never touches widgets.
- **`DemoRuntime` emits identical typed events** — demo beats added for plan updates, live
  tail, and summary collapse/expand, so the full flow is scriptable offline.
- **Goldens + snapshots updated in the same commit** as any rendering change (widths
  40/80/97/120; regen via `uv run python tests/goldens/regen.py`).
- **`app.py` < 500-line budget** — new wiring extracted to `app_support.py` as needed.

## Phasing

**Phase 1 — Plan panel**
`PlanPanel` + bottom-strip layout (lanes left / plan right) + todo rerouting from transcript
to panel via `plan_changed()`. Goldens + snapshot tests.

**Phase 2 — Delegate summary block**
`DelegateSummaryBlock` with collapse/expand/drill-into-lane; retire per-agent tree-line
blocks; replay-from-`events.jsonl` reconstruction. Demo beat + goldens.

**Phase 3 — Lane live tail**
Focused-lane delta feed into `LiveTail` with root preemption + throttling; demo beats;
`DESIGN-SPEC.md` updates (§2 layout, §3 block grammar, §8 lanes, §11 turn lifecycle).

## Out of scope (v2)

- Sub-agent todo merging into the plan panel
- Per-lane mini-tails (multiple simultaneous tails)
- Full lane transcript screen changes (drill-down reuses the existing lane view)
- Plan history / diffing across turns
- Todo editing from the panel

## Edge cases

| Case | Behavior |
|---|---|
| No todos this turn | Plan panel renders zero-height; strip is lanes-only; summary omits `Plan n/n` segment |
| More todos than panel height | Show current `▶` item ± neighbors + `⋮ +N more` |
| Delegate error / cancel | `✖` / `⊘` markers in summary entries (state `error` / `cancelled`) |
| Resume mid-turn | Summary block reconstructed from `events.jsonl` (ToolPre + AgentSpawned/Completed); expansion still works after `resume` |
| Approval bar active | Approval bar swap must not collide with the bottom strip — strip yields; plan count falls back to FooterBar |
| Lanes overflow the strip | `+N more` row in LanesPanel |
