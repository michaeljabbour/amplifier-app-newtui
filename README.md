# Amplifier TUI

A full-screen terminal UI for [Amplifier](https://github.com/microsoft/amplifier) — modes, steering, live subagent lanes, rewind, and cost tracking — built directly on amplifier-core and amplifier-foundation.

![The TUI running its built-in demo session](docs/images/demo-session.svg)

*The screenshot is the app's own `--demo` session (fully offline). Regenerate it with `uv run python scripts/regen_screenshot.py`.*

## Install

The current distribution is a **latest-source channel** for macOS, Linux, and WSL. This
single command installs the app and opens first-launch provider setup:

```sh
bash -o pipefail -c "curl --proto '=https' --tlsv1.2 -fsSL https://raw.githubusercontent.com/michaeljabbour/amplifier-app-tui/main/scripts/install.sh | bash -s -- --launch"
```

The installer gets `uv` from Astral when needed, resolves `main` once to a full commit,
checks out that exact revision, exports its committed `uv.lock`, installs the application
under those locked runtime dependency versions, verifies the command, handles its `PATH`,
and then launches the built-in setup flow. You need Bash, Git, curl, and an internet
connection; it never uses `sudo`. There is no separate `init` step. Bash `pipefail` makes a
failed bootstrap download return a failure instead of silently running an empty shell.

This is intentionally labeled a source install: the bootstrap URL follows `main`, and the
project does not yet publish a signed binary/PyPI release or background app updater. The
source commit and Python package versions are reproducible; the Python interpreter and
platform-specific wheel selected for macOS versus Linux can still differ. See
[the install guide](docs/INSTALL.md) for exact-SHA installs, what changes on disk, update
semantics, app-only uninstall behavior, system requirements, and the remaining
release-infrastructure gap.

- **No API key yet?** `amplifier-tui --demo` runs the full UI on a scripted session — free, offline, zero credentials. When you're ready, keys come from your provider (e.g. [console.anthropic.com](https://console.anthropic.com/settings/keys) — the packaged bundle uses Anthropic by default).
- **Already have `ANTHROPIC_API_KEY` exported?** The first launch reads it directly (environment variables win over saved keys).
- **`amplifier-tui: command not found`?** Run `uv tool update-shell` and restart your terminal.
- **Something off?** `amplifier-tui doctor` checks install, PATH, platform, Python/uv versions, permissions, settings, and the same bundle/provider/credential preflight used by a real launch (exit 0 = ready). It explains each fix in plain language — the exact command or shell line to run, not just what's wrong (`--demo` never needs a key).

Credentials and settings live in `~/.amplifier/` (`keys.env`, `settings.yaml`) — the same configuration the full [Amplifier](https://github.com/microsoft/amplifier) platform uses, in both directions: if you already run Amplifier, the TUI picks up your setup with zero extra configuration.

### Optional: the full Amplifier platform

The TUI bundles everything it needs, but the `amplifier` CLI itself (bundles, sessions, agents — see the [Amplifier README](https://github.com/microsoft/amplifier)) is one command away and shares the same `~/.amplifier/` configuration:

```sh
uv tool install git+https://github.com/microsoft/amplifier
amplifier init
```

The two commands coexist on purpose: this app installs `amplifier-tui`, the platform installs
`amplifier`. That is a settled decision, not a placeholder —
[ADR-0008](docs/decisions/ADR-0008-console-script-name.md) records why (a second package
claiming `amplifier` breaks both installs and self-update) and what the only viable path to a
plain `amplifier` TUI would be.

### From a clone (development)

```sh
git clone https://github.com/michaeljabbour/amplifier-app-tui
cd amplifier-app-tui
uv sync                       # installs everything, incl. pinned amplifier-core / amplifier-foundation
uv run amplifier-tui doctor   # verify: install, PATH, settings health; exit 0 = ready
uv run amplifier-tui --demo   # try it offline
```

`uv run` works inside the clone, but for daily use prefer the tool install — it gives the app a durable environment, so bundle modules install **once and persist** instead of re-deriving on a volatile project venv at every launch (`uv tool install /path/to/amplifier-app-tui` works on a local clone too).

## Run

```sh
amplifier-tui            # launch the full-screen TUI (real session — talks to your provider)
amplifier-tui --demo     # launch with the scripted DemoRuntime (no credentials needed)
```

Sessions are stored per project directory — `cd` into your project and launch. (Inside a clone without a tool install, prefix commands with `uv run`.)

Options and subcommands:

```sh
amplifier-tui --bundle NAME_OR_URI   # pick a bundle (default: settings/bundled)
amplifier-tui doctor                 # setup checkup; exit 1 when findings exist
amplifier-tui init                   # set up a provider key in ~/.amplifier/keys.env
amplifier-tui sessions               # list stored session ids for this project
amplifier-tui resume SESSION_ID      # relaunch the TUI resuming a stored session
amplifier-tui run "PROMPT"           # execute one prompt headlessly, print the response
printf 'PROMPT\n' | amplifier-tui run # stdin one-shot
amplifier-tui run --output-format json "PROMPT"       # JSON-only stdout
amplifier-tui run --output-format json-trace "PROMPT" # JSON + normalized event trace
amplifier-tui run --output-format jsonl "PROMPT"      # live versioned event stream
amplifier-tui allowed-dirs add ../shared --project     # persistent write capability
amplifier-tui denied-dirs add .git --project           # persistent write block
amplifier-tui bundle list            # bundles from the shared registry (--all incl. deps)
amplifier-tui bundle use NAME        # set the active bundle (--global/--project/--local)
amplifier-tui routing manage         # inspect and choose a routing matrix interactively
amplifier-tui routing use NAME       # choose a matrix directly (e.g. anthropic or runpod)
amplifier-tui update --check-only    # check the mounted bundles/modules for updates
```

A *bundle* is a packaged agent configuration — provider + tools + agents + behaviors. The app ships one (`tui`), so you never need `--bundle` to get started. The `bundle` group (`list · show · use · clear · current · add · remove · update`) reads and writes the same registry and settings the reference `amplifier` CLI uses.

`routing manage` numbers every available matrix. At its `choice:` prompt, enter the
displayed number or exact matrix name to select it (`1`, `anthropic`, `runpod`); use
`v NUMBER` or `v NAME` to inspect one first, `c` to create, `w` to change settings
scope, and `d` to finish. For the rare custom name that collides with a control or
is numeric, use `select NAME`; colon-prefixed controls such as `:done` stay
unambiguous. The selected matrix applies when the next session starts.

JSON modes reserve stdout for machine-readable output; module diagnostics go to stderr.
`json` and `json-trace` emit one document, while `jsonl` flushes `session.started`,
normalized `runtime.event`, and one terminal `turn.completed` or `error` record live.
That JSONL stream is the SDK contract: the dependency-free
[Python](sdk/python/README.md) and zero-runtime-dependency
[TypeScript](sdk/typescript/README.md) clients are thin subprocess wrappers, so they cannot
drift into a second implementation of Amplifier behavior.

Inside the TUI, `/` opens the command palette: mode/plan/rewind/ledger, live-session
commands, `/allowed-dirs` and `/denied-dirs` for session-scoped path capabilities, and
`/skills · /skill <name> · /mcp · /bundle · /module` (see
[User Guide §7](docs/USER-GUIDE.md#7-commands)).
Use ↑/↓ for prompt history, and ctrl+j or ctrl+enter for a newline. Type `@` after
whitespace to autocomplete a workspace file into the composer. The mounted filesystem
tool hard-enforces write paths; the kernel keeps approval and execution path policy as
separate decisions, with `.git`, `.agents`, `.codex`, and `AGENTS.md` protected by default.
Bundle-native modes such as `careful` can add confirmation policy without weakening that
path boundary.

### Faster boots (composing fewer bundles)

Every `bundle.app` overlay composes on **every** session and runs its boot hooks, so
a large overlay list slows startup. Two levers:

```yaml
# ~/.amplifier/settings.yaml — hold heavy overlays back from boot (opt-in)
bundle:
  deferred:
    # Upstream main verified and pinned 2026-08-05; review and bump deliberately.
    - git+https://github.com/microsoft/amplifier-bundle-digital-twin-universe@d89a2e508a197d0365cebe440ccd5872b978f372
    # …any bundle.app entry you don't need on every session
```

Deferred bundles are **not** composed at boot (faster startup); load one into the
running session on demand, or pre-install a bundle's modules once so a later boot only
ever skips. The live loader also accepts a `bundle added` name, a discovered local
bundle, or a direct path/URI:

```sh
# in-session
/bundle                         # list live-loadable bundle targets
/bundle load NAME_OR_URI        # compose additive tools/hooks/agents now
/module load tool-extra [URI]   # mount one additive tool or hook module now

# out-of-session
amplifier-tui bundle warm NAME     # install a bundle's modules ahead of time
```

With no `bundle.deferred` set, boot composes exactly what it did before — deferral is
opt-in and backward-compatible. Live bundle loads are idempotent for the session and
mount additive tools/hooks/agents only. Explicit `/module load` is narrower: tool and
hook modules only. Single-slot modules — providers, orchestrator, context — and explicit
agent modules attach at the next session start. A bundle's additive agent definitions load
now, but its root instruction/context prose also remains next-session-only because
Foundation currently exposes no safe live content-composition seam. The TUI reports each
boundary instead of pretending it hot-swapped it.

### Updating / uninstalling

```sh
bash -o pipefail -c "curl --proto '=https' --tlsv1.2 -fsSL https://raw.githubusercontent.com/michaeljabbour/amplifier-app-tui/main/scripts/install.sh | bash"  # update this app from source
amplifier-tui update                         # update the mounted bundles/modules (SHA-compare + re-fetch)
uv tool upgrade amplifier                    # update the Amplifier platform (if installed)
uv tool uninstall amplifier-app-tui          # remove this app
uv tool uninstall amplifier                  # remove the Amplifier platform
git pull && uv sync                          # update a development clone instead
```

The app does not update itself in the background; re-running the source installer resolves
and installs the then-current commit. `amplifier-tui update --check-only` reports available
bundle/module updates without changing anything; `--force` runs `uv cache clean` first so
`@main` sources genuinely re-fetch.
Every `update` run also prints the app's VERIFIED installed version (read from the installed
package's own metadata, not a hardcoded string) and, if it changed since your last run —
typically because you just ran the reinstall command above — confirms what you upgraded
from and to. `amplifier-tui version` shows the same verified version on demand.

## Providers

The packaged bundle ships `provider-anthropic`, but the provider is not hard-wired — settings overlay onto the mount plan, so you can add or reconfigure providers without editing the bundle. In `~/.amplifier/settings.yaml` (user), `.amplifier/settings.yaml` (project), or `.amplifier/settings.local.yaml` (gitignored):

```yaml
config:
  providers:
    # reconfigure the bundled provider (merged by module id)
    - module: provider-anthropic
      config: { default_model: claude-sonnet-4-5 }
    # …or append another provider entirely
    - module: provider-openai
      source: git+https://github.com/microsoft/amplifier-module-provider-openai@2f44edc9564c7bfd0d79f45c62e56308f8c0d3ae
      config: { api_key: "${OPENAI_API_KEY}", priority: 10 }
```

Entries merge by module id (bundled config wins on nothing, your overlay fills the rest); a new module id is appended. `${VAR}` / `${VAR:default}` placeholders expand from the environment. For a fully different stack, point `--bundle` at your own bundle file or URI. The complete settings reference (every key, merge order, env vars) is in [docs/SETTINGS.md](docs/SETTINGS.md).

## Copying text

Drag with the mouse to select transcript text (the app highlights it), then press **ctrl+c** — the selection is copied through your OS clipboard tool (pbcopy / wl-copy / xclip) *and* OSC 52, and a `copied · N chars` notice confirms it. Terminal caveats:

- **Over SSH** OSC 52 is the only path — on iTerm2 enable *Settings → General → Selection → "Applications in terminal may access clipboard"* or remote copies land nowhere.
- **⌘C** reaches the app (and copies) on kitty-protocol terminals; elsewhere use ctrl+c inside the TUI, or hold **⌥ Option while dragging** (iTerm2) / **Shift while dragging** (most Linux terminals) to bypass the app and use your terminal's native selection + ⌘C.

## Keybindings note

The app requests progressive keyboard enhancement (kitty keyboard protocol + xterm modifyOtherKeys), so **shift+enter** queues a full next-turn message natively on kitty, WezTerm, foot, Ghostty, and recent iTerm2/Windows Terminal. On legacy terminals **alt+enter** is the fallback; it works everywhere (the composer hint adapts automatically). While a turn runs, **alt+↑** (or clicking the orange queued strip) recalls that next-turn text so Enter can steer with it immediately. Full key reference: [docs/USER-GUIDE.md §8](docs/USER-GUIDE.md#8-keys).

Auto mode does not freeze on an ordinary tool failure or a parked decision: the failed or
blocked step remains visible, the model can continue independent work, and the decision is
answerable from **ctrl+y**. Choosing **Type your own** opens a persistent bottom decision
band; its text is submitted as the answer (never as a slash command, steer, or queued turn)
and the previous rich draft is restored afterward.

## Checkpoints and undo

Amplifier cuts a checkpoint **before each prompt**, so even the first prompt or one that is
still running has a meaningful restore point. Open the bottom checkpoint picker with
**ctrl+r**, `/rewind`, a click on a turn rule, or **esc esc** when the composer is empty;
choose **code + conversation**, **conversation only**, or **code only**. Conversation
restore returns the selected prompt to the composer so you can revise and resend it.

Code restore is intentionally conservative. It covers direct root-session changes made by
the structured `write_file`, `edit_file`, `create_file`, `delete_file`, and `apply_patch`
tools. Shell commands, subagents, MCP/external tools, and manual edits are not recorded, and
a file changed since the checkpoint is skipped rather than overwritten. This is a
conflict-safe undo convenience, not a replacement for Git, and there is no redo stack. See
[User Guide §10](docs/USER-GUIDE.md#10-rewind) for scope semantics, exclusions, retention,
and partial-restore warnings.

## Layout

```
src/amplifier_app_tui/   the installable app (kernel / model / ui / commands)
tests/                      offline test suite (no credentials required)
docs/                       user guide, architecture, design spec, ADRs (docs/notes/ is local scratch, gitignored)
scripts/                    maintenance utilities (README screenshot regen)
sdk/python/                 thin typed Python client over CLI JSONL
sdk/typescript/             thin typed TypeScript client over CLI JSONL
bundle.md                   the repo's amplifier bundle (packaged copy kept byte-identical)
```

## Documentation

| Read | For |
|---|---|
| [docs/USER-GUIDE.md](docs/USER-GUIDE.md) | driving the TUI: modes, steering, approvals, lanes, rewind, keys, commands |
| [docs/SETTINGS.md](docs/SETTINGS.md) | configuration reference: every key, file locations, merge order, env vars |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | how it's built, module by module |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | working on the code: tests, goldens, layering rules, PR checklist |
| [docs/DESIGN-SPEC.md](docs/DESIGN-SPEC.md) | the behavioral spec the app is built to (authoritative) |
| [docs/BACKLOG.md](docs/BACKLOG.md) | what's next, calibrated against what's already shipped |
| [docs/design-v3-cohesive.html](docs/design-v3-cohesive.html) | executable mockup — exact strings, colors, timing, state machines |
| [docs/decisions/](docs/decisions/) | ADRs — why it's shaped this way (ADR-0007 = the architecture rules; ADR-0008 = the `amplifier-tui` command name) |
| [docs/plans/](docs/plans/) | dated implementation plans, each with a status banner (all landed to date) |

## Architecture

Four strictly-layered packages ([ADR-0007](docs/decisions/ADR-0007-tui-ground-up-architecture.md)): `ui/` and `commands/` depend on `model/`; `kernel/` is the **only** package that touches amplifier-core/foundation and never imports Textual; the UI sees the kernel exclusively through normalized `UIEvent`s. The full walk-through — boot, event pipeline, governance, subagents, persistence — is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

![tui architecture and topology](docs/diagrams/tui-architecture.png)

![tui data flow](docs/diagrams/tui-dataflow.png)

![tui and Amplifier integration](docs/diagrams/tui-amplifier-integration.png)

## Development

```sh
uv sync                # install dependencies
uv run pytest -q       # full test suite (offline)
uv run ruff check .    # lint
uv run pyright src/    # types
```

See [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) for the full workflow: running single tests, regenerating goldens, diagrams and the README screenshot, the layering rules, and the PR checklist.
