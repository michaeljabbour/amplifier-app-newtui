"""Native mode wiring: packaged mode defs + hooks-mode search_paths injection.

Approvals are OFF by default (nothing gated until a posture activates a
mode); these guard the shipped mode definitions and the injection that
makes them discoverable on a clean install.
"""

from __future__ import annotations

import yaml

from amplifier_app_newtui.kernel.config import (
    inject_mode_search_paths,
    packaged_modes_dir,
)

SHIPPED_MODES = ("plan", "brainstorm", "careful")


def test_packaged_modes_exist_with_valid_frontmatter() -> None:
    modes_dir = packaged_modes_dir()
    for name in SHIPPED_MODES:
        path = modes_dir / f"{name}.md"
        assert path.is_file(), f"missing shipped mode: {name}"
        text = path.read_text(encoding="utf-8")
        assert text.startswith("---")
        data = yaml.safe_load(text.split("---", 2)[1])
        mode = data["mode"]
        assert mode["name"] == name
        assert "default_action" in mode
        assert "tools" in mode


def test_careful_mode_confirms_writes_and_shell() -> None:
    data = yaml.safe_load(
        (packaged_modes_dir() / "careful.md").read_text(encoding="utf-8").split("---", 2)[1]
    )
    confirm = set(data["mode"]["tools"].get("confirm", []))
    assert {"write_file", "edit_file", "bash"} <= confirm


def test_plan_mode_is_read_only() -> None:
    data = yaml.safe_load(
        (packaged_modes_dir() / "plan.md").read_text(encoding="utf-8").split("---", 2)[1]
    )
    assert data["mode"]["default_action"] == "block"  # unlisted tools denied
    safe = set(data["mode"]["tools"].get("safe", []))
    assert "read_file" in safe
    assert "write_file" not in safe  # writes are not safe in plan


def test_inject_mode_search_paths_adds_to_hooks_mode() -> None:
    plan = {"hooks": [{"module": "hooks-mode", "config": {"search_paths": []}}]}
    inject_mode_search_paths(plan, packaged_modes_dir())
    assert str(packaged_modes_dir()) in plan["hooks"][0]["config"]["search_paths"]
    # Idempotent.
    inject_mode_search_paths(plan, packaged_modes_dir())
    assert plan["hooks"][0]["config"]["search_paths"].count(str(packaged_modes_dir())) == 1


def test_inject_mode_search_paths_noop_without_hooks_mode() -> None:
    plan = {"hooks": [{"module": "hooks-approval", "config": {}}]}
    inject_mode_search_paths(plan, packaged_modes_dir())  # must not raise
    assert plan["hooks"][0]["config"] == {}
    # Also fine with no hooks section at all.
    empty: dict = {}
    inject_mode_search_paths(empty, packaged_modes_dir())
    assert empty == {}
