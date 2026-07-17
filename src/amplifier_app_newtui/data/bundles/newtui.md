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
