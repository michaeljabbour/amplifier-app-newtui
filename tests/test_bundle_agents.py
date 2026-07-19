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
