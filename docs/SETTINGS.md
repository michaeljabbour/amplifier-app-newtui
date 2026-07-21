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
| `routing.matrix` | Active model-routing matrix name for delegated sub-agents; feeds `hooks-routing` (`default_matrix`) when that hook is mounted (via an overlay). Not mounted in the base bundle | none (base) | global |
| `hooks.suppress` | Extra hook module IDs stripped from the mount plan at boot, unioned with the built-in suppression list (`hooks-streaming-ui`, `hooks-todo-display`, `hooks-insight-blocks`, `hooks-inline-blocks`, `hooks-logging`). A boot notice lists everything suppressed | none (built-ins always apply) | global or project |
| `routing.overrides` | Per-role candidate overrides merged onto the matrix | none | project |
| `config.providers` | Provider entries merged by identity (`id` \| `instance_id` \| `module`): reconfigure the bundled provider or append new ones (see the README's Providers section) | none | global (credentials via `${VAR}`) |
| `context.max_tokens` | Effective context window used by `context-simple` and `/context` | `300000` (inherited from the composed anchors bundle) | global or project |
| `context.compact_threshold` | `context-simple` window fraction that triggers automatic compaction (`0 < value <= 1`) | `0.8` (inherited from the composed anchors bundle) | global or project |
| `context.auto_compact` | Enable `context-simple` automatic compaction; the runtime binding also disables legacy threshold-only context modules truthfully | `true` (inherited from the composed anchors bundle) | global or project |
| `modules.tools` | Tool entries merged by identity; filesystem permission lists union across scopes | project root is implicitly writable | global / project / local / session |
| `permissions.write_boundary` | App-level write gate. `open` (default, amplifier-app-cli parity): no governance pre-flight for writes outside the project and no write-shaped shell gating — the mounted filesystem tool stays the sole write enforcement (graceful tool error, never an approval). `guarded`: outside writes are blocked pre-flight and write-shaped shell escapes are classified outside-project. Denied and protected paths are enforced in both | `open` | global or project |
| `pricing.live` | Live Helicone pricing: fresh `~/.amplifier/pricing_cache.json` (24 h TTL) applies at startup, else a background fetch swaps rates in for **new turns only**; `false` keeps the built-in offline table | `true` | global |
| `sources.modules` | Map of `module_id → source URI`: redirect where a module is fetched from | none | local (dev checkouts) |
| `overrides.<id>.source` | Per-module source redirect; wins over `sources.modules` | none | local |
| `overrides.<id>.config` | Dict deep-merged into that module's config (applied before `config.providers` / `modules.tools`, so those win) | none | project / local |

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

No `AMPLIFIER_*` environment variables are read by the app's own code. (Mounted bundle
modules may read their own — e.g. `tool-team-pulse` reads `AMPLIFIER_TEAM_PULSE_URL` /
`AMPLIFIER_TEAM_PULSE_KEY`.)

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
