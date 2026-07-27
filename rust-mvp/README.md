# amplifier-newtui-rs — ratatui MVP

A Rust/ratatui port of the Amplifier TUI experience, built for the Rust-migration
evaluation (`docs` in the parent repo; unit-by-unit ledger in the repo-root
`MIGRATION.md`). By default it is a **pure protocol client of the real Python
`amplifier-newtui serve` backend** (amplifier-core untouched); `--demo` runs a
scripted in-process turn with **no Python, no network**, and outside the checkout
it falls back to an offline mock backend with an explicit notice.

It deliberately mirrors the Python app's architecture so it reads as a port path,
not a toy:

| Rust module | Python analogue | Role |
|---|---|---|
| `event.rs` | `kernel/events.py` | the normalized `UiEvent` union (one boundary) |
| `runtime.rs` | `kernel/demo.py` | `Runtime` trait + `DemoScript` engine / `ScriptedDemoRuntime` (full port of the six demo turn scripts) + the legacy inline `DemoRuntime` |
| `protocol.rs` | `kernel/jsonl.py` (extended) | the wire protocol: events out, submissions in |
| `core_client.rs` | `ui/runtime_adapter.py` | **`CoreClientRuntime`: client of a backend over the protocol** |
| `live.rs` | (illustrative) | `LiveRuntime`: UI-calls-provider shortcut — NOT the target shape |
| `backend/serve_mock.py` | a `serve` mode in this repo's kernel | backend that owns the turn loop; wraps amplifier-core |
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

This is the Codex `codex-tui ⇄ codex-core` split. The backend here is a Python
`serve` shim (`backend/serve_mock.py`) standing in for a `serve` mode in this
repo's own kernel, which wraps amplifier-core's existing API. **Migrating the UI
to Rust requires zero changes to amplifier-core** — it stays a black box behind
the same Python boundary the Textual app uses today. When core later exposes a
Rust API, swap `CoreClientRuntime`'s backend for an in-process binding with the
UI untouched (the `Runtime` trait absorbs it).

## Runtimes (all behind one `Runtime` trait — UI can't tell them apart)
- **`CoreClientRuntime`** (default) — client of a backend process over the
  protocol. The real, interactive turn incl. approvals answered across the boundary.
- **`DemoRuntime`** (`--demo`) — scripted in-process turn; offline, deterministic.
- **`LiveRuntime`** (`--direct`) — illustrative UI-calls-provider shortcut in pure
  Rust; kept to show why it's the *wrong* shape (no core, agent loop would leak
  into the UI). Its SSE→event normalizer is still unit-tested offline.

## Run
```sh
cargo run --release            # core-client: spawns the REAL serve backend (see below)
cargo run --release -- --demo  # scripted in-process demo
cargo run --release -- --direct  # direct-to-provider shortcut (needs ANTHROPIC_API_KEY)
cargo test --release           # reducer + render + SSE + cross-process protocol turn
cargo test --release snapshot -- --nocapture   # print a rendered frame
```

Aliases (`.cargo/config.toml`): `cargo demo` = `cargo run -- --demo`;
`cargo mock` = `cargo run` — note that inside the checkout the default runtime
now spawns the REAL backend, so to actually get the offline mock use
`AMPLIFIER_SERVE_CMD="python3 backend/serve_mock.py" cargo mock` (the alias
comment predates real-by-default).

Opt-in boot-milestone log: `AMPLIFIER_PERF_LOG=<path>` appends JSONL lines
(`first_draw`, `session_started`, …) — the input to `PERFORMANCE.md`.

The default runtime mirrors the Python launcher — real session by default,
honest fallback: when the crate sits inside the amplifier-app-newtui checkout
(`rust-mvp/` next to `src/amplifier_app_newtui/`), it spawns the **real**
backend `uv run amplifier-newtui serve` (`kernel/serve.py`, wrapping the live
`RealRuntime` + `ApprovalBroker` — **zero changes to amplifier-core**).
Outside the checkout it falls back to the offline `backend/serve_mock.py`
with an explicit notice; the mock emits the same vocabulary (no key, no core)
so the cross-process test — a full interactive turn incl. an approval answered
by ticket id — runs anywhere.

`AMPLIFIER_SERVE_CMD` still overrides the backend command outright:

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
- image paste, composer-internal text selection, character-ranged transcript
  selection (drag selection is whole-line)
- the first-run provider gate (stays Python launcher-side; a provider-missing
  boot surfaces as the boot-failure diagnosis)

Everything backend-shaped (bundle loading, persistence, tool-use, real provider
turns) lives behind the `serve` protocol, so the Rust UI is already done with
respect to it.
