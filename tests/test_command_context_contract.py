"""Contract: both CommandContext implementations match the protocol.

Every pure command test drives ``FakeCommandContext`` while the running
app drives ``AppCommandContext`` — nothing structural ever asserted that
the two stay in step with ``commands.registry.CommandContext`` (classic
fake-drift risk, flagged by the 2026-07 repo audit). This test derives
the surface from the Protocol itself, so adding a member to the protocol
without teaching both implementations fails HERE, not in production.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from amplifier_app_newtui.commands.registry import CommandContext
from amplifier_app_newtui.ui.command_context import AppCommandContext

from .conftest import FakeCommandContext

PROTOCOL_MEMBERS: dict[str, Any] = {
    name: member for name, member in vars(CommandContext).items() if not name.startswith("_")
}


def _params(fn: Any) -> tuple[tuple[str, Any, Any], ...]:
    """Parameter (name, kind, default) triples — annotations deliberately
    ignored; the fake omits some and that is not drift."""
    return tuple((p.name, p.kind, p.default) for p in inspect.signature(fn).parameters.values())


def test_protocol_surface_is_nonempty() -> None:
    """Guard the guard: an import/refactor that empties the derived
    surface would silently vacuum the contract tests below."""
    assert len(PROTOCOL_MEMBERS) > 30
    assert "show_model" in PROTOCOL_MEMBERS
    assert isinstance(PROTOCOL_MEMBERS["ledger"], property)


def test_real_context_covers_the_protocol() -> None:
    """AppCommandContext: properties stay properties, methods keep the
    protocol's exact parameter list."""
    for name, member in PROTOCOL_MEMBERS.items():
        real = inspect.getattr_static(AppCommandContext, name, None)
        assert real is not None, f"AppCommandContext is missing {name!r}"
        if isinstance(member, property):
            assert isinstance(real, property), f"{name!r} must be a property"
        elif inspect.isfunction(member):
            assert inspect.isfunction(real), f"{name!r} must be a method"
            assert _params(real) == _params(member), f"{name!r} signature drifted"


def test_fake_context_covers_the_protocol() -> None:
    """FakeCommandContext: data surfaces may be plain attributes (duck
    typing satisfies the protocol) but every one must exist on a fresh
    instance, and methods keep the protocol's exact parameter list."""
    fake = FakeCommandContext()
    for name, member in PROTOCOL_MEMBERS.items():
        assert hasattr(fake, name), f"FakeCommandContext is missing {name!r}"
        if inspect.isfunction(member):
            impl = inspect.getattr_static(FakeCommandContext, name, None)
            assert inspect.isfunction(impl), f"{name!r} must be a method on the fake"
            assert _params(impl) == _params(member), f"{name!r} signature drifted"


@pytest.mark.parametrize("name", sorted(PROTOCOL_MEMBERS))
def test_member_is_documented(name: str) -> None:
    """The protocol is the commands↔app boundary spec — every member
    carries a docstring the handler authors rely on."""
    member = PROTOCOL_MEMBERS[name]
    doc = member.fget.__doc__ if isinstance(member, property) else member.__doc__
    assert doc and doc.strip(), f"{name!r} has no docstring in the protocol"
