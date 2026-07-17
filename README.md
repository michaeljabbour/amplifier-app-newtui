# Amplifier App — New TUI

Ground-up full-screen Textual TUI rebuild of amplifier-app-cli, 100% compliant with docs/DESIGN-SPEC.md (Amplifier TUI v3 — Cohesive).

## Run

```sh
uv run amplifier-newtui            # launch the full-screen TUI (real session)
uv run amplifier-newtui --demo     # launch with the scripted DemoRuntime (no credentials needed)
```

Options and subcommands:

```sh
uv run amplifier-newtui --bundle NAME_OR_URI   # pick a bundle (default: settings/bundled)
uv run amplifier-newtui doctor                 # setup checkup; exit 1 when findings exist
uv run amplifier-newtui sessions               # list stored session ids for this project
uv run amplifier-newtui resume SESSION_ID      # relaunch the TUI resuming a stored session
uv run amplifier-newtui run "PROMPT"           # execute one prompt headlessly, print the response
```

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
      source: git+https://github.com/microsoft/amplifier-module-provider-openai@main
      config: { api_key: "${OPENAI_API_KEY}", priority: 10 }
```

Entries merge by module id (bundled config wins on nothing, your overlay fills the rest); a new module id is appended. `${VAR}` / `${VAR:default}` placeholders expand from the environment. For a fully different stack, point `--bundle` at your own bundle file or URI.

## Copying text

Drag with the mouse to select transcript text (the app highlights it), then press **ctrl+c** — the selection is copied via OSC 52 and a `copied · N chars` notice confirms it. Two terminal caveats:

- **iTerm2 blocks clipboard writes by default**: enable *Settings → General → Selection → "Applications in terminal may access clipboard"*, or the copy silently lands nowhere.
- **⌘C never reaches a terminal app** on macOS — use ctrl+c inside the TUI, or hold **⌥ Option while dragging** (iTerm2) / **Shift while dragging** (most Linux terminals) to bypass the app entirely and use your terminal's native selection + ⌘C.

## Keybindings note

The app requests progressive keyboard enhancement (kitty keyboard protocol + xterm modifyOtherKeys), so **shift+enter** queues a full next-turn message natively on kitty, WezTerm, foot, Ghostty, and recent iTerm2/Windows Terminal. On legacy terminals **alt+enter** is the fallback; it works everywhere (the composer hint adapts automatically). See the full keymap in [docs/tui-v3-cohesive.md](docs/tui-v3-cohesive.md).

## Layout

```
src/amplifier_app_newtui/   the installable app (kernel / model / ui / commands)
tests/                      offline test suite (no credentials required)
docs/                       design spec, executable mockup, ADRs (docs/notes/ is local scratch, gitignored)
bundle.md                   the repo's amplifier bundle (packaged copy kept byte-identical)
```

## Architecture

Four strictly-layered packages ([ADR-0007](docs/decisions/ADR-0007-newtui-ground-up-architecture.md)): `ui/` and `commands/` depend on `model/`; `kernel/` is the **only** package that touches amplifier-core/foundation and never imports Textual; the UI sees the kernel exclusively through normalized `UIEvent`s.

![newtui architecture and topology](docs/diagrams/newtui-architecture.png)

- **`ui/`** (Textual-only) — `NewTuiApp` composition root, the `RuntimeAdapter` seam (`RealRuntimeAdapter` / `DemoRuntimeAdapter`), `TranscriptReducer`, and the widget stack: TitleBar → TranscriptView + LiveTail + NoticeSlot → overlay strips (Palette, Lanes, Rewind, Queued) → Composer ⇄ ApprovalBar → FooterBar.
- **`model/`** (framework-agnostic, no Textual, no amplifier-core) — transcript block grammar, `SteeringQueue`/`NeedsYouQueue`, interaction modes, `DenialLog` trust state, `OutcomeLedger`, `LaneRegistry`.
- **`commands/`** — slash commands as data: one `CommandRegistry` powers the palette, slash triggers, keybinds, and help.
- **`kernel/`** — the amplifier adapter layer: `RealRuntime` (foundation 7-step lifecycle), `resolve_config()` (3-scope settings merge + bundle discovery → `load_bundle` → compose → `prepare()`), `create_initialized_session()` (mount-plan verification), the `UIEvent` contract + `QueueBridge` (hooks → `asyncio.Queue[UIEvent]`, never blocks the engine), `ApprovalBroker`, `SessionStore`/`IncrementalSaver`, `SessionSpawner`, rewind, cost tracking, and the `tool:pre` governance hook.
- **Amplifier ecosystem** — `AmplifierSession` + coordinator from amplifier-core; `load_bundle`/`prepare()`/`fork_session_in_memory` from amplifier-foundation; the packaged **newtui bundle** mounts the `loop-streaming` orchestrator, `provider-anthropic`, `context-simple`, and the filesystem/bash/web/search/task tools. `_strip_printing_hooks()` removes ANSI-printing hooks that would corrupt the Textual screen.

The critical seam is the **thread boundary**: `RealRuntime` runs on a dedicated `real-runtime` daemon thread with its own asyncio loop so slow hooks can never starve rendering — calls marshal in via `run_coroutine_threadsafe` and events marshal out via `call_soon_threadsafe`.

### Data flow

![newtui data flow](docs/diagrams/newtui-dataflow.png)

A turn, end to end (colors in the diagram):

- **Input (blue)** — keypress → Composer → `adapter.submit` → thread hop → `RealRuntime.submit` (emits a synthetic `PromptSubmit` for instant echo, snapshots the git diff) → `session.execute` → orchestrator ⇄ Anthropic API ⇄ tools.
- **Event stream (green)** — coordinator hooks fire (Channel A: live `llm:stream_block_delta`; Channel B: durable `content_block:end` / `tool:pre/post` / `orchestrator:complete`) → `QueueBridge.normalize()` → typed `UIEvent` → app-loop queue → `TranscriptReducer` → TranscriptView/LiveTail.
- **Approvals (orange)** — tool ask → `ApprovalBroker` ticket → hop to the app loop → ApprovalBar → answer routes back to the kernel as an `ApprovalResponse`.
- **Steering (purple)** — mid-turn composer text → `SteeringQueue` → `StepBoundaryBridge` injects at step boundaries.
- **Subagents (teal)** — `tool-task` → `SessionSpawner` (`session.spawn` capability) → child session shares the same bridge → LanesPanel.
- **Persistence (gray)** — debounced `transcript.jsonl`/`metadata.json`, append-only `events.jsonl` (powers resume cost re-seed, evidence, replay); resume restores history into both the context and the transcript view.

Regenerate the diagrams after architectural changes:

```sh
dot -Tpng docs/diagrams/newtui-architecture.dot -o docs/diagrams/newtui-architecture.png
dot -Tpng docs/diagrams/newtui-dataflow.dot -o docs/diagrams/newtui-dataflow.png
```

## Development

```sh
uv sync                # install dependencies
uv run pytest -q       # full test suite
uv run ruff check .    # lint
```

Ground truth documents:

- [docs/DESIGN-SPEC.md](docs/DESIGN-SPEC.md) — the design spec
- [docs/design-v3-cohesive.html](docs/design-v3-cohesive.html) — executable mockup (exact strings, colors, timing, state machines)
- [docs/decisions/ADR-0007-newtui-ground-up-architecture.md](docs/decisions/ADR-0007-newtui-ground-up-architecture.md) — architecture rules
