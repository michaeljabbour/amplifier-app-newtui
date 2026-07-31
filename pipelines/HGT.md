# HGT — the Horizontal Gene-Transfer attractor

A **named, parameterized capability-transfer archetype.** Where `gene-transfer.dot`
was *vertical* (one donor → one host, same amplifier ecosystem), **HGT is
horizontal**: it moves a capability across a *species boundary* (a foreign codebase,
any language) and expresses it in **one or more hosts** — each in the host's own
machinery, never by grafting foreign tissue. `opencode → {tui-py, tui-rust}`
is HGT **instance #1**.

Biology earns the name: horizontal gene transfer is how a trait crosses between
unrelated organisms and is then transcribed by the *recipient's* own ribosomes. That
is exactly the discipline — **re-express the capability through the host's seams;
never copy the donor's code.**

## The three knobs (everything else is fixed machinery)

| Knob | What it is | Example (instance #1) |
|---|---|---|
| **`sources`** | One or more donor repos, read-only. Any language. | `/Users/michaeljabbour/dev/opencode` |
| **`hosts`** | One or more target repos, each tagged `path:kind`. `kind` selects the gate stack. A host may be a **new/empty repo** — HGT scaffolds it. | `…/amplifier-app-tui:python` · `…/amplifier-app-tui-rust:rust` |
| **`scope_prompt`** | Free-text that decides **what** transfers and what's excluded. This is "the opencode component controlled by a prompt." | *"Port opencode capabilities absent from both clients; skip cloud/hosted/plugin-system features; …"* |

Per-capability the ledger row names which host role(s) it lands in (`target`), so the
gate/branch/commit nodes touch only the right repo(s). Adding a host = add a `kind`
handler; adding a source or changing scope = edit two launch params. **The graph
never changes.**

## What makes it a *distinct type*, not just gene-transfer with paths

**1. Forge-woven QA — terminal QA at three points, not one final gate.**
It uses [`amplifier-skill-forge`](https://github.com/michaeljabbour/amplifier-skill-forge)
throughout the build, not just to validate at the end:
- **Observe the donor** — boot the *real* donor (forge can drive opencode itself) to
  watch the capability live and capture its true UX contract before re-expressing it.
- **Acceptance-first** — the `PlanTransfer` node authors the forge probe *before* any
  code, from the donor's user-facing contract. The probe is the spec.
- **Validate** — `ForgeValidate` boots the real target TUI(s) (Python Textual **and**
  the Rust `amplifier-tui-rs` client) and asserts the capability through a real
  terminal. An LLM never declares success; the terminal does.

**2. Feature + tests + CI co-built as one atom.**
The unit of work is a **vertical slice**: feature code + unit tests + forge probe +
CI wiring, authored together — never build-now-test-later. Two rules make it fast
*without* losing quality:
- **Gate = CI, exactly.** The local gate runs the *same* commands CI runs (per host
  kind). No drift → no "green locally, red in CI" round-trips (the failure mode that
  cost the parity-gap run its one manual retry). Branch protection re-runs the same
  gate as a second oracle.
- **New host ⇒ CI lands with capability #1.** If a host is a fresh repo, the first
  transfer scaffolds its `ci/gate.sh` + workflow, so tests and CI exist from the
  first line of the first feature.

**3. Cross-ecosystem + multi-host, first-class.** Donor language is irrelevant
(copying is impossible anyway); hosts are heterogeneous (a Python backend + a Rust
protocol client have *different* gate stacks and land the *same* capability in
different layers). HGT models "which host, which layer" as data (the ledger
`target`), so one capability can be a Python-backend row, a both-clients row, or a
`split` (backend-first, then client) pair.

## Host kinds and their gate stacks

| `kind` | Unit gate (also the CI gate) | Forge boot |
|---|---|---|
| `python` | `ruff check` · `ruff format --check` · `pyright src/` · `pytest -q` (+coverage if the repo enforces it) | real Python TUI |
| `rust` | `cargo test` · `cargo clippy --all-targets -- -D warnings` (+ the paired Python suite stays green — protocol-client discipline) | real `amplifier-tui-rs` (needs the Python `serve` backend up) |
| `new:<lang>` | `./ci/gate.sh` — scaffolded by transfer #1 alongside the feature | whatever the new app boots as |

## Instance #1 — opencode → both clients

- **Graph:** [`opencode-transfer.dot`](opencode-transfer.dot) — HGT with
  `sources=opencode`, `hosts={tui:python, tui-rust:rust}`.
- **Scope + triage:** [`OPENCODE.md`](OPENCODE.md) — candidate/skip/already-have
  tables. The scope_prompt is applied as a **gap-check first**: only capabilities
  absent from *both* clients get seeded.
- **Ledger:** [`opencode-ledger.tsv`](opencode-ledger.tsv) — `<slug>\t<target>\t<state>`,
  driven via the shared `ledger.py` (`LEDGER_FILE`-aware).
- **Launch prompt:** [`opencode-run-prompt.md`](opencode-run-prompt.md) — paste into a
  fresh amplifier session; runs the gap-check, then a **max-parallel** wave.

## Extension path (only when a 2nd instance needs it)

Today the gate/forge dispatch is inlined for `{python, rust}` in the `.dot`. When a
third host kind or a genuinely different source/host combo appears, lift the dispatch
into `pipelines/hgt_gates.sh` + `hgt_forge.sh` (host-kind → gate/boot), keeping the
`.dot` a pure orchestrator — the maximally-DRY "one logic home" split. Don't build
that indirection before the second consumer exists; until then it's ceremony.
