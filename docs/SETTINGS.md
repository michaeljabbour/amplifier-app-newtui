# Settings Reference

Every configuration surface the app reads, in one place. Startup settings loading lives in
`kernel/config.py` (`resolve_config()` — the single configuration golden path); live
session directory capabilities are administered in `kernel/directory_permissions.py`.

## Files and merge order

Three startup YAML scopes are merged in order (most specific wins; dicts merge recursively):

| Order | File | Scope |
|---|---|---|
| 1 | `~/.amplifier/settings.yaml` | global — you, on this machine |
| 2 | `<project>/.amplifier/settings.yaml` | project — shared, committed |
| 3 | `<project>/.amplifier/settings.local.yaml` | local — per-machine, gitignored |

A resumed session additionally reads
`~/.amplifier/projects/<slug>/sessions/<id>/settings.yaml`. `/allowed-dirs` and
`/denied-dirs` write that session scope and update mounted filesystem tools immediately.
The permission fields `allowed_write_paths`, `allowed_read_paths`, and
`denied_write_paths` are stable-unioned across scopes; other lists retain overlay-wins
semantics.

Missing or malformed files are skipped with a warning — settings can never block startup.
(`/doctor` surfaces parse failures.)

**Credentials — `~/.amplifier/keys.env`**: simple `KEY=value` lines (`#` comments allowed,
surrounding quotes stripped), loaded into the environment at startup. **Exported
environment variables always win** — a var already in your shell is never overwritten.
This is the same file `amplifier init` writes, so credentials are shared with the
Amplifier CLI.

**`${VAR}` / `${VAR:default}` placeholders** in any configuration string expand from the
environment: unset with a default → the default; unset without one → empty. Fail-safe: a
config value that is *exactly* one unset `${VAR}` with no default is dropped entirely
rather than expanded to `""` (this prevents e.g. a provider being handed an empty
`base_url`).

## Settings keys

This is the complete set of keys the app consumes:

| Key | Effect | Default | Typical scope |
|---|---|---|---|
| `bundle.active` | Which bundle to load when `--bundle` isn't passed (written by `bundle use`) | `newtui` (packaged) | global or project |
| `bundle.app` | List of overlay bundle URIs composed onto **every** session (behavior add-ons) | none | global |
| `bundle.added` | Registry of `name → URI` for discoverable bundles (written by `bundle add`) | none | global |
| `routing.matrix` | Active model-routing matrix name for delegated sub-agents. Naming a matrix opts in: the app auto-composes the `routing-matrix` overlay (which mounts `hooks-routing`) and feeds this value as its `default_matrix`. Not mounted in the base bundle (anchors parity) | none (off) | global |
| `routing.enabled` | Explicit routing on/off switch (wins over `routing.matrix`). `true` mounts `hooks-routing` even with no matrix named (uses the bundle default, `balanced`); `false` keeps it off even when a matrix is named | derived from `routing.matrix` | global or project |
| `hooks.suppress` | Extra hook module IDs stripped from the mount plan at boot, unioned with the built-in suppression list (`hooks-streaming-ui`, `hooks-todo-display`, `hooks-logging`, `hooks-notify`). A boot notice lists everything suppressed. `hooks-insight-blocks`/`hooks-inline-blocks` are no longer suppressed — they inject instructions (no stdout) and their blockquote callouts render natively with a `▌` gutter | none (built-ins always apply) | global or project |
| `routing.overrides` | Per-role candidate overrides merged onto the matrix | none | project |
| `config.providers` | Provider entries merged by identity (`id` \| `instance_id` \| `module`): reconfigure the bundled provider or append new ones (see the README's Providers section) | none | global (credentials via `${VAR}`) |
| `context.max_tokens` | Effective context window used by `context-simple` and `/context` | `300000` (inherited from the composed anchors bundle) | global or project |
| `context.compact_threshold` | `context-simple` window fraction that triggers automatic compaction (`0 < value <= 1`) | `0.8` (inherited from the composed anchors bundle) | global or project |
| `context.auto_compact` | Enable `context-simple` automatic compaction; the runtime binding also disables legacy threshold-only context modules truthfully | `true` (inherited from the composed anchors bundle) | global or project |
| `modules.tools` | Tool entries merged by identity; filesystem permission lists union across scopes | project root is implicitly writable | global / project / local / session |
| `permissions.write_boundary` | App-level write gate. `open` (default, amplifier-app-cli parity): no governance pre-flight for writes outside the project and no write-shaped shell gating — the mounted filesystem tool stays the sole write enforcement (graceful tool error, never an approval). `guarded`: outside writes are blocked pre-flight and write-shaped shell escapes are classified outside-project. Denied and protected paths are enforced in both. **Audit H2 safeguard:** `open` is only kept when a `tool-filesystem` is actually mounted to enforce it — if no filesystem write-enforcer is in the mount plan, the boundary auto-degrades to `guarded` at startup with a boot notice, so enforcement is never silently delegated to a non-existent tool. An explicit `guarded` is always honored silently | `open` (backed by a filesystem tool; else `guarded`) | global or project |
| `pricing.live` | Live Helicone pricing: fresh `~/.amplifier/pricing_cache.json` (24 h TTL) applies at startup, else a background fetch swaps rates in for **new turns only**; `false` keeps the built-in offline table | `true` | global |
| `resume.use_active_bundle` | `resume` normally reattaches a session under the **bundle it was stored with** (its module stack is part of its identity); `true` attaches under the currently active bundle instead. An explicit `--bundle` on the resume command always wins. Every divergent outcome is announced in a boot notice | `false` (honor stored) | global or project |
| `sources.modules` | Map of `module_id → source URI`: redirect where a module is fetched from | none | local (dev checkouts) |
| `overrides.<id>.source` | Per-module source redirect; wins over `sources.modules` | none | local |
| `overrides.<id>.config` | Dict deep-merged into that module's config (applied before `config.providers` / `modules.tools`, so those win) | none | project / local |
| `telemetry.*` | Configures the composed **context-intelligence-logging** behavior (module `hook-context-intelligence`): `telemetry.destinations` is the multi-destination fan-out map, `telemetry.server_url`/`api_key`/`workspace` the legacy single destination, plus dispatch tuning. A no-op unless that behavior is composed via `bundle.app`; see *Context-intelligence telemetry* below | none (local JSONL capture only) | global or project |
| `config.notifications.*` | Attention-notification config, honored via the `kernel/config` bridge: `suppress` silences the whole local ladder; `desktop.enabled` gates the OSC 777 rung (`false`→off, `true`→force any terminal); `push`/`ntfy` (`enabled`/`server`/`priority`/`tags`) feed the mounted `hooks-notify-push`. The ntfy **topic** is a secret — it lives in `keys.env` (`AMPLIFIER_NTFY_TOPIC`), never a settings scope. Env vars win over settings; written by the `notify` CLI. See *Attention notifications* below | none (env + native ladder) | global or project |

**Bundle discovery**, for `--bundle NAME` or `bundle.active`: `<project>/.amplifier/bundles/`
→ `~/.amplifier/bundles/` → the packaged `data/bundles/` — first hit wins. Names resolve as
`<name>.md`, `<name>.yaml`, or `<name>/bundle.md|bundle.yaml`. Drop a bundle file into one
of these directories and it's addressable by name. `bundle list` additionally enumerates the
shared foundation `BundleRegistry` (well-known + fetched bundles).

**MCP servers — `~/.amplifier/mcp.json`** (and `<project>/.amplifier/mcp.json`): top-level
`mcpServers` map (`name → {command, args, env}` for stdio, or `{url, type, headers}` for
http). The mounted `tool-mcp` reads these at session start and exposes each server's tools
as `mcp_<server>_<tool>`. `/mcp add|remove` edits this file (takes effect next launch).

**Native modes** are discovered from `<project>/.amplifier/modes/` → `~/.amplifier/modes/`
→ the app's packaged `data/modes/` (plan/brainstorm/careful) → composed bundles' `modes/`.
`hooks-mode` + `hooks-approval` + `tool-mode` arrive via the composed anchors bundle (same
modules, same configs). Those native hooks are idle without an active native mode; the app's
own posture/outside-project governance hook remains active and shares their approval
provider.

**Context-intelligence telemetry (`context-intelligence-logging`).** The app can fan session
events out to one or more telemetry destinations by composing the upstream
`context-intelligence-logging` behavior — the app mounts that behavior's `hook-context-intelligence`
sink and never reimplements one. Enable it with a `bundle.app` overlay, then configure destinations
under the `telemetry` settings section:

```yaml
bundle:
  app:
    # the telemetry-only layer of amplifier-bundle-context-intelligence
    - git+https://github.com/michaeljabbour/amplifier-bundle-context-intelligence@main#subdirectory=behaviors/context-intelligence-logging.yaml

telemetry:
  destinations:                      # multi-destination fan-out (upstream `destinations` map)
    team:
      url: https://ci.example.com
      api_key: ${CI_TEAM_KEY}        # secrets referenced from keys.env as ${VAR}
      include: ["*"]                 # .gitignore-style session routing (this dest gets everything)
      auth_mode: static              # static | entra
    scratch:
      url: http://localhost:8000
      exclude: ["*"]                 # routed away from this destination
  # dispatch tuning (all optional):
  dispatch_timeout: 30
  dispatch_failure_threshold: 3      # boot/turn unaffected when a server is unreachable
  # legacy single-destination form (older module builds), instead of `destinations`:
  # server_url: ${AMPLIFIER_CONTEXT_INTELLIGENCE_SERVER_URL}
  # api_key: ${AMPLIFIER_CONTEXT_INTELLIGENCE_API_KEY}
  # workspace: my-workspace
```

Semantics:

- **No destinations configured → local capture only.** With the behavior composed but no
  `telemetry.destinations` (or legacy `server_url`), the hook writes only its local per-session
  JSONL under `~/.amplifier/projects/<slug>/sessions/<id>/context-intelligence/` — no network.
- **Unreachable server never blocks the boot or a turn.** Dispatch is best-effort behind a circuit
  breaker (`dispatch_failure_threshold` consecutive failures opens it); the session runs regardless.
- **`delegate:*` events flow to it.** The behavior ships `additional_events` covering the delegate
  lifecycle (`agent_spawned`/`resumed`/`completed`/`cancelled`/`error`); `telemetry.additional_events`
  is *unioned* onto that list, never replacing it. The app's boot suppression list never strips
  `hook-context-intelligence` (add it to `hooks.suppress` yourself to opt out).
- **In-flight dispatches are drained on exit.** The hook's async `cleanup()` is awaited through the
  app's normal session teardown (`session.cleanup()`), bounded by `telemetry.close_drain_timeout`.
- **Relationship to the other logs.** This is a *third* writer, independent of the two per-session
  logs described in ARCHITECTURE §9: `hooks-logging` owns `events.jsonl` (canonical hook records)
  and the app owns `ui-events.jsonl` (normalized UIEvents). The context-intelligence hook keeps its
  own JSONL and fans out to servers; it shares no file or schema with either.

**Compaction accounting.** The runtime binds these settings directly to the mounted
context module. When that module accepts provider-observed input tokens, NewTUI forwards
exact `provider:response` usage and `/status` reports `provider-observed accounting`;
otherwise it reports `estimated accounting`. Native `context:compaction` events are
normalized into the same event stream as every other runtime event.

**Protected project paths.** The filesystem and recognized shell-target policy always
deny writes beneath `.git/`, `.agents/`, `.codex/`, and `AGENTS.md`. These are defaults,
not settings entries, so a broader allowed directory or approval cannot override them.

## Environment variables

| Variable | Effect |
|---|---|
| any `${VAR}` referenced in config | expanded into provider/tool/hook config (rules above) |
| anything in `~/.amplifier/keys.env` | injected at startup; your exported env wins |
| `TEXTUAL_DISABLE_KITTY_KEY` | force the shift+enter advertisement off (fallback hints) |
| `TERM`, `TMUX`, `TERM_PROGRAM`, `TERM_PROGRAM_VERSION`, `XTERM_VERSION`, `KITTY_WINDOW_ID`, `WEZTERM_PANE`, `GHOSTTY_RESOURCES_DIR`, `WT_SESSION` | terminal capability probe — affects only which key *hints* are advertised (bindings are unchanged) |
| `WAYLAND_DISPLAY`, `DISPLAY` | clipboard backend selection on Linux (wl-copy vs xclip) |
| `AMPLIFIER_NOTIFY` | Attention-notification ladder selector. `false`/`0`/`no`/`off` silences every rung; `bell` caps at the audible terminal bell; unset / `true` / `1` / `on` / `desktop` opens the full ladder (bell + an OSC 777 desktop notification when the window is unfocused) |
| `AMPLIFIER_TERMINAL_NOTIFICATIONS` | Desktop (OSC 777) rung gate. `off`/`0`/`false`/`never`/`none` silences the desktop notification anywhere; `force`/`on`/`1`/`true`/`always` enables it on any terminal (bypasses the render allowlist). Unset uses the built-in allowlist below |

The app's own code reads only the two attention-notification variables above
(`AMPLIFIER_NOTIFY`, `AMPLIFIER_TERMINAL_NOTIFICATIONS`); every other `AMPLIFIER_*`
variable belongs to a mounted bundle module — e.g. `tool-team-pulse` reads
`AMPLIFIER_TEAM_PULSE_URL` / `AMPLIFIER_TEAM_PULSE_KEY`, and `hooks-notify-push` sends
push notifications to the ntfy.sh topic named by `AMPLIFIER_NTFY_TOPIC` (the hook mounts
but stays inert when the variable is unset). When the `context-intelligence-logging`
behavior is composed, its `hook-context-intelligence` also reads the
`AMPLIFIER_CONTEXT_INTELLIGENCE_SERVER_URL` / `_API_KEY` / `_WORKSPACE` env vars as a fallback
for the `telemetry` settings above.

## Attention notifications

When the assistant needs you — a turn finishes after a long run, or a decision is deferred
to the needs-you queue — the app climbs a three-rung ladder instead of writing raw escapes
to the TTY (which would corrupt the full-screen Textual screen the way the suppressed
`hooks-notify` did):

1. **Bell** — Textual's driver-safe `App.bell`. Always the first rung; works on every
   terminal. Rings when a decision is deferred (always) or a turn finishes after ~10s.
2. **Desktop (OSC 777)** — an out-of-band `\x1b]777;notify;<title>;<body>` escape the
   terminal renders as a native OS notification, written through the same sanctioned
   driver path as the terminal title (never raw stdout). The ladder climbs here **only
   when the terminal window is unfocused** (you looked away), the terminal is on the
   render allowlist, and `AMPLIFIER_NOTIFY` was not capped at `bell`.
3. **Push** — off-machine ntfy push, owned by the mounted `hooks-notify-push` module
   (`AMPLIFIER_NTFY_TOPIC`), for when you are away from the machine entirely.

`AMPLIFIER_NOTIFY` gates the whole ladder (see the table above); `AMPLIFIER_NOTIFY=false`
is the historical kill switch and silences every rung. The desktop rung's terminal
**allowlist** — terminals known to render OSC notifications rather than print them as
garbage — is kitty (via `TERM`/`KITTY_WINDOW_ID`) and ghostty / iTerm2 / WezTerm / Warp
(via `TERM_PROGRAM`). `AMPLIFIER_TERMINAL_NOTIFICATIONS=force` opts any other terminal in;
`=off` opts any terminal out.

### Configuring notifications (`config.notifications.*` + the `notify` CLI)

The ladder above reads two env vars directly; the `config.notifications` settings section
lets you persist the same choices (and the ntfy push knobs) per scope. The
`kernel/config` bridge lowers them onto the same seams the runtime already uses, and
**an explicit env var always wins over a settings value** (settings only fill an unset
var). An unconfigured app is byte-identical to today.

Honored keys:

| Key | Effect | Maps to |
|---|---|---|
| `config.notifications.suppress` | `true` silences the whole local ladder (bell **and** desktop) | `AMPLIFIER_NOTIFY=off` (when unset) |
| `config.notifications.desktop.enabled` | `false` drops the desktop rung (bell still rings); `true` forces desktop on **any** terminal (bypasses the render allowlist) | `AMPLIFIER_TERMINAL_NOTIFICATIONS=off`/`force` (when unset) |
| `config.notifications.push.enabled` (alias `ntfy.enabled`) | Enable/disable off-machine ntfy push | `hooks-notify-push.enabled` (env `AMPLIFIER_NOTIFY_PUSH_ENABLED` wins) |
| `config.notifications.push.server` (alias `ntfy.server`) | ntfy server URL | `hooks-notify-push.server` (env `AMPLIFIER_NTFY_SERVER` wins) |
| `config.notifications.push.priority` | ntfy message priority (`min`\|`low`\|`default`\|`high`\|`urgent`) | `hooks-notify-push.priority` |
| `config.notifications.push.tags` | ntfy emoji tags (list or comma string) | `hooks-notify-push.tags` |

The `push`/`ntfy` blocks are aliases (ntfy is the only transport); on a field-level conflict
the `ntfy` block wins, matching amplifier-app-cli.

**The ntfy topic is a secret**, not a settings key. Public ntfy topics are world-readable, so
the push module reads the topic **only** from `AMPLIFIER_NTFY_TOPIC` (stored in
`~/.amplifier/keys.env`). `notify set topic <topic>` writes it there; it is never persisted to,
or displayed from, a settings scope.

The `notify` command group is the admin surface (same scope-file writers as `source`/`routing`;
`--global` default, `--project`, `--local`):

```
amplifier-newtui notify show                 # effective config (settings + env resolved)
amplifier-newtui notify set <key> <value>    # persist a key (unknown key -> error, exit 1)
amplifier-newtui notify enable|disable [desktop|push]   # toggle a channel (default: desktop)
amplifier-newtui notify set topic <topic>    # secret -> keys.env
amplifier-newtui notify test                 # fire a test through the REAL ladder
```

**Documented-unsupported.** amplifier-app-cli's desktop notifications go through its
OS-integration `hooks-notify` (terminal-notifier), which newtui suppresses at boot because it
writes raw OSC/BEL to stdout and corrupts the full-screen TUI. newtui's desktop rung is the
driver-safe OSC 777 path instead, which carries only a title + a bounded (240-char) body. So the
app-cli desktop sub-keys that have no OSC 777 channel are **accepted in a shared settings file but
not honored** by newtui: `desktop.sound` (OSC 777 has no sound channel), `desktop.show_device` /
`desktop.show_project` / `desktop.subtitle` / `desktop.show_preview` / `desktop.preview_length` /
`desktop.min_iterations` / `desktop.show_iteration_count`. `notify set` only accepts the keys
newtui actually honors, so it never lets you set a field that would silently do nothing.

## Quirks worth knowing

- **Theme is not persisted.** `/theme` switches at runtime only; every launch starts on
  `slate`. There is currently no settings key for it.
- **Approval timeout floor is fixed.** The app raises the kernel's 300 s approval default
  to a 1-hour floor (so approvals don't silently deny while you read); this is not
  user-configurable.
- **Pricing degrades silently.** Costs use provider-reported figures when present, else
  the live Helicone table (`pricing.live`, cached 24 h in
  `~/.amplifier/pricing_cache.json`), else the built-in offline table. A fetch failure
  never surfaces an error; rates land for new turns only, so a mid-session swap never
  changes already-recorded costs. Usage the app cannot price at all renders the footer
  and turn-rule `$` figures with a `~` prefix (the total is a floor, never a lie).
- **Silent resilience.** Malformed settings files, an unreadable `keys.env`, and
  unpriceable models are all skipped without errors — run `/doctor` (or
  `amplifier-newtui doctor`) when something seems ignored.
