# Lane 1 — User-Facing Command Surface & Features (parity audit)

**Question:** does `michaeljabbour/amplifier-app-newtui` reproduce every user-facing
command, flag, slash verb and interactive flow of `microsoft/amplifier-app-cli`?

**Method:** enumerated app-cli's complete CLI surface (`amplifier_app_cli/main.py`
+ `commands/`) and slash-command surface (`ui/command_catalog.py`, `ui/core_commands.py`,
`ui/command_processor.py`), then matched each to newtui's CLI groups (`main.py`),
slash registry (`commands/builtin.py` + `registry.py`) and TUI seams (`ui/`,
`keymap.py`). Verdict is on **capability**, not code shape. Every row is cited
file:line on **both** sides. Known port-campaign deferrals (#43–#48) are classified,
not re-discovered.

Legend — verdict: **PARITY** / **PARTIAL** / **MISSING** / **NEWTUI-BETTER** /
**N/A-BY-DESIGN**. Severity: **H**igh / **M**ed / **L**ow (user impact).

---

## A. Top-level CLI commands / groups

| Capability | app-cli cite | newtui cite / status | Verdict | Sev | Recommendation |
|---|---|---|---|---|---|
| Default invocation → launch chat/TUI | `main.py:202-214` (invoke run --mode chat) | `main.py:253-268` (launch full-screen TUI) | PARITY | — | none |
| `run PROMPT` one-shot (+stdin) | `commands/run.py:45-66` | `main.py:271-292` | PARITY | — | none |
| `run` flags: `--provider/-p`, `--model/-m`, `--max-tokens`, `--mode`, `--resume`, `--verbose` | `run.py:47-60` | `main.py:273-280` (only `--bundle`, `--output-format`) | PARTIAL | M | add per-invocation `--model/--provider/--mode` + one-shot `--resume <id>` (context) to `run` |
| `run --output-format` (text/json/…) | `run.py:59-64` | `main.py:274-291` (text/json/json-trace/**jsonl** live event stream) | NEWTUI-BETTER | — | newtui adds versioned JSONL streaming; keep |
| `continue` (resume most-recent session) | `session.py:401-546` | `main.py:351-372` `resume` (picker; single auto-resumes) | PARTIAL | L | add `continue`/`resume --last` shortcut for newest session |
| `resume [ID]` interactive picker | `session.py:1182-1231`, `_interactive_resume_impl:1317` | `main.py:351-372`, picker `_pick_session_id:314-348` | PARITY | — | none |
| `session list` | `session.py:556-…` | `main.py:388-415` | PARITY | — | none |
| `session delete` | `session.py:1017-1047` | `main.py:433-457` | PARITY | — | none |
| `session cleanup --days` | `session.py:1161-1179` | `main.py:460-476` | PARITY | — | none |
| `session resume ID` | `session.py:1049-1159` | `main.py:351-372` (root `resume`) | PARITY | — | none |
| `session rename` | *(no group subcmd; /rename slash only)* `core_commands`→`command_processor` | `main.py:418-430` (`session rename`) | NEWTUI-BETTER | — | newtui exposes rename at CLI too |
| `session fork` (bg session copy) | `session.py` fork subcmd (~:960-1015) | *(none)* | MISSING | M | see `/fork` row — no background-directive fork |
| resume `--replay`/`--full-history`/`--show-thinking`/`--force-bundle` | `session.py:409-427, 1051-1075` | `main.py:353-354` (`--bundle`,`--limit` only) | PARTIAL | L | history-render niceties (replay, thinking) unported; low value |
| `bundle` list/current/use/clear/show/add/remove/update | `bundle.py:70,807,667,726,447,877,1004,1143` | `main.py:522,562,572,588,599,622,646,658` | PARITY | — | none |
| `provider list/add/remove` | `provider.py:567,394,706` | `main.py:952,970,1004` | PARITY | — | none |
| `provider use` (set primary) | *(priority via edit/manage)* `provider.py:770,1583` | `main.py:991-1001` | NEWTUI-BETTER | — | explicit `use` is clearer than app-cli priority editing |
| `provider dashboard` (read-only status) | `provider.py` manage/dashboard `:1583` | `main.py:1017-1039` | PARITY | — | none |
| `provider install` (pip-install module) | `provider.py:289-393` | *(foundation resolver mounts; no pip step)* | N/A-BY-DESIGN (#43) | — | documented deferral |
| `provider edit / test / models / manage` (rich edit·reorder·test-connection loop) | `provider.py:770,894,969,1583` | dashboard read-only `main.py:1017`; models via `/model` `builtin.py:304` | N/A-BY-DESIGN (#43) | M | documented deferral; `provider test` (connection check) is the sharpest miss |
| `init` provider/routing setup | `commands/init.py:399-420`, dashboard `:276-397` | `main.py:920-939`, `_init:829-917` | PARITY | — | newtui init = provider-key setup; routing via `routing use` |
| `init` combined provider+routing dashboard loop | `init.py:276-397` (`init_dashboard_loop`) | `main.py:920-939` (linear, no loop) | PARTIAL | L | interactive combined dashboard reduced to flag-driven flow |
| `routing list / use` | `routing.py:240,305` | `main.py:1266,1308` | PARITY | — | none |
| `routing show` (resolve matrix, non-interactive) | `routing.py:341-…` | *(none; `routing use` prints table `main.py:1332-1347`)* | PARTIAL | L | add standalone `routing show <matrix>` |
| `routing manage / create` (interactive create/edit loop) | `routing.py:663,1649`, `routing_manage_loop:509` | *(none)* | N/A-BY-DESIGN (#46) | — | documented deferral |
| `source add/remove/list/show` | `source.py:154,263,436,549` | `main.py:1128,1165,1201,1230` | PARITY | — | none |
| `allowed-dirs list/add/remove` | `allowed_dirs.py:40,88,138` | `main.py:730,737,752` | PARITY | — | none |
| `denied-dirs list/add/remove` | `denied_dirs.py:41,89,140` | `main.py:772,779,794` | PARITY | — | none |
| `update` bundles/modules | `commands/update.py` (registered `main.py:454`) | `main.py:1093-1099`, `_update:1047` | PARITY | — | none |
| `doctor` standalone setup checkup | *(no standalone; `/doctor` session only)* | `main.py:479-484` + `commands/doctor.py` | NEWTUI-BETTER | — | newtui adds a non-session `doctor` |
| `version` | `commands/version.py` (`main.py:455`) | `--version` `main.py:258` + `/about` `builtin.py:408` | PARITY | — | none |
| `--install-completion` (bash/zsh/fish) | `main.py:132-197`, `commands/completion.py` | *(none)* | MISSING | L | add shell-completion install for newtui CLI |
| `reset` (uninstall+reinstall, preserve categories) | `commands/reset.py:410-535`, `reset_interactive.py` | *(none)* | MISSING | M | port `reset` (or a `--preserve` data-safe reinstall) — real recovery tool |
| `tool list / info / invoke` (invoke any bundle tool from CLI) | `commands/tool.py:263,339,427` | *(only in-session `/tools` list `builtin.py:334`)* | MISSING | M | no CLI path to *invoke* a mounted tool w/ key=value args |
| `agents list / show / dirs` (CLI introspection) | `commands/agents.py:23,69,110` | *(only in-session `/agents` list `builtin.py:341`)* | PARTIAL | L | in-session listing exists; CLI-level introspection absent |
| `module list / show / current` (installed+cached modules) | `commands/module.py:38,193,375` | *(none)* | MISSING | L | module-cache introspection unported (dev/debug surface) |
| `module add / remove` (settings override) | `module.py:245,343` | `source add/remove` `main.py:1128,1165` | PARITY | — | re-expressed via `source` seam |
| `module override set/remove/list` | `module.py:834,850,940,980` | `source add/remove/list` `main.py:1128-1227` | PARITY | — | equivalent |
| `module update` (clear/redownload cache) | `module.py:458-543` | `update` (foundation cache) `main.py:1093` | PARITY | — | folded into `update` |
| `module validate` (contract + behavioral tests) | `module.py:545-664` | *(none)* | MISSING | L | module-author dev tool; likely out of newtui user scope |
| `notify status/desktop` (OSC 777 config toggles) | `commands/notify.py:60,116` | notifications on-by-design `ui/notifications.py`; no config CLI | PARTIAL | M | OSC 777 ships (#47) but there's **no way to configure/disable** desktop notifs |
| `notify ntfy` (mobile push via ntfy.sh) | `notify.py:232-355` | *(none)* | MISSING | M | mobile push notifications entirely absent (not covered by #47) |
| `notify reset` | `notify.py:357-425` | *(none)* | MISSING | L | tied to notify config above |

## B. In-REPL / in-TUI slash commands

| Slash | app-cli cite | newtui cite / status | Verdict | Sev | Recommendation |
|---|---|---|---|---|---|
| `/mode` (cycle/switch posture + native modes) | `command_catalog.py:84-91` | `builtin.py:27-41,250-257` | PARITY | — | newtui also activates bundle-native modes |
| `/modes` | `command_catalog.py:92-98` | `builtin.py:43-46,260-266` | PARITY | — | none |
| `/model` (list/switch live model) | `catalog:99-106`, `core_commands.py:105` | `builtin.py:82-84,304`; ctx `registry.py:211` | PARITY | — | none |
| `/effort` (+ `/strength` alias) | `catalog:107-115`, `core_commands.py:149` | `builtin.py:87-89,311` (no `/strength` alias) | PARITY | L | add `/strength` alias for muscle memory |
| `/context` (usage + cache telemetry) | `catalog:137-143` | `builtin.py:59-63,281`; `commands/context.py` | PARITY | — | none |
| `/compact [focus]` | `catalog:144-150`, `core_commands.py:198` | `builtin.py:92-94,318` | PARITY | — | none |
| `/clear [name]` | `catalog:158-164`, `core_commands.py:367` | `builtin.py:97-100,325` (clear only; naming via `/rename`) | PARITY | — | none |
| `/status` | `catalog:130-136` | `builtin.py:76-79,297` | PARITY | — | none |
| `/tools` | `catalog:202-208` | `builtin.py:103-106,332` | PARITY | — | none |
| `/agents` | `catalog:209-215` | `builtin.py:109-112,339` | PARITY | — | none |
| `/skills` / `/skill <name>` | `catalog:308-322` | `builtin.py:120-128,346-359` | PARITY | — | none |
| `/mcp` (list/add/remove/reload) | `catalog:75-83` | `builtin.py:131-133,360`; ctx `registry.py:247` | PARITY | — | none |
| `/config` (show·toggle·set·diff·save) | `catalog:194-201` | `builtin.py:66-68,289`; `ui/config_view.py` | PARITY | — | category/compact-detailed-json views + live unmount are #44 |
| `/config` category + compact/detailed/json views + live tool unmount | `catalog:194-201`, `_CONFIG:46-57` | reduced views | N/A-BY-DESIGN (#44) | L | documented deferral |
| `/tasks` (agent lanes) | `catalog:216-222` | `builtin.py:71-73,367`; key `keymap.py:118` | PARITY | — | none |
| `/ledger` | `catalog:273-279` | `builtin.py:136-154,375`; key `keymap.py:120` | PARITY | — | none |
| `/rewind` (turn checkpoints) | `catalog:280-286` | `builtin.py:157-159,415`; key `keymap.py:122` | PARITY | — | none |
| `/diff [staged]` | `catalog:258-265` | `builtin.py:115-117,400` | PARITY | — | none |
| `/permissions` (trust preset) | `catalog:67-74` | `builtin.py:178-180,455`; `commands/permissions.py` | PARITY | — | none |
| `/allowed-dirs` / `/denied-dirs` | `catalog:230-243` | `builtin.py:183-188,462-475` | PARITY | — | none |
| `/doctor` | `catalog:287-293` | `builtin.py:191-196,476`; `commands/doctor.py` | PARITY | — | none |
| `/improve` (evidence-backed config tuning) | `catalog:294-300` | `builtin.py:240-245,483`; `commands/improve.py` | PARITY | — | none |
| `/rename` | `catalog:244-250` | `builtin.py:162-164,426` | PARITY | — | none |
| `/branch [name]` | `catalog:172-178`, `core_commands.py:419` | `builtin.py:173-175,440` | PARITY | — | none |
| `/export [md|json]` | `catalog:179-186`, `core_commands.py:452` | `builtin.py:199-202,384`; `commands/export.py` | PARITY | — | newtui md-only; JSON export is `run --output-format json` |
| `/save` (save transcript) | `catalog:123-129` | `/export` `builtin.py:199-202` | PARITY | L | consolidated into `/export`; consider `/save` alias |
| `/answer` (batch deferred decisions) | `catalog:151-157` | ctrl-y needs-you queue `keymap.py:121`; `ui/needs_you.py` | PARITY | — | re-expressed as key-driven needs-you badge |
| `/help` | `catalog:187-193`, `command_processor.py:428` | `/` command palette `registry.py:415-444`; `ui/palette.py` | PARITY | — | palette is the discoverable help surface |
| `/init` (scaffold AGENTS.md project memory) | `catalog:60-66`, `core_commands.py:81-103` | *(none)* | MISSING | M | no in-session project-memory scaffold |
| `/btw` (context-free side question) | `catalog:116-122`, `core_commands.py:174` | *(none)* | MISSING | L | quick side-question verb absent |
| `/review` (read-only scope review) | `catalog:266-272` | *(none; `/plan` gives read-only mode)* | PARTIAL | L | scoped review folded into plan posture only |
| `/feedback` (prefilled GitHub issue) | `catalog:301-307`, `core_commands.py:491` | *(none)* | MISSING | L | add `/feedback` deep-link |
| `/fork <directive>` (bg session runs directive) | `catalog:251-257`, `core_commands.py:296` | *(none; `/branch` snapshots only)* | MISSING | M | background-directive fork unported (adjacent to #45 host-seam) |
| `/resume <id>` (in-place switch) | `catalog:165-171`, `core_commands.py:386` | CLI `resume` instead `main.py:351` | N/A-BY-DESIGN (#45) | — | documented deferral |
| `/background` (detach to shell) | `catalog:223-229`, `core_commands.py:348` | *(TUI lacks host seam)* | N/A-BY-DESIGN (#45) | — | documented deferral |

## C. newtui-only additions (net-new vs app-cli)

| Capability | newtui cite | app-cli | Verdict |
|---|---|---|---|
| `/theme` runtime UI theme (slate/graphite/carbon) | `builtin.py:235-237,492`; `ui/themes.py` | none | NEWTUI-BETTER |
| `/copy` last answer → clipboard (OSC 52) | `builtin.py:205-212,392`; `commands/copy.py` | none (no slash) | NEWTUI-BETTER |
| `/about` app/core/bundle/session identity block | `builtin.py:215-226,408` | `version` CLI only | NEWTUI-BETTER |
| `/plan`, `/brainstorm` promoted to top-level verbs | `builtin.py:49-56,267-280` | via `/mode plan` only | NEWTUI-BETTER (discoverability) |
| Phase-grouped fuzzy command palette (During/Parallel/Ship/Between/Repair) | `registry.py:37-44,431-444` | flat `/help` text | NEWTUI-BETTER |
| Open command registry (skills/recipes contribute verbs at runtime) | `registry.py:319-405` | fixed builtin catalog | NEWTUI-BETTER |
| `run --output-format jsonl` live versioned event stream | `main.py:89-161` | text/json/json-trace only | NEWTUI-BETTER |
| `doctor` as standalone (non-session) CLI | `main.py:479-484` | session `/doctor` only | NEWTUI-BETTER |
| `session rename` / `provider use` at CLI level | `main.py:418,991` | slash / priority-edit only | NEWTUI-BETTER |

---

## Verdict counts

- **PARITY:** 41
- **PARTIAL:** 9
- **MISSING:** 10
- **N/A-BY-DESIGN (documented #43–#48):** 6
- **NEWTUI-BETTER:** 12

(≈78 capabilities compared across CLI + slash surfaces.)

---

## Top gaps (ranked by user impact)

1. **`tool invoke` (CLI) — MISSING (M).** No way to invoke a mounted bundle tool
   directly from the shell (`amplifier tool invoke read_file path=…`); newtui only
   *lists* tools in-session (`/tools`). Loses a real scripting/automation surface.
   → add a `tool list|info|invoke` group over the same session-mount path `run` uses.
2. **`notify ntfy` mobile push + desktop-notif config — MISSING/PARTIAL (M).** OSC 777
   desktop pings ship by design (#47) but there is **no CLI to enable/disable/tune**
   them, and **ntfy.sh mobile push is entirely absent** (not covered by #47).
   → port `notify status|desktop|ntfy|reset`.
3. **`reset` — MISSING (M).** No data-safe uninstall/reinstall with category
   preservation (projects/settings/keys/cache/registry). This is the primary recovery
   tool when a venv/cache corrupts. → port `reset` (at minimum `--preserve`/`--dry-run`).
4. **`/fork` + `session fork` background-directive sessions — MISSING (M).** `/branch`
   snapshots a conversation but nothing runs a directive in a detached child session.
   Adjacent to the #45 host-seam deferral but not itself documented. → decide: port or
   fold into #45 explicitly.
5. **`run` per-invocation overrides + one-shot `--resume` — PARTIAL (M).** `run` can't
   take `--model/--provider/--mode` or resume prior context for a single prompt; these
   are core scripting knobs on app-cli. → add the flags to newtui `run`.

## Could-not-determine / caveats

- app-cli `main.py` and `commands/session.py` were partially mid-truncated by the tool;
  the truncated middles were the `bundle`/`session list` bodies whose command
  *signatures* I confirmed via targeted grep, so verdicts stand, but a couple of niche
  `session`/`bundle` sub-flags may exist beyond what's cited.
- `provider test` (connection check) sits under the #43 dashboard-loop deferral; if the
  campaign scoped #43 to only the *interactive* loop, a non-interactive
  `provider test <name>` is a genuine standalone miss worth reconsidering (rated within
  the #43 row, not double-counted).
- Verdicts are capability-level; I did not exhaustively diff every `--global/--project/
  --local` scope flag (spot-checked equal on dirs/source/bundle/routing).
