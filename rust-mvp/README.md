# amplifier-newtui-rs — ratatui MVP

A self-contained Rust/ratatui proof-of-concept of the Amplifier TUI experience,
built for the Rust-migration evaluation (`docs` in the parent repo). **No Python,
no amplifier-core, no network** — a scripted `DemoRuntime` emits the same kind of
normalized events the real engine would.

It deliberately mirrors the Python app's architecture so it reads as a port path,
not a toy:

| Rust module | Python analogue | Role |
|---|---|---|
| `event.rs` | `kernel/events.py` | the normalized `UiEvent` union (one boundary) |
| `runtime.rs` | `kernel/demo.py` | `Runtime` trait + scripted `DemoRuntime` |
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
cargo run --release            # core-client: spawns the serve backend, real interactive turn
cargo run --release -- --demo  # scripted in-process demo
cargo run --release -- --direct  # direct-to-provider shortcut (needs ANTHROPIC_API_KEY)
cargo test --release           # reducer + render + SSE + cross-process protocol turn
cargo test --release snapshot -- --nocapture   # print a rendered frame
```

`AMPLIFIER_SERVE_CMD` overrides the backend command. The **real** backend now
exists — `amplifier-newtui serve` (`src/amplifier_app_newtui/kernel/serve.py`)
wraps the live `RealRuntime` + `ApprovalBroker` and speaks this exact wire, so:

```sh
AMPLIFIER_SERVE_CMD="uv run amplifier-newtui serve" cargo run --release
```

drives the Rust UI from a real session with **zero changes to amplifier-core**.
`backend/serve_mock.py` emits the same vocabulary offline (no key, no core) so the
cross-process test — a full interactive turn incl. an approval answered by ticket
id — runs anywhere.

## What it does NOT do (out of MVP scope)
Real bundle loading, subagent lanes, rewind, persistence, and tool-use over a real
provider — those are the backend/`kernel`+`foundation` seams a full migration ports
next. Crucially, they all live behind the protocol, so the Rust UI is already done
with respect to them.
