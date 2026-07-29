# Opencode-transfer attractor

Re-express capabilities from the SST **opencode** agent (`~/dev/opencode`, a
TypeScript/Bun client-server monorepo) into **both** amplifier terminal clients.
Same proven machine as the app-cli [`gene-transfer.dot`](gene-transfer.dot); the
differences are structural and worth reading before you seed a single row.

## How it differs from the app-cli port

1. **Cross-ecosystem.** The donor is TypeScript/Effect-TS. You can't graft code —
   every row is a pure *capability re-expression*. "Never copy" is free here; the
   discipline that actually matters is **triage** (below).
2. **Two targets that are not peers.**
   - `~/dev/amplifier-app-newtui` — Python/Textual. Owns the **backend**
     (kernel/model/commands + the `serve` stdio protocol) and one client UI.
   - `~/dev/amplifier-app-newtui-rust` — Rust/ratatui. A **pure protocol client**
     of the Python `serve` backend (codex-tui / codex-core split). It renders what
     the protocol exposes; it owns no session/agent logic.
   - So each capability is classified by **where it lives** (ledger column 2):

     | `target` | Lands in | Gates run |
     |---|---|---|
     | `python` | Python backend/CLI only; Rust gets it free over the protocol | ruff · pyright · pytest · forge(py) |
     | `both`   | Pure client UX — **both** UIs (Textual + ratatui), each with its own tests/goldens | py gates **+** cargo test · clippy · forge(rust) |
     | `split`  | Python backend **+** a `serve` protocol addition, **then** client render in both | py + rust gates |

   `split` rows should be seeded as **two ordered rows** — `<slug>-backend`
   (`python`) then `<slug>-client` (`both`) — so the protocol lands before the UI
   that consumes it.

Everything else is identical to the original: one row per loop, `LocateDonor →
PlanTransfer → Implement → UnitValidate → ForgeValidate → Commit`, bounded 3-retry,
`acknowledged` + human handoff on non-convergence, per-repo `opencode/<slug>`
branches + PRs, never `main`. Ledger is the source of truth
([`opencode-ledger.tsv`](opencode-ledger.tsv), driven via the shared `ledger.py`
with `LEDGER_FILE` set). Keyed by capability **slug** — no GitHub-issue dependency;
acceptance is derived from the donor by the pipeline itself.

## Triage FIRST (the load-bearing step)

opencode has ~45 user-facing capabilities. **Most should not be ported.** newtui is
already broad (post-app-cli campaign + the serve/rust work), several opencode
features are out of scope for a local terminal app, and a few conflict with
amplifier's philosophy. Seeding blindly would teach the harness to do busywork.

Before launch, run a **gap-check** against *both* current repos and drop
already-have / out-of-scope rows. Cheapest form — one delegation:

> Delegate a read-only pass: "For each candidate slug in pipelines/OPENCODE.md,
> check whether the capability already exists in ~/dev/amplifier-app-newtui
> (grep commands/, kernel/, model/, ui/) and ~/dev/amplifier-app-newtui-rust
> (src/). Return keep / already-have(cite) / partial for each."

### SKIP — out of scope or philosophy-mismatch (do not seed)

Cloud "workspaces" / remote sandboxes · ACP server (Zed backend) · Slack bot ·
Electron desktop · enterprise console · hosted stats service · GitHub-Action bot ·
hosted session-share links (`opncd.ai/share`) · the ~30-hook plugin system
(amplifier has its own module/hook system) · the 20+ bundled AI-SDK provider
adapters (amplifier providers are separate modules) · embedded in-process SDK.
Also **`session.background`/detach**: genuinely useful but blocked by the same
full-screen-TUI host-seam gap newtui already hit (#45/#108) — needs that seam
first, not a port.

### ALREADY IN NEWTUI — verify, don't re-port

sessions list/rename/delete/**fork**/resume · subagent/`task` delegation + fan-out ·
plan mode + plan→build handoff · permissions/approvals/needs-you · providers
add/list/use · routing matrix · MCP client · skills · desktop notifications ·
themes + `/theme` · `tool invoke` · `reset`. (The gap-check confirms these are real
before you exclude them.)

### CANDIDATES — the honest port list (seed the ones the gap-check keeps)

| slug | capability | opencode donor pointer | target |
|---|---|---|---|
| `codemode-execute` | Agent writes a sandboxed program that orchestrates many tool calls in one context-cheap pass (context economy) | `packages/codemode/`, `packages/opencode/src/tool/code-mode.ts` | `split` |
| `question-tool` | Agent pauses mid-turn with a structured multiple-choice / free-text question | `packages/opencode/src/tool/question.ts`, `question.txt` | `split` |
| `apply-patch-tool` | Unified-patch-format edit tool (matters for OpenAI-family models) | `packages/opencode/src/tool/` (`apply_patch`) | `python` |
| `lsp-tool` | Agent-callable diagnostics/hover/symbol lookup as a model tool | `packages/opencode/src/tool/lsp.ts` | `python` |
| `prompt-stash` | Save/restore in-progress draft prompts | `packages/tui/src/prompt/stash.tsx`, `dialog-stash.tsx` | `both` |
| `prompt-frecency-history` | Frecency-ranked prompt autocomplete + history recall | `packages/tui/src/prompt/frecency.tsx`, `history.tsx` | `both` |
| `model-variant-cycle` | Cycle a mid-session dimension orthogonal to model (e.g. thinking-effort tier) | `packages/tui/src/component/dialog-variant.tsx` (`variant.cycle`) | `split` |
| `session-tags` | Tag/label sessions for organization | `packages/tui/src/component/dialog-tag.tsx` | `split` |
| `stats-dashboard` | `stats --days --models --project` cost/usage dashboard | `packages/opencode/src/cli/cmd/stats.ts` | `python` |
| `sanitized-export-import` | Export a session with path/text/tool-IO redaction; import it back (verify vs newtui `/export`) | `packages/opencode/src/cli/cmd/{export,import}.ts` | `python` |

`split` rows expand to two: e.g. `codemode-execute-backend` (`python`) →
`codemode-execute-client` (`both`).

### Ready-to-paste seed block (after triage prunes it)

```sh
cd ~/dev/amplifier-app-newtui
L() { LEDGER_FILE=pipelines/opencode-ledger.tsv python3 pipelines/ledger.py "$@"; }
# split → two ordered rows (backend before client)
L add codemode-execute-backend python
L add codemode-execute-client  both
L add question-tool-backend     python
L add question-tool-client      both
L add model-variant-cycle-backend python
L add model-variant-cycle-client  both
L add session-tags-backend      python
L add session-tags-client       both
# single-home rows
L add apply-patch-tool          python
L add lsp-tool                  python
L add prompt-stash              both
L add prompt-frecency-history   both
L add stats-dashboard           python
L add sanitized-export-import   python
L stats
```

## Launch

Same two paths as gene-transfer (see [README.md](README.md) § Launch). Primary:
paste a driver prompt into an `amplifier` session — your session already has
`bash` + `delegate` + file tools, so it acts as the orchestrator (self-delegated
`claude-opus-4-8` workers, one capability per lane in git worktrees, gates
re-verified independently before each PR). Alternative: register a launcher bundle
and run `opencode-transfer.dot` through the real `loop-pipeline` engine.

Prereqs beyond gene-transfer's: `~/dev/opencode`, `~/dev/amplifier-app-newtui-rust`
present; a Rust toolchain (`cargo`, `clippy`); `gh` authed for **both** GitHub
repos; forge daemon up (it can boot the Rust binary `amplifier-newtui-rs` too).

## Decisions needed before a run

- **D1 — scope.** Which candidates survive the gap-check? (I did not seed any;
  the ledger is intentionally empty.)
- **D2 — Rust gate authority.** Confirm the Rust acceptance bar: `cargo test` +
  `cargo clippy -D warnings` + a forge boot of `amplifier-newtui-rs`, and that the
  Python suite must stay green (MIGRATION.md discipline). Adjust the `UnitValidate`
  node if the real bar differs (e.g. a coverage floor like the Python side).
- **D3 — split ordering across repos.** A `both`/`split` client row can't merge
  until its `serve` protocol addition ships. Seed backend-first (done above) and
  accept two PRs per capability, or hold the client row until the backend PR
  merges? Default: seed both, backend ordered first; the client row's forge gate
  will fail fast if the protocol isn't there yet, which the retry loop surfaces.
- **D4 — models.** `claude-opus-4-8` on all nodes (fable-5 refuses). Route
  `Implement` to a Rust-strong coder for `both`/`split` rows?
- **D5 — issues or slugs.** Slug-keyed (current design, self-contained) or file a
  GitHub issue per capability first (like the app-cli campaign) for tracking?
