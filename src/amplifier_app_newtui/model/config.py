"""Live session configuration state (the ``/config`` domain model).

Port of amplifier-app-cli's ``/config`` capability
(``ui/command_config.py`` + ``ui/command_config_dashboard.py``, backed by
``amplifier_foundation.configurator.SessionConfigurator``). The donor
threads a foundation configurator through a Rich-printing command mixin;
this is the amplifier-native re-expression on newtui's own seams: a pure,
Textual-free / amplifier-core-free state object that BOTH the demo and
real runtimes drive identically (ADR-0007 invariant 4).

The state is the session's live view of its bundle configuration:

- **categories** -- ``context`` / ``tools`` / ``hooks`` / ``providers`` /
  ``agents`` items, each enabled or disabled (``hooks`` is read-only,
  matching the donor: a runtime hook suspend/resume API does not exist);
- **overrides** -- ``set <path> <value>`` values with the donor's
  bool->int->float->string type inference;
- **snapshot / diff** -- an origin snapshot captured at startup so
  ``/config diff`` reports what changed this session.

Everything is pure data + logic so it unit-tests without a session and
serializes to a settings sub-tree the kernel persists on ``/config save``.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Literal

ConfigCategory = Literal["context", "tools", "hooks", "providers", "agents"]
"""The mount-plan sections ``/config`` surfaces (donor minus ``behaviors``:
newtui's plan has no behavior-group layer)."""

CONFIG_CATEGORIES: tuple[ConfigCategory, ...] = (
    "context",
    "tools",
    "hooks",
    "providers",
    "agents",
)
"""Display order of the categories in ``/config show`` (donor order)."""

READ_ONLY_CATEGORIES: frozenset[str] = frozenset({"hooks"})
"""Categories that render but cannot toggle. Donor parity: hook toggle
needs a core suspend/resume API that does not exist, so hooks are
inspection-only (``command_config_dashboard._handle_config_toggle``)."""

InvocationKind = Literal[
    "help",
    "show",
    "category",
    "item",
    "toggle",
    "set",
    "diff",
    "save",
    "error",
]

_SCOPES: frozenset[str] = frozenset({"global", "project", "local"})


def parse_value(text: str) -> bool | int | float | str:
    """Infer ``bool -> int -> float -> str`` from a raw ``/config set`` value.

    Verbatim port of the donor's ``_handle_config_set`` inference
    (``command_config_dashboard.py``): ``true``/``false`` (case-insensitive)
    become booleans, then integer, then float, else the string is kept.
    """
    lowered = text.strip().lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


@dataclass(frozen=True)
class ConfigItem:
    """One toggleable configuration entry within a category."""

    category: ConfigCategory
    name: str
    enabled: bool = True
    detail: str = ""
    """A short right-hand descriptor (module id, source, ...) for display."""

    @property
    def read_only(self) -> bool:
        return self.category in READ_ONLY_CATEGORIES


@dataclass(frozen=True)
class ConfigChange:
    """One line of ``/config diff`` -- what changed since startup."""

    category: str
    name: str
    action: str


@dataclass(frozen=True)
class ConfigInvocation:
    """The parsed intent of a ``/config ...`` command line."""

    kind: InvocationKind
    category: str = ""
    name: str = ""
    enable: bool = False
    path: str = ""
    value: str = ""
    scope: str = "global"
    message: str = ""


def parse_config_command(args: str) -> ConfigInvocation:
    """Route a raw ``/config`` argument string to a :class:`ConfigInvocation`.

    Port of the donor's ``_get_config_display`` dispatch (minus the Rich
    display ``--compact/--detailed/--trees/--format`` flags, which the TUI
    renders natively rather than as text views):

    - no args -> ``help``
    - ``show`` [``<category>`` [``<name>``]] -> ``show`` / ``category`` / ``item``
    - ``diff`` -> ``diff``
    - ``save`` [``--scope global|project|local``] -> ``save``
    - ``set <path> <value>`` -> ``set``
    - ``<category>`` -> ``category``
    - ``<category> enable|disable <name>`` -> ``toggle``
    - ``<category> <name>`` -> ``item``
    - anything else -> ``error`` with a usage line
    """
    parts = args.split()
    if not parts:
        return ConfigInvocation(kind="help")

    head = parts[0].lower()

    if head == "show":
        rest = parts[1:]
        if not rest:
            return ConfigInvocation(kind="show")
        category = rest[0].lower()
        if category not in CONFIG_CATEGORIES:
            return ConfigInvocation(
                kind="error",
                message=f"unknown category '{rest[0]}' \u00b7 {_category_hint()}",
            )
        if len(rest) == 1:
            return ConfigInvocation(kind="category", category=category)
        return ConfigInvocation(kind="item", category=category, name=rest[1])

    if head == "diff":
        return ConfigInvocation(kind="diff")

    if head == "save":
        return _parse_save(parts[1:])

    if head == "set":
        if len(parts) < 3:
            return ConfigInvocation(
                kind="error", message="usage: /config set <path> <value>"
            )
        return ConfigInvocation(kind="set", path=parts[1], value=parts[2])

    if head in CONFIG_CATEGORIES:
        rest = parts[1:]
        if not rest:
            return ConfigInvocation(kind="category", category=head)
        action = rest[0].lower()
        if action in ("enable", "disable"):
            if len(rest) < 2:
                return ConfigInvocation(
                    kind="error",
                    message=f"usage: /config {head} {action} <name>",
                )
            return ConfigInvocation(
                kind="toggle", category=head, name=rest[1], enable=action == "enable"
            )
        return ConfigInvocation(kind="item", category=head, name=rest[0])

    return ConfigInvocation(
        kind="error",
        message=f"unknown /config subcommand '{parts[0]}' \u00b7 try /config",
    )


def _parse_save(rest: list[str]) -> ConfigInvocation:
    scope = "global"
    index = 0
    while index < len(rest):
        token = rest[index]
        if token == "--scope" and index + 1 < len(rest):
            scope = rest[index + 1].lower()
            index += 2
            continue
        if token.startswith("--scope="):
            scope = token.split("=", 1)[1].lower()
        elif token.lower() in _SCOPES:
            scope = token.lower()
        index += 1
    if scope not in _SCOPES:
        return ConfigInvocation(
            kind="error",
            message=f"unknown scope '{scope}' \u00b7 use global | project | local",
        )
    return ConfigInvocation(kind="save", scope=scope)


def _category_hint() -> str:
    return "categories: " + ", ".join(CONFIG_CATEGORIES)


class _Unset:
    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return "<unset>"


_UNSET = _Unset()


class SessionConfigState:
    """The live, mutable configuration state for one session.

    Seeded from the resolved mount plan (real) or a representative demo
    snapshot, then mutated by ``/config <category> disable|enable`` and
    ``/config set``. :meth:`snapshot` freezes the startup state so
    :meth:`diff` can report the session's changes; :meth:`to_settings`
    serializes those changes for ``/config save``.
    """

    def __init__(
        self,
        items: list[ConfigItem] | tuple[ConfigItem, ...] = (),
        *,
        bundle: str = "",
        values: dict[str, Any] | None = None,
    ) -> None:
        self.bundle = bundle
        self._items: dict[tuple[str, str], ConfigItem] = {}
        self._order: list[tuple[str, str]] = []
        for item in items:
            key = (item.category, item.name)
            if key not in self._items:
                self._order.append(key)
            self._items[key] = item
        self._values: dict[str, Any] = dict(values or {})
        self._origin_enabled: dict[tuple[str, str], bool] = {}
        self._origin_values: dict[str, Any] = {}
        self.snapshot()

    # -- snapshot / diff ----------------------------------------------------

    def snapshot(self) -> None:
        """Freeze the current enabled-state + overrides as the diff origin."""
        self._origin_enabled = {key: item.enabled for key, item in self._items.items()}
        self._origin_values = deepcopy(self._values)

    def diff(self) -> tuple[ConfigChange, ...]:
        """Every enabled-state flip and override change since :meth:`snapshot`."""
        changes: list[ConfigChange] = []
        for key in self._order:
            item = self._items[key]
            origin = self._origin_enabled.get(key, item.enabled)
            if item.enabled != origin:
                changes.append(
                    ConfigChange(
                        category=item.category,
                        name=item.name,
                        action="enabled" if item.enabled else "disabled",
                    )
                )
        for path in sorted(set(self._values) | set(self._origin_values)):
            new = self._values.get(path, _UNSET)
            old = self._origin_values.get(path, _UNSET)
            if new == old:
                continue
            if new is _UNSET:
                changes.append(ConfigChange("set", path, "removed"))
            else:
                changes.append(ConfigChange("set", path, f"= {new!r}"))
        return tuple(changes)

    # -- queries ------------------------------------------------------------

    def items(self, category: str | None = None) -> tuple[ConfigItem, ...]:
        """All items, optionally filtered to *category*, in display order."""
        ordered = [self._items[key] for key in self._order]
        if category is None:
            return tuple(ordered)
        return tuple(item for item in ordered if item.category == category)

    def find(self, category: str, name: str) -> ConfigItem | None:
        return self._items.get((category, name))

    @property
    def overrides(self) -> dict[str, Any]:
        return dict(self._values)

    def value(self, path: str) -> Any:
        return self._values.get(path)

    # -- mutations ----------------------------------------------------------

    def toggle(self, category: str, name: str, *, enable: bool) -> tuple[bool, str]:
        """Enable/disable an item; returns ``(ok, message)``.

        Hooks are read-only (donor parity); an unknown item is refused.
        """
        if category in READ_ONLY_CATEGORIES:
            return (
                False,
                f"{category} are read-only \u00b7 visible for inspection, not toggleable",
            )
        item = self._items.get((category, name))
        if item is None:
            return (False, f"no {category} item named '{name}'")
        if item.enabled == enable:
            state = "enabled" if enable else "disabled"
            return (False, f"{name} already {state}")
        self._items[(category, name)] = ConfigItem(
            category=item.category,
            name=item.name,
            enabled=enable,
            detail=item.detail,
        )
        verb = "Enabled" if enable else "Disabled"
        return (True, f"\u2713 {verb} {name}")

    def set_value(self, path: str, raw_value: str) -> tuple[bool, str]:
        """Set an override with the donor's type inference; ``(ok, message)``."""
        if not path:
            return (False, "usage: /config set <path> <value>")
        parsed = parse_value(raw_value)
        self._values[path] = parsed
        return (True, f"\u2713 Set {path} = {parsed!r}")

    # -- persistence --------------------------------------------------------

    def to_settings(self) -> dict[str, Any]:
        """Serialize the session's changes for ``/config save``.

        Shape (stored under a ``configurator:`` settings key, donor parity):
        ``{"disabled": {category: [names...]}, "overrides": {path: value}}``.
        Only items the session actively disabled and any overrides are
        recorded -- an untouched default is not re-listed.
        """
        disabled: dict[str, list[str]] = {}
        for key in self._order:
            item = self._items[key]
            if not item.enabled and item.category not in READ_ONLY_CATEGORIES:
                disabled.setdefault(item.category, []).append(item.name)
        settings: dict[str, Any] = {}
        if disabled:
            settings["disabled"] = disabled
        if self._values:
            settings["overrides"] = deepcopy(self._values)
        return settings

    @property
    def change_count(self) -> int:
        return len(self.diff())


def _plan_entries(mount_plan: dict[str, Any], section: str) -> list[Any]:
    value = mount_plan.get(section)
    return value if isinstance(value, list) else []


def _entry_name(entry: Any) -> str:
    if isinstance(entry, dict):
        return str(
            entry.get("id")
            or entry.get("instance_id")
            or entry.get("name")
            or entry.get("module")
            or ""
        )
    return str(entry)


def _entry_detail(entry: Any) -> str:
    if isinstance(entry, dict):
        module = str(entry.get("module") or "")
        name = _entry_name(entry)
        return module if module and module != name else ""
    return ""


def state_from_mount_plan(
    mount_plan: dict[str, Any], *, bundle: str = ""
) -> SessionConfigState:
    """Build a :class:`SessionConfigState` from a resolved mount plan.

    Reads the plan's ``providers`` / ``tools`` / ``hooks`` / ``agents``
    lists plus the singular ``session.context`` module. Every mounted
    entry starts enabled (it is, in fact, mounted); the app's own
    disable actions ride on top. Pure dict work -- no amplifier import.
    """
    items: list[ConfigItem] = []

    session = mount_plan.get("session")
    if isinstance(session, dict):
        context = session.get("context")
        if isinstance(context, dict):
            module = str(context.get("module") or "context")
            items.append(ConfigItem("context", module, True, "session.context"))

    section_map: tuple[tuple[ConfigCategory, str], ...] = (
        ("context", "context"),
        ("tools", "tools"),
        ("hooks", "hooks"),
        ("providers", "providers"),
        ("agents", "agents"),
    )
    for category, section in section_map:
        for entry in _plan_entries(mount_plan, section):
            name = _entry_name(entry)
            if not name:
                continue
            items.append(ConfigItem(category, name, True, _entry_detail(entry)))

    agents = mount_plan.get("agents")
    if isinstance(agents, dict):
        for name in agents:
            if name in ("dirs", "include", "inline"):
                continue
            items.append(ConfigItem("agents", str(name), True, ""))

    return SessionConfigState(_dedupe(items), bundle=bundle)


def _dedupe(items: list[ConfigItem]) -> list[ConfigItem]:
    seen: set[tuple[str, str]] = set()
    result: list[ConfigItem] = []
    for item in items:
        key = (item.category, item.name)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


_DEMO_ITEMS: tuple[ConfigItem, ...] = (
    ConfigItem("context", "context-window", True, "session.context"),
    ConfigItem("tools", "read_file", True, "tool-filesystem"),
    ConfigItem("tools", "write_file", True, "tool-filesystem"),
    ConfigItem("tools", "bash", True, "tool-shell"),
    ConfigItem("tools", "load_skill", True, "tool-skills"),
    ConfigItem("hooks", "hooks-logging", True, "hooks-logging"),
    ConfigItem("hooks", "hooks-mode", True, "hooks-mode"),
    ConfigItem("hooks", "hooks-approval", True, "hooks-approval"),
    ConfigItem("providers", "anthropic", True, "provider-anthropic"),
    ConfigItem("agents", "general", True, ""),
    ConfigItem("agents", "coding", True, ""),
    ConfigItem("agents", "reasoning", True, ""),
)
"""Representative snapshot for the offline demo runtime (DESIGN-SPEC:
the demo must be a faithful stand-in the UI cannot distinguish)."""


def default_config_state(bundle: str = "") -> SessionConfigState:
    """A representative state for the demo / base runtime (no live session)."""
    return SessionConfigState(_DEMO_ITEMS, bundle=bundle)


@dataclass(frozen=True)
class ConfigSnapshotView:
    """An immutable, thread-hop-safe snapshot of the config state for the UI.

    The runtime lives on its own thread; the adapter marshals this frozen
    view out rather than the mutable :class:`SessionConfigState`.
    """

    bundle: str
    items: tuple[ConfigItem, ...] = field(default_factory=tuple)
    overrides: tuple[tuple[str, str], ...] = field(default_factory=tuple)
    changes: tuple[ConfigChange, ...] = field(default_factory=tuple)

    @classmethod
    def of(cls, state: SessionConfigState) -> ConfigSnapshotView:
        return cls(
            bundle=state.bundle,
            items=state.items(),
            overrides=tuple(
                (path, repr(value)) for path, value in state.overrides.items()
            ),
            changes=state.diff(),
        )

    def items_in(self, category: str) -> tuple[ConfigItem, ...]:
        return tuple(item for item in self.items if item.category == category)


__all__ = [
    "CONFIG_CATEGORIES",
    "READ_ONLY_CATEGORIES",
    "ConfigCategory",
    "ConfigChange",
    "ConfigInvocation",
    "ConfigItem",
    "ConfigSnapshotView",
    "SessionConfigState",
    "default_config_state",
    "parse_config_command",
    "parse_value",
    "state_from_mount_plan",
]
