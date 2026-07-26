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
| `runtime.rs` | `kernel/demo.py` | scripted event producer on a background thread |
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

## Run
```sh
cargo run --release            # interactive; Enter runs a scripted turn
cargo test --release           # headless render + reducer tests
cargo test --release snapshot -- --nocapture   # print a rendered frame
```

## What it does NOT do (out of MVP scope)
Real provider I/O, bundle loading, subagent lanes, rewind, persistence — those are
the `kernel/`+`foundation` seams a full migration would port next.
