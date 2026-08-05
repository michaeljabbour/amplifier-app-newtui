# Amplifier TUI v3 — Cohesive: Compliance Specification

Ground truth: `docs/design-v3-cohesive.html` (Amplifier TUI v3 - Cohesive.dc.html).
Every item below is a testable requirement. The rebuild is done when every requirement
can be demonstrated in the real terminal app. The literal `[ ]` markers are normative
checklist bullets, **not implementation-status indicators**; current status and evidence
live in the dated compliance ledger and audit artifacts.

> **Precedence:** this document is the authoritative behavioral spec. Where the earlier
> presentation spec (`docs/tui-v3-cohesive.md`) conflicts with it — palette groups,
> approval keys, footer hints, app naming — **this file wins**. User-facing documentation
> of shipped behavior lives in `docs/USER-GUIDE.md`.

## 1. Themes & design tokens

Four themes, switchable at runtime: three dark (slate/graphite/carbon, from the mockup)
plus one light (paper, added for AC4/#210 below). Exact token values:

| Token | slate | graphite | carbon | paper |
|---|---|---|---|---|
| bg-page | `#12151c` | `#131110` | `#0c0e12` | `#e7e2d3` |
| bg-term | `#232937` | `#211e1a` | `#14171d` | `#f7f5f0` |
| bg-chrome | `#191d27` | `#181512` | `#0f1116` | `#efece3` |
| bg-tab | `#2b3243` | `#2c2722` | `#1f242e` | `#dcd4bf` |
| fg | `#c9d1e0` | `#d6cfc4` | `#cdd6e4` | `#3a352c` |
| bright | `#eef2f8` | `#f2ede4` | `#f4f7fc` | `#1c1812` |
| dim | `#6b7487` | `#8a8175` | `#65718a` | `#6e6656` |
| dimmer | `#4a5163` | `#575047` | `#3d4657` | `#948c78` |
| green | `#7ec699` | `#98c28b` | `#6fd39c` | `#146536` |
| orange | `#e0a458` | `#dba15c` | `#e9b14f` | `#8a4d0a` |
| red | `#e06c75` | `#d97371` | `#ef6e7b` | `#9c2f27` |
| blue | `#7aa2f7` | `#90a4d8` | `#6f9df2` | `#1a45b8` |
| teal | `#6fc3c3` | `#80bcae` | `#57c8c8` | `#0a5f58` |
| rule | `#333b4d` | `#3a352e` | `#2a3140` | `#cfc6ae` |

- [ ] All UI color comes from these named tokens only (no ad-hoc colors).
- [ ] Theme switchable at runtime (settings/command), default `slate`.
- [ ] Monospace rendering; JetBrains-Mono-flavored glyph choices (❯ ● ✳ ✦ ✧ ■ ✔ □ ⊘ ◐ ├─ └ ↳ ▲ ▹ ‹ ›).
- [ ] Every token pair that renders text on a background meets a WCAG 2.1 contrast floor
      (4.5:1 body text, 3:1 large-scale/highlighted text) in every theme, light and dark —
      computed via relative luminance and asserted programmatically, not eyeballed
      (`ui/themes.py`'s `contrast_ratio`; `tests/test_ui_theme_contrast.py`).

**Resolution note (compliance 2026-08-04, item B1, AC4 — issue #210 closed):** a prior
round narrowed AC4 ("final-answer emphasis remains legible in light and dark themes and
does not rely on color alone") to the three dark themes above and tracked a full light
theme as a separate follow-up (issue #210, see docs/BACKLOG.md). That narrowing is
reverted here: `paper` (above) is a real, selectable fourth theme (`/theme paper`, or
reachable by cycling bare `/theme`), and every token pair that renders text on a
background — in `paper` and in the three dark themes alike — is contrast-tested against
a WCAG 2.1 floor (`tests/test_ui_theme_contrast.py`), so a future token edit cannot
silently regress legibility. The final-answer start marker (§3 below; `model/blocks.py`'s
`Answer.final`, rendered by `ui/transcript_render.py`'s `FINAL_ANSWER_MARKER`) was already
built from a label + bold weight, never color alone; it is now additionally verified
against `paper`'s actual resolved colors (bright-on-bg-term, ~16.2:1) in the same test
file. Issue #210 is closed by this change.

## 2. Screen layout (top → bottom)

1. **Title bar** (bg-chrome): centered title `amplifier-app-tui — Amplifier — <state> — <bundle> — <session-short>`; while running, prefix with orange spinner glyph cycling `✳ ✦ ✧ ✦` every ~260ms and mirror a visibly rotating braille frame into native terminal chrome; title's `<state>` reflects current plan step (lowercased) or `ready` / `planning` / `brainstorming` / `✳ coordinating N agents`.
2. **Transcript** (bg-term): scrollable region, the main body.
3. **Notice slot**: transient right-aligned dim text floating at transcript bottom edge (auto-dismiss ~4s), e.g. `mode plan · read-only`, `steer queued · shift+enter queues a full next-turn message`.
4. **Overlay strips** (each a bordered strip above composer, shown when active):
   - Command palette (max-height scrollable list)
   - Bottom strip: agent lanes panel (left) | plan panel (right — the turn's todo
     checklist, `Plan n/m` header); under 90 cols the two panels stack vertically
     so the plan's expand/collapse control remains usable
   - Rewind picker strip
   - Sessions picker strip (`↑/↓` highlight · `enter` detail · `r` resume); resume closes
     the current runtime cleanly and relaunches the selected stored session, while copying
     the equivalent CLI command only as a fallback
   - Queued-message strip
   - Decision-capture strip (persistent question + submit/newline/cancel instructions)
   - Approval bar (replaces composer while open)
5. **Composer**: left edge tinted 2px in mode accent; `[mode]` badge (clickable/cyclable) + green bold `❯` + input. Placeholder: `Message Amplifier…  ( ↑ history · ctrl+j newline · enter send · / commands )`.
6. **Footer status bar** (bg-chrome): left = `mode <mode>` (mode color) `· <trust> · <model> · <session-short> · $<cost><yield▲><queued q1>` and optional orange `N decisions waiting · ctrl-y`; right = context-sensitive hints. The active bundle is not repeated here — item 1's title bar is its one persistent home (compliance 2026-08-02, item D4: a bundle path duplicated between the title and footer was consolidated to the top).

- [ ] Layout matches order & styling above.
- [ ] The bundle path renders in exactly ONE persistent location (the title bar, item 1) — never a second copy in the footer.
- [ ] Footer hints change by state:
  - approval open → `arrows select · enter confirm · esc deny`
  - lane focused → `esc back to parent · transcript is the subagent's own`
  - palette open → `↑↓ select · enter run · esc close`
  - sessions open → `↑↓ select · enter open · r resume · esc close`
  - running → `esc interrupt · enter steer · shift+enter queue`
  - idle → *(empty — the generic reminder isn't a persistent-frame occupant; see `/keys` and the composer placeholder below)*
- [ ] `/keys` lists every keyboard shortcut, rendered from the same `ui/keymap.py` table the footer hints and key bindings read (item D4: removing a hint from the footer never costs discoverability).

## 3. Transcript block grammar

- [ ] **User line**: `❯ ` (green bold) + `[mode] ` (mode color) + text (bright). Mode badge stamps scrollback permanently.
- [ ] **Narration**: `● ` bright bullet + fg text.
- [ ] **Activity digest (collapsed)**: the whole run of tool calls since the last assistant text collapses into ONE dim line `  ● <humanized counts> ` + `· click to expand` in dimmer — e.g. `Read 3 files · searched 1× · ran 1 shell command`. Grows in place as tools complete; frozen when the model next speaks (or at turn end) and a fresh digest opens below the answer. Click/enter reveals an indented dimmer body with one line per op (`read cost.py`, `$ uv run pytest -q`, …). A denial is never folded in — it always gets its own `⊘ blocked` line.
- [ ] **Live activity tree**: while a single-agent turn runs, up to 3 recent ops render as `  └ `/`  ├ ` dimmer branches beneath the working line (the in-flight op is dim, settled ops dimmer). Ephemeral — it rides the working line and vanishes at turn end; the durable record is the digest above.
- [ ] **Plan checklist**: header `· ` orange + title + trailing dim telemetry `(Ns · ↓ X.Xk tok)` updated live; items `  □ ` dimmer/pending, `  ■ ` orange bold/active, `  ✔ ` green + dim text/done.
- [ ] **Blocked**: `  ⊘ blocked · <cmd> ` red + `· <reason> · <continuation>` dim. Never halts the turn by itself.
- [ ] **Working status line** (while running): pulsing spinner `✳/✦/✧` orange + `working · Ns · ↓ X.Xk tok · ` dim + `esc to interrupt · type to steer` dimmer, with the live activity tree beneath (above). Before any tool runs it shows the inline note (`thinking`, else `1 agent`) in place of the tree. A fan-out turn renders `Coordinating N agents · Ns · ↓ X.Xk tok · ` dim + `esc to interrupt` dimmer instead (mockup runAgentsTurn — no `working ·` prefix, no steer hint, dedicated agent tree not this one). Updates every second; removed at turn end.
- [ ] **Recap line** (turn end): `✳ ` dimmer + italic dim `Goal: <goal>. Next: <next>.`
- [ ] **Final answer**: fg text with selective bright/bold and teal inline code; clickable → evidence.
  The turn's one authoritative answer additionally opens with a stable `● Final answer`
  start marker (bright/bold — label + weight, not color alone, AC4) so its START stays
  identifiable after scrolling away and back, resume replay, or history navigation
  (AC2); `ctrl-f` (return-to-answer) jumps back to it for long turns.
- [ ] **Steer echo**: `  ↳ ` teal + `steer queued: "<text>" ` teal + `· applies at next step boundary` dimmer; steer application logged as narration `Applying steer: <text>`.
- [ ] **Turn rule**: full-width 1px rule (rule token) + right-aligned label `<Ns> · <X.Xk> tok, <N>% cached · $<cost> · <outcome>`; label dim when shipped, dimmer when answer-only/interrupted. Outcomes seen in mockup: `answer`, `3 files · +142/−38 · tests ✔`, `· interrupted`, `· plan ready`.
- [ ] Turn rules are clickable → open rewind picker at that checkpoint.
- [ ] **Delegate summary** (fan-out turns, at turn end): one durable line
  `● Used N delegates · Plan n/m · <duration> ▸`; click/enter expands (`▾`) to per-agent
  rows (`✔`/`✖`/`⊘` `<agent> <elapsed> · "<result snippet>"`) plus a final plan line.
  Every past summary in scrollback stays expandable; reconstructed from `ui-events.jsonl`
  on resume. The live todo checklist no longer appends to the transcript — while a turn
  runs it lives in the plan panel (§2) and folds into this summary at close.

## 4. Modes & trust

| mode | color | trust string |
|---|---|---|
| chat | dim | `ask all · auto read` |
| plan | blue | `read-only` |
| brainstorm | teal | `no tools` |
| build | green | `auto read,test · ask write,net,spend` |
| auto | orange | `auto read,write · asks if risky` |

- [ ] **Default mode is `auto`** (amendment 2026-07-16, user directive — the mockup's
  scripted history starts in chat, but the app boots in auto with amplifier's natural
  wide scope: read/write/test auto-allowed; net/spend/exec ask if risky (classifier-gated) with
  deny reserved for destructive shapes and unrequested outbound pushes).
- [ ] shift+tab cycles modes (also when input focused); clicking `[mode]` badge cycles.
- [ ] Mode change → notice `mode <id> · <trust>`.
- [ ] Mode tint appears in exactly three places: composer badge + composer left edge + footer. chat's composer edge uses rule token.
- [ ] Trust profiles actually gate tools: plan = read-only, brainstorm = no tools, chat = ask everything except reads, build = auto read/test ask write/net/spend, auto = auto read/write with policy gate.
- [ ] Plan mode produces a plan block marked `(read-only)`; recap: `Plan ready. shift+tab to build hands it over for execution.` Switching to build offers/executes the handoff.

## 5. Composer input semantics

- [ ] Idle + Enter → send as user turn.
- [ ] Running + Enter → **steer** this turn (applies at next step boundary; echoed with ↳; consumed steer removed).
- [ ] Running + Shift+Enter (or second steer) → **queue** full next-turn message; queued strip shows `▹ queued next: "<text>" · runs when this turn ends · alt+↑ recall to steer`; footer shows ` · q1`; auto-runs at turn end (`queued message picked up`). Alt+↑ or strip click atomically recalls it into an empty composer; a draft or pending steer prevents recall without losing the queue.
- [ ] A free-text needs-you answer has priority over submit/steer/queue and slash-command routing. Choosing `Type your own` opens a persistent decision-capture strip, parks the exact draft (including paste/image payloads), Enter submits, ctrl-j inserts a newline, and Esc cancels without interrupting the turn.
- [ ] `/` prefix opens the palette live-filtered as you type.
- [ ] Esc priority order: lane-focus → palette → rewind → lanes → interrupt-running.
  With an empty composer, a second Esc within 750ms opens the restore picker; during a
  running turn the first Esc interrupts and the second may open the picker while close-out
  finishes. At idle with a draft, double-Esc clears but preserves it for ↑ recall.

## 6. Command palette

- [ ] Opens on `/`, filters by substring, first row highlighted (bg-tab), Enter runs top match, click runs any row, esc closes.
- [ ] Rows: teal command (min-width aligned) + description + right-aligned dimmer tag (`built-in`/`skill`).
- [ ] When filter is exactly `/`, group headers show (uppercase dimmer 10.5px): During, Parallel, Ship, Between, Repair.
- [ ] Commands (minimum set): `/mode`, `/plan`, `/brainstorm`, `/context` (usage grid + bar `████████░░` conversation/tools/memory/free), `/tasks` (toggle lanes), `/ledger`, `/rewind`, `/permissions` (trust-slot editor), `/doctor` (checkup: ✔ healthy lines + numbered orange findings), `/improve` (proposals from ledger + denial log; never applies silently).
- [ ] Running a command echoes it as a user line first.

## 7. Approvals & needs-you queue

- [ ] Approval request → bar replaces composer: `Approval required ·` orange bold + prompt + options `Allow once / Allow always / Deny`; selected option prefixed `› `, bright on bg-tab; Deny styled red when unselected. Arrows/Tab cycle, Enter confirms, Esc = Deny. Clickable.
- [ ] Notice on open: `approval required · choose below the transcript`.
- [ ] If a lane is focused when approval arrives → auto-return to parent with notice.
- [ ] Deny → `⊘ blocked · <thing> · denied by user · continuing without <thing>` and the turn continues.
- [ ] Ctrl-y on a live approval parks an answerable needs-you item AND resolves the current approval to Deny immediately; hiding the bar must never leave a future waiting.
- [ ] Trust-boundary blocks in auto mode → deferred decision: narration explains, footer badge `1 decision waiting · ctrl-y`, run continues to a shipped-locally outcome.
- [ ] ctrl-y / badge click → `Needs you  N deferred decision` orange block listing numbered decisions with inline actionable choice chips (e.g. `[yes · push to fork]` green on bg-tab); acting on one logs `Applying decision: …` and clears the badge.
- [ ] Host `question` calls are posture-aware: interactive modes wait for their answer; auto mode parks the questions, returns a successful deferred result immediately, and injects any later answer once at a provider boundary.

## 8. Agent lanes & subagent focus

- [ ] ctrl-t (or `/tasks`) toggles lanes panel: header `Agent lanes · ↑↓ select · enter focus · ctrl-o tail · esc close` + one aligned line per subagent: `  <glyph> <name> · t<turn> · <activity> · <elapsed> · $<cost>` (glyph/color per state: ◐ teal running, ■ fg working, ✔ dim done, `!` orange attention (a discrete failure, pending child approval, or denied/blocked child action surfaced against a still-running lane), ✖ red error, ⊘ red cancelled — D5 AC1; error/cancelled glyphs match the post-turn delegate-summary block's own). Approval attention is projected from the normalized child event's `session_id`; the global approval bar remains the one answer control.
- [ ] Multi-agent turn: per-agent progress lives in the lanes panel and the delegate
  summary (§3), not per-agent transcript tree lines. Successful native file writes still
  aggregate into one expandable, diff-styled `Changed N files` row.
- [ ] **Lane live tail**: while lanes run and the root stream is idle, the LiveTail
  region shows the focused lane's stream — up to 3 dim `┆`-guttered lines, repainted at
  most every 0.05s. Focus defaults to the most-recently-streaming running lane; ctrl-o
  cycles the pin among running lanes; the tailed lane carries a `▸` after its name in
  the panel. The root stream always preempts instantly. Tail content is ephemeral —
  never a transcript block; durable child prose lives in the lane's own transcript.
- [ ] **Stream identity**: every visible live stream states producer and turn. Child tails
  inherit `<name> · t<turn>` from their containing lane row/focus banner; the root peek and
  revealed box render `main · t<turn>` from the reducer's active turn. Labels are
  presentation metadata only and never enter the consolidated answer.
- [ ] Selecting a lane focuses that subagent: transcript swaps to the child's own transcript with banner `focused: <name> · subagent of <parent-session> · own context window · results report back to parent · esc back`, its delegated brief as user-line `[delegated]`, its log, its state recap. Esc returns to parent (`back to parent session`).
- [ ] Title while coordinating: `… — ✳ coordinating N agents — …`.

## 9. Rewind & checkpoints

- [ ] Cut and expose a checkpoint **before every prompt runs**, including the first and an
  in-flight prompt; finalize that same checkpoint onto its turn rule. Record `{id: tN,
  restore_turn_id, label, cost-at-time, workspace_id}` so the target means “immediately
  before this prompt,” not “after this turn.” Retain the newest 100 picker/workspace targets.
- [ ] ctrl-r / `/rewind` / clicking a rule / double-Esc with an empty composer opens
  `‹ checkpoint · pick a prompt · before turn N · $<cost> · <label> ›` with
  `[↑↓ code + conversation] [enter restore] [esc close]`. ←/→ navigate checkpoints; ↑/↓
  select `code + conversation` (default), `conversation only`, or `code only`.
- [ ] Conversation restore uses Amplifier Foundation's live context boundary, removes the
  selected prompt and every later conversation/transcript/ledger turn only after the backend
  accepts the change, and returns the selected prompt to the composer. Conversation-only
  never touches files; code-only never touches conversation or composer.
- [ ] Confirming while a turn runs requests the normal graceful interrupt, waits for turn
  close-out, then restores. Merely opening/navigating the picker does not interrupt.
- [ ] Code restore covers root-session `write_file`, `edit_file`, `create_file`,
  `delete_file`, and `apply_patch` targets whose preimages can be safely captured. It undoes
  the selected prompt and later tracked edits with per-file compare-and-swap: changed or
  divergent files are skipped with explicit warnings while independent safe files restore.
- [ ] Never checkpoint shell/interpreter, subagent, MCP/external, editor, or manual changes;
  outside-workspace paths, `.git`, symlinks, hard links, non-regular files, and files over
  8 MiB are excluded and surfaced as skips/warnings rather than overwritten. The same is
  true for files with extended attributes/ACLs, unsafe ownership, or non-default flags.
- [ ] Store manifests/preimages privately inside the session, persist them across resume,
  replay interrupted restore/branch transactions before new work, and prune beyond 100
  checkpoints. Same-workspace structured turns/restores are mutually exclusive, and a
  failed pre-prompt checkpoint returns the unsent rich draft. This is best-effort undo, not
  Git; there is no redo.

## 10. Ledger, evidence, context

- [ ] ctrl-l / `/ledger` prints to scrollback: `· Session ledger  <session> · <bundle>` + `  N turns · $X.XX · N shipped · N answer-only · cache hit NN%`.
- [ ] Footer `▲` (green) appears when last turn shipped (yield glyph).
- [ ] Clicking a final answer prints evidence block: `· Evidence  1/N · ←/→ select · enter expand · d detail · esc close` + numbered teal claims `¹ "quote" → <tool call that grounds it>`.
- [ ] `d` on a focused evidence block opens a side panel (docked beside the transcript) with the selected claim's detail: producing tool call, input/query summary, timestamp, source/output, and originating agent — joined by the claim's tool-call correlation id, never by display order.
- [ ] `d` again on the same claim closes the panel and restores scroll position + keyboard focus to the evidence row; `←/→` while open re-targets a different claim without closing.
- [ ] The panel collapses below an 80-column terminal (content preserved, not discarded) and restores on widening back out; opening it below that width shows a notice instead of a dead control.
- [ ] A claim with no correlation id, one whose tool call no longer resolves, or output too large for the panel each render an explicit fallback message, never a blank panel.
- [ ] `/context`: `· Context  NN% of 200k` + usage bar line.

## 11. Turn lifecycle & telemetry

- [ ] Live token/second counting while running; per-turn cost computed from provider usage.
- [ ] Interrupt (esc while running): stops at step boundary, prints italic recap `Interrupted. Goal: <goal>. Context saved; resume or restate direction.`, rule labeled `· interrupted`.
- [ ] Turn end notice: `agents N done` (or `turn interrupted · context saved`).
- [ ] Fan-out close-out: the running chrome (lane tail, live plan panel state) collapses
  into the durable delegate summary (§3) at turn end; the tail clears; summary
  expansion still works after `resume` (rebuilt from `ui-events.jsonl`).
- [ ] Session banner on start: line 1 bright bold `Amplifier <version> · core <core-version>`; line 2 dim `Bundle: <bundle> | Provider: <provider> | <model> · session <id6>`.

## 12. Non-visual requirements

- [ ] Built the amplifier-native way: thin app over amplifier-core; providers/tools/hooks come from mounted modules; bundle-driven config.
- [ ] Real sessions: streaming from amplifier-core events; persistence with resume,
  conversation restore, and private workspace checkpoints.
- [ ] Keybindings work in real terminals (document kitty-protocol need for shift+enter; graceful fallback).
- [ ] Resize reflows transcript without corruption.
- [ ] Mouse: click targets for rules, tool lines, lanes, palette rows, approval options, mode badge, needs-you chips (graceful no-mouse fallback).
- [ ] Test suite covering block grammar, mode gating, palette filtering, approval flow, steer/queue, checkpoints/rewind, ledger math, theme tokens.
