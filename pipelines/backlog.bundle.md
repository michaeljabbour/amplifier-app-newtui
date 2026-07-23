---
bundle:
  name: backlog-attractor-runner
  version: 0.1.0
  description: >
    Launcher for the backlog attractor pipeline (pipelines/backlog.dot) — the
    routed generalization of gene-transfer.dot covering the WHOLE backlog:
    port / internal / decision treatments per pipelines/categories.tsv.
    Register once (`amplifier bundle add ./pipelines/backlog.bundle.md --app`)
    then `amplifier run --bundle backlog-attractor-runner "go"`.

includes:
  - bundle: attractor:bundles/attractor-pipeline

providers:
  - module: provider-anthropic
    source: git+https://github.com/microsoft/amplifier-module-provider-anthropic@main
    config:
      default_model: claude-opus-4-8

session:
  orchestrator:
    module: loop-pipeline
    source: git+https://github.com/microsoft/amplifier-bundle-attractor@main#subdirectory=modules/loop-pipeline
    config:
      profiles:
        anthropic: attractor-agent-anthropic
        openai: attractor-agent-openai
        gemini: attractor-agent-gemini
      dot_file: pipelines/backlog.dot
      logs_root: ./runs
      params:
        donor_path: /Users/michaeljabbour/dev/amplifier-app-cli
        newtui_path: /Users/michaeljabbour/dev/amplifier-app-newtui
        work_tree: /Users/michaeljabbour/dev/newtui-wt/wt1
        forge_tool: /Users/michaeljabbour/.claude/skills/amplifier-skill-forge/tools/forge.py
---

# Backlog attractor runner

Launches [`backlog.dot`](backlog.dot). All work happens in a git worktree
(`work_tree` param), never the user's checkout; branches are `auto/<slug>`,
one issue per branch, one PR each. See [README.md](README.md).
