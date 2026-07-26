# TUI Performance Benchmark — Rust MVP vs Python Textual app

Date: 2026-07-26 · Host: macOS (Darwin 25.5.0), Apple Silicon · Branch: `claude/rust-migration-evaluation-lpa45b`
Driver: [`perf/bench.py`](perf/bench.py) · Raw records: [`perf/results.jsonl`](perf/results.jsonl) · Screens: [`perf/screens/`](perf/screens/)

## Methodology

All candidates run inside real PTYs driven by the **forge terminal daemon**
(`amplifier-skill-forge`), each run in a **fresh zsh session (cold)** that is
destroyed afterwards. The driver is stdlib Python that shells out to
`forge.py`.

- **startup_ms** — wall clock from just before the launch command is typed into
  the PTY until the first *rendered-screen poll* that matches the candidate's
  ready regex. Screens are polled every 0.25 s and each poll costs a forge
  subprocess round-trip, so values carry **one-sided granularity of roughly
  +0.3–0.6 s** (they can overshoot, never undershoot).
- **Harness floor** — measured empirically with `echo MARKER` in the same
  pipeline: **~563–577 ms**. Any candidate whose wall startup sits at ~570 ms
  is *at or below the measurement floor*; for the Rust binary the internal
  perf log (below) is authoritative.
- **rss_mb** — after ready, `ps -axo pid,ppid,rss` summed over **every
  descendant of the session shell** (app plus children: `serve_mock.py`, the
  `uv run … serve` backend, node children, …). The zsh shell itself (~2 MB) is
  excluded. Per-process breakdowns are in `results.jsonl`.
- **turn_ms** (demo TUIs only) — wall clock from just before typing
  `Add a health check endpoint` + Enter until a new `+N/−M` diffstat rule
  appears in the transcript. If an approval bar (`Allow once`) appears the
  driver confirms it with Enter.
- **"Ready" per candidate** — rust-demo/rust-mock/py-demo/rust-live/py-live:
  the composer placeholder `Message`; rust-live is additionally gated on the
  `session_started` wire event in its perf log; py-live is additionally gated
  on session identity (model name) appearing in the footer; amplifier-cli:
  `Interactive` banner; codex/claude: first interactive screen (see notes).
- **Cold vs warm** — every run is a cold app start in a fresh PTY, but the OS
  page cache is warm after run 1 and `uv` was pre-warmed
  (`uv run python -c pass`) so Python numbers measure the app, not uv
  resolution. py-demo run 1 (3 345 ms) shows the remaining first-touch cost;
  runs 2–3 are steady-state.
- **Live candidates were boot-only.** No prompt was ever submitted to
  rust-live, py-live, amplifier-cli, codex, or claude (a real turn costs
  money). They were booted, measured, screenshotted, and quit.

## Results

Medians over 3 cold runs for the cheap candidates; single cold run for the
expensive ones.

| Candidate | Startup ms (wall, median) | RSS MB | Demo turn ms (median) | Notes |
|---|---:|---:|---:|---|
| rust-demo | 577 † | 4.2 | 1 953 | scripted demo; turn includes 1 approval round-trip |
| rust-mock | 578 † | 21.1 | — | binary 4 MB + `serve_mock.py` python child ~17 MB |
| py-demo (Textual) | 1 639 | 89.5 | 9 563 | run 1 was 3 345 ms (first-touch); turn is a *different, longer* demo script with no approval |
| rust-live | 569 † (UI) / 33 756 (session) | 229.7 | n/a | UI interactive at floor; `session_started` wire event at 33.8 s (single run; includes `uv run amplifier-newtui serve` + amplifier core boot) |
| py-live | 1 587 (UI) / 18 349 (session) | 242.3 | n/a | composer draws at 1.6 s; model/session identity in footer at 18.3 s (single run) |
| amplifier-cli | 14 929 | 218.7 | n/a | reference point only; time to "Amplifier Interactive Session" banner |
| codex | 940 ‡ | 49.5 ‡ | n/a | reference point only; **time-to-update-gate**, not full UI (see notes) |
| claude | 2 575 | 602.8 | n/a | reference point only; full welcome UI (node) |

† At or below the ~570 ms harness measurement floor — see internal numbers below.
‡ Codex presented an interactive "Update available 0.144.6 → 0.145.0" gate on
the recorded run; 940 ms is time-to-gate and RSS is at the gate. An earlier
uninstrumented run (regex miss, kept in `results.jsonl` as a failure) reached
the full prompt UI, so full-UI startup is in the same low-seconds range.

### Rust internal milestones (`AMPLIFIER_PERF_LOG`, ms since process start)

| Candidate | first_draw | session_started |
|---|---:|---:|
| rust-demo (3 runs) | 1.0 / 1.9 / 2.5 | — |
| rust-mock (3 runs) | 1.7 / 1.7 / 4.8 | 27.1 / 31.8 / 45.0 |
| rust-live (1 run) | 4.9 | 33 755.6 |

The Rust binary paints its first frame in **1–5 ms** and completes the mock
protocol handshake in **under 50 ms**. Its wall-clock "startup" in the table is
entirely PTY-harness overhead.

## Honest comparison

On the axes a Rust migration is meant to move, the gap is unambiguous:
**first paint ~2 ms vs ~1.0–1.6 s** (py-demo steady-state wall startup minus
the ~0.57 s harness floor) and **4 MB vs ~90 MB RSS** for the equivalent
demo UI (rust-mock's honest full-stack figure is 21 MB because it carries a
Python mock-server child). The demo `turn_ms` column should **not** be read as
an engine comparison: the two apps play different scripted turns (the Rust
demo scripts a short health-check turn with one approval; the Python demo
plays a longer session-store-refactor script with no approval), so those
numbers mostly measure demo-script pacing plus, for Rust, one approval
round-trip.

For live sessions the TUI is not the bottleneck: both rust-live (33.8 s) and
py-live (18.3 s) spend nearly all their boot inside the same Python
`amplifier` core/runtime (network auth, bundle load), and each was measured
once, so the 15 s spread between them is not evidence that the Rust path is
slower — it is one sample of a network-dominated boot, and rust-live adds an
extra `uv run … serve` subprocess hop. What the Rust client *does* change is
memory attribution: of rust-live's 230 MB tree, only ~4 MB is the UI process;
in py-live the UI and runtime share one ~100 MB+ Python process.

codex, claude, and amplifier-cli are **different products** with different
boot work (auth, update checks, model/context discovery) and are included only
as environmental reference points for what established agent TUIs cost to
boot on this machine: ~0.9 s (to codex's update gate), ~2.6 s / 600 MB
(claude, node), ~15 s / 219 MB (amplifier CLI). Against that backdrop, a TUI
client that is interactive in single-digit milliseconds and idles at 4 MB is a
different class of footprint.

Caveats: single-machine, single-day numbers; wall-clock resolution bounded by
the ~0.3–0.6 s polling harness; expensive candidates ran once; live boots are
network-dependent; `results.jsonl` also retains one premature py-live record
(composer-only ready marker, superseded by the gated re-run) and two codex
regex-miss failures for full transparency.

## Reproducing

```sh
export FORGE=~/.claude/skills/amplifier-skill-forge/tools/forge.py   # daemon must be running
uv run python -c pass                                                # warm uv
python3 rust-mvp/perf/bench.py                                       # all candidates
python3 rust-mvp/perf/bench.py rust-demo py-demo                     # or a subset
```

Live candidates only boot and quit — no prompts are submitted and no model
calls are made.
