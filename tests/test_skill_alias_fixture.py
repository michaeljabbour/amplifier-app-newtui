"""The ONE shared skill+alias fixture for CLI/TUI resolution parity.

Compliance B2 AC5: "Parity tests use one shared alias fixture against
CLI and TUI command resolution." Before this file, ``test_commands_skills.py``
(the registry-level "CLI" resolution path — ``CommandRegistry.parse_and_run``
against a plain ``CommandContext``, no Textual) and ``test_flow_skill_aliases.py``
(the interactive "TUI" path — the real ``TuiApp`` over Textual's Pilot) each
described their own "cranky-old-sam / cosam" skill independently: they
happened to agree, but nothing PROVED it. This module is the single
source of truth both import, so a change to the fixture data changes
both surfaces' expectations together.

This file intentionally defines no tests (mirrors ``test_flow_helpers.py``).
"""

from __future__ import annotations

from amplifier_app_tui.kernel.session_ops import SkillInfo

SKILL_ALIAS_FIXTURE: tuple[SkillInfo, ...] = (
    SkillInfo("cranky-old-sam", "crusty code review", shortcut="cosam"),
    SkillInfo("release-notes", "draft the release notes", shortcut="relnotes"),
)
"""The baseline fixture: two skills, each with a distinct short alias.
Used for happy-path resolution (AC1), autocomplete/help (AC2), and the
CLI/TUI parity test (AC5)."""

COLLIDING_ALIAS_FIXTURE: tuple[SkillInfo, ...] = (
    SkillInfo("cranky-old-sam", "crusty code review", shortcut="cosam"),
    SkillInfo("crusty-old-sam", "a decoy skill sharing cosam", shortcut="cosam"),
)
"""Two DIFFERENT skills that both declare the same ``shortcut:`` — a
genuine alias collision (AC4), independent of any built-in shadowing."""

BUILTIN_SHADOW_FIXTURE: tuple[SkillInfo, ...] = (
    SkillInfo("status", "a skill that shadows the /status built-in"),
)
"""A skill whose bare name shadows an existing built-in command — the
other AC4 collision shape (a discovered skill vs. a seeded built-in,
not skill vs. skill)."""


__all__ = [
    "BUILTIN_SHADOW_FIXTURE",
    "COLLIDING_ALIAS_FIXTURE",
    "SKILL_ALIAS_FIXTURE",
]
