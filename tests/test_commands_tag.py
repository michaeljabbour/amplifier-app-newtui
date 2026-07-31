"""/tag command wiring (HGT: session-tags-backend).

The pure command layer: the /tag spec is registered and its handler routes
every sub-verb to ``CommandContext.manage_tags`` (recorded by the fake).
"""

from __future__ import annotations

import pytest

from amplifier_app_tui.commands.builtin import BUILTIN_COMMANDS, build_registry

from .conftest import FakeCommandContext


def test_tag_command_is_registered() -> None:
    spec = next((c for c in BUILTIN_COMMANDS if c.name == "/tag"), None)
    assert spec is not None
    assert spec.group == "Between"
    assert spec.tag == "built-in"
    assert "session tags" in spec.desc  # discoverable phrasing (forge probe greps it)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("/tag", "manage_tags:"),
        ("/tag list", "manage_tags:list"),
        ("/tag add frontend urgent", "manage_tags:add frontend urgent"),
        ("/tag rm urgent", "manage_tags:rm urgent"),
        ("/tag sessions frontend", "manage_tags:sessions frontend"),
    ],
)
def test_tag_handler_routes_args_to_manage_tags(text: str, expected: str) -> None:
    ctx = FakeCommandContext()
    registry = build_registry()
    assert registry.parse_and_run(ctx, text) is True
    assert ctx.calls == [expected]
