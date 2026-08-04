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

Compliance B2 (alias fixture parity, 2026-08-02 audit follow-up) added
three things on top of the story #1 baseline, all in this one file so
the registry stays the single source of truth for both the CLI-style
headless resolution path (``CommandRegistry.parse_and_run`` against a
plain ``CommandContext`` — see ``tests/test_commands_skills.py``) and
the interactive TUI path (the real app over Textual — see
``tests/test_flow_skill_aliases.py``); both import the same fixture
from ``tests/test_skill_alias_fixture.py``:

1. **Argument forwarding** (judgment call, recorded here since the
   compliance brief left it open): text after a skill's name-as-command
   or its alias is now forwarded to :meth:`CommandContext.load_skill`
   using the EXACT same concatenation the built-in ``/skill <name>
   <rest>`` already uses (``ctx.load_skill(f"{name} {rest}")``). This
   was chosen over inventing a new argument channel because it makes
   all three spellings — ``/skill cranky-old-sam draft it``,
   ``/cranky-old-sam draft it``, ``/cosam draft it`` — issue byte
   identical ``ctx.load_skill`` calls (CLI/TUI parity, AC1), and because
   this package does not own (and should not guess at) the mounted
   skills tool's own argument contract.
2. **Collision diagnostics** (:class:`AliasCollision`, returned by
   :func:`plan_skill_commands` / :func:`register_skill_commands_reporting`):
   a skill name or shortcut that is already taken — by a built-in or by
   an earlier skill/alias in the same discovery pass — used to vanish
   silently (first registration wins, nothing said). It is now reported
   deterministically so the caller can surface it at configuration load
   (``ui.app.TuiApp._register_skill_commands``, AC4) instead of a quiet
   skip.
3. **Nearby suggestions** for an unrecognized slash command/alias are
   :meth:`~.registry.CommandRegistry.suggest` (AC3), a small addition to
   the registry itself (every registered trigger, skill or built-in,
   benefits — not just skills).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any, NamedTuple, Protocol

from pydantic import ValidationError

from ..model.blocks import Segment
from .registry import CommandContext, CommandRegistry, CommandSpec


class SkillLike(Protocol):
    """What a discovered skill must offer (``session_ops.SkillInfo`` shape)."""

    @property
    def name(self) -> str: ...
    @property
    def description(self) -> str: ...
    @property
    def shortcut(self) -> str: ...


class AliasCollision(NamedTuple):
    """One slash trigger a discovered skill wanted but couldn't have.

    Computed by :func:`plan_skill_commands`: the registry's own collision
    policy (first registration wins) already decided the OUTCOME — this
    just names it instead of leaving it a silent skip (AC4).
    """

    trigger: str
    """The slash trigger that collided, e.g. ``/cosam`` or ``/status``."""
    skill: str
    """The skill that wanted *trigger* and did not get it."""
    owner: str
    """Who already holds *trigger*: ``built-in``, ``skill`` (claimed by a
    prior call against this registry), or the specific skill name that
    claimed it earlier in THIS discovery pass."""


class SkillPlan(NamedTuple):
    """What :func:`plan_skill_commands` would register, plus any
    collisions found while deciding that (AC4)."""

    specs: tuple[CommandSpec, ...]
    collisions: tuple[AliasCollision, ...]


def _load_handler(skill_name: str) -> Any:
    def handler(ctx: CommandContext, args: str) -> None:
        # Forward trailing text exactly like the built-in ``/skill <name>
        # <rest>`` path (``_cmd_skill`` in commands/builtin.py) already
        # does — see the module docstring's judgment-call note (AC1).
        rest = args.strip()
        target = f"{skill_name} {rest}" if rest else skill_name
        ctx.load_skill(target)

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


def plan_skill_commands(registry: CommandRegistry, skills: Iterable[SkillLike]) -> SkillPlan:
    """Compute the palette rows *skills* would add to *registry*, and any
    alias collisions found along the way (AC4).

    One row per skill name plus one per distinct ``shortcut`` alias (the
    alias row names its target; the canonical row now also names ITS
    alias, so autocomplete/help show both directions — AC2). A trigger
    already claimed — by a built-in, by an earlier skill in *skills*, or
    by a previous call against this same *registry* — is never
    double-registered (first registration wins, unchanged from the
    story #1 baseline); it is instead recorded as an :class:`AliasCollision`
    so the caller can choose to surface it rather than stay silent.

    A skill whose canonical name collides skips its alias too (matching
    the pre-B2 behavior exactly) — there is no useful "half a skill"
    registration.
    """
    specs: list[CommandSpec] = []
    collisions: list[AliasCollision] = []
    # trigger -> who holds it. Seeded from the registry's CURRENT state
    # (built-ins, plus anything a previous discovery pass already added)
    # using each existing spec's own display tag ("built-in"/"skill") —
    # the same word already shown in the palette's dimmer tag column.
    owner: dict[str, str] = {}
    for existing_name in registry.names:
        existing_spec = registry.get(existing_name)
        owner[existing_name] = existing_spec.tag if existing_spec is not None else "built-in"

    for skill in skills:
        name = str(skill.name or "").strip()
        desc = " ".join(str(skill.description or "").split()) or f"load skill {name}"
        shortcut = str(skill.shortcut or "").strip()
        has_alias = bool(shortcut) and shortcut != name
        canonical_desc = f"{desc} · alias /{shortcut}" if has_alias else desc

        spec = _spec_for(name, canonical_desc, name)
        if spec is None:
            continue  # not a valid slash trigger — nothing to collide with
        if spec.name in owner:
            collisions.append(AliasCollision(trigger=spec.name, skill=name, owner=owner[spec.name]))
            continue  # canonical name lost the trigger: alias isn't attempted either
        specs.append(spec)
        owner[spec.name] = name

        if not has_alias:
            continue
        alias_spec = _spec_for(shortcut, f"{name} · {desc}", name)
        if alias_spec is None:
            continue
        if alias_spec.name in owner:
            collisions.append(
                AliasCollision(trigger=alias_spec.name, skill=name, owner=owner[alias_spec.name])
            )
            continue
        specs.append(alias_spec)
        owner[alias_spec.name] = name

    return SkillPlan(specs=tuple(specs), collisions=tuple(collisions))


def skill_command_specs(
    registry: CommandRegistry, skills: Iterable[SkillLike]
) -> tuple[CommandSpec, ...]:
    """Palette rows for *skills* that don't collide with *registry*.

    Thin view over :func:`plan_skill_commands` for callers that only
    want the specs (collision-blind); kept for back-compat with the
    story #1 API.
    """
    return plan_skill_commands(registry, skills).specs


def alias_collision_spans(collisions: Sequence[AliasCollision]) -> tuple[Segment, ...]:
    """A rich diagnostic listing for skill alias collisions (AC4): one
    row per trigger a skill wanted but couldn't claim, naming who already
    holds it. Empty when there is nothing to report. Textual-free (plain
    :class:`~amplifier_app_tui.model.blocks.Segment` spans posted as an
    ``Answer`` block by the caller) — matches the house style of
    ``ui/session_ops_view.py``'s other diagnostic listings
    (``/status``, ``/skills``, ...).
    """
    if not collisions:
        return ()
    count = len(collisions)
    noun = "collision" if count == 1 else "collisions"
    spans: list[Segment] = [
        Segment(text="· ", style_token="blue"),
        Segment(text="Skill aliases", style_token="bright", bold=True),
        Segment(
            text=f"  {count} {noun} · first registration wins\n",
            style_token="dim",
        ),
    ]
    width = max(len(collision.trigger) for collision in collisions)
    for collision in collisions:
        spans.append(Segment(text=f"  {collision.trigger.ljust(width)}  ", style_token="orange"))
        spans.append(
            Segment(
                text=f"wanted by {collision.skill} · already claimed by {collision.owner}\n",
                style_token="dim",
            )
        )
    return tuple(spans)


def register_skill_commands_reporting(
    registry: CommandRegistry, skills: Iterable[SkillLike]
) -> SkillPlan:
    """Register *skills* (names + shortcuts) into *registry*; returns
    both the specs actually added AND any alias collisions found (AC4).

    Rides the open-registry mechanism (story #2): each row registers as
    a ``skill``-sourced contribution, so ``registry.contributions("skill")``
    lists them and the registry's own collision policy (existing command
    wins, skip with a log line) backstops the prefilter in
    :func:`plan_skill_commands`.
    """
    plan = plan_skill_commands(registry, skills)
    added = tuple(spec for spec in plan.specs if registry.register(spec, source="skill"))
    return SkillPlan(specs=added, collisions=plan.collisions)


def register_skill_commands(
    registry: CommandRegistry, skills: Iterable[SkillLike]
) -> tuple[CommandSpec, ...]:
    """Register *skills* (names + shortcuts) into *registry*; returns the
    specs actually added — ``()`` when everything was already present.

    Back-compat wrapper over :func:`register_skill_commands_reporting`
    for callers that don't need collision diagnostics; the TUI boot path
    (AC4) calls the reporting version directly instead.
    """
    return register_skill_commands_reporting(registry, skills).specs


__all__ = [
    "AliasCollision",
    "SkillLike",
    "SkillPlan",
    "alias_collision_spans",
    "plan_skill_commands",
    "register_skill_commands",
    "register_skill_commands_reporting",
    "skill_command_specs",
]
