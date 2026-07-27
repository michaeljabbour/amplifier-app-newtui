# amplifier-newtui-rs — ratatui MVP

A Rust/ratatui port of the Amplifier TUI experience, built for the Rust-migration
evaluation (`docs` in the parent repo; unit-by-unit ledger in the repo-root
`MIGRATION.md`). By default it is a **pure protocol client of the real Python
`amplifier-newtui serve` backend** (amplifier-core untouched); `--demo` runs the
scripted in-process demo with **no Python, no network** (the same explicit
feature the Python app ships as `--demo`). There is no other scripted path: if
the backend cannot spawn, the app renders the honest boot-failure diagnosis
(`⊘ session failed to start · …` + the doctor hint) — never a silent demo.

It deliberately mirrors the Python app's architecture so it reads as a port path,
not a toy:

| Rust module | Python analogue | Role |
|---|---|---|
| `event.rs` | `kernel/events.py` | the normalized `UiEvent` union (one boundary) |
| `runtime.rs` | `kernel/demo.py` | `Runtime` trait + `DemoScript` engine / `ScriptedDemoRuntime` (full port of the six demo turn scripts) |
| `protocol.rs` | `kernel/jsonl.py` (extended) | the wire protocol: events out, submissions in |
| `core_client.rs` | `ui/runtime_adapter.py` | **`CoreClientRuntime`: client of a backend over the protocol** |
| `backend/serve_mock.py` | (test fixture) | offline serve-shaped backend, spawned ONLY by the cross-process test — never a production launch path |
| `model.rs` | `model/blocks.py`, `modes.py` | pure block/mode domain state |
| `app.rs` | `ui/reducer.py` | stateful `UiEvent → mutation` reducer (never draws) |
| `ui.rs` | `ui/transcript.py` | pure `draw(state)` render; theme tokens isolated |
| `main.rs` | `ui/app.py` | composition root + single app-loop channel |

## What works
- Full-screen layout: title bar (live spinner) · transcript · composer · footer
- Streaming answer into a single mutable live-tail region (word-by-word)
- Tool lines, user echo, narration, end-of-turn rule (`files N · +A/−D · $cost`)
- Inline **approval bar** that parks the turn (`y` allow / `n` deny) and resumes
- Five modes cycled with Shift+Tab; running cost/token tallies
- Headless `TestBackend` render tests (the ratatui analogue of Textual `Pilot`)

## Architecture: front-end is a *client*, core is never touched

The canonical runtime (`CoreClientRuntime`, the default) owns **nothing but
rendering**. A spawned backend process owns the turn loop; they talk over a
bidirectional line protocol — the externalized form of the app's in-process seam:

```
Rust ratatui UI  ⇄  submissions (stdin)  ⇄  backend process  ⇄  amplifier-core
(pure client)       events (stdout)          (owns the loop)     (UNCHANGED)
```

This is the Codex `codex-tui ⇄ codex-core` split. The backend is the real
Python `amplifier-newtui serve` (`kernel/serve.py`), which wraps
amplifier-core's existing API. **Migrating the UI to Rust requires zero changes
to amplifier-core** — it stays a black box behind the same Python boundary the
Textual app uses today. When core later exposes a Rust API, swap
`CoreClientRuntime`'s backend for an in-process binding with the UI untouched
(the `Runtime` trait absorbs it).

## Run modes (both behind one `Runtime` trait — UI can't tell them apart)
- **`CoreClientRuntime`** (default) — client of the real `serve` backend
  process over the protocol. The real, interactive turn incl. approvals
  answered across the boundary. If the backend cannot spawn, the app boots
  into the boot-failure diagnosis (`⊘ session failed to start · <spawn error>`
  + doctor hint) — it never substitutes a scripted stand-in.
- **`ScriptedDemoRuntime`** (`--demo`, explicit opt-in only) — the
  `kernel/demo.py` port: the six scripted demo turns, in-process; offline,
  deterministic. Feature parity with the Python app's `--demo`.

`backend/serve_mock.py` is a **test fixture**: an offline serve-shaped backend
spawned explicitly by the cross-process protocol test (and available via
`AMPLIFIER_SERVE_CMD` for manual offline runs). No production launch path ever
resolves to it.

## Run
```sh
cargo run --release            # core-client: spawns the REAL serve backend (see below)
cargo run --release -- --demo  # scripted in-process demo (explicit feature)
cargo test --release           # reducer + render + cross-process protocol turn
cargo test --release snapshot -- --nocapture   # print a rendered frame
```

Alias (`.cargo/config.toml`): `cargo demo` = `cargo run -- --demo`.

Opt-in boot-milestone log: `AMPLIFIER_PERF_LOG=<path>` appends JSONL lines
(`first_draw`, `session_started`, …) — the input to `PERFORMANCE.md`.

The default runtime mirrors the Python launcher — real session by default,
honest failure: when the crate sits inside the amplifier-app-newtui checkout
(`rust-mvp/` next to `src/amplifier_app_newtui/`), it spawns the **real**
backend `uv run amplifier-newtui serve` (`kernel/serve.py`, wrapping the live
`RealRuntime` + `ApprovalBroker` — **zero changes to amplifier-core**).
Outside the checkout it spawns the installed `amplifier-newtui serve` from
PATH. If the spawn fails, the boot-failure diagnosis renders — there is no
scripted fallback of any kind.

`AMPLIFIER_SERVE_CMD` overrides the backend command outright:

```sh
AMPLIFIER_SERVE_CMD="uv run amplifier-newtui serve" cargo run --release
```

The Python launcher's TUI-relevant flags are accepted and forwarded to the
backend `serve` command (and `--mode` also seeds the opening posture):

```sh
cargo run --release -- --bundle newtui -p anthropic -m claude-sonnet-4-5 \
    --mode plan --resume core-0123
```

On exit, a real session prints the same resume hint as the Python app
(`resume this session: amplifier-newtui resume <id>`).

## What it does NOT do (recorded gaps — the MIGRATION.md ui/app row is canonical)
- per-widget shimmer timers (one global tick clock at Python cadences instead —
  it drives the title spinner, lanes shimmer, splash, the working line's 1s
  pulse, and the working label's shimmer band)
- steer wire delivery (no serve op yet — the client queues/echoes only)
- needs-you decision actions inside the listing (chips render, clicks don't resolve)
- resume replay (`--resume` forwards to serve; no history replay into the transcript)
- session ops over the wire (rename/fork/etc. answer "session still starting"
  until the protocol grows ops)
- OSC 777 notify / OSC 8 hyperlinks / OSC 52 clipboard (selection copies use the
  OS clipboard tool only, synchronously)
- image paste, composer-internal text selection
- the first-run provider gate (stays Python launcher-side; a provider-missing
  boot surfaces as the boot-failure diagnosis)

Everything backend-shaped (bundle loading, persistence, tool-use, real provider
turns) lives behind the `serve` protocol, so the Rust UI is already done with
respect to it.
