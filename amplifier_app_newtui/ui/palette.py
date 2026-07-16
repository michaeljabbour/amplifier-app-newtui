"""Command palette strip (DESIGN-SPEC §6, §2 overlay strips).

A bordered strip docked ABOVE the composer (never a ModalScreen —
ADR-0007/mockup): opens on ``/``, live-filters by substring as the user
types, and shows uppercase dimmer group headers (During / Parallel /
Ship / Between / Repair) only when the filter is exactly ``"/"``.

Rows: teal command (min-width aligned column) + description + right-
aligned dimmer tag (``built-in``/``skill``). The selected row (first by
default) is highlighted ``bg-tab`` with its description brightened to
``fg``. ``↑``/``↓`` move the selection, Enter runs the selected row,
click runs any row. Esc closes — but is resolved by the app via
``keymap.ESC_CHAIN`` (spec §5), never by a local binding here.

The palette is data-driven and *controlled*: it consumes a list of
:class:`CommandSpec` objects (provided by the commands package) and a
filter string (slaved to the composer text via :meth:`PaletteStrip.apply_filter`).
It never executes commands itself — it posts
:class:`PaletteStrip.CommandRun` / :class:`PaletteStrip.Closed` messages
and the app reacts (running a command echoes it as a user line first,
per spec §6).

All color flows through theme tokens: TCSS uses ``$rule``/``$bg-tab``/…
variables; rich renderables resolve token names via
``app.theme_variables`` at paint time, so a theme switch is a repaint.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from rich.style import Style
from rich.table import Table
from rich.text import Text
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.message import Message
from textual.widgets import Static

PALETTE_GROUPS: tuple[str, ...] = ("During", "Parallel", "Ship", "Between", "Repair")
"""Group header order per the mockup's command table."""

CMD_COL_MIN_WIDTH = 20
"""Command column minimum width: the mockup's 150px at JetBrains Mono
12.5px (7.5px/cell) is exactly 20 cells."""


@runtime_checkable
class CommandSpec(Protocol):
    """What the palette needs to know about one slash command.

    The commands package owns the registry; any object with these
    attributes renders as a palette row.

    - ``name``: the slash trigger, e.g. ``/mode``.
    - ``desc``: one-line description (field name matches
      ``commands.registry.CommandSpec``).
    - ``tag``: right-aligned dimmer origin tag (``built-in``/``skill``).
    - ``group``: spec §6 group header (one of :data:`PALETTE_GROUPS`).

    Read-only properties so narrower registry types (``Literal`` tags and
    groups) satisfy the protocol.
    """

    @property
    def name(self) -> str: ...

    @property
    def desc(self) -> str: ...

    @property
    def tag(self) -> str: ...

    @property
    def group(self) -> str: ...


def filter_commands(
    commands: Sequence[CommandSpec], filter_text: str
) -> tuple[CommandSpec, ...]:
    """Substring filter over command names (mockup: ``cmd.includes(filter)``).

    The filter includes its leading ``/`` so ``"/"`` matches everything.
    """
    return tuple(c for c in commands if filter_text in c.name)


def show_group_headers(filter_text: str) -> bool:
    """Group headers appear only when the filter is exactly ``/`` (spec §6)."""
    return filter_text == "/"


def command_row_cells(spec: CommandSpec) -> tuple[str, str, str]:
    """The three text cells of one palette row: (command, description, tag)."""
    return (spec.name, spec.desc, spec.tag)


def group_header_text(group: str) -> str:
    """Displayed header text — uppercase dimmer per the mockup CSS."""
    return group.upper()


class _GroupHeader(Static):
    """Uppercase dimmer group header row (spec §6)."""

    DEFAULT_CSS = """
    _GroupHeader {
        width: 100%;
        height: 1;
        color: $dimmer;
        padding: 0 2;
    }
    """

    def __init__(self, group: str) -> None:
        super().__init__(Text(group_header_text(group)))
        self.group = group


class _CommandRow(Static):
    """One clickable command row: teal cmd + desc + right dimmer tag."""

    DEFAULT_CSS = """
    _CommandRow {
        width: 100%;
        height: 1;
        padding: 0 2;
    }
    _CommandRow.-selected {
        background: $bg-tab;
    }
    """

    def __init__(self, spec: CommandSpec, index: int) -> None:
        super().__init__(id=f"palette-row-{index}")
        self.spec = spec
        self.index = index

    def render(self) -> Table:
        tokens = self.app.theme_variables
        selected = self.has_class("-selected")
        cmd, desc, tag = command_row_cells(self.spec)
        grid = Table.grid(expand=True, padding=(0, 1))
        grid.add_column(min_width=CMD_COL_MIN_WIDTH, no_wrap=True)
        grid.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        grid.add_column(justify="right", no_wrap=True)
        grid.add_row(
            Text(cmd, style=Style(color=tokens.get("teal"))),
            Text(desc, style=Style(color=tokens.get("fg" if selected else "dim"))),
            Text(tag, style=Style(color=tokens.get("dimmer"))),
        )
        return grid

    def on_click(self) -> None:
        self.post_message(PaletteStrip.CommandRun(self.spec))


class PaletteStrip(VerticalScroll):
    """The command palette overlay strip (DESIGN-SPEC §6).

    Controlled widget: the host calls :meth:`set_commands` once and
    :meth:`apply_filter` on every composer change (``None`` or a non-``/``
    string closes it). It posts messages instead of acting:

    - :class:`CommandRun` — Enter on the selection or click on any row.
    - :class:`Closed` — :meth:`action_close` ran (Esc itself is resolved
      by the app via ``keymap.ESC_CHAIN``, spec §5).
    """

    can_focus = True

    DEFAULT_CSS = """
    PaletteStrip {
        display: none;
        width: 100%;
        height: auto;
        max-height: 12;
        border-top: solid $rule;
        background: $bg-page;
        padding: 0;
        scrollbar-size-vertical: 1;
        /* All UI color comes from the §1 tokens — never Textual-derived. */
        scrollbar-color: $rule;
        scrollbar-color-hover: $dim;
        scrollbar-color-active: $dim;
        scrollbar-background: $bg-page;
        scrollbar-background-hover: $bg-page;
        scrollbar-background-active: $bg-page;
    }
    """

    BINDINGS = [
        Binding("up", "cursor_up", "↑↓ select", show=False),
        Binding("down", "cursor_down", "↑↓ select", show=False),
        Binding("enter", "run", "enter run", show=False),
        # No local escape binding: Esc must bubble to the app so it resolves
        # via keymap.ESC_CHAIN (spec §5 — lane-focus closes before the
        # palette even while this strip holds keyboard focus). The chain
        # calls the app's ``close_palette`` when the palette step is reached.
    ]

    class CommandRun(Message):
        """The user ran a palette row (Enter on selection or click)."""

        def __init__(self, command: CommandSpec) -> None:
            self.command = command
            super().__init__()

    class Closed(Message):
        """:meth:`action_close` ran while the palette was open."""

    def __init__(
        self,
        commands: Sequence[CommandSpec] = (),
        *,
        id: str | None = None,  # noqa: A002 - Textual widget API
    ) -> None:
        super().__init__(id=id)
        self._commands: tuple[CommandSpec, ...] = tuple(commands)
        self._filter: str | None = None
        self._filtered: tuple[CommandSpec, ...] = ()
        self._selected = 0

    # -- public API ----------------------------------------------------

    @property
    def is_open(self) -> bool:
        return bool(self.display)

    @property
    def filter_text(self) -> str | None:
        return self._filter

    @property
    def filtered_commands(self) -> tuple[CommandSpec, ...]:
        """Currently displayed commands, in row order."""
        return self._filtered

    @property
    def selected_command(self) -> CommandSpec | None:
        if not self._filtered:
            return None
        return self._filtered[self._selected]

    def set_commands(self, commands: Sequence[CommandSpec]) -> None:
        """Replace the command list (re-applies the current filter)."""
        self._commands = tuple(commands)
        self.apply_filter(self._filter)

    def apply_filter(self, filter_text: str | None) -> None:
        """Slave the palette to the composer text.

        ``None`` (or text not starting with ``/``) closes the strip; a
        ``/…`` filter rebuilds the rows. Zero matches also hide the strip
        (mockup: ``paletteOpen = filter != null && entries.length``).
        """
        if filter_text is None or not filter_text.startswith("/"):
            self._filter = None
            self._filtered = ()
            self._selected = 0
            self.display = False
            self.remove_children()
            return
        self._filter = filter_text
        self._filtered = filter_commands(self._commands, filter_text)
        self._selected = 0
        self._rebuild()

    def move_selection(self, delta: int) -> None:
        """Move the highlighted row by *delta*, clamped to the list."""
        if not self._filtered:
            return
        self._selected = max(0, min(len(self._filtered) - 1, self._selected + delta))
        self._apply_selection()

    def run_selected(self) -> None:
        """Post :class:`CommandRun` for the highlighted row."""
        command = self.selected_command
        if command is not None:
            self.post_message(self.CommandRun(command))

    # -- key actions ----------------------------------------------------

    def action_cursor_up(self) -> None:
        self.move_selection(-1)

    def action_cursor_down(self) -> None:
        self.move_selection(1)

    def action_run(self) -> None:
        self.run_selected()

    def action_close(self) -> None:
        self.post_message(self.Closed())

    # -- internals -------------------------------------------------------

    def _rebuild(self) -> None:
        if not self._filtered:
            self.display = False
            self.remove_children()
            return
        self.display = True
        # remove_children is asynchronous: await it before remounting so
        # rebuilt rows never collide with the ids of outgoing ones.
        self.call_later(self._remount_rows)

    async def _remount_rows(self) -> None:
        await self.remove_children()
        if not self._filtered:
            return
        rows: list[Static] = []
        headers = show_group_headers(self._filter or "")
        last_group: str | None = None
        for index, spec in enumerate(self._filtered):
            if headers and spec.group != last_group:
                last_group = spec.group
                rows.append(_GroupHeader(spec.group))
            rows.append(_CommandRow(spec, index))
        await self.mount(*rows)
        self._apply_selection()

    def _apply_selection(self) -> None:
        rows = list(self.query(_CommandRow))
        for row in rows:
            row.set_class(row.index == self._selected, "-selected")
        if 0 <= self._selected < len(rows):
            rows[self._selected].scroll_visible()


__all__ = [
    "CMD_COL_MIN_WIDTH",
    "CommandSpec",
    "PALETTE_GROUPS",
    "PaletteStrip",
    "command_row_cells",
    "filter_commands",
    "group_header_text",
    "show_group_headers",
]
