# Lane 3 — Runtime & Composition parity audit

**Scope:** bundle/session plumbing, settings surface, runtime behaviors.
**Reference (donor):** `microsoft/amplifier-app-cli` → `amplifier_app_cli/`.
**Target:** `michaeljabbour/amplifier-app-newtui` @ e6b50cd → `src/amplifier_app_newtui/kernel/`.
**Method:** capability parity, not code parity. All paths are repo-relative to each package.
Verdicts: PARITY | PARTIAL | MISSING | NEWTUI-BETTER | N/A-BY-DESIGN.

Structural note: app-cli is a prompt_toolkit line-mode REPL; newtui is a full-screen
Textual app. Several app-cli capabilities (patch_stdout offload, stdin arbiter, printing
hooks) exist *only* to survive a shared-stdout REPL and are N/A-by-design here, because
newtui owns the screen and normalizes every hook payload into a `UIEvent` queue
(`kernel/events.py`) instead of printing.

---

## 1. Bundle composition & settings surface

| Capability | app-cli cite | newtui cite / status | Verdict | Sev | Recommendation |
|---|---|---|---|---|---|
| Three-scope deep merge (global→project→local) | `lib/settings.py:117-137` | `kernel/config.py:119-187` | PARITY | — | — |
| Session-scope settings (`sessions/<id>/settings.yaml`) merged at resolve | `lib/settings.py:78-91,1068-1088` (session tool overrides) | `docs/SETTINGS.md:17-22`; applied live via `kernel/directory_permissions.py` (`/allowed-dirs`), NOT merged in `resolve_config` | PARTIAL | Low | Fold session-scope YAML into the boot merge for full-surface (not just dir) parity, or document the narrowing |
| `bundle.active` selection | `lib/settings.py:141-150` | `kernel/config.py:246-253,693-722` | PARITY | — | — |
| `bundle.app` overlays composed onto every session | `runtime/config.py:134-136` | `kernel/config.py:256-264,297-309,828-835` | PARITY | — | — |
| `bundle.added` name→URI registry (write + list) | `lib/settings.py:235-283` | `kernel/bundle_admin.py:136-192,274-276` | PARITY | — | — |
| `bundle.added` resolved **by name at session boot** | `lib/bundle_loader/discovery.py:174-177` (user registry loaded into resolver) | `kernel/config.py:664-722` `discover_bundle` searches URIs/paths/dirs only — does NOT consult `added_bundles` | PARTIAL | Med | Feed `added_bundles(settings)` into `resolve_bundle_source` so `bundle use <added-name>` boots instead of falling back to default |
| `config.providers` overrides merged by identity (`id`\|`module`) | `lib/settings.py:357-422`; `runtime/config.py:218-241` | `kernel/config.py:364-367,432-451` + id→instance_id map `201-215,854` | PARITY | — | — |
| Provider `id`→kernel `instance_id` bridge | `runtime/config_providers.map_provider_ids_to_instance_ids` (via `runtime/config.py:239`) | `kernel/config.py:201-215` | PARITY | — | — |
| `sources.modules` source override | `lib/settings.py:488-491`; `runtime/config.py:143,159` | `kernel/config.py:312-335`; `kernel/source_admin.py:107-156` | PARITY | — | — |
| `overrides.<id>.source` (wins over sources.modules) | `lib/settings.py:1151-1161`; `runtime/config.py:140,159` | `kernel/config.py:326-331` | PARITY | — | — |
| `overrides.<id>.config` deep-merge (providers/tools/hooks + agents) | `lib/settings.py:1163-1170`; `runtime/config.py:195-216` | `kernel/config.py:346-362` (root sections; agents not walked) | PARTIAL | Low | app-cli also applies config overrides to agent-scoped declarations (`runtime/config.py:205-215`); newtui applies to root sections only |
| `sources.bundles` bundle-source override consumed at prepare | `lib/settings.py:520-523`; `runtime/config.py:162,173` (`bundle_source_overrides=`) | write/list only: `kernel/source_admin.py:229-256`; `kernel/config.build_source_resolver` (312-335) covers modules only — not fed to `prepare()` | PARTIAL | Med | Wire `sources.bundles` into the `prepare()`/discovery path so a bundle-URI redirect actually takes effect (today it is writable but inert) |
| `modules.tools` overrides + filesystem path-list union | `lib/settings.py:1054-1133` (`_merge_tool_configs` union 1115-1133); `runtime/config.py:245-256` | `kernel/config.py:62-116,369-372` (union fields 62-64) | PARITY | — | — |
| `allowed_write_paths`/`allowed_read_paths`/`denied_write_paths` union + cwd/project default | `lib/settings.py:788-1050`; `runtime/config_policies._ensure_cwd_in_write_paths` | `kernel/config.py:62-87` + `ensure_project_write_path:629-648` | PARITY | — | — |
| `config.notifications.*` field-level config (desktop show_device/preview/sound/suppress_if_focused; push server/priority/tags) → hook config | `lib/settings.py:581-677`; `runtime/config.py:264` | No settings→hook bridge. Desktop = native OSC-777 ladder via env (`docs/SETTINGS.md:151-186`); push = `bundle.md:76-79` + `AMPLIFIER_NTFY_TOPIC` | PARTIAL | Med | Common on/off is covered by the native ladder, but per-field notification config keys are silently ignored; add a `config.notifications`→hook bridge or document the env-only surface |
| `${VAR}`/`${VAR:default}` expansion (+ drop-empty fail-safe) | `runtime/config_merge.expand_env_vars`; `runtime/config.py:318` | `kernel/config.py:377-425,864` | NEWTUI-BETTER | — | newtui drops an exactly-one-unset `${VAR}` instead of expanding to `""` (avoids empty base_url) |
| `keys.env` load at startup (exported env wins) | `key_manager.KeyManager._load_keys` | `kernel/config.py:218-243,807` | PARITY | — | — |
| `context.max_tokens`/`compact_threshold`/`auto_compact` settings keys | inherited from bundle; no dedicated keys | `kernel/config.py:849`; `kernel/compaction.py:139-194` | NEWTUI-BETTER | — | first-class validated context knobs |
| `pricing.live` + on-disk pricing cache | none (in-memory estimator only, `cost_history.py`) | `kernel/cost.py:462-588` | NEWTUI-BETTER | — | 24h cached Helicone table, new-turns-only swap |
| `permissions.write_boundary` (open/guarded governance gate) | allowed/denied paths only | `kernel/directory_permissions.py`; `docs/SETTINGS.md:57` | NEWTUI-BETTER | — | app-level write boundary w/ H2 auto-degrade |
| `resume.use_active_bundle` | resume reattaches under stored bundle (implicit) | `docs/SETTINGS.md:59` | NEWTUI-BETTER | — | explicit switch + boot notice |
| `hooks.suppress` (strip printing/OSC hooks at boot) | N/A (REPL uses printing hooks) | `bundle.md:12-16`; `docs/SETTINGS.md:50`; `runtime.py:113-118` | N/A-BY-DESIGN | — | required because Textual owns the screen |
| `config.tui.startup_mode/startup_permission` | `lib/settings.py:723-744` | app has its own startup + `permissions.write_boundary` | N/A-BY-DESIGN | — | app-cli-REPL-specific seed |
| top-level `provider` (legacy active-provider block) | `lib/settings.py:340-353` | none — `config.providers` is the sole surface | N/A-BY-DESIGN | — | legacy shape; superseded |
| Malformed-settings resilience (skip, never block boot) | `lib/settings.py:135-136` (silent) | `kernel/config.py:151-198` + user-facing notice | NEWTUI-BETTER | — | surfaces the skipped scope instead of a silent warning |

## 2. Routing matrix — see dedicated section below

| Capability | app-cli cite | newtui cite / status | Verdict | Sev | Recommendation |
|---|---|---|---|---|---|
| Routing opt-in trigger | `runtime/config.py:266-268` (`get_routing_config()` non-empty) | `kernel/config.py:279-309` (`routing.enabled` bool OR `routing.matrix`) | PARTIAL | Low | overrides-only opt-in diverges (below) |
| `matrix`→`default_matrix` | `runtime/config.py:273-274` | `kernel/config.py:496-497` | PARITY | — | — |
| `overrides`→hook `overrides` | `runtime/config.py:275-276` | `kernel/config.py:498-499` | PARITY | — | — |
| `~/.amplifier/routing`→`custom_routing_dirs` (if dir exists) | `runtime/config.py:277-281`; `lib/settings.py:25-37` | `kernel/config.py:500-507` | PARITY | — | — |
| extra `overrides.hooks-routing.config` keys bridged | `runtime/config.py:282-296` (explicit merge, routing keys win) | generic `apply_module_overrides` (`kernel/config.py:346-362`) then `inject_routing_config` last → routing keys win | PARITY | — | same precedence via the generic path |
| `routing.enabled` explicit on/off switch | none | `kernel/config.py:279-294` | NEWTUI-BETTER | — | — |
| routing-instructions context + skills mounted with hook | not added at session start (config-override only) | composed whole bundle `kernel/config.py:267-272,306-308` | NEWTUI-BETTER | — | — |

## 3. Session lifecycle

| Capability | app-cli cite | newtui cite / status | Verdict | Sev | Recommendation |
|---|---|---|---|---|---|
| Session store layout (transcript/metadata, atomic + backup) | `session_store.py`; `incremental_save.py:21` | `kernel/persistence.py:73-297` | PARITY | — | shared `~/.amplifier/projects/<slug>/sessions/<id>/` layout |
| Create / resume | `runtime/session_*.py`; `session_runner.py` | `kernel/runtime.py`; `kernel/session_factory.py`; `kernel/session_manager.py` | PARITY | — | — |
| Incremental save on `tool:post` (debounced, crash recovery) | `incremental_save.py:26-190` (prio 900) | `kernel/persistence.py:437-507` (prio 900) | PARITY | — | — |
| Transcript/metadata `.backup` corruption recovery | `session_store.py` | `kernel/persistence.py:248-297` (+ `transcript_recovery_failed` notice) | PARITY | — | — |
| Fork / rewind (checkpoint-addressed, confirm-then-trim) | `ui/outcome_ledger.py`, `ui/turn_outcomes.py`; foundation `fork_session` | `kernel/rewind.py:75-213` | PARITY | — | newtui addresses by checkpoint id (never label) |
| Branch (persisted fork to new top-level id + provenance) | `commands/session.py`; `ui/command_sessions.py` | `kernel/session_manager.py:29-31` + branch fn | PARITY | — | — |
| Cost tracking + re-seed on resume | `cost_history.py:44-134` (reads `events.jsonl` `llm:response.cost_usd`; `register_contributor`) | `kernel/cost.py:227-412` (reads `ui-events.jsonl`; `restore_session_cost`) | PARITY | — | different log, same re-seed capability |
| Steering (root, one steer per step boundary) | `steering_input.py`; `stdin_arbiter.py` | `kernel/steering.py:31-97` (`provider:request`, prio 950) | PARITY | — | — |
| Per-lane steering (steer a delegate at its own boundary) | not present | `kernel/steering.py:99-123` (issue #39) | NEWTUI-BETTER | — | — |
| In-process subagent spawn (depth, inheritance, routing, cancellation) | `session_spawner.py`; `runtime/session_spawn_*.py` | `kernel/spawner.py:96-306` | PARITY | — | tool-delegate contract verbatim; depth default 2 |
| Compaction / context management | `runtime/config_behaviors.py`; context module | `kernel/compaction.py:38-194`; provider-observed vs estimated accounting | PARITY | — | newtui adds accounting-mode reporting |
| Esc-interrupt / turn-aborted boundary | `runtime/execution_interrupt.py` | `kernel/runtime.py:85-89` (`TURN_ABORTED_MARKER`) + cancellation | PARITY | — | — |
| Delete / cleanup-old / rename stored sessions | `commands/session.py`; `session_store.py` | `kernel/persistence.py:388-434`; `kernel/session_manager.py:1-52` | PARITY | — | — |
| Background/detach-to-shell model (`/background`, run in bg session copy) | `ui/layered_repl_terminal.py:142-201`; `ui/command_catalog.py:224-253` | no detach-to-shell; git-yield snapshot only (`kernel/git_yield.py`) | PARTIAL | Low | Textual can't hand the TTY to a bg shell the way the REPL does; decide if a detached-run capability is in scope or N/A |
| `stdin` arbiter (approval vs steering exclusivity) | `stdin_arbiter.py:9-21` | Textual focus + `kernel/approval.py` ApprovalBroker | N/A-BY-DESIGN | — | Textual owns input routing |
| `stdout` offload (patch_stdout event-loop-freeze fix) | `stdout_offload.py:157-205` | no prompt_toolkit; Textual compositor | N/A-BY-DESIGN | — | freeze class doesn't exist here |

## 4. Module / provider machinery

| Capability | app-cli cite | newtui cite / status | Verdict | Sev | Recommendation |
|---|---|---|---|---|---|
| Provider discovery via ModuleLoader + `get_info()` schema | `provider_loader.py`; `provider_manager.py` | `kernel/setup.py:63-143,224-246` | PARITY | — | — |
| Provider list / use / remove (priority = primary) | `provider_manager.py`; `lib/settings.py:424-477` | `kernel/setup.py:473-655` | PARITY | — | — |
| Credential env detection table | `provider_env_detect.py:9-50` | `kernel/setup.py:310-385` | PARITY | Low | app-cli gates on installed entry_points + ollama-last; newtui table-only (functionally equivalent) |
| First-run auto-init from env (headless/CI) | init flow (`main.py`) | `kernel/setup.py:388-408` (`auto_init_from_env`) | PARITY | — | — |
| `keys.env` write (atomic, chmod 600, environ update) | `key_manager.KeyManager.save_key` | `kernel/setup.py:189-218` | PARITY | — | — |
| Generic module add/remove by scope (tool/hook/provider) | `module_manager.py:51-222` | provider→`setup.py`, source→`source_admin.py`, bundle→`bundle_admin.py`; no unified `module add tool-x` | PARTIAL | Low | provider/source/bundle admin exist; a generic module-registration CLI is thinner (edit `modules.tools` by hand) |
| Update/upgrade of composed bundles/modules | `commands/update.py` (WELL_KNOWN + uv self-update) | `kernel/updater.py:75-150` (foundation `check_bundle_status`/`update_bundle`) | PARITY | — | app self-update is a hint (`self_update_hint:144-150`) since newtui isn't the umbrella tool — correct by design |
| MCP server config store (`mcp.json`, `/mcp add/remove`) | `McpConfigStore` | `kernel/mcp_config.py:23-88` | PARITY | — | — |
| Agent config (bundle-local agents, spawn overlay merge) | `agent_config.py`; `session_spawner` | six anchors agents via `bundle.md`; overlay merge `kernel/spawner.py:319-428` | PARITY | — | — |
| Effective-config introspection | `effective_config.py:42-90` | `kernel/config_ops.py:49-51`; `kernel/runtime.py:101-110`; `model.config` | PARITY | — | powers `/config` + banner |
| `/config save` (persist session changes to a scope) | `SessionConfigurator.save` (`configurator:` block) | `kernel/config_ops.py:54-89` (same `configurator` key, file-compatible) | PARITY | — | — |

## 5. Observability / telemetry

| Capability | app-cli cite | newtui cite / status | Verdict | Sev | Recommendation |
|---|---|---|---|---|---|
| Canonical hook event log (`events.jsonl`) | foundation `hooks-logging` | `hooks-logging` native (`bundle.md:15-16`, `108-109`) | PARITY | — | — |
| App UIEvent log (append-only, replay/cost/evidence) | `ui/ui_events.py` | `kernel/persistence.py:49,301-341`; `kernel/events.py` | PARITY | — | newtui splits `ui-events.jsonl` from canonical `events.jsonl` (schema safety) |
| Context-intelligence telemetry hook (multi-dest + legacy) | composed via settings | `kernel/config.py:510-605` (`inject_telemetry_config`); `docs/SETTINGS.md:83-131` | PARITY | — | mounts upstream sink, never reimplements (issue #51) |
| Notifications — ntfy push | `bundle-notify`/`hooks-notify-push` via `config.notifications` | `bundle.md:76-79`; `AMPLIFIER_NTFY_TOPIC` | PARITY | — | function parity; config surface PARTIAL (see §1) |
| Notifications — desktop OSC / bell ladder | `hooks-notify` (OSC-777/BEL to stdout) | native 3-rung ladder `docs/SETTINGS.md:164-186`; `AMPLIFIER_NOTIFY` | NEWTUI-BETTER | — | driver-safe; raw-escape hooks-notify is suppressed to protect the TUI |
| Cost history / spend accounting | `cost_history.py`; streaming-ui estimator | `kernel/cost.py` (Decimal end-to-end, unpriced floor marking) | NEWTUI-BETTER | — | — |
| Banner / effective-config UX (function) | `banners/`, `console.py`, `effective_config.format_banner_line` | `kernel/runtime.py:92-110`; UI header | PARITY | — | aesthetic differences out of scope |
| Trace collection | `trace_collector.py` | via `hooks-logging` + `ui-events.jsonl` + evidence (`kernel/evidence.py`) | PARITY | — | — |

---

## Routing matrix (the explicit question, answered head-on)

**Is the opt-in default equivalent?** Almost — with one narrow, real divergence.

- **app-cli** mounts the routing bridge whenever `app_settings.get_routing_config()` is
  **non-empty** — i.e. *any* `routing:` section at all opts in (`runtime/config.py:266-268`,
  `lib/settings.py:555-566`). A settings file with **only** `routing.overrides` (no matrix,
  no enable flag) still injects `hooks-routing` (config carries `overrides`, no
  `default_matrix`, hook falls back to its bundle default `balanced`).
- **newtui** opts in via `routing_enabled()` (`kernel/config.py:279-294`): `routing.enabled`
  (bool) **wins**, else a non-empty `routing.matrix` string. **Any other shape ⇒ off.** So an
  **overrides-only** settings block does **not** enable routing on newtui — the overrides are
  ignored until a matrix is named or `enabled: true` is set. This is the single opt-in
  divergence (PARTIAL, Low severity — edge shape; the docs at `docs/SETTINGS.md:48-51` state
  the matrix/enabled contract clearly). In exchange newtui adds an explicit `routing.enabled`
  kill/enable switch app-cli lacks.

**Which keys does newtui bridge vs app-cli?** Equivalent set, same precedence:

| Key | app-cli | newtui |
|---|---|---|
| `matrix` → `default_matrix` | `runtime/config.py:273-274` | `kernel/config.py:496-497` |
| `overrides` (per-role candidate overrides) | `runtime/config.py:275-276` | `kernel/config.py:498-499` |
| `custom_routing_dirs` (+`~/.amplifier/routing` if present) | `runtime/config.py:277-281` | `kernel/config.py:500-507` |
| extra `overrides.hooks-routing.config` keys | explicit filtered merge, routing keys win (`runtime/config.py:282-296`) | generic `apply_module_overrides` first, `inject_routing_config` last → routing keys win (`kernel/config.py:346-362,859`) |
| role fallbacks | owned by the `hooks-routing` module (both bridge only `overrides`) | same — `kernel/spawner._apply_routing:593-629` resolves per-role prefs at spawn |

**How is `hooks-routing` actually put in the plan (the real difference)?**
app-cli **appends a bare `{module: hooks-routing, config}` override** to the hooks list
(`runtime/config.py:268-306`) — it relies on the module being independently resolvable and
does **not** add routing instructions/skills at session start. newtui instead **composes the
whole `amplifier-bundle-routing-matrix` overlay** when routing is enabled
(`kernel/config.py:267-272,297-309`), which mounts `hooks-routing` with a pinned source **plus**
the routing-instructions context file and routing skills, then bridges settings onto it. This
is strictly more complete and more robust.

**Is `routing-matrix` special-cased in app-cli's discovery, and does that change effective
defaults?** It is *registered* as a well-known bundle
(`lib/bundle_loader/discovery.py:107-111`, `show_in_list=False`) purely so
`amplifier update`/`bundle list` can track/fetch it and the `routing` CLI can lazy-fetch it
into cache (`commands/routing.py:36,77-126`). It is **not** composed onto sessions by virtue of
being well-known — session composition is still gated by the `routing:` settings section. So
the special-casing does **not** change effective user defaults: routing is off until opted in
on both sides. newtui reaches the same list/fetch behavior through the **shared foundation
`BundleRegistry`** (`kernel/bundle_admin.py:206-243`, `kernel/routing_admin.py:59-105`) rather
than an app-local table, and hardcodes the identical curated URI
(`kernel/config.py:267`, matching `discovery.py:109`).

**Verdict — Routing matrix: PARITY** (bridged keys, precedence, custom dirs, and effective
opt-in default all match), with **one PARTIAL**: an *overrides-only* `routing:` block opts in on
app-cli but is inert on newtui. newtui is otherwise NEWTUI-BETTER (explicit `routing.enabled`;
composes instructions+skills, not just a hook stub).

---

## Top gaps (ranked)

1. **`sources.bundles` is writable but inert** (PARTIAL, Med) — `source add <bundle>` persists
   `sources.bundles.<name>` (`kernel/source_admin.py:229-256`) but nothing feeds it to
   `prepare()`/discovery; app-cli passes `bundle_source_overrides` through
   (`runtime/config.py:162,173`). A user redirecting a bundle URI gets silently ignored.
   → Wire `sources.bundles` into `resolve_config`'s resolver/discovery.
2. **`bundle.added` not resolved by name at boot** (PARTIAL, Med) — added bundles are
   registered and listed (`kernel/bundle_admin.py:136-192`) but `discover_bundle`
   (`kernel/config.py:664-722`) never consults the map, so `bundle use <added-name>` falls back
   to the default with a notice. app-cli loads the user registry into its resolver
   (`discovery.py:174-177`). → Consult `added_bundles(settings)` in `resolve_bundle_source`.
3. **`config.notifications.*` field-level config keys ignored** (PARTIAL, Med) — desktop
   (show_device/preview_length/sound/suppress_if_focused/min_iterations) and push
   (server/priority/tags) are configurable settings in app-cli
   (`lib/settings.py:581-677`); newtui exposes only env gates + bundle config. Common on/off is
   covered by the native ladder, but the config surface is narrower. → Add a
   `config.notifications`→hook bridge or document the env-only surface as intentional.
4. **Overrides-only routing opt-in diverges** (PARTIAL, Low) — a `routing:` block with only
   `overrides` opts in on app-cli, is inert on newtui (`kernel/config.py:279-294`). → Either
   treat a non-empty `routing.overrides` as opt-in, or document the matrix/enabled requirement
   (already noted in `docs/SETTINGS.md:48-51`).
5. **Session-scope settings not merged at boot** (PARTIAL, Low) — resumed sessions apply
   session-scope YAML only for directory permissions (`kernel/directory_permissions.py`), not
   the full settings merge app-cli performs (`lib/settings.py:1068-1088`). → Fold session scope
   into `load_merged_settings_reporting` for complete parity, or document the narrowing.

Secondary/near-parity: `overrides.<id>.config` not applied to agent-scoped sections
(PARTIAL, Low); generic `module add/remove` CLI thinner than app-cli's `ModuleManager`
(PARTIAL, Low); background/detach-to-shell absent (PARTIAL, Low — arguably N/A for Textual).

## Undetermined
- Whether foundation's `source_resolver` incidentally covers bundle-include URIs would
  partially mitigate gap #1; not confirmed from `resolve_config` alone (resolver only carries
  module-id entries — `kernel/config.py:312-335`).
- Whether newtui's `commands/` layer resolves an added-bundle name→URI *before* writing
  `bundle.active` (which would sidestep gap #2); the kernel golden path does not.

## N/A-by-design (structural, not gaps)
`stdout_offload` (prompt_toolkit freeze fix), `stdin_arbiter`, printing hooks, and
`config.tui.startup_*` all exist to survive app-cli's shared-stdout REPL. newtui owns the
Textual screen and normalizes hooks into a `UIEvent` queue, so these carry no function here.
