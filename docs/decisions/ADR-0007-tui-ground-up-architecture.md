# ADR-0007: amplifier-app-tui ground-up architecture

Status: accepted · 2026-07-16
Context: rebuild of amplifier-app-cli as a new Textual full-screen TUI, 100% compliant
with `docs/DESIGN-SPEC.md` (Amplifier TUI v3 — Cohesive), built the amplifier-native way.
Grounding: `docs/RESEARCH-BRIEF.md` (synthesis of 10 deep-readers).

## Stack

- **Textual `~=8.2` (>=8.2.6)**, Python 3.12+, fully async entry.
- `amplifier-core>=1.6.0` (top-level imports ONLY), `amplifier-foundation` (git-pinned),
  rich, pydantic v2, click (thin shell), pyyaml, filelock.
- hatchling + uv, `package = true`. Entry point: `amplifier-tui`.

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

0. **Default posture (amended 2026-07-16, user directive)**: the app boots in
   `auto` mode — amplifier's natural wide scope. Auto statically allows
   read/write/test; net/spend/exec run through the classifier, whose offline
   fallback is wide (allow) except destructive shapes and unrequested
   `git push`, which deny-and-continue into the needs-you queue.
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
   the checkpoint is cut before execution and records `{turn_id, restore_turn_id,
   transcript_message_index, cost_at, label, workspace_id}`. `restore_turn_id` is the
   conversation boundary before the selected prompt; the opaque workspace id is never
   reused after restore. Steers rolled forward do not increment turn_id; queued messages do.
5. **Needs-you semantics**: deny-and-continue. Deferring a live approval immediately
   resolves that attempt to `Deny` (so the model receives a tool result and continues),
   records the denial, and parks a retro-answerable NeedsYouQueue item. Answering later
   injects context through the mockup's "Applying decision" flow. Auto-mode structured
   questions use the same nonblocking queue; interactive postures may wait for an answer.
6. **Transcript virtualization**: the widget-per-block v1 failed the 5k synthetic-block
   budget (<16ms/frame during streaming), so the planned hybrid landed: a selectable,
   action-aware archive for finalized older history plus independent widgets for the newest
   ~1000 blocks. The conversation remains untruncated while compositor work stays bounded.
7. **Subagent spawn**: in-process only for v1. `kernel/spawner.py` re-attaches shared
   trackers to child coordinators, registers child cancellation, inherits
   approval/display. Lanes keyed by `session_id`/`parent_id`. Recursion depth enforced
   in the spawn capability.
8. **Scrollback/copy**: Textual text-selection + explicit copy affordance + plain
   transcript dump to stdout on exit. No alt-pager in v1.
9. **events.jsonl**: yes — append-only per-session event log (normalized UIEvents).
   Powers cost re-seed on resume, evidence links, lane replay, conversation-restore replay
   markers, and contract tests. Workspace preimages live separately in private session data.
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
bar answers the head; ctrl-y parks the head in NeedsYouQueue and resolves this attempt to
Deny immediately. Options always include
verbatim "Allow once" / "Allow always" / "Deny" (Rust fail-closed string matching).
"Allow always" scoping: file tools by parent dir, bash by 2-token prefix.

## Steering

Exactly one path: bounded `SteeringQueue` (32 items / 32KB), consumed one-per-
`provider:request` returning `HookResult(action="inject_context",
context_injection_role="user")`, root session only. Leftover steers are silently
discarded at turn end (mockup state machine: `runTurn` resets `this.steer = null` and
never replays an unconsumed steer — it must not become a turn the user never sent).

## Rewind

Checkpoint semantics are **pre-prompt**, not post-turn: `OutcomeLedger.begin_turn` exposes
the target before execution and the reducer stamps the same id on the eventual turn rule.
The restore picker keeps the newest 100 targets and offers three explicit scopes:

- **both** — restore conversation to before the prompt and undo its/later tracked code edits;
- **conversation only** — restore context/transcript/ledger and return the prompt to the
  composer, without touching files;
- **code only** — restore tracked files without changing conversation or composer.

Conversation restore remains native and confirm-then-trim: Foundation slices messages and
`context.set_messages()` commits the boundary (including an empty list before turn one);
the ledger and transcript trim only after success. Confirming during an active turn first
requests the ordinary graceful interrupt and waits for close-out.

Core/Foundation do not preserve filesystem preimages, so workspace undo is a narrow
TUI-owned adapter rather than a second session implementation. A private per-session store
captures root-session structured-file targets (`write_file`, `edit_file`, `create_file`,
`delete_file`, `apply_patch`) durably at `tool:pre`, records after-states, and restores each
path only when a compare-and-swap check proves the current state still matches the tracked
chain. Conflicts and unsafe files are explicit per-path skips; independent paths may restore.
Shell/interpreter, subagent, MCP/external, editor, and manual changes are not tracked.
Outside-workspace/`.git` paths, symlinks, hard links, non-regular files, and files over
8 MiB are excluded, as are files with extended attributes/ACLs, unsafe ownership, or
non-default flags. One prompt is bounded to 512 snapshots / 64 MiB. A workspace-keyed lease
serializes structured turns and restores across TUI sessions; pre-prompt durability failure
rejects the unsent prompt. Private checkpoint files and restart-completable restore/branch
journals persist with the session, prune after 100, and have no redo graph. This is safe
best-effort undo, not a substitute for Git.

## Testing

- Pure-logic tests for model/ and trackers (consume events directly).
- Textual Pilot + snapshot tests for every DESIGN-SPEC section, driven by DemoRuntime.
- Golden width-matrix (40/80/97/120) for the transcript renderer.
- Contract tests replaying captured events.jsonl files.
- Perf spike: 5k-block transcript streaming at budget.
