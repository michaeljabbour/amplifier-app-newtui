# TUI Findings Ledger — amplifier-app-newtui audit (2026-07-16)

Baseline: 689 passed / 1 xfailed. Forge sessions tag: newtui-audit.
Severity: P0 broken flow · P1 spec violation · P2 jank/polish · P3 nice-to-have.

| # | Sev | Area | Finding | Repro | Status |
|---|-----|------|---------|-------|--------|
| 1 | P1 | /permissions | Renders `<bound method TrustSlot.label of TrustSlot(...)>` — `slot.label` never called in transcript renderer | demo: type /permissions + enter | FIXED — slot.label() called; exceptions/blocks rows added; regression test |
| 2 | P3 | /context | `tools 32` reads oddly without unit when <1k (mockup always shows Nk) | demo: /context after seed | NOT-A-BUG — format_tokens is mockup-verbatim (raw count <1k) |
| 3 | P3 | /improve | Posts only header line, no rows and no "no proposals" placeholder when ledger empty | demo: /improve early | FIXED — empty-state placeholder row in /improve renderer |
| 4 | P1 | steer | Mid-turn steer: no ↳ echo block appears in transcript (spec §3/§5); notice fires but echo missing | demo: agents turn, type text+enter while running | NOT-A-BUG — echo renders; earlier grep raced the panel |
| 5 | P0 | steer | Steer text silently lost at turn end — no "Applying steer", no roll-forward follow-up turn, no notice (ADR-0007 steering contract: leftovers roll forward with notice) | demo: steer during agents turn, wait for turn end | SPEC'D (ADR-0007 silent discard) + FIXED honesty notice 'steer not applied · discarded at turn end' |
| 6 | P1 | queue | alt+enter queue: no ▹ queued-strip, no footer q1 badge (spec §5) — message did run as next turn though | demo: alt+enter mid/end of turn | FIXED — Textual drops alt on named keys (ESC CR → 'enter'); patched parser restores alt+enter; verified live |
| 7 | P2 | notices | "steer queued · shift+enter queues a full next-turn message" hardcodes shift+enter; footer/composer adapt to alt+enter on legacy terms | demo on non-kitty PTY | FIXED — STEER_NOTICE adapts to alt+enter on legacy terminals |
| 8 | P3 | lanes | Panel elapsed/cost (41s/2m/55s) are static DEMO_LANES strings, inconsistent with live tree values (4s/6s/2s) | demo agents turn | OPEN |
| 9 | P1 | /improve | Never produces proposal rows even with denial log + override + answer-only turns (mockup lines 512-515 show 2 proposals from this data) | demo: deny pytest, apply needs-you decision, /improve | FIXED(partial) — action join key plumbed (NeedsYouItem.action), empty state added; thresholds (3/2) are by design |
| 10 | P1 | lanes | Auto-opened lanes panel (agents turn) doesn't take key focus — ↑↓/enter dead until ctrl-t reopen | demo: agents turn, wait auto-panel, press down+enter | FIXED — empty-composer ↑↓/enter drive the auto-opened lanes panel |
| 11 | P2 | composer | No keyboard newline (amplifier CLI has ctrl-j); paste-with-newlines works, TextArea grows. Add ctrl+j insert-newline | demo: type, press ctrl-j | FIXED — ctrl+j inserts newline (ignored while empty: CRLF automation) |
