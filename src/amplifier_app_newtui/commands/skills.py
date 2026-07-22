"""Skill aliases — discovered skills as first-class palette commands.

Brian's story #1: ``/cranky-old-sam`` (and its ``shortcut:`` alias
``/cosam``) must resolve exactly like any built-in before slash input
can fall through as a chat turn. Rather than a second lookup table,
each discovered skill registers additively into the ONE command
registry (ADR-0007: commands are data + callables) — so the palette,
help listing, ``parse_and_run`` dispatch and the unknown-command check
all see skills for free.

Layering: skills arrive duck-typed (``name`` / ``description`` /
``shortcut`` attributes, i.e. ``kernel.session_ops.SkillInfo``) — this
package still imports nothing above ``model/``. Handlers invoke the
skill through :meth:`CommandContext.load_skill`, the same path the
built-in ``/skill <name>`` takes.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol

from pydantic import ValidationError

from .registry import CommandContext, CommandRegistry, CommandSpec


class SkillLike(Protocol):
    """What a discovered skill must offer (``session_ops.SkillInfo`` shape)."""

    @property
    def name(self) -> str: ...
    @property
    def description(self) -> str: ...
    @property
    def shortcut(self) -> str: ...


def _load_handler(skill_name: str) -> Any:
    def handler(ctx: CommandContext, args: str) -> None:
        del args  # alias arguments are not plumbed through load_skill (yet)
        ctx.load_skill(skill_name)

    return handler


def _spec_for(trigger: str, desc: str, skill_name: str) -> CommandSpec | None:
    """A ``skill``-tagged spec for *trigger*, or ``None`` when the token
    is not a valid slash trigger (spaces, empty — validator decides)."""
    try:
        return CommandSpec(
            group="During",
            name=f"/{trigger}",
            desc=desc,
            tag="skill",
            handler=_load_handler(skill_name),
        )
    except ValidationError:
        return None


def skill_command_specs(
    registry: CommandRegistry, skills: Iterable[SkillLike]
) -> tuple[CommandSpec, ...]:
    """Palette rows for *skills* that don't collide with *registry*.

    One row per skill name plus one per distinct ``shortcut`` alias
    (the alias row names its target so the palette reads as an alias).
    Collisions with already-registered commands — built-ins or earlier
    skills — are skipped: first registration wins, never overridden.
    """
    specs: list[CommandSpec] = []
    taken = set(registry.names)
    for skill in skills:
        name = str(skill.name or "").strip()
        desc = " ".join(str(skill.description or "").split()) or f"load skill {name}"
        spec = _spec_for(name, desc, name)
        if spec is None or spec.name in taken:
            continue
        specs.append(spec)
        taken.add(spec.name)
        shortcut = str(skill.shortcut or "").strip()
        if shortcut and shortcut != name:
            alias = _spec_for(shortcut, f"{name} · {desc}", name)
            if alias is not None and alias.name not in taken:
                specs.append(alias)
                taken.add(alias.name)
    return tuple(specs)


def register_skill_commands(
    registry: CommandRegistry, skills: Iterable[SkillLike]
) -> tuple[CommandSpec, ...]:
    """Register *skills* (names + shortcuts) into *registry*; returns the
    specs actually added — ``()`` when everything was already present.

    Rides the open-registry mechanism (story #2): each row registers as
    a ``skill``-sourced contribution, so ``registry.contributions("skill")``
    lists them and the registry's own collision policy (existing command
    wins, skip with a log line) backstops the prefilter above.
    """
    return tuple(
        spec
        for spec in skill_command_specs(registry, skills)
        if registry.register(spec, source="skill")
    )


__all__ = ["SkillLike", "register_skill_commands", "skill_command_specs"]
