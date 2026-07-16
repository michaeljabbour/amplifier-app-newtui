# Amplifier TUI v3 — Cohesive: Compliance Specification

Ground truth: `docs/design-v3-cohesive.html` (Amplifier TUI v3 - Cohesive.dc.html).
Every item below is a testable requirement. The rebuild is done when every checkbox
can be demonstrated in the real terminal app.

## 1. Themes & design tokens

Three themes, switchable at runtime. Exact token values (from the mockup):

| Token | slate | graphite | carbon |
|---|---|---|---|
| bg-page | `#12151c` | `#131110` | `#0c0e12` |
| bg-term | `#232937` | `#211e1a` | `#14171d` |
| bg-chrome | `#191d27` | `#181512` | `#0f1116` |
| bg-tab | `#2b3243` | `#2c2722` | `#1f242e` |
| fg | `#c9d1e0` | `#d6cfc4` | `#cdd6e4` |
| bright | `#eef2f8` | `#f2ede4` | `#f4f7fc` |
| dim | `#6b7487` | `#8a8175` | `#65718a` |
| dimmer | `#4a5163` | `#575047` | `#3d4657` |
| green | `#7ec699` | `#98c28b` | `#6fd39c` |
| orange | `#e0a458` | `#dba15c` | `#e9b14f` |
| red | `#e06c75` | `#d97371` | `#ef6e7b` |
| blue | `#7aa2f7` | `#90a4d8` | `#6f9df2` |
| teal | `#6fc3c3` | `#80bcae` | `#57c8c8` |
| rule | `#333b4d` | `#3a352e` | `#2a3140` |

- [ ] All UI color comes from these named tokens only (no ad-hoc colors).
- [ ] Theme switchable at runtime (settings/command), default `slate`.
- [ ] Monospace rendering; JetBrains-Mono-flavored glyph choices (❯ ● ✳ ✦ ✧ ■ ✔ □ ⊘ ◐ ├─ └ ↳ ▲ ▹ ‹ ›).

## 2. Screen layout (top → bottom)

1. **Title bar** (bg-chrome): centered title `amplifier-app-newtui — Amplifier — <state> — <bundle> — <session-short>`; while running, prefix with orange spinner glyph cycling `✳ ✦ ✧ ✦` every ~260ms; title's `<state>` reflects current plan step (lowercased) or `ready` / `planning` / `brainstorming` / `✳ coordinating N agents`.
2. **Transcript** (bg-term): scrollable region, the main body.
3. **Notice slot**: transient right-aligned dim text floating at transcript bottom edge (auto-dismiss ~4s), e.g. `mode plan · read-only`, `steer queued · shift+enter queues a full next-turn message`.
4. **Overlay strips** (each a bordered strip above composer, shown when active):
   - Command palette (max-height scrollable list)
   - Agent lanes panel
   - Rewind picker strip
   - Queued-message strip
   - Approval bar (replaces composer while open)
5. **Composer**: left edge tinted 2px in mode accent; `[mode]` badge (clickable/cyclable) + green bold `❯` + input. Placeholder: `Message Amplifier…  ( / commands · shift+tab mode · enter send · type mid-turn to steer )`.
6. **Footer status bar** (bg-chrome): left = `mode <mode>` (mode color) `· <trust> · <bundle> · <session-short> · $<cost><yield▲><queued q1>` and optional orange `N decisions waiting · ctrl-y`; right = context-sensitive hints.

- [ ] Layout matches order & styling above.
- [ ] Footer hints change by state:
  - approval open → `arrows select · enter confirm · esc deny`
  - lane focused → `esc back to parent · transcript is the subagent's own`
  - palette open → `↑↓ select · enter run · esc close`
  - running → `esc interrupt · enter steer · shift+enter queue`
  - idle → `/ commands · shift+tab mode · ctrl-t tasks`

## 3. Transcript block grammar

- [ ] **User line**: `❯ ` (green bold) + `[mode] ` (mode color) + text (bright). Mode badge stamps scrollback permanently.
- [ ] **Narration**: `● ` bright bullet + fg text.
- [ ] **Tool line (collapsed)**: `  ● <summary> ` in dim + `· click to expand` in dimmer. Click/enter toggles an indented dimmer body line below. One dim line per tool batch (e.g. `Ran 2 shell commands`).
- [ ] **Live command line**: `  └ ` dimmer + `$ <cmd>` dim while executing; replaced by collapsed tool line when done.
- [ ] **Plan checklist**: header `· ` orange + title + trailing dim telemetry `(Ns · ↓ X.Xk tok)` updated live; items `  □ ` dimmer/pending, `  ■ ` orange bold/active, `  ✔ ` green + dim text/done.
- [ ] **Blocked**: `  ⊘ blocked · <cmd> ` red + `· <reason> · <continuation>` dim. Never halts the turn by itself.
- [ ] **Working status line** (while running): pulsing spinner `✳/✦/✧` orange + `working · Ns · ↓ X.Xk tok · N agent(s) · ` dim + `esc to interrupt · type to steer` dimmer. Updates every second; removed at turn end.
- [ ] **Recap line** (turn end): `✳ ` dimmer + italic dim `Goal: <goal>. Next: <next>.`
- [ ] **Final answer**: fg text with selective bright/bold and teal inline code; clickable → evidence.
- [ ] **Steer echo**: `  ↳ ` teal + `steer queued: "<text>" ` teal + `· applies at next step boundary` dimmer; steer application logged as narration `Applying steer: <text>`.
- [ ] **Turn rule**: full-width 1px rule (rule token) + right-aligned label `<Ns> · <X.Xk> tok, <N>% cached · $<cost> · <outcome>`; label dim when shipped, dimmer when answer-only/interrupted. Outcomes seen in mockup: `answer`, `3 files · +142/−38 · tests ✔`, `· interrupted`, `· plan ready`.
- [ ] Turn rules are clickable → open rewind picker at that checkpoint.

## 4. Modes & trust

| mode | color | trust string |
|---|---|---|
| chat | dim | `ask all · auto read` |
| plan | blue | `read-only` |
| brainstorm | teal | `no tools` |
| build | green | `auto read,test · ask write,net,spend` |
| auto | orange | `auto read,write · classifier-gated` |

- [ ] shift+tab cycles modes (also when input focused); clicking `[mode]` badge cycles.
- [ ] Mode change → notice `mode <id> · <trust>`.
- [ ] Mode tint appears in exactly three places: composer badge + composer left edge + footer. chat's composer edge uses rule token.
- [ ] Trust profiles actually gate tools: plan = read-only, brainstorm = no tools, chat = ask everything except reads, build = auto read/test ask write/net/spend, auto = auto read/write with policy gate.
- [ ] Plan mode produces a plan block marked `(read-only)`; recap: `Plan ready. shift+tab to build hands it over for execution.` Switching to build offers/executes the handoff.

## 5. Composer input semantics

- [ ] Idle + Enter → send as user turn.
- [ ] Running + Enter → **steer** this turn (applies at next step boundary; echoed with ↳; consumed steer removed).
- [ ] Running + Shift+Enter (or second steer) → **queue** full next-turn message; queued strip shows `▹ queued next: "<text>" · runs when this turn ends`; footer shows ` · q1`; auto-runs at turn end (`queued message picked up`).
- [ ] `/` prefix opens the palette live-filtered as you type.
- [ ] Esc priority order: lane-focus → palette → rewind → lanes → interrupt-running.

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
- [ ] Trust-boundary blocks in auto mode → deferred decision: narration explains, footer badge `1 decision waiting · ctrl-y`, run continues to a shipped-locally outcome.
- [ ] ctrl-y / badge click → `Needs you  N deferred decision` orange block listing numbered decisions with inline actionable choice chips (e.g. `[yes · push to fork]` green on bg-tab); acting on one logs `Applying decision: …` and clears the badge.

## 8. Agent lanes & subagent focus

- [ ] ctrl-t (or `/tasks`) toggles lanes panel: header `Agent lanes · ↑↓ select · enter focus · esc close` + one aligned line per subagent: `  <glyph> <name> · <activity> · <elapsed> · $<cost>` (glyph/color per state: ◐ teal running, ■ fg working, ✔ dim done).
- [ ] Multi-agent turn renders a live tree in transcript: `  ├─ ● <name> · <activity> · $<cost>` dim, completing to `  ├─ ✔ … · done · <result> · <t> · $<cost>` green check.
- [ ] Selecting a lane focuses that subagent: transcript swaps to the child's own transcript with banner `focused: <name> · subagent of <parent-session> · own context window · results report back to parent · esc back`, its delegated brief as user-line `[delegated]`, its log, its state recap. Esc returns to parent (`back to parent session`).
- [ ] Title while coordinating: `… — ✳ coordinating N agents — …`.

## 9. Rewind & checkpoints

- [ ] Every turn rule records a checkpoint `{id: tN, label, cost-at-time}`.
- [ ] ctrl-r / `/rewind` / clicking a rule opens picker strip: `‹ rewind › tN · $<cost> · <label> › [enter fork] [esc close]`; ‹/› navigate, fork forks the session from that checkpoint.
- [ ] Forking actually restores conversation state to that point (session fork in the store).

## 10. Ledger, evidence, context

- [ ] ctrl-l / `/ledger` prints to scrollback: `· Session ledger  <session> · <bundle>` + `  N turns · $X.XX · N shipped · N answer-only · cache hit NN%`.
- [ ] Footer `▲` (green) appears when last turn shipped (yield glyph).
- [ ] Clicking a final answer prints evidence block: `· Evidence  1/N · ←/→ select · enter expand · esc close` + numbered teal claims `¹ "quote" → <tool call that grounds it>`.
- [ ] `/context`: `· Context  NN% of 200k` + usage bar line.

## 11. Turn lifecycle & telemetry

- [ ] Live token/second counting while running; per-turn cost computed from provider usage.
- [ ] Interrupt (esc while running): stops at step boundary, prints italic recap `Interrupted. Goal: <goal>. Context saved; resume or restate direction.`, rule labeled `· interrupted`.
- [ ] Turn end notice: `agents N done` (or `turn interrupted · context saved`).
- [ ] Session banner on start: line 1 bright bold `Amplifier <version> · core <core-version>`; line 2 dim `Bundle: <bundle> | Provider: <provider> | <model> · session <id6>`.

## 12. Non-visual requirements

- [ ] Built the amplifier-native way: thin app over amplifier-core; providers/tools/hooks come from mounted modules; bundle-driven config.
- [ ] Real sessions: streaming from amplifier-core events; persistence with resume + fork.
- [ ] Keybindings work in real terminals (document kitty-protocol need for shift+enter; graceful fallback).
- [ ] Resize reflows transcript without corruption.
- [ ] Mouse: click targets for rules, tool lines, lanes, palette rows, approval options, mode badge, needs-you chips (graceful no-mouse fallback).
- [ ] Test suite covering block grammar, mode gating, palette filtering, approval flow, steer/queue, checkpoints/rewind, ledger math, theme tokens.
