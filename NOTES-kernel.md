# NOTES — kernel (approval/governance/steering/trackers/queue-bridge/spawner/display)

For the integrator. My ownership: `kernel/approval.py`, `governance_hook.py`,
`steering.py`, `display.py`, `spawner.py`, `queue_bridge.py`,
`kernel/trackers/{stream_status,runtime_status,task_status}.py` + tests
`test_kernel_approval*.py`, `test_kernel_trackers*.py`, `test_kernel_steering*.py`.

## Requests for other owners

1. **`kernel/events.py` (contracts owner):** `CONSUMED_EVENTS` — the canonical
   tuple of every raw hook name `normalize()` produces a UIEvent for — currently
   lives in `kernel/queue_bridge.py`. It arguably belongs next to `normalize()`
   in `events.py`. Move it there and re-export from `queue_bridge` if you want a
   single source; the tuple is asserted against in `test_kernel_trackers.py`.
2. **commands owner:** `tests/test_commands_builtin.py` and
   `tests/test_commands_registry.py` fail COLLECTION
   (`ModuleNotFoundError: test_commands_helpers`) — they import a helper module
   that doesn't exist / isn't on the path. Everything else in `tests/` passes
   (362 tests). Fix by adding `tests/test_commands_helpers.py` or a conftest.
3. **runtime/session-factory owner:**
   - `SessionSpawner` registers only `session.spawn`. `session.resume` is not
     implemented in kernel/spawner.py (v1 scope); wire it in the session factory
     or extend the spawner when persistence lands.
   - `RuntimeStatusTracker(cost_fn=…)` expects a per-usage-event cost function
     (signature `Callable[[ProviderResponseUsage], Decimal]`) — plug in
     `kernel/cost.py` (`estimate_cost` port) when that module exists; without it
     costs stay 0 and everything else works.
   - `GovernanceHook(classifier=…)` accepts any `AutoClassifier` protocol impl.
     The default is the deterministic `OfflineAutoClassifier` (fail-closed).
     The provider-backed reasoning-blind evaluator (app-cli
     `authorization_stage.py`) needs a mounted provider, so constructing it is
     runtime wiring: build it after `create_session` and pass it in.

## UI wiring contract (for ui/app.py)

- **Approval bar:** render `broker.head` (first non-deferred ticket);
  `broker.answer(ticket_id, choice)` on enter, `broker.defer(ticket_id)` on
  ctrl-y. `broker.add_listener` fires on every queue change. Presented options
  always start with the verbatim `Allow once / Allow always / Deny`.
- **Deferred decisions:** after `defer()`, the ticket times out to deny →
  DenialLog + the NeedsYouItem stays `pending` (retro-answerable). The UI
  answers it via `needs_you.answer(decision_id, text)`; the StepBoundaryBridge
  injects answered decisions at the next `provider:request` and marks them
  consumed ("Applying decision: …").
- **Steering:** enqueue steers with `steering.enqueue(text)` (kind="steer") and
  queued messages with kind="next_turn". At turn end the APP must call
  `steering.drain_steers()` and roll leftovers forward as a follow-up turn with
  a visible notice — the bridge deliberately does not do this.
- **Governance:** construct `GovernanceHook(root_id, mode=lambda: current_mode,
  denial_log=…, broker=…, needs_you=…)` and `register_hooks(hooks)` (priority
  1000, before display hooks). Mode is read live per tool call — mode switches
  need no re-registration.
- **Spawner:** `SessionSpawner(trackers=[stream, runtime, task, queue_bridge],
  approval_system=broker, display_system=display)` then
  `spawner.register(session.coordinator)` AFTER create_session, BEFORE execute.
  Depth default 2; refusals come back as `{"status": "error"}` tool results
  (deny-and-continue).

## Design notes / accepted trade-offs

- **Detail staging is prompt-keyed** (instance-scoped FIFO per prompt on the
  broker, popped once per `request_approval`). The kernel ask_user path only
  carries `(prompt, options)` across, so a request id cannot cross the kernel;
  concurrent identical prompts pair with details in FIFO order, which is
  correct as long as the kernel preserves hook→approval call order (it does).
  This replaces app-cli's module-global `stage_approval_detail` smuggling.
- `ApprovalBroker.request_approval` uses `asyncio.timeout` (not `wait_for`), so
  a timeout never cancels the ticket future out from under a concurrent
  `answer()`; late answers on a timed-out ticket raise `KeyError` (ticket gone).
- Trackers normalize raw payloads through `kernel/events.normalize` internally,
  so delta-key variance (`delta|text|content`), `result|tool_response`, and
  `task:agent_*` vs legacy `task:*` names are absorbed at the one boundary.
- `TaskStatusTracker` tolerates all three races: child-before-parent (depth
  retro-patch via LaneRegistry), `session:start` racing `task:agent_spawned`
  (idempotent register), and completion-before-spawn (open-then-close).
