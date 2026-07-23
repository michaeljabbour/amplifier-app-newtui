"""The single command registry (DESIGN-SPEC §6, ADR-0007).

One table of :class:`CommandSpec` powers the palette rows, the keybinding
wiring and the help output — the opencode lesson: commands are data plus
callables, defined once, never inheritance hierarchies. The registry
knows nothing about Textual; command handlers act on the app exclusively
through the :class:`CommandContext` protocol (post messages / mutate
model state — never direct widget calls).

Palette semantics (DESIGN-SPEC §6):

- rows filter by substring of the command name (mockup:
  ``c.name.includes(filter)``);
- when the filter is exactly ``/``, group headers show in phase order
  (:data:`GROUP_ORDER`);
- running a command echoes it as a user line first
  (:meth:`CommandRegistry.run` calls ``ctx.echo_user_line`` before the
  handler).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from decimal import Decimal
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, field_validator

from ..model.queues import NeedsYouQueue, SteeringQueue
from ..model.trust import DenialLog
from ..model.turn import OutcomeLedger

CommandGroup = Literal["During", "Parallel", "Ship", "Between", "Repair"]
"""Palette group headers, exactly as the mockup COMMANDS table names them."""

GROUP_ORDER: tuple[CommandGroup, ...] = (
    "During",
    "Parallel",
    "Ship",
    "Between",
    "Repair",
)
"""Group header display order when the palette filter is exactly ``/``."""

CommandTag = str
"""Right-aligned dimmer tag on each palette row (DESIGN-SPEC §6).

Open by design (story #2): built-ins use ``built-in``; dynamic
contributions conventionally show their source name (``skill``, and
later ``recipe`` / ``pipeline``) — new capabilities must be able to
register verbs without a registry change, so this is not a Literal.
"""

CommandSource = str
"""Origin label of a registration — who contributed the command.

Well-known values today: ``builtin`` (seeded at construction) and
``skill`` (discovered skills + shortcuts). Future mounted capabilities
(``recipe``, ``pipeline``, …) pick their own label; the registry needs
no change to accept them.
"""

BUILTIN_SOURCE: CommandSource = "builtin"
"""The seed source: collides loudly, wins collisions, never unregisters."""

_log = logging.getLogger(__name__)


@runtime_checkable
class CommandContext(Protocol):
    """Everything a command handler may touch on the app.

    Implemented by the Textual app (posting messages under the hood) and
    by plain fakes in tests. Handlers must go through this protocol only —
    no widget imports, no direct rendering.
    """

    # --- data surfaces -------------------------------------------------
    @property
    def ledger(self) -> OutcomeLedger:
        """The session outcome ledger (``/ledger``, ``/improve``)."""
        ...

    @property
    def denial_log(self) -> DenialLog:
        """Deny-and-continue accounting (``/improve``)."""
        ...

    @property
    def steering(self) -> SteeringQueue:
        """The bounded steer / next-turn queue."""
        ...

    @property
    def needs_you(self) -> NeedsYouQueue:
        """Deferred decisions behind the ctrl-y badge."""
        ...

    @property
    def session_cost(self) -> Decimal:
        """Cumulative session cost — the footer $ (mockup ``this.cost``)."""
        ...

    @property
    def session_short(self) -> str:
        """Short session id shown in the ledger header/footer."""
        ...

    @property
    def bundle_name(self) -> str:
        """Active bundle name shown in the ledger header/footer."""
        ...

    def next_block_id(self) -> str:
        """Mint the next stable transcript block id."""
        ...

    def context_usage(self) -> object:
        """Current :class:`~amplifier_app_newtui.commands.context.ContextUsage`."""
        ...

    def approval_tallies(self) -> tuple[object, ...]:
        """Recorded :class:`~amplifier_app_newtui.commands.improve.ApprovalTally` rows."""
        ...

    def overridden_denials(self) -> tuple[object, ...]:
        """Recorded :class:`~amplifier_app_newtui.commands.improve.OverriddenDenial` rows."""
        ...

    def mcp_server_stats(self) -> tuple[object, ...]:
        """:class:`~amplifier_app_newtui.commands.doctor.McpServerStats` rows for /doctor."""
        ...

    # --- actions (message posts on the real app) -----------------------
    def echo_user_line(self, text: str) -> None:
        """Echo a command invocation as a ``❯ [mode]`` user line."""
        ...

    def post_block(self, block: object) -> None:
        """Append a TranscriptBlock to the transcript."""
        ...

    def show_notice(self, text: str) -> None:
        """Show a transient right-aligned dim notice."""
        ...

    def cycle_mode(self) -> None:
        """Advance the shift+tab mode cycle by one."""
        ...

    def set_mode(self, mode_id: str) -> None:
        """Jump directly to a mode by id."""
        ...

    def set_theme(self, name: str) -> None:
        """Switch the UI theme (``/theme``); empty name cycles (spec §1)."""
        ...

    def toggle_lanes(self) -> None:
        """Toggle the agent-lanes panel (``/tasks`` / ctrl-t)."""
        ...

    def open_rewind(self) -> None:
        """Open the rewind picker strip (``/rewind`` / ctrl-r)."""
        ...

    def open_permissions(self) -> None:
        """Open the trust-slot editor (``/permissions``)."""
        ...

    def manage_directories(self, kind: str, args: str) -> None:
        """List/add/remove allowed or denied session directories."""
        ...

    def quit_app(self) -> None:
        """Exit the app (``/quit`` — ctrl-d and ctrl-q are the key paths)."""
        ...

    def export_transcript(self) -> str:
        """Write the transcript markdown export; returns the written path
        (the ``/export`` handler surfaces it in the notice)."""
        ...

    def copy_answer(self) -> int:
        """Copy the last assistant answer to the clipboard (OSC 52);
        returns the number of chars copied (0 = no answer yet)."""
        ...

    def about_info(self) -> tuple[str, str, str, str]:
        """The identity data the session banner shows —
        ``(app_version, core_version, bundle_name, session_short)``;
        the ``/about`` handler posts it as a transcript block."""
        ...

    def show_modes(self) -> None:
        """Print the bundle-composed native mode catalog (``/modes``)."""
        ...

    def set_native_mode(self, name: str | None) -> None:
        """Activate (or clear with ``None``) a bundle-provided mode —
        actioned through the mounted mode tool, never an app-local list."""
        ...

    # -- in-session ops over the live amplifier coordinator -----------------

    def show_status(self) -> None:
        """Post the live session status block (``/status``)."""
        ...

    def show_model(self, arg: str) -> None:
        """``/model``: list models (empty arg) or switch to ``arg``."""
        ...

    def apply_effort(self, arg: str) -> None:
        """``/effort``: show current level (empty arg) or set to ``arg``."""
        ...

    def compact_context(self, focus: str) -> None:
        """``/compact``: compact context, optionally focused on ``focus``."""
        ...

    def clear_context(self) -> None:
        """``/clear``: clear the conversation context."""
        ...

    def show_tools(self) -> None:
        """``/tools``: post the mounted-tools roster."""
        ...

    def show_agents(self) -> None:
        """``/agents``: post the delegatable-agents roster."""
        ...

    def show_diff(self, arg: str) -> None:
        """``/diff``: post the working-tree (or ``staged``) patch."""
        ...

    def show_skills(self) -> None:
        """``/skills``: post the available-skills roster."""
        ...

    def load_skill(self, name: str) -> None:
        """``/skill <name>``: load a skill via the mounted skills tool."""
        ...

    def manage_mcp(self, args: str) -> None:
        """``/mcp``: list / add / remove MCP servers (mcp.json)."""
        ...

    def manage_config(self, args: str) -> None:
        """``/config``: show/toggle/set/diff/save live session config."""
        ...


CommandHandler = Callable[[CommandContext, str], None]
"""Handler signature: ``(ctx, args)`` where ``args`` is the text after the
command name (may be empty). Handlers post messages via ctx and return."""


class CommandSpec(BaseModel):
    """One palette command: group + name + description + tag + handler.

    - ``name``: the slash trigger including the leading ``/``.
    - ``desc``: palette row description — EXACT mockup strings for the
      built-in set (DESIGN-SPEC §6).
    - ``tag``: right-aligned dimmer tag — ``built-in``, ``skill``, or a
      future contribution's own label (open string, story #2).
    - ``key_action``: optional keymap action id this command duplicates
      (e.g. ``/tasks`` ↔ ``toggle_lanes``) so keybinds and palette stay a
      single source.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    group: CommandGroup
    name: str
    desc: str
    tag: CommandTag
    handler: CommandHandler
    key_action: str | None = None

    @field_validator("name")
    @classmethod
    def _name_is_slash_trigger(cls, value: str) -> str:
        if not value.startswith("/") or len(value) < 2 or " " in value:
            raise ValueError(f"command name must be a single /trigger, got {value!r}")
        return value

    @field_validator("desc")
    @classmethod
    def _desc_required(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("command description is required")
        return value

    @field_validator("tag")
    @classmethod
    def _tag_required(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("command tag is required")
        return value


class CommandRegistry:
    """Ordered registry of :class:`CommandSpec` — the palette's row source.

    Registration order is display order within the full list (the mockup
    table is already in phase order); :meth:`grouped_rows` regroups by
    :data:`GROUP_ORDER` for the headers-visible state.

    Open registry (story #2): built-ins seed at construction; any mounted
    capability may :meth:`register` verbs at runtime under its own
    ``source`` label (``skill`` today; ``recipe`` / ``pipeline`` later)
    and :meth:`unregister` them when it unmounts. Collision policy:
    built-ins win (a duplicate *built-in* is a programming error and
    raises); a dynamic registration whose name is taken is skipped with a
    log line, first registration wins. :meth:`subscribe` observers hear
    every successful change so the palette/help stay a live reflection.
    """

    def __init__(self, specs: tuple[CommandSpec, ...] = ()) -> None:
        self._specs: list[CommandSpec] = []
        self._by_name: dict[str, CommandSpec] = {}
        self._sources: dict[str, CommandSource] = {}
        self._listeners: list[Callable[[], None]] = []
        for spec in specs:
            self.register(spec)

    @property
    def specs(self) -> tuple[CommandSpec, ...]:
        return tuple(self._specs)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(spec.name for spec in self._specs)

    def register(self, spec: CommandSpec, *, source: CommandSource = BUILTIN_SOURCE) -> bool:
        """Add a command under *source*; returns whether it was added.

        Duplicate built-ins fail loudly (a bug in the seed table); a
        dynamic contribution whose name is already taken is skipped with
        a log line — the existing command, built-in or earlier dynamic
        registration, always wins.
        """
        if spec.name in self._by_name:
            if source == BUILTIN_SOURCE:
                raise ValueError(f"command already registered: {spec.name}")
            _log.warning(
                "command %s from %r skipped: already registered by %r",
                spec.name,
                source,
                self._sources[spec.name],
            )
            return False
        self._specs.append(spec)
        self._by_name[spec.name] = spec
        self._sources[spec.name] = source
        self._notify()
        return True

    def unregister(self, name: str) -> bool:
        """Remove a dynamic command by name; returns whether it existed.

        Built-ins are permanent — trying to unregister one raises.
        """
        key = name.strip()
        spec = self._by_name.get(key)
        if spec is None:
            return False
        if self._sources[key] == BUILTIN_SOURCE:
            raise ValueError(f"built-in command cannot be unregistered: {key}")
        self._specs.remove(spec)
        del self._by_name[key]
        del self._sources[key]
        self._notify()
        return True

    def source_of(self, name: str) -> CommandSource | None:
        """Who registered *name* — ``None`` when unknown."""
        return self._sources.get(name.strip())

    def contributions(self, source: CommandSource) -> tuple[CommandSpec, ...]:
        """All commands registered under *source*, in registration order."""
        return tuple(
            spec for spec in self._specs if self._sources[spec.name] == source
        )

    def subscribe(self, listener: Callable[[], None]) -> None:
        """Call *listener* after every successful register/unregister
        (skipped collisions and no-op unregisters stay silent) — the
        palette re-reads :attr:`specs` on each change."""
        self._listeners.append(listener)

    def _notify(self) -> None:
        for listener in tuple(self._listeners):
            listener()

    def get(self, name: str) -> CommandSpec | None:
        return self._by_name.get(name.strip())

    # --- palette -------------------------------------------------------
    def filter_rows(self, query: str) -> tuple[CommandSpec, ...]:
        """Rows whose name contains *query* (mockup substring semantics).

        ``"/"`` (or empty) matches everything. Matching is on the command
        name only, exactly like the mockup's ``c[1].includes(filter)``.
        """
        needle = query.strip()
        if needle in {"", "/"}:
            return self.specs
        return tuple(spec for spec in self._specs if needle in spec.name)

    @staticmethod
    def show_group_headers(query: str) -> bool:
        """Group headers show only when the filter is exactly ``/``."""
        return query.strip() == "/"

    def grouped_rows(
        self, query: str = "/"
    ) -> tuple[tuple[CommandGroup, tuple[CommandSpec, ...]], ...]:
        """Matching rows grouped in :data:`GROUP_ORDER`; empty groups omitted.

        Also serves as the help listing (same single source).
        """
        rows = self.filter_rows(query)
        grouped: list[tuple[CommandGroup, tuple[CommandSpec, ...]]] = []
        for group in GROUP_ORDER:
            members = tuple(spec for spec in rows if spec.group == group)
            if members:
                grouped.append((group, members))
        return tuple(grouped)

    # --- keybinds ------------------------------------------------------
    def keybound(self) -> dict[str, CommandSpec]:
        """Keymap action id → command, for wiring key chords to handlers."""
        return {
            spec.key_action: spec for spec in self._specs if spec.key_action is not None
        }

    # --- execution -----------------------------------------------------
    def run(self, name: str, ctx: CommandContext, args: str = "") -> None:
        """Run a command by name: echo it as a user line, then dispatch.

        DESIGN-SPEC §6: running a command echoes it as a user line first.
        Unknown names raise ``KeyError`` (the palette only offers real
        rows; a typo reaching here is a bug).
        """
        spec = self.get(name)
        if spec is None:
            raise KeyError(f"unknown command: {name}")
        invocation = spec.name if not args.strip() else f"{spec.name} {args.strip()}"
        ctx.echo_user_line(invocation)
        spec.handler(ctx, args.strip())

    def parse_and_run(self, ctx: CommandContext, input_text: str) -> bool:
        """Dispatch raw composer text like ``/mode plan``.

        Returns False when the text is not a known command (the composer
        treats it as a normal message).
        """
        text = input_text.strip()
        if not text.startswith("/"):
            return False
        name, _, args = text.partition(" ")
        if self.get(name) is None:
            return False
        self.run(name, ctx, args)
        return True


__all__ = [
    "BUILTIN_SOURCE",
    "CommandContext",
    "CommandGroup",
    "CommandHandler",
    "CommandRegistry",
    "CommandSource",
    "CommandSpec",
    "CommandTag",
    "GROUP_ORDER",
]
