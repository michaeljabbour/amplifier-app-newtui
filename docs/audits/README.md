# app-cli → newtui parity audit (2026-07-23)

A three-lane, read-only audit comparing Microsoft's **amplifier-app-cli** (donor/reference)
against **amplifier-app-newtui** (this repo, clean main @ `e6b50cd`, 1814 tests green), to
answer one question: **can newtui fully supplant app-cli** — every function, capability, and
safeguard? Verdicts judge *capability* parity, not code parity (newtui deliberately
re-expresses app-cli behavior through its own kernel/model/ui/commands seams).

Verdict legend: **PARITY** · **PARTIAL** (narrower/weaker) · **MISSING** · **NEWTUI-BETTER**
(newtui hardens beyond app-cli) · **N/A-BY-DESIGN**.

## Lanes

| Lane | Scope | Report |
|---|---|---|
| 1 | User-facing command surface & features | [lane1-commands.md](lane1-commands.md) |
| 2 | Safeguards (security / trust / safety) | [lane2-safeguards.md](lane2-safeguards.md) |
| 3 | Runtime & composition (bundle/session/settings/routing) | [lane3-runtime.md](lane3-runtime.md) |

## Headline result

newtui is at or beyond parity on the large majority of the surface, with **zero wholly-missing
runtime capabilities** and several places where it is materially **stronger** than app-cli
(protected paths, embedded interpreter-write scan, write-boundary enforcer assertion,
value-pattern secret scrubbing, child-governance re-registration, approval-timeout floor).

| Lane | PARITY | PARTIAL | MISSING | NEWTUI-BETTER | N/A |
|---|---|---|---|---|---|
| 1 Commands (~78) | 41 | 9 | 10 | 12 | 6 |
| 2 Safeguards (34) | 20 | 3 | 2 | 6 | 3 |
| 3 Runtime (66) | 42 | 8 | 0 | 11 | 5 |

## Routing matrix (the explicitly-asked question)

**Optional-by-default is correct parity, not a regression.** Routing is opt-in on *both* sides:
app-cli only mounts `hooks-routing` when a `routing:` settings section is present
(`runtime/config.py:266-300`); newtui only composes the routing-matrix overlay when
`routing.enabled` or `routing.matrix` is set (`kernel/config.py:279-309`). `routing-matrix`
being a "well-known" bundle in app-cli's `discovery.py:107-111` feeds only `update`/`list`,
never session defaults. newtui is actually **more complete** — it composes the whole
routing-matrix *bundle* (hook + instructions + skills) and adds an explicit `routing.enabled`
switch, vs app-cli's bare hook-config append. One narrow divergence: an **overrides-only**
`routing:` block opts in on app-cli but is inert on newtui (tracked as a low-severity gap).

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
