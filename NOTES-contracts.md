# NOTES from the contracts/scaffold agent

For the integrator and later builders. Everything below is about files I own;
no changes needed in anyone else's files yet.

## Placeholders to replace

- `amplifier_app_newtui/main.py` — prints version and exits 0. Replace the body
  of `_async_main` with real runtime selection (`--demo` flag is already wired
  through). Keep `main()` as the console-script entry and keep the single
  `asyncio.run` at the top.

## Contract decisions builders must know

1. **`kernel/events.py` is pure** (no amplifier-core imports). The hook adapter
   that registers on the coordinator and calls `normalize(event, data)` still
   needs to be written (suggested: `kernel/bridge.py` + `kernel/trackers/*`).
   `normalize` returns `None` for unconsumed events — drop those silently.
2. **Context-compaction events are NOT in the UIEvent union.**
   (`context:pre_compact/post_compact/compaction` from RESEARCH-BRIEF were not
   in the contract list I was given.) If the transcript needs compaction
   notices, extend the union + `normalize` — additive, non-breaking.
3. **`trust.resolve` assumes within-project scope.** The `within_project` /
   `outside-project` slot from the old app was intentionally left to the kernel
   governance hook (model/ has no cwd knowledge). The hook should downgrade
   out-of-project read/write to `ask` before/after calling `resolve`. Auto-mode
   `TrustDecision(classifier_gated=True)` means: run the two-stage classifier;
   the carried `decision="ask"` is the fail-closed fallback.
4. **Naming**: `model/blocks.py` has `NeedsYouEntry` (render item inside
   `NeedsYouBlock`); `model/queues.py` has `NeedsYouItem` (queue state). Same
   concept, two layers — map queue items to entries when building the block.
5. **`SteeringQueue` unifies steers and queued next-turn messages** in one
   bounded queue (`kind="steer" | "next_turn"`). `drain_steers()` at turn end
   returns leftovers that must roll forward as a follow-up turn with a notice
   (ADR-0007 steering contract).
6. **Theme registration**: themes register under `amplifier-slate|graphite|
   carbon` (use `ui.themes.theme_id`). All 14 spec tokens are Textual theme
   *variables* named exactly like the spec tokens, so TCSS uses `$bg-page`,
   `$dimmer`, etc. `tests/test_ui_themes.py` fails the build if any hex color
   appears outside `ui/themes.py` — keep it that way.
7. **Keymap**: `enter` maps to different actions per context (`submit` idle,
   `steer` running, `palette_run`, `focus_lane`, `rewind_fork`,
   `approval_confirm`, `evidence_expand`). Esc is resolved via `ESC_CHAIN`
   (spec §5 priority) — do not hand-roll if/else esc handling. On legacy
   terminals swap the advertised queue hint via
   `hint_label("queue_message", {"queue_message": "alt+enter"})` after the
   terminal probe; the `alt+enter` binding itself is always registered.
8. **Stable block ids**: mint via `BlockIdAllocator` (one per session
   transcript). Expansion/live updates are `model_copy(update=...)` keyed by
   `id`. `TurnRule.checkpoint_id` is stamped from
   `OutcomeLedger.next_checkpoint_id()` at emit time.
9. **`OutcomeLedger.trim_to(checkpoint_id)`** implements the UI side of
   confirm-then-trim — call it only AFTER the backend confirms the fork.
10. **pyproject**: amplifier-core resolves from PyPI (1.6.0);
    amplifier-foundation is git-pinned to the SAME rev as amplifier-app-cli
    (`dc010423d010da9a52e1b49808a1865666008c25`). `uv sync` verified working;
    `uv.lock` is committed-ready. `pytest-textual-snapshot` is already in the
    dev group for the Pilot/snapshot suites.
