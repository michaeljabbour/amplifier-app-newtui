---
bundle:
  name: newtui
  version: 0.1.0
  description: |
    Default bundle for amplifier-app-newtui — the Amplifier full-screen
    Textual TUI (v3 Cohesive). A lean, real bundle: streaming orchestrator
    (both event channels), simple context, Anthropic provider, core tools.
    The TUI renders everything itself, so no printing hook modules are
    mounted here (hooks-streaming-ui must never be mounted by this app).

session:
  raw: true
  orchestrator:
    module: loop-streaming
    source: git+https://github.com/microsoft/amplifier-module-loop-streaming@main
    config:
      extended_thinking: true
  context:
    module: context-simple
    source: git+https://github.com/microsoft/amplifier-module-context-simple@main
    config:
      max_tokens: 200000
      compact_threshold: 0.8
      auto_compact: true

providers:
  - module: provider-anthropic
    source: git+https://github.com/microsoft/amplifier-module-provider-anthropic@main
    config:
      priority: 1

tools:
  - module: tool-filesystem
    source: git+https://github.com/microsoft/amplifier-module-tool-filesystem@main
  - module: tool-bash
    source: git+https://github.com/microsoft/amplifier-module-tool-bash@main
  - module: tool-web
    source: git+https://github.com/microsoft/amplifier-module-tool-web@main
  - module: tool-search
    source: git+https://github.com/microsoft/amplifier-module-tool-search@main
  - module: tool-task
    source: git+https://github.com/microsoft/amplifier-module-tool-task@main
  # MCP servers: tool-mcp reads ~/.amplifier/mcp.json (+ ./.amplifier/mcp.json)
  # and mounts each remote server's tools as mcp_<server>_<tool>. No mcp.json
  # ⇒ no-op. Managed in-app via /mcp.
  - module: tool-mcp
    source: git+https://github.com/microsoft/amplifier-module-tool-mcp@main
  # Skills: tool-skills exposes the load_skill tool (list/load). Driven
  # in-app via /skills and /skill. visibility.enabled: false matches the
  # anchors default (no per-request auto-injection — the TUI lists on demand).
  - module: tool-skills
    source: git+https://github.com/microsoft/amplifier-bundle-skills@main#subdirectory=modules/tool-skills
    config:
      visibility:
        enabled: false
  # Native modes: tool-mode lets the app switch the active mode (the app
  # drives it from its shift+tab posture bridge). gate_policy warn = the
  # first agent-initiated set is confirmed (the app retries once).
  - module: tool-mode
    source: git+https://github.com/microsoft/amplifier-bundle-modes@main#subdirectory=modules/tool-mode
    config:
      gate_policy: warn

# Approval / mode enforcement — OFF BY DEFAULT (DESIGN: docs/notes/feature-mapping.md).
# hooks-mode (tool:pre pri -20) sets require_approval_tools from the active
# mode; hooks-approval (pri -10) prompts via the app's ApprovalBroker.
# policy_driven_only + no active mode ⇒ require_approval_tools empty ⇒ NOTHING
# is gated. Gating only turns on when a posture activates a mode whose YAML
# lists confirm/block/warn tools. default_action: continue ⇒ a provider
# timeout falls through to allow, never a spurious deny.
hooks:
  - module: hooks-mode
    source: git+https://github.com/microsoft/amplifier-bundle-modes@main#subdirectory=modules/hooks-mode
    config:
      search_paths: []
  - module: hooks-approval
    source: git+https://github.com/microsoft/amplifier-module-hooks-approval
    config:
      rules: []
      default_action: continue
      policy_driven_only: true
# Model routing (hooks-routing) is intentionally NOT mounted here — the
# anchors default doesn't mount it either; it arrives via a bundle.app
# overlay (routing-matrix) when the user wants it. The spawner still threads
# provider_preferences/model_role, so routing works the moment an overlay
# registers the model_role_resolver capability.

agents:
  include:
    - foundation:explorer
    - foundation:zen-architect
    - foundation:bug-hunter
    - foundation:test-coverage
    - foundation:modular-builder
    - foundation:web-research
---

# Amplifier NewTUI Bundle

This is the app's REAL bundle — `resolve_config()` discovers it by name
(`newtui`), loads it via foundation's `load_bundle`, composes any settings
overlays (`bundle.app`), and `prepare()`s it exactly once per app start.

A packaged copy ships inside the wheel at
`amplifier_app_newtui/data/bundles/newtui.md` (lowest-precedence search
path); project (`.amplifier/bundles/`) and user (`~/.amplifier/bundles/`)
bundles override it by name.

You are Amplifier, driven through a full-screen terminal UI. Be direct and
concrete. Prefer running tools over speculating. When you complete work that
changes files, summarize what shipped; when you only answer, keep it tight.
