# ADR-0007: amplifier-app-newtui ground-up architecture

Status: accepted · 2026-07-16
Context: rebuild of amplifier-app-cli as a new Textual full-screen TUI, 100% compliant
with `docs/DESIGN-SPEC.md` (Amplifier TUI v3 — Cohesive), built the amplifier-native way.
Grounding: `docs/RESEARCH-BRIEF.md` (synthesis of 10 deep-readers).

## Stack

- **Textual `~=8.2` (>=8.2.6)**, Python 3.12+, fully async entry.
- `amplifier-core>=1.6.0` (top-level imports ONLY), `amplifier-foundation` (git-pinned),
  rich, pydantic v2, click (thin shell), pyyaml, filelock.
- hatchling + uv, `package = true`. Entry point: `amplifier-newtui`.

## Layering (enforced; import-linter contract)

`ui/` → `model/` → `kernel/` → amplifier-core/foundation.
- `kernel/` never imports Textual. `model/` imports neither Textual nor amplifier-core.
- `ui/app.py` is a composition root with a hard budget (<500 lines). No mixin god-objects:
  widgets own their state and communicate via Textual messages.

## Event architecture

- All amplifier-core events are normalized at exactly ONE boundary: `kernel/events.py`,
  producing a typed `UIEvent` union (pydantic, frozen) with envelope
  `{event_id, session_id, parent_id, ts}`. Both channels consumed:
  Channel A `llm:stream_block_*` (live deltas), Channel B `tool:pre/post/error`,
  `content_block:*`, `orchestrator:complete` (durable records). Never reconstruct one
  from the other. Tool correlation by `tool_call_id` only.
- Hook handlers push into an `asyncio.Queue`; the Textual app consumes the queue and
  posts messages. Delta paint throttled to ~30–60Hz batches.
- Two-region transcript: durable history (pure function of `(blocks, width)`) + one
  mutable live-tail widget consolidated on `llm:stream_block_end`.

## Resolutions of RESEARCH-BRIEF open questions

1. **Mode transitions**: modes are an app-layer posture. Trust gating lives entirely in
   the app's governance hook on `tool:pre` (`HookResult` ask_user/deny) + mode-specific
   system-prompt overlay injected at `provider:request`. No session teardown, no
   provider setattr mutation.
2. **Approval detail**: kernel contract stays minimal `(prompt, options)`. Our
   ApprovalSystem carries a structured `ApprovalTicket` (unique id, command, cwd, rule,
   capability class) end-to-end — we own both ends. No global-keyed-by-prompt smuggling.
3. **Delta canonicalization**: wrapped behind `kernel/events.py` normalization; accept
   per-provider variance there (keys `delta|text|content`).
4. **Turn identity**: app-assigned monotonic `turn_id` stamped at `prompt:submit`;
   checkpoint records `{turn_id, transcript_message_index, cost_at, label}`. Steers
   rolled forward do not increment turn_id; queued messages do.
5. **Needs-you semantics**: deny-and-continue. A deferred approval ticket keeps its
   future; on `approval_timeout` it resolves to default (deny), lands in DenialLog AND
   stays in NeedsYouQueue as retro-answerable — answering later injects a next-turn
   user instruction (the mockup's "Applying decision" flow).
6. **Transcript virtualization**: v1 = widget-per-block (amplifier-tui precedent),
   lazy-mounted; perf spike test with 5k synthetic blocks in CI. Escalate to hybrid
   Line-API history only if the spike fails budget (<16ms/frame during streaming).
7. **Subagent spawn**: in-process only for v1. `kernel/spawner.py` re-attaches shared
   trackers to child coordinators, registers child cancellation, inherits
   approval/display. Lanes keyed by `session_id`/`parent_id`. Recursion depth enforced
   in the spawn capability.
8. **Scrollback/copy**: Textual text-selection + explicit copy affordance + plain
   transcript dump to stdout on exit. No alt-pager in v1.
9. **events.jsonl**: yes — append-only per-session event log (normalized UIEvents).
   Powers cost re-seed on resume, evidence links, lane replay, and contract tests.
10. **Versioning**: pin `amplifier-core>=1.6.0` like current app; lockfile committed.
    No `amplifier.modules` entry points — 100% in-process handlers.
11. **Theme switch**: colors are NEVER baked into block state; widgets render via
    Textual theme variables ($token names mirroring the spec tokens), so runtime theme
    switch is a repaint, not a rebuild.
12. **Partial mounts**: after `initialize()`, verify mounted tools/providers against
    the plan; missing provider = hard fail with doctor pointer; missing tools = start
    degraded with a blocking notice line in transcript.

## Runtimes

- `RealRuntime`: foundation 7-step lifecycle — `load_bundle` → compose overlays →
  `prepare()` once → `create_session` per conversation → register spawn/resume
  capabilities (after create_session, BEFORE execute) → ephemeral hooks → `execute`.
- `DemoRuntime` (`--demo`): scripted UIEvent sequences replicating the mockup's five
  demo turns (build, auto/blocked, plan, brainstorm, multi-agent) with deterministic
  timing. Used for snapshot/Pilot compliance tests and offline demos. Same UIEvent
  contract — the UI cannot tell the difference.

## Approvals

`kernel/approval.py` is a request broker: FIFO of `ApprovalTicket`s; inline approval
bar answers the head; ctrl-y defers head to NeedsYouQueue. Options always include
verbatim "Allow once" / "Allow always" / "Deny" (Rust fail-closed string matching).
"Allow always" scoping: file tools by parent dir, bash by 2-token prefix.

## Steering

Exactly one path: bounded `SteeringQueue` (32 items / 32KB), consumed one-per-
`provider:request` returning `HookResult(action="inject_context",
context_injection_role="user")`, root session only. Leftover steers are silently
discarded at turn end (mockup state machine: `runTurn` resets `this.steer = null` and
never replays an unconsumed steer — it must not become a turn the user never sent).

## Rewind

Confirm-then-trim: UI requests fork via foundation
`fork_session(parent_dir, turn=N, handle_orphaned_tools="complete")` /
`fork_session_in_memory` + `context.set_messages()`; the transcript trims only after
the backend confirms. Checkpoint ids stamped on turn-rule blocks at emit time.

## Testing

- Pure-logic tests for model/ and trackers (consume events directly).
- Textual Pilot + snapshot tests for every DESIGN-SPEC section, driven by DemoRuntime.
- Golden width-matrix (40/80/97/120) for the transcript renderer.
- Contract tests replaying captured events.jsonl files.
- Perf spike: 5k-block transcript streaming at budget.
