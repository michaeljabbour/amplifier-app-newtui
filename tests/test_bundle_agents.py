"""Guard: the packaged newtui bundle must advertise delegatable agents.

Without a top-level ``agents:`` section the coordinator's ``config["agents"]``
is empty and the mounted ``tool-task`` reports itself unavailable, making
multi-agent delegation silently inert. This test parses the packaged bundle's
YAML frontmatter and asserts the ``agents.include`` list is non-empty and
contains the chosen foundation agents.
"""

from __future__ import annotations

import yaml

from amplifier_app_newtui.kernel.config import packaged_bundles_dir

EXPECTED_AGENTS = {
    "foundation:explorer",
    "foundation:zen-architect",
    "foundation:bug-hunter",
    "foundation:test-coverage",
    "foundation:modular-builder",
    "foundation:web-research",
}


def _parse_frontmatter(text: str) -> dict:
    assert text.startswith("---"), "bundle must open with a YAML frontmatter fence"
    # Split on the fence lines: '', <frontmatter>, <body...>
    parts = text.split("---", 2)
    frontmatter = parts[1]
    data = yaml.safe_load(frontmatter)
    assert isinstance(data, dict)
    return data


def test_newtui_bundle_advertises_agents() -> None:
    bundle_path = packaged_bundles_dir() / "newtui.md"
    data = _parse_frontmatter(bundle_path.read_text(encoding="utf-8"))

    agents = data.get("agents")
    assert isinstance(agents, dict), "bundle frontmatter must have an 'agents' section"

    include = agents.get("include")
    assert isinstance(include, list) and include, "agents.include must be a non-empty list"

    assert EXPECTED_AGENTS.issubset(set(include)), (
        f"expected {sorted(EXPECTED_AGENTS)} in agents.include, got {include}"
    )


def test_newtui_bundle_mounts_mcp_and_skills_tools() -> None:
    """Bucket A: tool-mcp + tool-skills must be declared so MCP servers and
    skills are actually usable (not just displayed)."""
    bundle_path = packaged_bundles_dir() / "newtui.md"
    data = _parse_frontmatter(bundle_path.read_text(encoding="utf-8"))
    modules = {t.get("module") for t in (data.get("tools") or []) if isinstance(t, dict)}
    assert "tool-mcp" in modules
    assert "tool-skills" in modules


def test_newtui_bundle_mounts_native_mode_system() -> None:
    """Native modes: tool-mode + hooks-mode must be mounted so postures can
    activate modes."""
    data = _parse_frontmatter((packaged_bundles_dir() / "newtui.md").read_text(encoding="utf-8"))
    tools = {t.get("module") for t in (data.get("tools") or []) if isinstance(t, dict)}
    hooks = {h.get("module") for h in (data.get("hooks") or []) if isinstance(h, dict)}
    assert "tool-mode" in tools
    assert "hooks-mode" in hooks
    assert "hooks-approval" in hooks


def test_approvals_are_off_by_default() -> None:
    """hooks-approval must be policy-driven (no built-in gating) with a
    continue fallback, so NOTHING is gated until a mode opts in."""
    data = _parse_frontmatter((packaged_bundles_dir() / "newtui.md").read_text(encoding="utf-8"))
    approval = next(
        h for h in data["hooks"] if isinstance(h, dict) and h.get("module") == "hooks-approval"
    )
    config = approval.get("config") or {}
    assert config.get("policy_driven_only") is True  # skip built-in high-risk checks
    assert config.get("default_action") == "continue"  # timeout/degraded → allow, not deny
    assert config.get("rules") == []  # no static gating rules
