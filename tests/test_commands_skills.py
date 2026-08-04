"""Skill aliases in the command registry (Brian's story #1 + B2 compliance).

Discovered skills register as palette commands — ``/cranky-old-sam``
plus its ``shortcut:`` alias ``/cosam`` — so the SAME registry that
powers the palette, help and dispatch resolves them before any slash
input can fall through as a chat turn. Registration is additive and
duck-typed (name/description/shortcut), never a registry refactor.

B2 compliance (2026-08-02 audit follow-up) adds coverage for:

- AC1/AC5: alias arguments forward exactly like the built-in ``/skill
  <name> <rest>`` path, proven with the ONE shared fixture
  (``tests/test_skill_alias_fixture.py``) also used by the TUI-side
  ``test_flow_skill_aliases.py`` and the cross-surface
  ``test_skill_alias_parity.py``.
- AC2: the canonical name-as-command row now names its alias too (not
  just the alias row naming its canonical target).
- AC4: collisions — a skill vs. a built-in, or a skill vs. another
  skill's alias — are reported deterministically by
  ``plan_skill_commands`` / ``register_skill_commands_reporting``
  instead of vanishing silently.
"""

from __future__ import annotations

from types import SimpleNamespace

from amplifier_app_tui.commands.builtin import build_registry
from amplifier_app_tui.commands.skills import (
    AliasCollision,
    plan_skill_commands,
    register_skill_commands,
    register_skill_commands_reporting,
)

from .test_skill_alias_fixture import (
    BUILTIN_SHADOW_FIXTURE,
    COLLIDING_ALIAS_FIXTURE,
    SKILL_ALIAS_FIXTURE,
)

CRANKY_OLD_SAM = SKILL_ALIAS_FIXTURE[0]  # SkillInfo("cranky-old-sam", ..., shortcut="cosam")


def _skill(name: str, description: str = "", shortcut: str = "") -> SimpleNamespace:
    return SimpleNamespace(name=name, description=description, shortcut=shortcut)


def test_registers_skill_and_shortcut_rows() -> None:
    registry = build_registry()
    added = register_skill_commands(registry, (CRANKY_OLD_SAM,))
    assert [spec.name for spec in added] == ["/cranky-old-sam", "/cosam"]
    spec = registry.get("/cranky-old-sam")
    assert spec is not None and spec.tag == "skill"
    assert "crusty code review" in spec.desc
    alias = registry.get("/cosam")
    assert alias is not None and alias.tag == "skill"
    assert "cranky-old-sam" in alias.desc  # alias row names its target


def test_canonical_row_also_names_its_alias() -> None:
    """AC2: autocomplete/help show aliases alongside canonical names —
    both directions. The alias row already named its target; now the
    canonical row names its alias too."""
    registry = build_registry()
    register_skill_commands(registry, (CRANKY_OLD_SAM,))
    canonical = registry.get("/cranky-old-sam")
    assert canonical is not None
    assert "alias /cosam" in canonical.desc


def test_parse_and_run_resolves_name_and_shortcut(fake_command_context) -> None:
    registry = build_registry()
    register_skill_commands(registry, (CRANKY_OLD_SAM,))
    assert registry.parse_and_run(fake_command_context, "/cranky-old-sam")
    assert registry.parse_and_run(fake_command_context, "/cosam")
    # Both routes invoke the skill exactly like ``/skill <name>`` does.
    assert fake_command_context.calls == [
        "load_skill:cranky-old-sam",
        "load_skill:cranky-old-sam",
    ]
    assert fake_command_context.user_lines == ["/cranky-old-sam", "/cosam"]


def test_alias_and_canonical_forward_arguments_like_builtin_skill_command(
    fake_command_context,
) -> None:
    """AC1 + the judgment call (module docstring): alias arguments ARE
    forwarded, using the same concatenation ``/skill <name> <rest>``
    already uses — so all three spellings issue byte-identical
    ``load_skill`` calls (CLI/TUI parity)."""
    registry = build_registry()
    register_skill_commands(registry, (CRANKY_OLD_SAM,))

    registry.parse_and_run(fake_command_context, "/skill cranky-old-sam draft the release notes")
    registry.parse_and_run(fake_command_context, "/cranky-old-sam draft the release notes")
    registry.parse_and_run(fake_command_context, "/cosam draft the release notes")

    assert fake_command_context.calls == [
        "load_skill:cranky-old-sam draft the release notes",
        "load_skill:cranky-old-sam draft the release notes",
        "load_skill:cranky-old-sam draft the release notes",
    ]


def test_alias_without_arguments_still_loads_the_bare_name(fake_command_context) -> None:
    registry = build_registry()
    register_skill_commands(registry, (CRANKY_OLD_SAM,))
    registry.parse_and_run(fake_command_context, "/cosam")
    assert fake_command_context.calls == ["load_skill:cranky-old-sam"]


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
    skills = (CRANKY_OLD_SAM,)
    register_skill_commands(registry, skills)
    assert register_skill_commands(registry, skills) == ()


def test_skill_rows_are_skill_sourced_contributions() -> None:
    # Story #2: skills ride the open-registry mechanism — their rows are
    # 'skill'-sourced contributions, unregisterable as a group, distinct
    # from the seeded built-ins.
    registry = build_registry()
    added = register_skill_commands(registry, (CRANKY_OLD_SAM,))
    assert registry.contributions("skill") == added
    assert registry.source_of("/cosam") == "skill"
    assert registry.source_of("/mode") == "builtin"
    assert registry.unregister("/cosam")
    assert registry.get("/cosam") is None
    assert registry.get("/cranky-old-sam") is not None


# --- collision diagnostics (B2 compliance AC4) --------------------------


def test_plan_skill_commands_reports_builtin_shadow_collision() -> None:
    registry = build_registry()
    plan = plan_skill_commands(registry, BUILTIN_SHADOW_FIXTURE)
    assert plan.specs == ()
    assert plan.collisions == (AliasCollision(trigger="/status", skill="status", owner="built-in"),)


def test_plan_skill_commands_reports_skill_vs_skill_alias_collision() -> None:
    """Two DIFFERENT skills declaring the SAME ``shortcut:`` is a genuine
    alias collision, independent of any built-in shadowing. First
    registration wins (unchanged); the loser is reported, not silent."""
    registry = build_registry()
    plan = plan_skill_commands(registry, COLLIDING_ALIAS_FIXTURE)
    assert [spec.name for spec in plan.specs] == ["/cranky-old-sam", "/cosam", "/crusty-old-sam"]
    assert plan.collisions == (
        AliasCollision(trigger="/cosam", skill="crusty-old-sam", owner="cranky-old-sam"),
    )


def test_plan_skill_commands_no_collisions_is_empty_tuple() -> None:
    registry = build_registry()
    plan = plan_skill_commands(registry, SKILL_ALIAS_FIXTURE)
    assert plan.collisions == ()
    assert len(plan.specs) == 4  # 2 skills, each with a distinct alias


def test_register_skill_commands_reporting_backs_the_thin_wrapper() -> None:
    """``register_skill_commands`` (back-compat) returns exactly the
    ``specs`` half of ``register_skill_commands_reporting`` — additive,
    not a fork of behavior."""
    registry_a = build_registry()
    registry_b = build_registry()
    added = register_skill_commands(registry_a, COLLIDING_ALIAS_FIXTURE)
    plan = register_skill_commands_reporting(registry_b, COLLIDING_ALIAS_FIXTURE)
    # Compare by name, not spec identity/equality: each call mints its own
    # ``_load_handler`` closures, so the CommandSpec objects are never
    # ``==`` to one another even when they describe the identical row.
    assert [spec.name for spec in added] == [spec.name for spec in plan.specs]
    assert len(plan.collisions) == 1


def test_registering_the_same_skills_twice_reports_a_collision_the_second_time() -> None:
    # Collisions are computed against the registry's CURRENT state, so a
    # second discovery pass against the same registry (not a production
    # path today — boot runs this once — but a defensive case) reports
    # rather than silently re-skipping.
    registry = build_registry()
    register_skill_commands_reporting(registry, (CRANKY_OLD_SAM,))
    second = register_skill_commands_reporting(registry, (CRANKY_OLD_SAM,))
    assert second.specs == ()
    # The canonical name collides first, so — matching the pre-B2 "a
    # canonical collision skips the alias too" behavior — the alias is
    # never independently attempted; only the canonical collision reports.
    assert {c.trigger for c in second.collisions} == {"/cranky-old-sam"}
