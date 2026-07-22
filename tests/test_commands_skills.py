"""Skill aliases in the command registry (Brian's story #1).

Discovered skills register as palette commands — ``/cranky-old-sam``
plus its ``shortcut:`` alias ``/cosam`` — so the SAME registry that
powers the palette, help and dispatch resolves them before any slash
input can fall through as a chat turn. Registration is additive and
duck-typed (name/description/shortcut), never a registry refactor.
"""

from __future__ import annotations

from types import SimpleNamespace

from amplifier_app_newtui.commands.builtin import build_registry
from amplifier_app_newtui.commands.skills import register_skill_commands


def _skill(name: str, description: str = "", shortcut: str = "") -> SimpleNamespace:
    return SimpleNamespace(name=name, description=description, shortcut=shortcut)


def test_registers_skill_and_shortcut_rows() -> None:
    registry = build_registry()
    added = register_skill_commands(
        registry, (_skill("cranky-old-sam", "crusty review", "cosam"),)
    )
    assert [spec.name for spec in added] == ["/cranky-old-sam", "/cosam"]
    spec = registry.get("/cranky-old-sam")
    assert spec is not None and spec.tag == "skill"
    assert "crusty review" in spec.desc
    alias = registry.get("/cosam")
    assert alias is not None and alias.tag == "skill"
    assert "cranky-old-sam" in alias.desc  # alias row names its target


def test_parse_and_run_resolves_name_and_shortcut(fake_command_context) -> None:
    registry = build_registry()
    register_skill_commands(
        registry, (_skill("cranky-old-sam", "crusty review", "cosam"),)
    )
    assert registry.parse_and_run(fake_command_context, "/cranky-old-sam")
    assert registry.parse_and_run(fake_command_context, "/cosam")
    # Both routes invoke the skill exactly like ``/skill <name>`` does.
    assert fake_command_context.calls == [
        "load_skill:cranky-old-sam",
        "load_skill:cranky-old-sam",
    ]
    assert fake_command_context.user_lines == ["/cranky-old-sam", "/cosam"]


def test_skips_collisions_with_existing_commands(fake_command_context) -> None:
    registry = build_registry()
    added = register_skill_commands(
        registry,
        (
            _skill("status", "shadows a built-in"),  # /status is built-in
            _skill("review", "fine", shortcut="skill"),  # /skill is built-in
        ),
    )
    assert [spec.name for spec in added] == ["/review"]
    # The built-in survives untouched.
    registry.parse_and_run(fake_command_context, "/status")
    assert fake_command_context.calls == ["show_status"]


def test_skips_tokens_that_are_not_slash_triggers() -> None:
    registry = build_registry()
    added = register_skill_commands(
        registry, (_skill("bad name with spaces"), _skill(""), _skill("ok"))
    )
    assert [spec.name for spec in added] == ["/ok"]


def test_shortcut_equal_to_name_registers_once() -> None:
    registry = build_registry()
    added = register_skill_commands(registry, (_skill("simplify", "cut", "simplify"),))
    assert [spec.name for spec in added] == ["/simplify"]


def test_empty_description_gets_a_default() -> None:
    registry = build_registry()
    register_skill_commands(registry, (_skill("terse"),))
    spec = registry.get("/terse")
    assert spec is not None and spec.desc.strip()


def test_registering_twice_is_idempotent() -> None:
    registry = build_registry()
    skills = (_skill("cranky-old-sam", "crusty review", "cosam"),)
    register_skill_commands(registry, skills)
    assert register_skill_commands(registry, skills) == ()


def test_skill_rows_are_skill_sourced_contributions() -> None:
    # Story #2: skills ride the open-registry mechanism — their rows are
    # 'skill'-sourced contributions, unregisterable as a group, distinct
    # from the seeded built-ins.
    registry = build_registry()
    added = register_skill_commands(
        registry, (_skill("cranky-old-sam", "crusty review", "cosam"),)
    )
    assert registry.contributions("skill") == added
    assert registry.source_of("/cosam") == "skill"
    assert registry.source_of("/mode") == "builtin"
    assert registry.unregister("/cosam")
    assert registry.get("/cosam") is None
    assert registry.get("/cranky-old-sam") is not None
