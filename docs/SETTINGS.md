# Settings Reference

Every configuration surface the app reads, in one place. All settings loading lives in
`kernel/config.py` (`resolve_config()` — the single configuration golden path); nothing
else in the app consumes the merged settings, so this document is exhaustive.

## Files and merge order

Three YAML scopes, deep-merged in order (most specific wins; dicts merge recursively):

| Order | File | Scope |
|---|---|---|
| 1 | `~/.amplifier/settings.yaml` | global — you, on this machine |
| 2 | `<project>/.amplifier/settings.yaml` | project — shared, committed |
| 3 | `<project>/.amplifier/settings.local.yaml` | local — per-machine, gitignored |

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
| `routing.overrides` | Per-role candidate overrides merged onto the matrix | none | project |
| `config.providers` | Provider entries merged by identity (`id` \| `instance_id` \| `module`): reconfigure the bundled provider or append new ones (see the README's Providers section) | none | global (credentials via `${VAR}`) |
| `modules.tools` | Tool entries merged by identity into the mount plan, same mechanics as providers | none | project |
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
The base bundle mounts `hooks-mode` + `hooks-approval` + `tool-mode` matching the reference
`anchors` default, with **approvals off** (`policy_driven_only: true`, no active mode ⇒
nothing gated). Switch to a gating posture/mode to opt in.

## Environment variables

| Variable | Effect |
|---|---|
| any `${VAR}` referenced in config | expanded into provider/tool/hook config (rules above) |
| anything in `~/.amplifier/keys.env` | injected at startup; your exported env wins |
| `TEXTUAL_DISABLE_KITTY_KEY` | force the shift+enter advertisement off (fallback hints) |
| `TERM`, `TMUX`, `TERM_PROGRAM`, `TERM_PROGRAM_VERSION`, `XTERM_VERSION`, `KITTY_WINDOW_ID`, `WEZTERM_PANE`, `GHOSTTY_RESOURCES_DIR`, `WT_SESSION` | terminal capability probe — affects only which key *hints* are advertised (bindings are unchanged) |
| `WAYLAND_DISPLAY`, `DISPLAY` | clipboard backend selection on Linux (wl-copy vs xclip) |

No `AMPLIFIER_*` environment variables are read.

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
