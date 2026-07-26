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
| `live.rs` | `kernel/runtime.py` + provider module | `LiveRuntime`: real Anthropic streaming, pure Rust |
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

## Runtimes
Both implement one `Runtime` trait, so the UI is identical regardless of source:
- **`LiveRuntime`** (default) — a real, streamed turn against the Anthropic Messages
  API in pure Rust (`ureq` + rustls). Multi-turn history, usage-priced cost. Used
  automatically when `ANTHROPIC_API_KEY` is set.
- **`DemoRuntime`** (`--demo`, or when no key) — the scripted offline turn incl. the
  approval-park arc.

The SSE→`UiEvent` normalization is the real integration logic and is unit-tested
offline against a captured stream fixture — verified with no key and no network.

## Run
```sh
cargo run --release            # live turn if ANTHROPIC_API_KEY is set, else scripted
cargo run --release -- --demo  # force the scripted demo
cargo test --release           # headless render + reducer + SSE-normalizer tests
cargo test --release snapshot -- --nocapture   # print a rendered frame
```

## What it does NOT do (out of MVP scope)
Tool-use over the live provider, bundle loading, subagent lanes, rewind, persistence
— those are the `kernel/`+`foundation` seams a full migration ports next. The live
runtime does text turns only; tools/approvals remain exercised via the demo.
