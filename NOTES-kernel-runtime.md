# NOTES — kernel-runtime (config / session_factory / persistence / cost / rewind / bundles)

For the integrator. My ownership: `kernel/config.py`, `kernel/session_factory.py`,
`kernel/persistence.py`, `kernel/cost.py`, `kernel/rewind.py`, repo-root
`bundle.md`, `amplifier_app_newtui/data/bundles/*`, tests
`test_kernel_session*.py`, `test_kernel_persistence*.py`, `test_kernel_cost*.py`
(73 tests, all offline — no API keys, no network).

## Golden-path wiring (for main.py / RealRuntime owner)

```python
resolved = await resolve_config(bundle_or_none, progress=cb)      # prepare() runs ONCE here
initialized = await create_initialized_session(SessionRequest(
    resolved=resolved,
    approval_system=broker, display_system=display,               # kernel/approval.py owner
    spawn_capability=spawn_fn, resume_capability=resume_fn,       # kernel/spawner.py owner
    initial_transcript=transcript_or_none,                        # presence ⇒ resume
))
```

- Spawn/resume callables are registered under `session.spawn` / `session.resume`
  AFTER `create_session`, BEFORE execute (constants `SPAWN_CAPABILITY` /
  `RESUME_CAPABILITY`). **Coordination with the other kernel agent**: their
  `SessionSpawner.register(coordinator)` also registers `session.spawn`
  directly — pick ONE path at integration time. Either pass
  `spawner.spawn`-style callables through `SessionRequest`, or pass `None`
  here and call `spawner.register()` yourself right after the factory
  returns; do not do both. My factory covers `session.resume` (theirs does
  not, per their notes) — a resume callable can be built over
  `SessionStore.load` + `create_initialized_session(initial_transcript=…)`.
- Missing provider ⇒ `ProviderMountError` raised (session already cleaned up,
  message carries the doctor pointer) — hard fail per ADR-0007 res. 12.
- Missing/short tools ⇒ `initialized.degraded_notice` (str) — the UI must
  render it as the blocking transcript notice line.
- `resolved.mount_plan` **is** `prepared.mount_plan` (same object, settings
  overrides applied in place). Never copy it (RESEARCH-BRIEF risk #9).
- Append every ephemeral-hook unregister callable to
  `initialized.unregister_handles`; `initialized.cleanup()` runs them in
  reverse, then `session.cleanup()`.

## events.jsonl contract

- `SessionStore.append_event(session_id, ui_event)` writes
  `ui_event.model_dump(mode="json")` one object per line (append-only,
  best-effort, never raises). The queue-bridge owner should call this for
  every normalized UIEvent (ADR-0007 resolution 9).
- Resume cost re-seed reads records with `kind == "provider_response_usage"`:
  `cost.restore_session_cost(tracker, store.events_path(sid))`. Keep those
  field names stable if the UIEvent union evolves.
- Storage layout + project slug match amplifier-app-cli byte-for-byte
  (`~/.amplifier/projects/<slug>/sessions/<id>/`), so sessions interoperate.
- Incremental save: `IncrementalSaver(store, sid, session=session,
  base_metadata={"bundle": …, "model": …}).register(hooks)` — debounced
  `tool:post` save at priority 900, unregister handle returned.

## Cost

- Offline by default (Decimal fallback table). For live prices call
  `fetch_live_pricing()` in a background worker and assign the returned table
  to `CostTracker.pricing` when non-None. Never called implicitly.
- Turn lifecycle: `start_turn()` at `prompt:submit`, `record(usage_event)` per
  `provider_response_usage`, `end_turn()` at the rule → `TurnUsage.cost /
  tokens_down / cached_pct` feed `model.turn.TurnTelemetry`.
- The other kernel agent's `RuntimeStatusTracker(cost_fn=…)` wants
  `Callable[[ProviderResponseUsage], Decimal]` — use
  `lambda u: cost_of(u) or Decimal("0")`.

## Rewind — ⚠️ one contract concern (turn-numbering invariant)

- `RewindController(ledger, session_dir=…).fork_from(checkpoint_or_id)` forks
  via foundation `fork_session(parent_dir, turn=checkpoint.turn_id,
  handle_orphaned_tools="complete")` and trims the ledger only AFTER the
  backend confirms (confirm-then-trim). `fork_in_memory(…, messages=…,
  set_messages=context.set_messages)` is the live-context variant.
- **`Checkpoint.turn_id` MUST equal foundation's 1-indexed count of "real user
  messages" in transcript.jsonl at that rule** (role=user, no tool_call_id,
  content not wrapped in `<system-reminder>`). If the steering bridge injects
  steers as plain `role="user"` messages, they will count as turns and skew
  every later checkpoint. Either wrap steer injections in
  `<system-reminder>` tags (foundation then ignores them), or stamp turn_id
  from `amplifier_foundation.session.messages.count_turns()` at rule-emit
  time instead of the app counter. ADR-0007 resolution 4 ("steers do not
  increment turn_id") only holds if one of these is done — please decide at
  integration.

## Bundles

- Packaged default: `amplifier_app_newtui/data/bundles/newtui.md` is a
  byte-for-byte copy of repo-root `bundle.md`. If you edit one, re-copy
  (`cp bundle.md amplifier_app_newtui/data/bundles/newtui.md`); a CI equality
  check would be cheap. Discovery precedence: project `.amplifier/bundles/`
  → user `~/.amplifier/bundles/` → packaged.
- The bundle mounts loop-streaming (both event channels), context-simple,
  provider-anthropic (priority 1), tools filesystem/bash/web/search/task, and
  deliberately NO printing hooks (never mount hooks-streaming-ui).
- `resolve_config` honors settings: `bundle.active`, `bundle.app` overlays
  (composed before prepare), `sources.modules` / `overrides.<id>.source`
  (source resolver), `config.providers` / `modules.tools` /
  `overrides.<id>.config` (mount-plan merges).

## Misc

- `config.get_project_slug()` is the single slug source — do not re-derive.
- Foundation registry/cache state honors `AMPLIFIER_HOME` (tests set it to
  tmp); the doctor command should surface it.
