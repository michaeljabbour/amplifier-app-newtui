"""Guard: the packaged tui bundle is a THIN WRAPPER over anchors.

The bundle composes foundation's `anchors` bundle (ref-pinned include: tracks
foundation @main, the only ref that ships bundles/anchors) and
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

from amplifier_app_tui.kernel.config import packaged_bundles_dir

ANCHORS_INCLUDE_RE = re.compile(
    r"^git\+https://github\.com/microsoft/amplifier-foundation"
    r"@(?P<ref>[^\s#]+)#subdirectory=bundles/anchors/bundle\.md$"
)


def _frontmatter() -> dict:
    text = (packaged_bundles_dir() / "tui.md").read_text(encoding="utf-8")
    assert text.startswith("---"), "bundle must open with a YAML frontmatter fence"
    data = yaml.safe_load(text.split("---", 2)[1])
    assert isinstance(data, dict)
    return data


def test_wrapper_keeps_bundle_name() -> None:
    """Discovery/override mechanics depend on the name staying `tui`."""
    assert _frontmatter().get("bundle", {}).get("name") == "tui"


def test_wrapper_includes_ref_pinned_anchors() -> None:
    includes = _frontmatter().get("includes")
    assert isinstance(includes, list) and len(includes) == 1
    uri = includes[0].get("bundle", "")
    assert ANCHORS_INCLUDE_RE.match(uri), (
        f"includes[0].bundle must be a ref-pinned anchors URI (tag/branch/SHA), got {uri!r}"
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
    assert "agents" not in data, "anchors ships 6 bundle-local agents"


def test_wrapper_overlays_only_redaction_hook() -> None:
    """anchors brings hooks-mode/hooks-approval; the wrapper overlays exactly
    one hook. Push is app-owned and consumes durable attention events, so the
    wrapper must not mount upstream hooks-notify-push as a second completion
    producer. hooks-notify remains suppressed at boot because its raw OSC/BEL
    output would corrupt the full-screen TUI. hook-redaction is re-mounted only
    to extend its allowlist with the
    delegate routing ids — without that, live-bus scrubbing rewrites
    sub_session_id/parent_session_id ("[REDACTED:PII]…", found live) and
    child→lane telemetry/focus routing degrades."""
    hooks = _frontmatter().get("hooks") or []
    modules = {h.get("module") for h in hooks if isinstance(h, dict)}
    assert modules == {"hook-redaction"}
    redaction_mounts = [
        h for h in hooks if isinstance(h, dict) and h.get("module") == "hook-redaction"
    ]
    assert len(redaction_mounts) == 1
    allowlist = redaction_mounts[0].get("config", {}).get("allowlist", [])
    assert set(allowlist) == {"sub_session_id", "parent_session_id"}


def test_wrapper_overlays_only_tui_specific_tools() -> None:
    tools = _frontmatter().get("tools") or []
    modules = {t.get("module") for t in tools if isinstance(t, dict)}
    # tool-task is gone (was inert; superseded by anchors' tool-delegate);
    # filesystem/bash/web/search/mode etc. arrive via anchors. tool-skills
    # is re-mounted deliberately: anchors pins it to the foundation skill
    # set, which replaces the ~/.amplifier/skills default scan — the
    # wrapper restores the user dir (later bundles override earlier ones).
    assert modules == {"tool-mcp", "tool-team-pulse", "tool-skills"}


def test_wrapper_tool_skills_keeps_foundation_set_and_adds_user_dir() -> None:
    tools = _frontmatter().get("tools") or []
    skills_mounts = [t for t in tools if isinstance(t, dict) and t.get("module") == "tool-skills"]
    assert len(skills_mounts) == 1
    sources = skills_mounts[0].get("config", {}).get("skills", [])
    assert any("amplifier-foundation" in s and "skills" in s for s in sources)
    assert "~/.amplifier/skills" in sources
