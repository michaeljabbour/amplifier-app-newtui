# Paste into a fresh amplifier session to run the opencode HGT migration

Copy everything in the fenced block below into a new `amplifier` session. It has no
prior context, so the block carries every absolute path and rule it needs.

```
You are the ORCHESTRATOR/ENGINE for an HGT (Horizontal Gene-Transfer) attractor run.
Mission: transfer the opencode capabilities that are NOT already present in either
amplifier client into both clients — at maximum useful parallelism — forge-gated,
feature+tests+CI co-built, one PR per capability per repo, never on main.

ABSOLUTE PATHS
- Donor (SOURCE, READ-ONLY): /Users/michaeljabbour/dev/opencode
    SST "opencode", a TypeScript/Bun client-server monorepo. NEVER import/vendor/copy
    its code — you re-express the CAPABILITY, not the TypeScript.
- Host A (Python backend + Textual client): /Users/michaeljabbour/dev/amplifier-app-newtui
    Branch main. Owns the backend (kernel/model/commands) AND the `serve` stdio
    protocol. ADR-0007 layering: only kernel/ imports amplifier-core/foundation;
    ui/ and commands/ never do; client-visible behavior ships through `serve`.
    Gate == CI: uv run ruff check . && uv run ruff format --check . && uv run pyright src/ && uv run pytest -q
- Host B (Rust ratatui client): /Users/michaeljabbour/dev/amplifier-app-newtui-rust
    Branch main. A PURE PROTOCOL CLIENT of Host A's `amplifier-newtui serve` (codex-tui
    / codex-core split) — it renders protocol state, owns no session/agent logic. Read
    its MIGRATION.md + PARITY.md and honor that discipline (behavioral-equivalence
    tests; never force-green; keep the Python suite green).
    Gate == CI: cargo test && cargo clippy --all-targets -- -D warnings
- Pipeline machine (already written, in Host A): /Users/michaeljabbour/dev/amplifier-app-newtui/pipelines/
    HGT.md (the archetype), opencode-transfer.dot (the graph — the per-capability
    slice + edge logic), OPENCODE.md (triage table: candidates, SKIP list,
    already-have list, per-capability target), opencode-ledger.tsv (EMPTY — you seed
    it), ledger.py (LEDGER_FILE-aware: `LEDGER_FILE=pipelines/opencode-ledger.tsv python3 pipelines/ledger.py {earliest|add <slug> <target>|update <slug> <state>|stats}`).
- Forge (terminal QA daemon): /Users/michaeljabbour/.claude/skills/amplifier-skill-forge/tools/forge.py
    Load it: load_skill("amplifier-skill-forge"). Confirm up: python3 <forge> doctor.
    Forge can boot the Python TUI, the Rust client, AND opencode itself.
- Worktree roots (create; NEVER touch the primary checkouts):
    Host A lanes: /Users/michaeljabbour/dev/newtui-wt/<slug>
    Host B lanes: /Users/michaeljabbour/dev/newtui-rust-wt/<slug>

MODELS: use claude-opus-4-8 for ALL delegated workers (claude-fable-5 refuses this
autonomous porting work). You may route Rust implementation to a Rust-strong coder.

TARGETS (ledger column 2, from OPENCODE.md): python = Host A only (Rust gets it free
over the protocol) · both = pure client UX in BOTH clients (each with its own
tests/goldens) · split = Host A backend + `serve` addition FIRST, then client render
in both (seed as two ordered rows: <slug>-backend python, then <slug>-client both).

PHASE 0 — GAP-CHECK (this defines scope; do it first, report before building).
Read pipelines/OPENCODE.md. For each candidate slug there — plus anything else in
opencode you judge worth transferring — determine whether it ALREADY EXISTS in Host A
(grep commands/, kernel/, model/, ui/) and Host B (grep src/). Where UX matters, boot
opencode in forge to see the real capability. Produce a keep/drop table and KEEP only
capabilities absent from BOTH clients and NOT on the OPENCODE.md SKIP list. Seed
pipelines/opencode-ledger.tsv with the survivors via `ledger.py add`; expand split
rows into their two ordered rows. Print the seeded ledger (`ledger.py stats` + the
rows) and the keep/drop rationale, THEN proceed.

PHASE 1 — MAX-PARALLEL BUILD.
Build a dependency-aware WAVE plan and run it at maximum useful parallelism:
- One self-delegated claude-opus-4-8 worker PER capability, each in its OWN git
  worktree per targeted repo (Host A under newtui-wt/, Host B under newtui-rust-wt/).
- All INDEPENDENT capabilities (disjoint files) run concurrently. Ordering
  constraints: (1) a split capability's client row starts only AFTER its backend PR
  is green (the client needs the protocol); (2) capabilities that touch the same file
  run in sequence, not parallel.
- Concurrency cap ~4–6 simultaneous lanes: forge screen-scrape probes get flaky under
  heavy load (known lesson) — if a forge assertion is the ONLY failure, re-run it in
  isolation before treating it as real or burning a retry.

Each worker performs the HGT slice (per opencode-transfer.dot):
  1. LOCATE — document opencode's behavioral contract for the capability (inputs,
     outputs, config keys, protocol/wire shape, UX). Never copy TS.
  2. PLAN — design the re-expression through the host's OWN seams (Host A: ADR-0007 +
     serve protocol for anything the client must see; Host B: idiomatic ratatui client
     consuming the protocol). AUTHOR THE FORGE PROBE FIRST from the acceptance — the
     probe is the spec.
  3. IMPLEMENT the VERTICAL SLICE in one pass — feature + unit tests + forge probe +
     CI parity together (the local gate MUST equal what CI runs; never build-now-
     test-later). Regenerate goldens if a pure renderer changed.
  4. UNIT GATE — run the exact host gate(s) above for every targeted repo.
  5. FORGE GATE — boot the REAL Python TUI (uv run amplifier-newtui --demo) and, for
     both/split, the REAL Rust client (needs Host A `amplifier-newtui serve` up) via
     forge, and assert the capability from the screen. An LLM never declares success;
     the terminal does.
  6. LAND — you (orchestrator) re-verify the gates INDEPENDENTLY, then commit + push +
     PR per targeted repo (branch opencode/<slug>, label opencode-transfer), and
     `ledger.py update <slug> implemented`.

BOUNDED: max 3 attempts per capability. On non-convergence, `ledger.py update <slug>
acknowledged`, save the plan to Host A .ai/oc_blocked/<slug>.md, and MOVE ON — never
stall the queue.

HARD RULES: never commit to main (branch + PR; branch protection re-runs the gates);
never import/vendor/copy opencode; Host A ADR-0007; Host B stays a pure protocol
client and keeps the Python suite green; a PR opens only when its gates are green.

DONE when every seeded ledger row is implemented (green-gated PR open) or acknowledged
(human handoff). Final report: PRs opened per repo, acknowledged rows + reasons, and
the Phase-0 gap table.
```
