---
bundle:
  name: newtui
  version: 0.2.0
  description: |
    Thin wrapper bundle for amplifier-app-newtui — the Amplifier full-screen
    Textual TUI. Composes foundation's `anchors` bundle (the amplifier-app-cli
    default: streaming orchestrator, 300k context, standard tool roster with
    tool-delegate subagents, and six bundle-local agents) and overlays only
    what the TUI needs: a default provider so fresh installs boot, tool-mcp,
    tool-team-pulse, hooks-notify-push, and the terminal response contract.
    The TUI renders everything itself; printing hooks composed in via
    anchors and the OSC/BEL-writing hooks-notify are suppressed at boot
    by the app kernel (built-in suppression list + the `hooks.suppress`
    setting). hooks-logging mounts natively and owns the canonical
    events.jsonl; the app's UIEvent log lives in ui-events.jsonl.

includes:
  # anchors, pinned to a specific amplifier-foundation commit.
  # PARTIAL PIN: this pins only anchors' own bundle.md — its internal
  # includes (behaviors/*.yaml) and module sources still reference @main
  # and keep floating until upstream pins them. No worse than the previous
  # vendored bundle (which floated 8 modules @main).
  - bundle: git+https://github.com/microsoft/amplifier-foundation@93615d9847ce40313cc0d60583cb886de4337f9e#subdirectory=bundles/anchors/bundle.md

providers:
  # anchors is provider-agnostic by design; this app hard-fails boot at zero
  # providers, so the wrapper keeps a default. Reconfigure or add providers
  # via settings `config.providers`.
  - module: provider-anthropic
    source: git+https://github.com/microsoft/amplifier-module-provider-anthropic@main
    config:
      priority: 1

tools:
  # MCP servers: tool-mcp reads ~/.amplifier/mcp.json (+ ./.amplifier/mcp.json)
  # and mounts each remote server's tools as mcp_<server>_<tool>. No mcp.json
  # ⇒ no-op. Managed in-app via /mcp.
  - module: tool-mcp
    source: git+https://github.com/microsoft/amplifier-module-tool-mcp@main
  # team-pulse: read-only lens over a team corpus (all GET endpoints). url/key
  # are empty here by design — mount() resolves them from settings or the
  # AMPLIFIER_TEAM_PULSE_URL / _KEY env vars, and is skipped (degraded, not
  # fatal) when unconfigured, so a clean install without a corpus still boots.
  - module: tool-team-pulse
    source: git+https://github.com/microsoft/amplifier-bundle-team-pulse@main#subdirectory=modules/tool-team-pulse
    config:
      url: ""
      key: ""
  # Skills: anchors pins tool-skills to the foundation skill set, which
  # REPLACES tool-skills' default scan of ~/.amplifier/skills (its source-
  # resolution priority 1 wins). Re-mount here (later bundles override
  # earlier ones) with the same foundation set PLUS the user dir, so skills
  # installed for other harnesses (Claude Code, Codex) are visible to
  # amplifier too. Missing local dirs are skipped, not fatal.
  - module: tool-skills
    source: git+https://github.com/microsoft/amplifier-bundle-skills@main#subdirectory=modules/tool-skills
    config:
      skills:
        - "git+https://github.com/microsoft/amplifier-foundation@main#subdirectory=skills"
        - "~/.amplifier/skills"

hooks:
  # Unattended-session push notifications via ntfy.sh — a clean HTTP
  # side-channel (aiohttp POST, no stdout, TUI-safe). No-op unless
  # configured: without AMPLIFIER_NTFY_TOPIC in the environment, mount()
  # disables itself with a log warning. listen_event is pinned to the raw
  # orchestrator:complete event because the default (notify:turn-complete)
  # is emitted by hooks-notify, which the app kernel suppresses at boot
  # (raw OSC-777/BEL stdout corrupts the full-screen Textual TUI).
  - module: hooks-notify-push
    source: git+https://github.com/microsoft/amplifier-bundle-notify@main#subdirectory=modules/hooks-notify-push
    config:
      listen_event: "orchestrator:complete"
---

# Amplifier NewTUI Bundle

This is the app's REAL bundle — `resolve_config()` discovers it by name
(`newtui`), loads it via foundation's `load_bundle`, composes any settings
overlays (`bundle.app`), and `prepare()`s it exactly once per app start.

It is a THIN WRAPPER: the session (streaming orchestrator + 300k context),
tool roster (including `tool-delegate` subagents), hooks, and the six
bundle-local agents all come from the composed `anchors` bundle above. This
file overlays only the default provider, two TUI-specific tools, and the
terminal response contract below (which composes alongside anchors'
system.md). Printing hooks and the OSC/BEL-writing `hooks-notify`
composed in via anchors are stripped at boot by the app kernel's
suppressed-hooks mechanism; `hooks-logging` mounts natively (it owns the
canonical `events.jsonl`; the app's UIEvent log is `ui-events.jsonl`),
and the wrapper's own `hooks-notify-push` (ntfy HTTP push) survives it —
a stdout-free side-channel that no-ops unless `AMPLIFIER_NTFY_TOPIC` is
set.

A packaged copy ships inside the wheel at
`amplifier_app_newtui/data/bundles/newtui.md` (lowest-precedence search
path); project (`.amplifier/bundles/`) and user (`~/.amplifier/bundles/`)
bundles override it by name.

## Terminal response contract

You are Amplifier, driven through a full-screen terminal UI. Prefer running
tools over speculating. This surface renders a supported Markdown subset:

- Lead with the answer, result, or current blocker.
- Default to short, direct responses with small paragraphs or flat lists.
- Do not repeat the prompt, tool logs, task state, or internal narration that
  the UI already displays.
- Close implementation work with what changed, verification, and any blocker
  or required next action.
- Do not emit Markdown images. Keep tables to four columns or fewer and lists
  shallow.
- Put layout-sensitive or copyable structured content in language-tagged fenced
  code blocks.
- Expand only when the user asks or correctness requires the detail.
