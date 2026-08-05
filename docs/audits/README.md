# app-cli → tui parity audit (2026-07-23)

> **Current implementation status (2026-08-05):** Use
> [feedback-status-2026-08-05.md](feedback-status-2026-08-05.md) for the complete
> 23-story acceptance ledger and
> [b7-b9-boundary-2026-08-05.md](b7-b9-boundary-2026-08-05.md) for the final
> notification/source-lock boundary proof. The lane counts below are a dated
> donor-parity snapshot at commit `e6b50cd`, retained for chronology rather
> than presented as current implementation status.

A three-lane, read-only audit comparing Microsoft's **amplifier-app-cli** (donor/reference)
against **amplifier-app-tui** (this repo, clean main @ `e6b50cd`, 1814 tests green), to
answer one question: **can tui fully supplant app-cli** — every function, capability, and
safeguard? Verdicts judge *capability* parity, not code parity (tui deliberately
re-expresses app-cli behavior through its own kernel/model/ui/commands seams).

Verdict legend: **PARITY** · **PARTIAL** (narrower/weaker) · **MISSING** · **TUI-BETTER**
(tui hardens beyond app-cli) · **N/A-BY-DESIGN**.

## Lanes

| Lane | Scope | Report |
|---|---|---|
| 1 | User-facing command surface & features | [lane1-commands.md](lane1-commands.md) |
| 2 | Safeguards (security / trust / safety) | [lane2-safeguards.md](lane2-safeguards.md) |
| 3 | Runtime & composition (bundle/session/settings/routing) | [lane3-runtime.md](lane3-runtime.md) |

## Headline result

tui is at or beyond parity on the large majority of the surface, with **zero wholly-missing
runtime capabilities** and several places where it is materially **stronger** than app-cli
(protected paths, embedded interpreter-write scan, write-boundary enforcer assertion,
value-pattern secret scrubbing, child-governance re-registration, approval-timeout floor).

| Lane | PARITY | PARTIAL | MISSING | TUI-BETTER | N/A |
|---|---|---|---|---|---|
| 1 Commands (~78) | 41 | 9 | 10 | 12 | 6 |
| 2 Safeguards (34) | 20 | 3 | 2 | 6 | 3 |
| 3 Runtime (66) | 42 | 8 | 0 | 11 | 5 |

## Routing matrix (the explicitly-asked question)

**Optional-by-default is correct parity, not a regression.** Routing is opt-in on *both* sides:
app-cli only mounts `hooks-routing` when a `routing:` settings section is present
(`runtime/config.py:266-300`); tui only composes the routing-matrix overlay when
`routing.enabled` or `routing.matrix` is set (`kernel/config.py:279-309`). `routing-matrix`
being a "well-known" bundle in app-cli's `discovery.py:107-111` feeds only `update`/`list`,
never session defaults. tui is actually **more complete** — it composes the whole
routing-matrix *bundle* (hook + instructions + skills) and adds an explicit `routing.enabled`
switch, vs app-cli's bare hook-config append. One narrow divergence: an **overrides-only**
`routing:` block opts in on app-cli but is inert on tui (tracked as a low-severity gap).

## Gap tracker

Every genuine gap was filed as a `parity-gap` issue on 2026-07-23:

| # | Gap | Lane | Severity |
|---|---|---|---|
| 100 | Prompt-injection probe on tool output | Safeguard | **High** (implemented same day) |
| 101 | Deferred-decision dependency blocking | Safeguard | Low-Med |
| 102 | Two-stage / provider-backed classifier seam | Safeguard | Low-Med |
| 103 | Provider key-store advisory lock | Safeguard | Low |
| 104 | `sources.bundles` not fed to `prepare()` | Runtime | Med |
| 105 | `bundle.added` not resolved by name at boot | Runtime | Med |
| 106 | Notification config keys + CLI (ntfy push, desktop) | Runtime/Cmd | Med |
| 107 | `tool invoke` CLI | Commands | Med |
| 108 | `/fork` + session fork (background child) | Commands | Med |
| 109 | `run` per-invocation `--model/--provider/--mode` + `--resume` | Commands | Med |
| 110 | Data-safe `reset` command | Commands | Med |

Nothing here blocks daily use; the one High-severity safeguard (injection probe, #100) was
implemented immediately. The rest are tracked, ranked, and independently landable.

## Re-running this audit — the owner-gated loop

This audit is a **pass in a loop**, not a one-off. Parity drifts in both directions (app-cli
ships something new; tui hardens past it), so the audit is designed to be re-run against a
later release and to record what each re-run found.

Two versioned TSV artifacts hold the loop's state, written by
[`pipelines/parity_loop.py`](../../pipelines/parity_loop.py) (stdlib only, never raises) and
driven by [`pipelines/parity-loop.dot`](../../pipelines/parity-loop.dot):

| Artifact | One row per | Fields |
|---|---|---|
| [`parity-passes.tsv`](../../pipelines/parity-passes.tsv) | read-only re-audit pass | `pass · date · commit · outcome · gaps_found · gap_ids · note` |
| [`parity-gates.tsv`](../../pipelines/parity-gates.tsv) | discovered gap | `gap_id · slug · disposition · owner · date · note` |

The 2026-07-23 audit above is **pass 1** (commit `e6b50cd`, 11 gaps: #100–#110). To re-run:
redo the three lanes read-only against the new commit, then record what the pass found —

```sh
python3 pipelines/parity_loop.py record-pass <sha> 119:new-gap-slug   # or `-` if clean
python3 pipelines/parity_loop.py should-continue                      # CONTINUE | DONE
```

### Passes on record

| Pass | Date | Target commit | Donor ref | Found | Report |
|---|---|---|---|---|---|
| 1 | 2026-07-23 | `e6b50cd` | `30c0a65` / `origin/main` `51194ef` | 11 gaps (#100–#110) | the three lanes above |
| 2 | 2026-08-04 | `7030527` | `30c0a65` / `origin/main` `f1fcb66` | 8 gaps (#111–#118) | [pass2-2026-08-04.md](pass2-2026-08-04.md) |

`clean_streak=0/3` — no pass has yet come back clean. Pass 2 confirmed all eleven
baseline gaps are **closed in code**, and found eight fresh divergences: one new
donor capability (`/goal`), one new donor policy (fail-loud module activation),
and six rows pass 1 recorded as `MISSING` but never filed as tracked gaps.

**The run stops after three consecutive clean passes, or when a named owner ends it.**
`end-run "Michael Jabbour" "reason"` refuses placeholders at write time; validation and
`should-continue` also reject a hand-edited unsigned `owner-ended` row. That
counter is over *read-only re-audits*, and it is deliberately not the transfer pipeline's
per-gap fix-retry budget (also 3, also bounded, entirely unrelated) — see the side-by-side
table in [pipelines/README.md](../../pipelines/README.md#owner-gated-parity-loop-continuous-re-audit).

**No gap becomes code without an owner saying so.** Every newly-discovered gap lands
`pending`, and only an `accepted` disposition opens a code-changing route
(`parity_loop.py gate <id>` → `PROCEED` / `BLOCKED`). The dispositions exist because parity
is a *decision process*, not a mandate to copy every app-cli behavior:

| Disposition | Meaning |
|---|---|
| `pending` | discovered, not yet ruled on — **blocks** (the safe default) |
| `accepted` | the owner wants it; may enter the transfer pipeline |
| `rejected` | not worth building here — including "belongs below the harness or on another surface" |
| `deferred` | real, but not now |
| `already-covered` | tui reaches the capability by another route; not a gap |

**A disposition needs a real owner.** `TBD`, `owner`, `team`, `unknown`, `?`, blank and
their relatives are refused at write time and read back as `unattributed` — which blocks
exactly like `pending` — if hand-edited into the file. The list lives in one place
(`PLACEHOLDER_OWNERS` in `parity_loop.py`); `parity_loop.py validate` audits both gate
decisions and owner-ended pass rows for actions nobody signed. A decision nobody signed is
not a decision. Gene transfer rechecks an `accepted` disposition both when selecting the
queue head and immediately before its first code-changing node, so a direct ledger edit
cannot bypass this boundary.

### 19 gaps await a human

All 19 gaps on record (#100–#118) are `pending` — the baseline eleven were filed and ranked
before this gate existed, and pass 2's eight are fresh. **Triage is the next action on this
loop, and it is the only one an agent cannot do.**

→ **[parity-decision-sheet.md](parity-decision-sheet.md)** is the ready-to-review working
document: every undispositioned gap with its evidence, its user-visible consequence, a
proposed disposition, and a paste-ready `decide` command. One sitting, ~20 minutes. Where
implementation is recommended and the design is non-trivial, a spike is attached
([gap #111, `/goal`](../plans/2026-08-04-spike-goal-command-parity.md)); where a one-line
remediation suffices, the sheet says so per gap.

Until then every gap blocks, which is the safe default working as designed.
