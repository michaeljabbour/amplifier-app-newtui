"""Slash commands and minimal CLI subcommands.

One command registry powers keybinds, slash triggers, the palette and
help (opencode pattern) — commands are data + callables, never
inheritance hierarchies.

Layering: this package may import ``model/`` (and stdlib) only — no
Textual, no amplifier-core. Handlers reach the app exclusively through
the :class:`~amplifier_app_newtui.commands.registry.CommandContext`
protocol.
"""

from .builtin import BUILTIN_COMMANDS, build_registry
from .registry import (
    BUILTIN_SOURCE,
    GROUP_ORDER,
    CommandContext,
    CommandGroup,
    CommandRegistry,
    CommandSource,
    CommandSpec,
    CommandTag,
)

__all__ = [
    "BUILTIN_COMMANDS",
    "BUILTIN_SOURCE",
    "CommandContext",
    "CommandGroup",
    "CommandRegistry",
    "CommandSource",
    "CommandSpec",
    "CommandTag",
    "GROUP_ORDER",
    "build_registry",
]
