# NOTES from the transcript agent (ui/transcript.py, ui/segments.py, ui/live_tail.py)

## Wiring contract for the integrator (app.py)

1. **Streaming lifecycle**: call `TranscriptView.set_streaming(True)` on
   `stream_block_start` and `set_streaming(False)` after consolidation.
   This is what arms the deferred-resize-reflow path (75ms trailing
   debounce; deferred while streaming; exactly one forced reflow on
   `set_streaming(False)`).
2. **LiveTail flow**: `open_stream(block_type)` on start, `feed(text)` per
   delta (paints self-throttle to 30Hz), `consolidate(block_id)` on end —
   it returns the durable `Answer` AND posts `LiveTail.Consolidated`; the
   app appends that Answer to the TranscriptView, then `set_streaming(False)`.
   Mount the LiveTail as the transcript's last sibling (bottom of the
   scroll region). Evidence refs are attached post-hoc via
   `LiveTail.attach_evidence(answer, links)` + `view.replace(...)`.
3. **Esc chain**: the keymap action `lane_unfocus` maps to
   `await view.restore_main()`; `focus_lane(lane_id, blocks)` swaps to a
   subagent's block list. Both post `LaneFocusChanged` so the footer/title
   can react. While a lane is focused, `append/replace` address the
   *visible* (subagent) list — the app should route main-lane updates only
   after restore (or buffer them).
4. **Messages to handle**: `ShowEvidence(block_id, links)` (answer click),
   `OpenRewind(checkpoint_id)` (turn-rule click), `ToolLineToggled`
   (informational — toggle already happened in place),
   `LaneFocusChanged(lane_id | None)`.
5. **Working status**: the widget self-pulses ✳/✦/✧ every 260ms; the
   per-second text (`working · Ns · ↓ X.Xk tok · N agents`) comes from the
   app replacing the block each second (`view.replace(block.model_copy(...))`).
   Remove the block at turn end with `remove_block(id)`.
6. **Blank line before user turns** comes from CSS
   (`BlockWidget.kind-user-line { margin-top: 1 }`), not from a "\n" in
   block text — don't add one upstream.

## Change requests in files I don't own

- **model/turn.py `_format_elapsed`** renders sub-10s values as `8.0s`
  (ported from amplifier-app-cli). The mockup shows integer seconds
  (`8s`) in the working line / plan suffix. If we want mockup-exact live
  telemetry, change `_format_elapsed` to `f"{seconds:.0f}s"` under 10s (or
  add a coarse mode). My golden tests assert the current model behavior
  (`8.0s`) and must be updated in the same commit if that changes
  (tests/test_ui_transcript_render.py::test_working_status_exact_and_spinner_frames).

## Contract observations (no action needed)

- DESIGN-SPEC §3 says tool hint `· click to expand`; the older
  tui-v3-cohesive.md says `· click or ctrl-o expand`. I followed
  DESIGN-SPEC (the compliance contract). If ctrl-o lands in the keymap,
  only `TOOL_EXPAND_HINT` in ui/transcript.py needs the new text.
- Theme variables in markup (`[$dim]…`) resolve only when the app's theme
  is one of the registered `amplifier-*` themes; any test app that mounts
  these widgets must `register_themes(app)` + set `app.theme` (see the
  harnesses in tests/test_ui_transcript_view.py).
- Table holdback releases a table once a paragraph break follows it
  mid-stream (blank line = table complete); a trailing table run is
  otherwise withheld until consolidation, which always carries the full
  source.
