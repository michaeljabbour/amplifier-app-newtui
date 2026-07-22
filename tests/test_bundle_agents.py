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
    assert "agents" not in data, "anchors ships 6 bundle-local agents"


def test_wrapper_overlays_only_push_notify_hook() -> None:
    """anchors brings hooks-mode/hooks-approval; the wrapper overlays exactly
    one hook: hooks-notify-push (ntfy HTTP side-channel — no stdout, no-op
    without AMPLIFIER_NTFY_TOPIC). It must listen to orchestrator:complete
    directly: its default event (notify:turn-complete) is emitted by
    hooks-notify, which the kernel suppresses at boot (raw OSC/BEL stdout)."""
    hooks = _frontmatter().get("hooks") or []
    modules = {h.get("module") for h in hooks if isinstance(h, dict)}
    assert modules == {"hooks-notify-push"}
    push_mounts = [
        h for h in hooks if isinstance(h, dict) and h.get("module") == "hooks-notify-push"
    ]
    assert len(push_mounts) == 1
    assert push_mounts[0].get("config", {}).get("listen_event") == "orchestrator:complete"


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
    skills_mounts = [
        t for t in tools if isinstance(t, dict) and t.get("module") == "tool-skills"
    ]
    assert len(skills_mounts) == 1
    sources = skills_mounts[0].get("config", {}).get("skills", [])
    assert any("amplifier-foundation" in s and "skills" in s for s in sources)
    assert "~/.amplifier/skills" in sources
