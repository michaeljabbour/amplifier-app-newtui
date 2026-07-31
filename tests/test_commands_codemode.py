"""``/codemode`` command: built-in registration + Code Mode preview block."""

from __future__ import annotations

from amplifier_app_tui.commands.builtin import build_registry
from amplifier_app_tui.commands.codemode import (
    CODEMODE_COMMAND,
    build_codemode_block,
    governed_catalog,
)
from amplifier_app_tui.model.blocks import ToolLine


def test_codemode_is_a_during_group_builtin() -> None:
    registry = build_registry()
    spec = registry.get(CODEMODE_COMMAND)
    assert spec is not None
    assert spec.group == "During"
    assert registry.source_of(CODEMODE_COMMAND) == "builtin"


def test_governed_catalog_reflects_the_trust_map_and_excludes_execute() -> None:
    catalog = governed_catalog()
    paths = {spec.path for spec in catalog.specs}
    assert "read.read_file" in paths
    assert "write.write_file" in paths
    # A code-mode program does not orchestrate code mode.
    assert not any(spec.name == "execute" for spec in catalog.specs)


def test_build_codemode_block_is_a_visible_greppable_block() -> None:
    block = build_codemode_block(governed_catalog(), block_id="b1")
    assert isinstance(block, ToolLine)
    assert "Code Mode" in block.summary
    assert "execute()" in block.summary
    assert block.status == "completed"
    # The instructions ride in the body for the on-screen catalog.
    assert any("Available tools" in line for line in block.body)


def test_command_posts_the_block_through_the_registry(fake_command_context) -> None:
    ctx = fake_command_context
    registry = build_registry()
    registry.run(CODEMODE_COMMAND, ctx)
    assert ctx.user_lines == [CODEMODE_COMMAND]
    assert len(ctx.blocks) == 1
    assert isinstance(ctx.blocks[0], ToolLine)
    assert "Code Mode" in ctx.blocks[0].summary
