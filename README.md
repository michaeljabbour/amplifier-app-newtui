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
