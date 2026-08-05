---
bundle:
  name: tui
  version: 0.2.0
  description: |
    Thin wrapper bundle for amplifier-app-tui — the Amplifier full-screen
    Textual TUI. Composes foundation's `anchors` bundle (the amplifier-app-cli
    default: streaming orchestrator, 300k context, standard tool roster with
    tool-delegate subagents, and six bundle-local agents) and overlays only
    what the TUI needs: a default provider so fresh installs boot, tool-mcp,
    tool-team-pulse, and the terminal response contract. The app runtime owns
    ntfy delivery from its durable attention events rather than mounting a
    second raw-completion producer.
    The TUI renders everything itself; printing hooks composed in via
    anchors and the OSC/BEL-writing hooks-notify are suppressed at boot
    by the app kernel (built-in suppression list + the `hooks.suppress`
    setting). hooks-logging mounts natively and owns the canonical
    events.jsonl; the app's UIEvent log lives in ui-events.jsonl.

includes:
  # Anchors is pinned to the Foundation commit reviewed on 2026-08-05. The
  # pinned Foundation dependency now resolves non-tip SHAs by cloning and
  # checking out the commit (cold-cache integration-tested), so the historical
  # @main exception is no longer necessary. Anchors' own descriptors still
  # spell nested sources as @main; kernel/source_lock.py applies the packaged
  # anchors-source-lock.json at both Foundation resolver seams (includes and
  # modules), including source URIs nested in tool configuration.
  - bundle: git+https://github.com/microsoft/amplifier-foundation@dea5bd8fe11a7617dbcfc61c47f9f4f2fdc0b134#subdirectory=bundles/anchors/bundle.md

providers:
  # anchors is provider-agnostic by design; this app hard-fails boot at zero
  # providers, so the wrapper keeps a default. Reconfigure or add providers
  # via settings `config.providers`.
  # Pinned 2026-08-02 (compliance B9): no release tag exists upstream, so this
  # is the repo's current @main HEAD SHA. Re-resolve via `git ls-remote` / `gh`
  # and bump here + tui.md together.
  - module: provider-anthropic
    source: git+https://github.com/microsoft/amplifier-module-provider-anthropic@94a435482a879a1c506b2ea9076a951875e89c9d
    config:
      priority: 1

tools:
  # MCP servers: tool-mcp reads ~/.amplifier/mcp.json (+ ./.amplifier/mcp.json)
  # and mounts each remote server's tools as mcp_<server>_<tool>. No mcp.json
  # ⇒ no-op. Managed in-app via /mcp.
  # Pinned 2026-08-02 (compliance B9) to @main's current HEAD SHA (no release
  # tag exists upstream).
  - module: tool-mcp
    source: git+https://github.com/microsoft/amplifier-module-tool-mcp@22f3d14cabc3789b3344661ab16e8d487431c4ac
  # team-pulse: read-only lens over a team corpus (all GET endpoints). url/key
  # are empty here by design — mount() resolves them from settings or the
  # AMPLIFIER_TEAM_PULSE_URL / _KEY env vars, and is skipped (degraded, not
  # fatal) when unconfigured, so a clean install without a corpus still boots.
  # Pinned 2026-08-02 (compliance B9) to @main's current HEAD SHA (no release
  # tag exists upstream; matches the team-pulse-lib rev already pinned in
  # pyproject.toml's [tool.uv.sources]).
  - module: tool-team-pulse
    source: git+https://github.com/microsoft/amplifier-bundle-team-pulse@e89574d2b90814a0c10a2164aa7d5c9cc43bd3ce#subdirectory=modules/tool-team-pulse
    config:
      url: ""
      key: ""
  # Skills: anchors pins tool-skills to the foundation skill set, which
  # REPLACES tool-skills' default scan of ~/.amplifier/skills (its source-
  # resolution priority 1 wins). Re-mount here (later bundles override
  # earlier ones) with the same foundation set PLUS the user dir, so skills
  # installed for other harnesses (Claude Code, Codex) are visible to
  # amplifier too. Missing local dirs are skipped, not fatal.
  # Pinned 2026-08-02 (compliance B9): amplifier-bundle-skills has a release
  # tag (v1.1.0, confirmed to still ship modules/tool-skills); the foundation
  # skills/ scan below stays on the reviewed v2.1.2 release artifact
  # (confirmed to ship skills/) and is updated deliberately. amplifier-app-
  # cli's packaged skill source is pinned independently: this exposes native
  # first-party skills such as /goalify without importing or copying CLI
  # runtime code. Workspace/user dirs follow app-cli's precedence contract.
  - module: tool-skills
    source: git+https://github.com/microsoft/amplifier-bundle-skills@v1.1.0#subdirectory=modules/tool-skills
    config:
      skills:
        - "git+https://github.com/microsoft/amplifier-foundation@v2.1.2#subdirectory=skills"
        - "git+https://github.com/microsoft/amplifier-app-cli@5462f1e04099269e6487519676875fccd0980bd5#subdirectory=amplifier_app_cli/data/skills"
        - ".amplifier/skills"
        - "~/.amplifier/skills"

hooks:
  # Redaction allowlist extension (module-native config; the module unions
  # user entries with its structural defaults). anchors' redaction behavior
  # scrubs live event payloads, and the delegate lifecycle carries its
  # routing ids in sub_session_id / parent_session_id — fields NOT in the
  # module's DEFAULT_ALLOWLIST (session_id/parent_id are). Verified live:
  # without this, those ids arrive as "[REDACTED:PII]…" and child→lane
  # routing (telemetry, focus transcripts, banners) degrades or breaks.
  # Pinned 2026-08-02 (compliance B9) to @main's current HEAD SHA (no release
  # tag exists upstream).
  - module: hook-redaction
    source: git+https://github.com/microsoft/amplifier-bundle-redaction@094d4948ab24414b574964d8398a8663b96cdd15#subdirectory=modules/hook-redaction
    config:
      allowlist:
        - sub_session_id
        - parent_session_id
---

# Amplifier TUI Bundle

This is the app's REAL bundle — `resolve_config()` discovers it by name
(`tui`), loads it via foundation's `load_bundle`, composes any settings
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
while the app runtime's stdout-free attention destination consumes only
normalized `attention:recorded` / `attention:acknowledged` events. It no-ops
unless `AMPLIFIER_NTFY_TOPIC` is set and never mounts a second
`orchestrator:complete` notification producer.

A packaged copy ships inside the wheel at
`amplifier_app_tui/data/bundles/tui.md` (lowest-precedence search
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
