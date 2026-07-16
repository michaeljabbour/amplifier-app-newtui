# NOTES — flows (end-to-end flow tests, tests/test_flow_*.py)

Owner: flow-tests. All 7 flow files pass (24 tests); full suite 593 passed / 1 xfailed.

## App fixes made (spec violations, cross-owner files — for the integrator's awareness)

Ground truth for all of these: `docs/design-v3-cohesive.html` + DESIGN-SPEC checkboxes.

1. **kernel/events.py** — `ApprovalDenied` gained `command` + `continuation`
   fields (and `normalize()` reads them). Spec §7: the deny line is
   `⊘ blocked · <thing> · denied by user · continuing without <thing>`;
   the event previously could not carry the continuation or the blocked
   command (mockup line 352 shows `uv run pytest`, not the approval prompt).
2. **kernel/demo.py** —
   - deny branch now emits `command=DENY_BLOCKED_CMD ("uv run pytest")` and
     `continuation=DENY_CONTINUATION` (new exported constant `DENY_BLOCKED_CMD`);
   - new `steer_source` hook polled once at every store-turn step boundary,
     emitting the `Applying steer: <text>` narration (mockup lines 326-329;
     spec §3 "steer application logged as narration" and §5 "applies at next
     step boundary; consumed steer removed"). Previously the demo never
     consumed steers at all.
3. **ui/demo_wiring.py** — wires `steer_source` to
   `SteeringQueue.consume_next_steer`; tracks a denied pytest approval and
   returns `build_denied_spec()` from `turn_spec` so the denied build turn
   closes out on the mockup's denied telemetry (7s / $0.11 / no `tests ✔`).
   `build_denied_spec` existed but was unwired.
4. **ui/reducer.py** —
   - `_finish_turn` re-resolves the close-out spec at turn end
     (`spec_lookup(prompt) or turn.spec`) so mid-turn denials change the rule
     label;
   - `_approval_denied` renders `event.command or event.prompt` and passes
     the continuation through to the `Blocked` block.
5. **ui/app.py + ui/app_support.py** — "consumed steer removed" (spec §5):
   the app now records steer message_id → ↳-echo block id
   (`app.steer_echoes`) and a `SteeringQueue` listener
   (`app_support.sync_steer_echoes`) removes the echo when the steer leaves
   the queue (applied at a boundary or drained at turn end). New helpers
   `app_support.echo_steer` / `handle_lane_focus_change` keep `ui/app.py`
   under the 500-line budget (now 494).
6. **ui/app.py `on_lane_focus_changed`** (via `app_support.handle_lane_focus_change`) —
   when an approval auto-returns the transcript from a focused lane (§7), the
   deferred `LaneFocusChanged(None)` message used to refocus the composer and
   steal the keyboard from the approval bar; it now keeps focus on the bar
   (spec §7: the bar owns the keyboard while open).
7. **ui/transcript.py `_render_context`** — the free segment of the /context
   bar was matched with `label == "free"`, but `usage_segments` emits labels
   like `"free 116k"`, so the bar rendered fully filled. Now matches the
   bucket name (first word); the bar renders `████████░░` per spec §6/§10.

## Contract observations (no change made)

- `MODE_CYCLE` is chat → build → plan → auto → brainstorm per ADR-0005 (the
  mockup's MODES array is chat/plan/brainstorm/build/auto). ADR wins per the
  build instructions; flow tests assert the ADR order. The §4 "plan → build
  handoff" is exercised via `/mode build` (direct plan→build transition),
  which fires the `plan handed to build` notice.
- DemoRuntime has no interrupt path (§11): Esc-while-running shows the
  `turn interrupted · context saved` notice but the scripted turn plays on.
  Flow tests only assert the §5 esc-chain priority, not §11 semantics.
- While a lane is focused, reducer appends turn blocks to the *visible*
  (lane) list; blocks appended between focus and restore are dropped with
  the stash swap. Not spec-pinned, but worth a look when wiring the real
  runtime (kernel-runtime owner).
