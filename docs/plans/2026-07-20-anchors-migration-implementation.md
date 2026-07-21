# Anchors Migration Implementation Plan

> **Execution:** Use the subagent-driven-development workflow to implement this plan.
> **For execution:** `/execute-plan`

**Goal:** Replace amplifier-app-newtui's vendored `newtui` bundle with a thin wrapper that composes foundation's `anchors` bundle (the amplifier-app-cli default) plus TUI-specific overlays, and adapt the kernel so everything anchors brings works in the TUI.

**Architecture:** `bundle.md` keeps `name: newtui` (discovery unchanged) but becomes a thin wrapper: an `includes:` of anchors pinned to foundation SHA `93615d9847ce40313cc0d60583cb886de4337f9e`, plus only a default provider (`provider-anthropic`) and two TUI-specific tools (`tool-mcp`, `tool-team-pulse`). The kernel's hardcoded `_PRINTING_HOOKS` strip becomes a settings-extensible suppressed-hooks mechanism (defaults: the four printers + `hooks-logging`, which would double-write the app-owned `events.jsonl`), with a user-visible notice at boot. Resume gains a bundle-mismatch notice. The event pipeline (normalize → queue bridge → task tracker) learns the three `delegate:*` event names it doesn't yet handle, so anchors' `tool-delegate` drives the subagent lanes.

**Tech Stack:** Python 3.12, pytest (offline suite), ruff, pyright, PyYAML, Graphviz (`/opt/homebrew/bin/dot`), amplifier-core/foundation (kernel layer only).

---

## Ground rules (read before Task 1)

- **Repo:** `/Users/michaeljabbour/dev/amplifier-app-newtui`, branch `main`. Run everything from the repo root.
- **Layering:** `ui/` → `model/` → `kernel/`. Only `kernel/` imports amplifier-core/foundation. `kernel/` never imports Textual. This plan only touches `kernel/`, `tests/`, `bundle.md`, and docs.
- **Test suite is offline.** `uv run pytest -q` must pass with no network and no credentials (~2 min). Do NOT add any test that fetches the real anchors bundle — real composition is proven by the live smoke in Phase 3.
- **Formatting:** run `uv run ruff format <changed files only>`. NEVER `ruff format .` — the repo is not globally format-clean and CI only runs `ruff check .`.
- **Bundle byte-identity:** after any edit to `bundle.md`, copy it over `src/amplifier_app_newtui/data/bundles/newtui.md` and verify with `cmp`. This is a PR checklist item and an existing test (`tests/test_kernel_session_config.py:562`).
- **Golden files:** this plan does not touch the transcript renderer, so `tests/goldens/` must NOT change. If a golden test fails, you broke something — do not regenerate.
- **Commits:** conventional commits, one per green task, each ending with the Amplifier footer:

```
🤖 Generated with [Amplifier](https://github.com/microsoft/amplifier)

Co-Authored-By: Amplifier <240397093+microsoft-amplifier@users.noreply.github.com>
```

Use this exact shell pattern for every commit in this plan (substitute the subject/body):

```sh
git commit -m "$(cat <<'EOF'
<type>: <subject>

<optional body>

🤖 Generated with [Amplifier](https://github.com/microsoft/amplifier)

Co-Authored-By: Amplifier <240397093+microsoft-amplifier@users.noreply.github.com>
EOF
)"
```

### Verified reference facts (do not re-derive)

| Fact | Where verified |
|---|---|
| Pinned foundation SHA for the anchors include | `git ls-remote https://github.com/microsoft/amplifier-foundation main` → `93615d9847ce40313cc0d60583cb886de4337f9e` (resolved 2026-07-20; re-resolve ONLY if instructed) |
| anchors bundle contents (300k context, tool roster incl. tool-delegate/todo/apply-patch/recipes, hooks incl. todo-reminder/todo-display/session-naming/mode/approval + streaming-ui/status-context/redaction/logging via includes, 6 `anchors:*` agents) | `~/.amplifier/cache/amplifier-foundation-c909465861f9d6ce/bundles/anchors/bundle.md` |
| `_PRINTING_HOOKS` frozenset (FOUR entries: hooks-streaming-ui, hooks-todo-display, hooks-insight-blocks, hooks-inline-blocks) | `src/amplifier_app_newtui/kernel/runtime.py:90-97` |
| `_strip_printing_hooks` def / sole call site | `runtime.py:142` / `runtime.py:299` (inside `RealRuntime.start()`, which both the TUI and the headless `run` subcommand use — one call site serves both paths) |
| Settings-resolver pattern to copy | `write_boundary_setting()` at `kernel/directory_permissions.py:39-43`, tested at `tests/test_directory_permissions.py:64-69` |
| Session metadata already carries the bundle name | `runtime.py:397` — `base_metadata={"bundle": resolved.bundle_name}` fed to `IncrementalSaver`; merged into `metadata.json` on every save (`kernel/persistence.py:366-374`) |
| Resume already loads metadata (currently discarded) | `runtime.py:315` — `transcript, _metadata = store.load(session_id)` |
| normalize() already handles `delegate:agent_spawned` / `delegate:agent_completed` | `kernel/events.py:725,732` |
| Tracker SUBSCRIBED tuple lacks all `delegate:*` names | `kernel/trackers/task_status.py:33-40` |
| `CONSUMED_EVENTS` has spawned/completed but NOT resumed/cancelled/error | `kernel/queue_bridge.py:59-66` |
| tool-delegate payload shapes (spawn path: `agent` + `sub_session_id`; resume path: child id under **`session_id`**, no `sub_session_id`) | `~/.amplifier/cache/amplifier-foundation-c909465861f9d6ce/modules/tool-delegate/amplifier_module_tool_delegate/__init__.py:979,1100,1151,1175,1237,1270,1303,1341,1364` |
| `DEFAULT_BUNDLE = "newtui"` — do NOT change | `kernel/config.py:39` |

### Pin honesty (state this in docs and PR description)

The include URI pins only **anchors' own `bundle.md`** to the SHA. Anchors' internal includes (`behaviors/*.yaml`) and every module `source:` inside it still reference `@main` and keep floating until upstream pins them. This is no worse than today (the current vendored bundle floats 8 modules `@main`), but the pin is partial. Do not claim full reproducibility anywhere.

---

# Phase 1 — Thin bundle + hook suppression + resume notice (6 tasks)

### Task 1: Bundle sanity tests (RED)

The existing bundle-content tests assert the vendored shape and must be rewritten to pin the wrapper shape. Write the new tests FIRST and watch them fail against the current bundle.

**Files:**
- Rewrite: `tests/test_bundle_agents.py` (replace entire contents)
- Modify: `tests/test_kernel_session_config.py:596-602` (delete one stale test)

**Step 1: Replace the entire contents of `tests/test_bundle_agents.py` with:**

```python
"""Guard: the packaged newtui bundle is a THIN WRAPPER over anchors.

The bundle composes foundation's `anchors` bundle (SHA-pinned includes) and
overlays only a default provider, tool-mcp, and tool-team-pulse. Everything
else — session (300k context), tool roster (incl. tool-delegate subagents),
hooks, and the six bundle-local agents — arrives via the include. These tests
parse the packaged bundle's YAML frontmatter and pin that shape offline.

NOTE: the pin covers only anchors' own bundle.md — its internal includes and
module sources still float @main (partial pin, documented in docs).
"""

from __future__ import annotations

import re

import yaml

from amplifier_app_newtui.kernel.config import packaged_bundles_dir

ANCHORS_INCLUDE_RE = re.compile(
    r"^git\+https://github\.com/microsoft/amplifier-foundation"
    r"@(?P<sha>[0-9a-f]{40})#subdirectory=bundles/anchors/bundle\.md$"
)


def _frontmatter() -> dict:
    text = (packaged_bundles_dir() / "newtui.md").read_text(encoding="utf-8")
    assert text.startswith("---"), "bundle must open with a YAML frontmatter fence"
    data = yaml.safe_load(text.split("---", 2)[1])
    assert isinstance(data, dict)
    return data


def test_wrapper_keeps_bundle_name() -> None:
    """Discovery/override mechanics depend on the name staying `newtui`."""
    assert _frontmatter().get("bundle", {}).get("name") == "newtui"


def test_wrapper_includes_sha_pinned_anchors() -> None:
    includes = _frontmatter().get("includes")
    assert isinstance(includes, list) and len(includes) == 1
    uri = includes[0].get("bundle", "")
    assert ANCHORS_INCLUDE_RE.match(uri), (
        f"includes[0].bundle must be a SHA-pinned anchors URI, got {uri!r}"
    )


def test_wrapper_keeps_default_provider() -> None:
    """anchors is provider-agnostic; the app hard-fails boot at 0 providers,
    so the wrapper must keep a default for fresh installs."""
    providers = _frontmatter().get("providers")
    modules = {p.get("module") for p in (providers or []) if isinstance(p, dict)}
    assert "provider-anthropic" in modules


def test_wrapper_has_no_vendored_sections() -> None:
    data = _frontmatter()
    assert "session" not in data, "inherit anchors' 300k context"
    assert "hooks" not in data, "anchors brings hooks-mode/hooks-approval"
    assert "agents" not in data, "anchors ships 6 bundle-local agents"


def test_wrapper_overlays_only_tui_specific_tools() -> None:
    tools = _frontmatter().get("tools") or []
    modules = {t.get("module") for t in tools if isinstance(t, dict)}
    # tool-task is gone (was inert; superseded by anchors' tool-delegate);
    # filesystem/bash/web/search/skills/mode etc. arrive via anchors.
    assert modules == {"tool-mcp", "tool-team-pulse"}
```

**Step 2: Delete the stale compaction test**

In `tests/test_kernel_session_config.py`, delete the whole function `test_packaged_bundle_declares_automatic_compaction` (lines 596-602 — it asserts `"max_tokens: 200000" in text`, which the wrapper intentionally no longer declares; the guard is replaced by `test_wrapper_has_no_vendored_sections`). Do NOT touch `test_packaged_bundle_matches_repo_root_bundle` (byte-identity) or `test_packaged_bundle_declares_cli_response_contract` (the contract stays in the wrapper body).

**Step 3: Run the new tests to verify they fail**

Run: `uv run pytest tests/test_bundle_agents.py -q`
Expected: FAIL — `test_wrapper_includes_sha_pinned_anchors` fails with "includes[0].bundle must be a SHA-pinned anchors URI" (current bundle has no `includes`), `test_wrapper_has_no_vendored_sections` fails on `"session" not in data`, `test_wrapper_overlays_only_tui_specific_tools` fails (current tools set is larger). `test_wrapper_keeps_bundle_name` and `test_wrapper_keeps_default_provider` PASS (they hold for both shapes — that is fine).

**Step 4: Run the session-config tests to verify nothing else broke**

Run: `uv run pytest tests/test_kernel_session_config.py -q`
Expected: PASS (the deleted test is simply gone).

Do NOT commit yet — Task 2 turns these red tests green and commits both together (the repo must never hold a commit where the suite is red).

---

### Task 2: Write the thin wrapper bundle.md + sync packaged copy (GREEN)

**Files:**
- Rewrite: `bundle.md` (repo root — replace entire contents)
- Sync: `src/amplifier_app_newtui/data/bundles/newtui.md` (byte-identical copy)

**Step 1: Replace the entire contents of `bundle.md` with EXACTLY this** (the `## Terminal response contract` section must stay byte-for-byte identical to today's — `tests/test_kernel_session_config.py:572` asserts the exact block):

````markdown
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
    tool-team-pulse, and the terminal response contract. The TUI renders
    everything itself; printing hooks composed in via anchors and the
    double-writing hooks-logging are suppressed at boot by the app kernel
    (built-in suppression list + the `hooks.suppress` setting).

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
system.md). Printing hooks and `hooks-logging` composed in via anchors are
stripped at boot by the app kernel's suppressed-hooks mechanism.

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
````

**Step 2: Sync the packaged copy byte-identically**

Run:
```sh
cp bundle.md src/amplifier_app_newtui/data/bundles/newtui.md
cmp bundle.md src/amplifier_app_newtui/data/bundles/newtui.md && echo IDENTICAL
```
Expected: `IDENTICAL` (no other output — `cmp` is silent on match).

**Step 3: Run the bundle tests to verify they pass**

Run: `uv run pytest tests/test_bundle_agents.py tests/test_kernel_session_config.py -q`
Expected: PASS (all — including the byte-identity and terminal-contract tests).

**Step 4: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS. If anything else asserts old bundle content (search first: `grep -rn "tool-task\|200000\|foundation:explorer" tests/`), fix that test to match the wrapper in this task — but only tests that parse bundle CONTENT; do not touch behavior tests.

**Step 5: Lint and commit**

Run:
```sh
uv run ruff format tests/test_bundle_agents.py
uv run ruff check .
git add bundle.md src/amplifier_app_newtui/data/bundles/newtui.md tests/test_bundle_agents.py tests/test_kernel_session_config.py
```
Commit (pattern from Ground rules): `feat: replace vendored newtui bundle with thin wrapper over SHA-pinned anchors`

---

### Task 3: `suppressed_hooks_setting` — settings resolver (TDD)

Copy the `write_boundary_setting` pattern: pure function, merged-settings dict in, validated value out.

**Files:**
- Test: `tests/test_runtime_offline.py` (add tests next to `test_strip_printing_hooks_removes_line_mode_printers`, currently line 570)
- Modify: `src/amplifier_app_newtui/kernel/runtime.py`

**Step 1: Write the failing test** — add to `tests/test_runtime_offline.py` (module-level sync test, same style as the existing strip test):

```python
def test_suppressed_hooks_setting_defaults_and_union() -> None:
    """Built-in suppression list (4 printers + hooks-logging) unions with the
    hooks.suppress settings key; junk shapes fall back to the defaults."""
    from amplifier_app_newtui.kernel.runtime import (
        _SUPPRESSED_HOOKS_DEFAULT,
        suppressed_hooks_setting,
    )

    assert _SUPPRESSED_HOOKS_DEFAULT == frozenset(
        {
            "hooks-streaming-ui",
            "hooks-todo-display",
            "hooks-insight-blocks",
            "hooks-inline-blocks",
            "hooks-logging",
        }
    )
    assert suppressed_hooks_setting({}) == _SUPPRESSED_HOOKS_DEFAULT
    assert suppressed_hooks_setting({"hooks": "junk"}) == _SUPPRESSED_HOOKS_DEFAULT
    assert (
        suppressed_hooks_setting({"hooks": {"suppress": "not-a-list"}})
        == _SUPPRESSED_HOOKS_DEFAULT
    )
    resolved = suppressed_hooks_setting({"hooks": {"suppress": ["hooks-custom", ""]}})
    assert "hooks-custom" in resolved
    assert "" not in resolved
    assert _SUPPRESSED_HOOKS_DEFAULT <= resolved
```

**Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_runtime_offline.py::test_suppressed_hooks_setting_defaults_and_union -q`
Expected: FAIL with `ImportError: cannot import name '_SUPPRESSED_HOOKS_DEFAULT'`

**Step 3: Write the minimal implementation**

In `src/amplifier_app_newtui/kernel/runtime.py`, directly BELOW the existing `_PRINTING_HOOKS` block (after its docstring, ~line 106), add:

```python
_SUPPRESSED_HOOKS_DEFAULT = _PRINTING_HOOKS | frozenset({"hooks-logging"})
"""Hook module IDs stripped from the mount plan at boot.

The four printers write raw ANSI under the full-screen TUI (see
``_PRINTING_HOOKS``); ``hooks-logging`` (composed in via the anchors
include) writes the same ``events.jsonl`` this app's IncrementalSaver
owns — two writers, one file. Extensible per-user via the
``hooks.suppress`` settings key (see :func:`suppressed_hooks_setting`).
"""


def suppressed_hooks_setting(settings: dict[str, Any]) -> frozenset[str]:
    """Resolve ``hooks.suppress`` from merged settings, unioned with defaults."""
    hooks = settings.get("hooks")
    raw = hooks.get("suppress") if isinstance(hooks, dict) else None
    if not isinstance(raw, list):
        return _SUPPRESSED_HOOKS_DEFAULT
    extra = {str(item).strip() for item in raw if str(item).strip()}
    return _SUPPRESSED_HOOKS_DEFAULT | extra
```

(Keep `_PRINTING_HOOKS` for now — Task 4 folds it in.)

**Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_runtime_offline.py::test_suppressed_hooks_setting_defaults_and_union -q`
Expected: PASS (1 passed)

**Step 5: Lint and commit**

Run: `uv run ruff format src/amplifier_app_newtui/kernel/runtime.py tests/test_runtime_offline.py && uv run ruff check . && uv run pyright src/`
Expected: clean.
`git add src/amplifier_app_newtui/kernel/runtime.py tests/test_runtime_offline.py`
Commit: `feat: settings-extensible suppressed-hooks resolver (hooks.suppress)`

---

### Task 4: `_apply_hook_suppression` — strip + user-visible notice (TDD)

Replaces `_strip_printing_hooks`. The new function strips every suppressed hook from the mount plan, returns the sorted removed IDs, and emits ONE `Notification` listing them (observability for the blocklist — the validated backlog finding).

**Files:**
- Test: `tests/test_runtime_offline.py` (REPLACE `test_strip_printing_hooks_removes_line_mode_printers`, lines 570-587)
- Modify: `src/amplifier_app_newtui/kernel/runtime.py`

**Step 1: Write the failing tests** — in `tests/test_runtime_offline.py`, DELETE the function `test_strip_printing_hooks_removes_line_mode_printers` (lines 570-587) and add in its place:

```python
def test_apply_hook_suppression_strips_and_notifies() -> None:
    """Suppressed hooks (printers + hooks-logging) are stripped from the
    mount plan and ONE notification names exactly what was removed."""
    from amplifier_app_newtui.kernel.runtime import _apply_hook_suppression

    emitted = []
    plan = {
        "hooks": [
            {"module": "hooks-streaming-ui"},
            {"module": "hooks-approval"},
            {"module": "hooks-todo-display"},
            {"module": "hooks-insight-blocks"},
            {"module": "hooks-inline-blocks"},
            {"module": "hooks-logging"},
            {"module": "hooks-mode"},
        ]
    }
    removed = _apply_hook_suppression(plan, {}, emitted.append)
    assert removed == [
        "hooks-inline-blocks",
        "hooks-insight-blocks",
        "hooks-logging",
        "hooks-streaming-ui",
        "hooks-todo-display",
    ]
    assert [h["module"] for h in plan["hooks"]] == ["hooks-approval", "hooks-mode"]
    assert len(emitted) == 1
    assert emitted[0].kind == "notification"
    for module_id in removed:
        assert module_id in emitted[0].message


def test_apply_hook_suppression_settings_extension_and_odd_shapes() -> None:
    from amplifier_app_newtui.kernel.runtime import _apply_hook_suppression

    emitted = []
    plan = {"hooks": [{"module": "hooks-custom"}, {"module": "hooks-mode"}]}
    settings = {"hooks": {"suppress": ["hooks-custom"]}}
    assert _apply_hook_suppression(plan, settings, emitted.append) == ["hooks-custom"]
    assert [h["module"] for h in plan["hooks"]] == ["hooks-mode"]
    assert len(emitted) == 1

    # Nothing to strip → no notice; odd shapes tolerated.
    assert _apply_hook_suppression(plan, settings, emitted.append) == []
    assert _apply_hook_suppression({}, {}, emitted.append) == []
    assert len(emitted) == 1
```

**Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_runtime_offline.py::test_apply_hook_suppression_strips_and_notifies tests/test_runtime_offline.py::test_apply_hook_suppression_settings_extension_and_odd_shapes -q`
Expected: FAIL with `ImportError: cannot import name '_apply_hook_suppression'`

**Step 3: Write the minimal implementation** in `src/amplifier_app_newtui/kernel/runtime.py`:

3a. Add `Notification` to the events import at lines 29-37 (keep alphabetical):

```python
from .events import (
    ApprovalDenied,
    ContentBlockEnd,
    ContextInjected,
    Notification,
    PromptComplete,
    PromptSubmit,
    ProviderResponseUsage,
    UIEvent,
)
```

3b. DELETE the `_strip_printing_hooks` function (lines 142-147) and the standalone `_PRINTING_HOOKS` frozenset + its docstring (lines 90-106). Inline the four printer IDs into `_SUPPRESSED_HOOKS_DEFAULT` (added in Task 3), which becomes:

```python
_SUPPRESSED_HOOKS_DEFAULT = frozenset(
    {
        "hooks-streaming-ui",  # green "Amplifier:" line-mode streaming printer
        "hooks-todo-display",  # todo-table stdout printer
        "hooks-insight-blocks",  # insight-panel stdout printer
        "hooks-inline-blocks",  # inline-panel stdout printer
        "hooks-logging",  # double-writer of the app-owned events.jsonl
    }
)
"""Hook module IDs stripped from the mount plan at boot.

This app owns its rendering and its persistence: a hook writing raw ANSI
(cursor moves, line erases) under the full-screen TUI corrupts the Textual
screen (found live: the whole turn rendered blank), and ``hooks-logging``
(composed in via the anchors include) writes the same
``~/.amplifier/projects/{project}/sessions/{id}/events.jsonl`` this app's
IncrementalSaver owns — two writers, one file. Also applied on the headless
``run`` subcommand path (same ``RealRuntime.start()``), where the printers
double-echo. Extensible per-user via the ``hooks.suppress`` settings key.
"""
```

3c. Add the new function where `_strip_printing_hooks` used to be (~line 142):

```python
def _apply_hook_suppression(
    mount_plan: dict[str, Any],
    settings: dict[str, Any],
    emit: Callable[[UIEvent], None],
) -> list[str]:
    """Strip suppressed hook modules from the plan; notice what was removed.

    Returns the sorted module IDs removed. Emits one user-visible
    ``Notification`` naming them (blocklists rot silently otherwise);
    emits nothing when nothing was stripped. Tolerates odd plan shapes.
    """
    suppressed = suppressed_hooks_setting(settings)
    hooks = mount_plan.get("hooks")
    if not isinstance(hooks, list):
        return []
    removed = sorted(
        {
            str(h.get("module"))
            for h in hooks
            if isinstance(h, dict) and h.get("module") in suppressed
        }
    )
    if not removed:
        return []
    mount_plan["hooks"] = [
        h for h in hooks if not (isinstance(h, dict) and h.get("module") in suppressed)
    ]
    emit(
        Notification(
            session_id="",  # pre-session boot phase; no id minted yet
            message="suppressed hooks: " + ", ".join(removed),
            level="info",
            source="bundle",
        )
    )
    return removed
```

3d. In `RealRuntime.start()`, replace the call at line 299:

```python
        _strip_printing_hooks(resolved.mount_plan)
```
with:
```python
        _apply_hook_suppression(resolved.mount_plan, resolved.settings, self.bridge.emit)
```

**Step 4: Run the new tests, then the full suite**

Run: `uv run pytest tests/test_runtime_offline.py -q`
Expected: PASS (the offline lifecycle tests exercise `start()` end-to-end, so the wiring is covered).
Run: `uv run pytest -q`
Expected: PASS. If anything still references `_strip_printing_hooks`, you missed a call/import site — `grep -rn "_strip_printing_hooks\|_PRINTING_HOOKS" src/ tests/` must return nothing.

**Step 5: Lint, typecheck, commit**

Run: `uv run ruff format src/amplifier_app_newtui/kernel/runtime.py tests/test_runtime_offline.py && uv run ruff check . && uv run pyright src/`
Expected: clean.
`git add src/amplifier_app_newtui/kernel/runtime.py tests/test_runtime_offline.py`
Commit: `feat: generalize printing-hook strip into observable suppressed-hooks mechanism`

---

### Task 5: Resume bundle-mismatch notice (TDD)

`metadata.json` already carries `bundle` (stamped by `IncrementalSaver` via `base_metadata` — `runtime.py:397`); resume currently discards the loaded metadata. Compare and notice.

**Files:**
- Test: `tests/test_runtime_offline.py` (add after `test_offline_resume_restores_transcript_and_turn_base`, line 530)
- Modify: `src/amplifier_app_newtui/kernel/runtime.py`

**Step 1: Write the failing test** — add to `tests/test_runtime_offline.py`. It needs one extra import at the top of the file: add `from amplifier_app_newtui.kernel.persistence import SessionStore` below the existing `RealRuntime` import (line 36).

```python
async def test_offline_resume_notices_bundle_mismatch(offline_env) -> None:
    """Resuming a session created under a different bundle emits a
    user-visible notice (was X, now Y). Same-bundle resume stays silent."""
    first = await _started_runtime(offline_env["project"])
    try:
        answer = asyncio.create_task(_answer_next_approval(first, ALLOW_ONCE))
        await first.submit("please write hello.txt with hi")
        await answer
        session_id = first._initialized.session_id
    finally:
        await first.cleanup()

    # Tamper AFTER cleanup (cleanup's final save re-stamps base_metadata).
    store = SessionStore(project_dir=offline_env["project"])
    assert store.get_metadata(session_id).get("bundle") == "offline"
    store.update_metadata(session_id, {"bundle": "some-old-bundle"})

    resumed = RealRuntime(
        bundle="offline",
        resume_id=session_id[:8],
        project_dir=offline_env["project"],
        mode=lambda: "chat",
    )
    await resumed.start()
    try:
        notices = []
        while not resumed.queue.empty():
            event = resumed.queue.get_nowait()
            if event.kind == "notification":
                notices.append(event.message)
        assert any(
            "resumed under a different bundle (was some-old-bundle, now offline)"
            in message
            for message in notices
        ), notices
    finally:
        await resumed.cleanup()
```

**Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_runtime_offline.py::test_offline_resume_notices_bundle_mismatch -q`
Expected: FAIL on the final `assert any(...)` — `notices` contains no bundle-mismatch message (it may contain other notifications; that is why the assert matches the exact phrase).

**Step 3: Write the minimal implementation** in `src/amplifier_app_newtui/kernel/runtime.py`, inside `start()`:

3a. Capture the stored bundle at the resume load (currently line ~311-319). Change:

```python
        session_id: str | None = None
        transcript: list[dict[str, Any]] | None = None
        if self._resume_id:
            session_id = store.find_session(self._resume_id)
            transcript, _metadata = store.load(session_id)
```
to:
```python
        session_id: str | None = None
        transcript: list[dict[str, Any]] | None = None
        stored_bundle = ""
        if self._resume_id:
            session_id = store.find_session(self._resume_id)
            transcript, metadata = store.load(session_id)
            stored_bundle = str(metadata.get("bundle") or "")
```
(keep the two lines that follow — `self.turn_base = ...` and `self.restored_history = ...` — unchanged; they used `transcript`, not `_metadata`).

3b. After `self.bundle_name = resolved.bundle_name` (line ~429), add:

```python
        if stored_bundle and stored_bundle != resolved.bundle_name:
            # Old sessions silently running under a new module stack is a
            # trap during bundle transitions — say so once, visibly.
            self.bridge.emit(
                Notification(
                    session_id=initialized.session_id,
                    message=(
                        f"resumed under a different bundle "
                        f"(was {stored_bundle}, now {resolved.bundle_name})"
                    ),
                    level="warning",
                    source="bundle",
                )
            )
```

A missing/empty stored bundle (session never saved metadata) stays silent by design.

**Step 4: Run the test, then the full suite**

Run: `uv run pytest tests/test_runtime_offline.py -q`
Expected: PASS (including the existing same-bundle resume tests — they must NOT gain a notice).
Run: `uv run pytest -q`
Expected: PASS.

**Step 5: Lint, typecheck, commit**

Run: `uv run ruff format src/amplifier_app_newtui/kernel/runtime.py tests/test_runtime_offline.py && uv run ruff check . && uv run pyright src/`
`git add src/amplifier_app_newtui/kernel/runtime.py tests/test_runtime_offline.py`
Commit: `feat: notice when a session resumes under a different bundle`

---

### Task 6: Phase 1 gate

**Step 1: Run the full CI-equivalent gate**

Run:
```sh
uv run ruff check .
uv run pyright src/
uv run pytest -q
cmp bundle.md src/amplifier_app_newtui/data/bundles/newtui.md && echo IDENTICAL
```
Expected: all clean, `IDENTICAL`, zero failed tests (baseline before this plan: 982 passed / 1 xfailed; the count will have shifted by this plan's additions/deletions — what matters is 0 failed).

**Step 2: Fix anything found, re-run, commit only if fixes were needed**

Commit (if needed): `fix: phase-1 gate cleanup`

---

# Phase 2 — Delegation compatibility (5 tasks)

anchors mounts `tool-delegate` (spawning through the `session.spawn` capability newtui's SessionSpawner registers — that part already works). What is missing: three `delegate:*` event names in normalize, the tracker subscription list, and the queue-bridge consumption list. Payload shapes are verified from tool-delegate source (see reference table): **spawn-path** events carry `agent` + `sub_session_id`; **resume-path** events carry the CHILD id under `session_id` with no `sub_session_id`.

### Task 7: Normalize `delegate:agent_resumed` → AgentSpawned (TDD)

**Files:**
- Test: `tests/test_kernel_events_normalize.py` (append at end)
- Modify: `src/amplifier_app_newtui/kernel/events.py` (after line 741, the end of the completed arm)

**Step 1: Write the failing test:**

```python
def test_delegate_resumed_reopens_lane_as_agent_spawned() -> None:
    """tool-delegate's resume path carries the CHILD id under ``session_id``
    (no sub_session_id) — verified from the module source."""
    event = normalize(
        "delegate:agent_resumed",
        {
            "session_id": "sess-1-9f_worker",
            "parent_session_id": "sess-1",
            "tool_call_id": "c1",
        },
    )
    assert isinstance(event, AgentSpawned)
    assert event.sub_session_id == "sess-1-9f_worker"
    assert event.parent_session_id == "sess-1"
```

**Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_kernel_events_normalize.py::test_delegate_resumed_reopens_lane_as_agent_spawned -q`
Expected: FAIL with `assert isinstance(event, AgentSpawned)` → `event is None` (normalize's `case _` returns None).

**Step 3: Write the minimal implementation** — in `src/amplifier_app_newtui/kernel/events.py`, after the `"task:agent_completed" | ...` case arm (ends line 741), add:

```python
        case "delegate:agent_resumed":
            # Resume-path payloads carry the CHILD id under ``session_id``
            # (tool-delegate's resume branch emits no sub_session_id).
            return AgentSpawned(
                **env,
                agent=_str(payload, "agent", "agent_name", "name"),
                sub_session_id=_str(
                    payload, "sub_session_id", "child_session_id", "session_id"
                ),
                parent_session_id=_str(payload, "parent_session_id"),
            )
```

**Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_kernel_events_normalize.py -q`
Expected: PASS (all — existing normalize tests untouched).

**Step 5: Lint and commit**

Run: `uv run ruff format src/amplifier_app_newtui/kernel/events.py tests/test_kernel_events_normalize.py && uv run ruff check . && uv run pyright src/`
`git add src/amplifier_app_newtui/kernel/events.py tests/test_kernel_events_normalize.py`
Commit: `feat: normalize delegate:agent_resumed as a lane re-open`

---

### Task 8: Normalize `delegate:agent_cancelled` + `delegate:error` → AgentCompleted (TDD)

`AgentCompleted.result` is a short lane summary by contract (`events.py:352`); the full error text still reaches the user through the tool result path, so map error → `"error"`, cancelled → `"cancelled"`.

**Files:**
- Test: `tests/test_kernel_events_normalize.py` (append)
- Modify: `src/amplifier_app_newtui/kernel/events.py`

**Step 1: Write the failing tests:**

```python
def test_delegate_cancelled_completes_lane_as_cancelled() -> None:
    # Spawn-path shape (agent + sub_session_id present).
    event = normalize(
        "delegate:agent_cancelled",
        {
            **SID,
            "agent": "worker",
            "sub_session_id": "sess-1-9f_worker",
            "parent_session_id": "sess-1",
        },
    )
    assert isinstance(event, AgentCompleted)
    assert event.success is False
    assert event.result == "cancelled"
    assert event.sub_session_id == "sess-1-9f_worker"

    # Resume-path shape (child id under session_id only).
    resumed = normalize(
        "delegate:agent_cancelled",
        {"session_id": "sess-1-9f_worker", "parent_session_id": "sess-1"},
    )
    assert isinstance(resumed, AgentCompleted)
    assert resumed.sub_session_id == "sess-1-9f_worker"


def test_delegate_error_completes_lane_as_error() -> None:
    event = normalize(
        "delegate:error",
        {
            **SID,
            "agent": "worker",
            "sub_session_id": "sess-1-9f_worker",
            "parent_session_id": "sess-1",
            "error": "Agent delegation failed (ValueError): boom",
        },
    )
    assert isinstance(event, AgentCompleted)
    assert event.success is False
    assert event.result == "error"
    assert event.agent == "worker"
```

**Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_kernel_events_normalize.py -q`
Expected: the two new tests FAIL (`event is None`); everything else passes.

**Step 3: Write the minimal implementation** — in `events.py`, directly after the `delegate:agent_resumed` arm from Task 7, add:

```python
        case "delegate:agent_cancelled":
            return AgentCompleted(
                **env,
                agent=_str(payload, "agent", "agent_name", "name"),
                sub_session_id=_str(
                    payload, "sub_session_id", "child_session_id", "session_id"
                ),
                parent_session_id=_str(payload, "parent_session_id"),
                success=False,
                result="cancelled",
            )
        case "delegate:error":
            # payload["error"] is a long human message; the lane line wants a
            # short status — the full text reaches the user via the tool result.
            return AgentCompleted(
                **env,
                agent=_str(payload, "agent", "agent_name", "name"),
                sub_session_id=_str(
                    payload, "sub_session_id", "child_session_id", "session_id"
                ),
                parent_session_id=_str(payload, "parent_session_id"),
                success=False,
                result="error",
            )
```

**Step 4: Run to verify pass**

Run: `uv run pytest tests/test_kernel_events_normalize.py -q`
Expected: PASS (all).

**Step 5: Lint and commit**

Run: `uv run ruff format src/amplifier_app_newtui/kernel/events.py tests/test_kernel_events_normalize.py && uv run ruff check . && uv run pyright src/`
`git add src/amplifier_app_newtui/kernel/events.py tests/test_kernel_events_normalize.py`
Commit: `feat: normalize delegate cancelled/error as lane completions`

---

### Task 9: TaskStatusTracker subscribes to `delegate:*` and shows the normalized status (TDD)

Two changes: (a) the `EVENTS` tuple gains all five `delegate:*` names; (b) `consume()` prefers `normalized.result` when closing a lane so "cancelled"/"error" show instead of the generic "failed" (legacy behavior preserved when result is empty).

**Files:**
- Test: `tests/test_kernel_trackers.py` (add after `test_task_tracker_ignores_root_session_events`, line 326)
- Modify: `src/amplifier_app_newtui/kernel/trackers/task_status.py`

**Step 1: Write the failing tests:**

```python
def test_task_tracker_subscribes_to_delegate_lifecycle() -> None:
    """anchors' tool-delegate emits delegate:* — the lanes panel and the
    working-line agent count go blind without these subscriptions."""
    for name in (
        "delegate:agent_spawned",
        "delegate:agent_completed",
        "delegate:agent_resumed",
        "delegate:agent_cancelled",
        "delegate:error",
    ):
        assert name in TaskStatusTracker.EVENTS, name


def test_task_tracker_delegate_spawn_and_complete() -> None:
    tracker = TaskStatusTracker(ROOT)
    tracker.consume(
        "delegate:agent_spawned",
        {
            "session_id": ROOT,
            "agent": "worker",
            "sub_session_id": "kid-1_worker",
            "parent_session_id": ROOT,
        },
    )
    assert tracker.active_count == 1
    tracker.consume(
        "delegate:agent_completed",
        {
            "session_id": ROOT,
            "sub_session_id": "kid-1_worker",
            "parent_session_id": ROOT,
            "success": True,
        },
    )
    assert tracker.active_count == 0


def test_task_tracker_delegate_resume_reopens_lane() -> None:
    tracker = TaskStatusTracker(ROOT)
    tracker.consume(
        "delegate:agent_resumed",
        {"session_id": "kid-1_worker", "parent_session_id": ROOT},
    )
    assert tracker.active_count == 1
    lane = tracker.lane("kid-1_worker")
    assert lane is not None
    assert lane.lane.name == "worker"  # recovered from the session-id suffix


def test_task_tracker_delegate_cancelled_shows_cancelled() -> None:
    tracker = TaskStatusTracker(ROOT)
    tracker.consume(
        "delegate:agent_spawned",
        {
            "session_id": ROOT,
            "agent": "worker",
            "sub_session_id": "kid-1_worker",
            "parent_session_id": ROOT,
        },
    )
    tracker.consume(
        "delegate:agent_cancelled",
        {
            "session_id": ROOT,
            "agent": "worker",
            "sub_session_id": "kid-1_worker",
            "parent_session_id": ROOT,
        },
    )
    lane = tracker.lane("kid-1_worker")
    assert lane is not None
    assert lane.lane.state == "done"
    assert "cancelled" in lane.lane.activity
```

**Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_kernel_trackers.py -q`
Expected: the four new tests FAIL — the subscription test on the missing names; spawn/complete/resume/cancelled tests on `active_count == 0` / `lane is None` (unsubscribed names still normalize, but `consume` is only reached through hooks in production — here `consume` IS called directly, so spawn/complete/resume actually PASS already via normalize; the subscription test and the "cancelled" activity assertion are the ones that must fail. Read the failures and confirm at least `test_task_tracker_subscribes_to_delegate_lifecycle` and `test_task_tracker_delegate_cancelled_shows_cancelled` are red for the right reasons before proceeding).

**Step 3: Write the minimal implementation** in `src/amplifier_app_newtui/kernel/trackers/task_status.py`:

3a. Replace the `EVENTS` tuple (lines 33-40) with:

```python
    EVENTS = (
        "task:agent_spawned",
        "task:agent_completed",
        "task:spawned",
        "task:completed",
        "delegate:agent_spawned",
        "delegate:agent_completed",
        "delegate:agent_resumed",
        "delegate:agent_cancelled",
        "delegate:error",
        "session:start",
        "session:end",
    )
```

3b. In `consume()`, change the completion (lines 127-129) from:

```python
            self.lanes.complete(
                child_id, result="" if normalized.success else "failed"
            )
```
to:
```python
            self.lanes.complete(
                child_id,
                result=normalized.result or ("" if normalized.success else "failed"),
            )
```

3c. Update the module docstring's first line to mention both families, e.g. change `"""Task status tracker: agent lanes from ``task:agent_*`` events.` to `"""Task status tracker: agent lanes from ``task:agent_*`` / ``delegate:*`` events.`

**Step 4: Run to verify pass — including the legacy regression tests**

Run: `uv run pytest tests/test_kernel_trackers.py -q`
Expected: PASS (all — in particular `test_task_tracker_legacy_event_names` still sees `"failed"` because legacy payloads carry no result).

**Step 5: Lint and commit**

Run: `uv run ruff format src/amplifier_app_newtui/kernel/trackers/task_status.py tests/test_kernel_trackers.py && uv run ruff check . && uv run pyright src/`
`git add src/amplifier_app_newtui/kernel/trackers/task_status.py tests/test_kernel_trackers.py`
Commit: `feat: task tracker consumes the full delegate:* lifecycle`

---

### Task 10: QueueBridge consumes the remaining `delegate:*` names (TDD)

`CONSUMED_EVENTS` (`kernel/queue_bridge.py:23-67`) already lists spawned/completed; add resumed/cancelled/error so the UI queue (and `events.jsonl` via the tap) sees them.

**Files:**
- Test: `tests/test_kernel_trackers.py` (QueueBridge section, after line ~345)
- Modify: `src/amplifier_app_newtui/kernel/queue_bridge.py`

**Step 1: Write the failing tests:**

```python
def test_consumed_events_cover_delegate_lifecycle() -> None:
    for name in (
        "delegate:agent_spawned",
        "delegate:agent_completed",
        "delegate:agent_resumed",
        "delegate:agent_cancelled",
        "delegate:error",
    ):
        assert name in CONSUMED_EVENTS, name


@pytest.mark.asyncio
async def test_queue_bridge_normalizes_delegate_error() -> None:
    bridge = QueueBridge()
    await bridge.handle_event(
        "delegate:error",
        {
            "session_id": ROOT,
            "agent": "worker",
            "sub_session_id": "kid-1_worker",
            "parent_session_id": ROOT,
            "error": "boom",
        },
    )
    event = bridge.queue.get_nowait()
    assert event.kind == "agent_completed"
    assert event.success is False
```

**Step 2: Run to verify failure**

Run: `uv run pytest "tests/test_kernel_trackers.py::test_consumed_events_cover_delegate_lifecycle" "tests/test_kernel_trackers.py::test_queue_bridge_normalizes_delegate_error" -q`
Expected: `test_consumed_events_cover_delegate_lifecycle` FAILS on `delegate:agent_resumed`. (`test_queue_bridge_normalizes_delegate_error` PASSES already — `handle_event` normalizes regardless of registration; it guards the pipeline end-to-end. That is acceptable: the red test here is the consumption list.)

**Step 3: Write the minimal implementation** — in `kernel/queue_bridge.py`, replace lines 64-65:

```python
    "delegate:agent_spawned",
    "delegate:agent_completed",
```
with:
```python
    "delegate:agent_spawned",
    "delegate:agent_completed",
    "delegate:agent_resumed",
    "delegate:agent_cancelled",
    "delegate:error",
```

**Step 4: Run to verify pass**

Run: `uv run pytest tests/test_kernel_trackers.py -q`
Expected: PASS (all).

**Step 5: Lint and commit**

Run: `uv run ruff format src/amplifier_app_newtui/kernel/queue_bridge.py tests/test_kernel_trackers.py && uv run ruff check . && uv run pyright src/`
`git add src/amplifier_app_newtui/kernel/queue_bridge.py tests/test_kernel_trackers.py`
Commit: `feat: queue bridge consumes delegate resumed/cancelled/error`

---

### Task 11: Phase 2 gate

**Step 1:** Run:
```sh
uv run ruff check .
uv run pyright src/
uv run pytest -q
```
Expected: all clean, 0 failed.

**Step 2:** Fix anything found; commit only if fixes were needed: `fix: phase-2 gate cleanup`

---

# Phase 3 — Docs, diagrams, live verification (7 tasks)

Docs tasks have no unit tests; the verification step for each is the stated grep coming back clean plus a proofread of the exact edits. Keep every claim honest: partial pin, suppression list contents (FIVE defaults), 300k context.

### Task 12: docs/SETTINGS.md

**Files:**
- Modify: `docs/SETTINGS.md`

**Step 1:** In the settings table, add this row after the `routing.matrix` row (line 48):

```markdown
| `hooks.suppress` | Extra hook module IDs stripped from the mount plan at boot, unioned with the built-in suppression list (`hooks-streaming-ui`, `hooks-todo-display`, `hooks-insight-blocks`, `hooks-inline-blocks`, `hooks-logging`). A boot notice lists everything suppressed | none (built-ins always apply) | global or project |
```

**Step 2:** Update the three context rows (lines 51-53) — change each "Default" cell:
- `context.max_tokens`: `` `200000` in the packaged newtui bundle `` → `` `300000` (inherited from the composed anchors bundle) ``
- `context.compact_threshold`: `` `0.8` in the packaged newtui bundle `` → `` `0.8` (inherited from the composed anchors bundle) ``
- `context.auto_compact`: `` `true` in the packaged newtui bundle `` → `` `true` (inherited from the composed anchors bundle) ``

**Step 3:** Around line 74, update `The base bundle mounts hooks-mode + hooks-approval + tool-mode matching the reference` to say these arrive **via the composed anchors bundle** (same modules, same configs).

**Step 4:** Sweep the file: `grep -n "200000\|tool-task\|foundation:" docs/SETTINGS.md` — expected: no matches (fix any stragglers).

**Step 5:** Commit: `docs: SETTINGS — hooks.suppress key, anchors-inherited context defaults`

---

### Task 13: docs/ARCHITECTURE.md

**Files:**
- Modify: `docs/ARCHITECTURE.md`

**Step 1:** Line 198 (the gating/no-printing-hooks bullet): replace the `_strip_printing_hooks()` mention with the suppressed-hooks mechanism — e.g.:

```markdown
   corrupt the Textual screen; `_apply_hook_suppression()` strips them (plus
   `hooks-logging`, which would double-write the app-owned `events.jsonl`) from
   the mount plan at boot, emits a notice naming what was removed, and honours
   the `hooks.suppress` settings key for user extensions.
```
(adjust the leading sentence fragment so the paragraph still reads; check the surrounding lines 195-200 first.)

**Step 2:** §7.2 (line 435+): the bullet `The bundle-native stack remains mounted to match `anchors`:` becomes `The bundle-native stack arrives FROM the composed `anchors` bundle (the packaged newtui bundle is a thin wrapper that includes anchors at a pinned SHA):`.

**Step 3:** Mounted-modules/bundle inventory sweep — run:

```sh
grep -n "tool-task\|200k\|200000\|foundation:explorer\|foundation:zen-architect\|six foundation agents\|6 foundation agents" docs/ARCHITECTURE.md
```
For every hit, update to the anchors composition: context is 300k; delegation is `tool-delegate` (via anchors); agents are the six anchors bundle-local agents (`explorer`, `architect`, `builder`, `debugger`, `git-ops`, `researcher`); the tool roster adds `tool-todo`, `tool-apply-patch`, `tool-recipes` via anchors; the wrapper overlays `tool-mcp` + `tool-team-pulse` + `provider-anthropic`. Re-run the grep — expected: no matches.

**Step 4:** Commit: `docs: ARCHITECTURE — thin-wrapper bundle + suppressed-hooks mechanism`

---

### Task 14: docs/DEVELOPMENT.md, README.md, docs/USER-GUIDE.md

**Files:**
- Modify: `docs/DEVELOPMENT.md` (§"Customizing / swapping the bundle", lines 87-102)
- Modify: `README.md`, `docs/USER-GUIDE.md` (grep-driven)

**Step 1:** In `docs/DEVELOPMENT.md` lines 87-102: rewrite the section body to describe the wrapper — `bundle.md` is a thin wrapper that `includes:` foundation's anchors bundle at a pinned SHA (state the partial-pin caveat in one sentence) and overlays provider + tool-mcp + tool-team-pulse; byte-identity rule unchanged; line 100's `(_strip_printing_hooks)` becomes `(_apply_hook_suppression; extend via the hooks.suppress setting)`.

**Step 2:** Sweep the remaining docs:

```sh
grep -n "tool-task\|200k\|200000\|foundation:explorer\|_strip_printing_hooks" README.md docs/USER-GUIDE.md docs/DEVELOPMENT.md
```
Update every hit to the anchors-composition reality. Re-run — expected: no matches. (Do NOT edit `docs/notes/` history files — they are point-in-time records.)

**Step 3:** Commit: `docs: DEVELOPMENT/README/USER-GUIDE reflect the anchors wrapper`

---

### Task 15: Diagrams

**Files:**
- Modify: `docs/diagrams/newtui-architecture.dot` (lines 80-89, `cluster_bundle`)
- Modify: `docs/diagrams/newtui-amplifier-integration.dot` (line 19, `bundle_md` node)
- Check/modify: `.ai/diagrams/newtui-architecture.dot` (mirror copy — line 81 has the same label)
- Regenerate: three PNGs + one SVG

**Step 1:** In `docs/diagrams/newtui-architecture.dot`, replace the `cluster_bundle` subgraph (lines 80-89) with:

```dot
        subgraph cluster_bundle {
            label="newtui bundle — thin wrapper over anchors (packaged: data/bundles/newtui.md)\n_apply_hook_suppression(): printers + hooks-logging stripped at boot";
            style="rounded,filled"; fillcolor="#EDF2F7"; color="#2D3748";
            anchors_inc [label="includes: anchors @ pinned SHA\n(amplifier-foundation bundles/anchors)", fillcolor="#E2E8F0"];
            orch [label="orchestrator: loop-streaming\n(extended thinking) — via anchors", fillcolor="#E2E8F0"];
            provider [label="provider: provider-anthropic\n(wrapper overlay)", fillcolor="#E2E8F0"];
            ctx [label="context: context-simple\n(300k tokens, auto-compact) — via anchors", fillcolor="#E2E8F0"];
            tools [label="tools via anchors: filesystem · bash · web · search\ntodo · apply-patch · delegate (subagents) · skills · mode · recipes\n+ wrapper overlay: mcp · team-pulse", fillcolor="#E2E8F0"];
            bhooks [label="hooks via anchors: todo-reminder · session-naming\nhooks-mode · hooks-approval · status-context · redaction\n(streaming-ui / todo-display / logging suppressed at boot)", fillcolor="#E2E8F0"];
            bagents [label="agents via anchors: explorer · architect · builder\ndebugger · git-ops · researcher", fillcolor="#E2E8F0"];
        }
```

**Step 2:** In `docs/diagrams/newtui-amplifier-integration.dot`, replace the `bundle_md` node (line 19) with:

```dot
        bundle_md     [label="bundle: newtui.md (thin wrapper)\nincludes: anchors @ pinned SHA (amplifier-foundation)\nloop-streaming · context-simple 300k · provider-anthropic (overlay)\nfs/bash/web/search/todo/apply-patch/delegate/skills/mode/recipes + mcp/team-pulse\nhooks via anchors (printers + logging suppressed) · 6 anchors agents", fillcolor="#fff3cd"];
```

**Step 3:** Mirror check: `diff docs/diagrams/newtui-architecture.dot .ai/diagrams/newtui-architecture.dot`. If the only differences are your Step-1 edits, copy the file over: `cp docs/diagrams/newtui-architecture.dot .ai/diagrams/newtui-architecture.dot`. If they diverged before this plan, leave `.ai/` alone and note it in the commit body.

**Step 4:** Regenerate renders:

```sh
/opt/homebrew/bin/dot -Tpng docs/diagrams/newtui-architecture.dot -o docs/diagrams/newtui-architecture.png
/opt/homebrew/bin/dot -Tpng docs/diagrams/newtui-amplifier-integration.dot -o docs/diagrams/newtui-amplifier-integration.png
/opt/homebrew/bin/dot -Tpng docs/diagrams/newtui-dataflow.dot -o docs/diagrams/newtui-dataflow.png
/opt/homebrew/bin/dot -Tsvg docs/diagrams/newtui-amplifier-integration.dot -o docs/diagrams/newtui-amplifier-integration.svg
```
Expected: exit 0 each, PNGs/SVG regenerated (a `dot` syntax error means a malformed label — fix and re-run).

**Step 5:** Commit: `docs: diagrams reflect thin-wrapper bundle composition`

---

### Task 16: Offline final gate

**Step 1:** Run the exact CI sequence plus the byte-identity check:

```sh
uv sync --frozen
uv run ruff check .
uv run pyright src/
uv run pytest -q
cmp bundle.md src/amplifier_app_newtui/data/bundles/newtui.md && echo IDENTICAL
grep -rn "_strip_printing_hooks\|_PRINTING_HOOKS\b" src/ tests/ docs/*.md ; echo "grep exit: $?"
```
Expected: sync clean, lint clean, pyright clean, 0 failed tests, `IDENTICAL`, and the grep exits 1 (no stale references — `docs/notes/` is intentionally excluded).

**Step 2:** Fix anything found; commit only if fixes were needed: `fix: phase-3 offline gate cleanup`

---

### Task 17: Live smoke (network + provider spend — accepted budget ~$2)

This is the ONE place real composition is proven: the offline suite deliberately never fetches anchors. First boot with the new bundle fetches anchors + its modules from git — allow several minutes.

**Tooling:** load the `amplifier-skill-forge` skill and drive `uv run amplifier-newtui` in a persistent PTY session per that skill's workflow. Launch from the repo root so the packaged wrapper resolves. If any assert fails: capture the evidence (screen dump + file paths), STOP, and report — do not improvise fixes inside the smoke; take the failure back through a normal TDD task.

**Assert (a) — delegation drives the lanes (the Phase 2 fix under test):**
Submit a prompt that forces a delegation, e.g. `Delegate to the explorer agent: summarize README.md in two sentences.` Watch the screen: a subagent lane must appear AND the working line's agent count must increment (`1 agent`). This is precisely the TaskStatusTracker + `delegate:*` path.

**Assert (b) — single-writer events.jsonl (hooks-logging suppressed):**
Identify the session id from the banner, then:
```sh
ls ~/.amplifier/projects/*/sessions/<session-id>/
python3 -c "
import json, sys, glob
path = glob.glob('$HOME/.amplifier/projects/*/sessions/<session-id>/events.jsonl')[0]
kinds = set()
for line in open(path):
    record = json.loads(line)
    assert 'kind' in record, f'non-newtui record (hooks-logging leaked?): {record}'
    kinds.add(record['kind'])
print('OK — single-writer format;', len(kinds), 'kinds')
"
```
Expected: every line is a newtui UIEvent dump with a `kind` field (hooks-logging writes a different schema — any line without `kind` is a double-writer leak). Also confirm the boot notice listed the suppressed hooks on screen (`suppressed hooks: …`).

**Assert (c) — body composition across includes (asserted offline, never yet verified live):**
The composed system prompt must carry BOTH the wrapper's terminal response contract AND anchors' behavioral principles. Pick a distinctive phrase from each source first:
```sh
grep -m1 -o "Lead with the answer.*" bundle.md
sed -n '1,20p' ~/.amplifier/cache/amplifier-foundation-*/bundles/anchors/context/system.md
```
Then check the live session — either via the app's `/context` command, or by inspecting the stored system content for the session on disk. Both phrases must be present.

**Assert (d) — TodoBlocks render live (rendering existed; never had a live tool feeding it):**
Submit: `Use your todo tool to plan a 3-step task list for tidying a git repo, then mark step 1 complete.` Expected: a TodoBlock renders in the transcript and updates (anchors brings `tool-todo` + `hooks-todo-reminder`; `hooks-todo-display` is suppressed — the app renders todos itself).

**Wrap-up:** exit the app, close the forge session, and record the four assert outcomes (pass/fail + evidence paths) in the PR description. No commit from this task unless a fix was needed (any fix goes through RED→GREEN first).

---

### Task 18: Finish

**Step 1:** `git log --oneline main..HEAD` (or since the plan started) — confirm every commit is green-suite and conventional.
**Step 2:** Run the full gate one final time (same commands as Task 16, Step 1). Expected: all clean.
**Step 3:** Present the work for review with: the four live-smoke assert outcomes, the partial-pin caveat, and the resume-notice behavior (old sessions resuming under the new stack will show the "was newtui, now newtui" — nothing, since the NAME is unchanged; the notice fires only for sessions stored under a *different* bundle name, e.g. `offline` test bundles or user-overridden bundles — say this plainly so nobody expects a notice for the anchors transition itself, where the bundle name stays `newtui`).

---

## Task count

- Phase 1: 6 tasks (Tasks 1-6)
- Phase 2: 5 tasks (Tasks 7-11)
- Phase 3: 7 tasks (Tasks 12-18)
