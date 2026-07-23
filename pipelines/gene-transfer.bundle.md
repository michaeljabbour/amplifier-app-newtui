---
bundle:
  name: gene-transfer-runner
  version: 0.1.0
  description: >
    Launcher bundle for the gene-transfer pipeline. Composes the attractor
    multi-provider pipeline (which mounts the loop-pipeline orchestrator and the
    per-provider child agents) and points the orchestrator at
    pipelines/gene-transfer.dot with this machine's paths. Run it headless with
    `amplifier run --bundle pipelines/gene-transfer.bundle.md "go"` — no
    run_pipeline tool required; the orchestrator IS the pipeline runner.

includes:
  - bundle: attractor:bundles/attractor-pipeline

# Orchestrator reasoning + child nodes run on opus-4-8 (verified working here;
# fable-5's dual-use safety measures refuse autonomous self-porting).
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
      # Re-declared from attractor-pipeline so a non-recursive config merge can't
      # drop them (llm_provider in each DOT node maps to a child agent here).
      profiles:
        anthropic: attractor-agent-anthropic
        openai: attractor-agent-openai
        gemini: attractor-agent-gemini
      dot_file: pipelines/gene-transfer.dot
      logs_root: ./runs
      params:
        donor_path: /Users/michaeljabbour/dev/amplifier-app-cli
        newtui_path: /Users/michaeljabbour/dev/amplifier-app-newtui
        forge_tool: /Users/michaeljabbour/.claude/skills/amplifier-skill-forge/tools/forge.py
---

# Gene-transfer runner

This bundle exists only to launch [`gene-transfer.dot`](gene-transfer.dot). See
[README.md](README.md) for the full runbook, monitoring, and guardrails.

```sh
cd /Users/michaeljabbour/dev/amplifier-app-newtui
amplifier run --bundle pipelines/gene-transfer.bundle.md "run the gene-transfer pipeline"
```

The orchestrator ignores the prompt text (the goal lives in the graph) and walks the
pipeline, writing progress to `./runs/checkpoint.json` and advancing
`pipelines/ledger.tsv`.
