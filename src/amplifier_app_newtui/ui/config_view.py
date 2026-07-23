"""Segment renderers for the ``/config`` command output.

``/config show`` / ``<category>`` / ``<name>`` / ``diff`` / help each post an
:class:`~amplifier_app_newtui.model.blocks.Answer` to the transcript; these
pure functions turn the frozen
:class:`~amplifier_app_newtui.model.config.ConfigSnapshotView` into the flat
``Segment`` stream that block carries, matching the house style of
:mod:`amplifier_app_newtui.ui.session_ops_view` (blue ``\u00b7`` marker,
bright-bold header, dim/teal detail). Pure and Textual-free so they
unit-test as span tuples.
"""

from __future__ import annotations

from ..model.blocks import Segment
from ..model.config import (
    CONFIG_CATEGORIES,
    ConfigChange,
    ConfigItem,
    ConfigSnapshotView,
)


def _header(label: str, detail: str) -> list[Segment]:
    return [
        Segment(text="\u00b7 ", style_token="blue"),
        Segment(text=label, style_token="bright", bold=True),
        Segment(text=f"  {detail}\n", style_token="dim"),
    ]


def _item_line(item: ConfigItem) -> list[Segment]:
    glyph = "\u25cf " if item.enabled else "\u25cb "
    glyph_token = "green" if item.enabled else "dimmer"
    name_token = "teal" if item.enabled else "dimmer"
    spans = [
        Segment(text=f"    {glyph}", style_token=glyph_token),
        Segment(text=item.name, style_token=name_token, bold=item.enabled),
    ]
    if item.detail:
        spans.append(Segment(text=f"  {item.detail}", style_token="dim"))
    if item.read_only:
        spans.append(Segment(text="  (read-only)", style_token="dimmer"))
    spans.append(Segment(text="\n", style_token="dim"))
    return spans


def _category_block(view: ConfigSnapshotView, category: str) -> list[Segment]:
    items = view.items_in(category)
    if not items:
        return []
    enabled = sum(1 for item in items if item.enabled)
    spans = [
        Segment(text=f"  {category}", style_token="bright", bold=True),
        Segment(text=f"  {enabled}/{len(items)} on\n", style_token="dim"),
    ]
    for item in items:
        spans.extend(_item_line(item))
    return spans


def _overrides_block(view: ConfigSnapshotView) -> list[Segment]:
    if not view.overrides:
        return []
    spans = [Segment(text="  set values\n", style_token="bright", bold=True)]
    width = max(len(path) for path, _ in view.overrides)
    for path, value in view.overrides:
        spans.append(Segment(text=f"    {path.ljust(width)}  ", style_token="teal"))
        spans.append(Segment(text=f"{value}\n", style_token="dim"))
    return spans


def config_show_spans(
    view: ConfigSnapshotView, *, category: str | None = None
) -> tuple[Segment, ...]:
    """``/config show`` (all categories) or ``/config <category>`` (one)."""
    change_count = len(view.changes)
    changes = f"{change_count} change(s)" if change_count else "no changes"
    detail = f"{view.bundle or 'session'} \u00b7 {changes} \u00b7 /config set|diff|save"
    spans: list[Segment] = _header("Config", detail)
    categories = (category,) if category is not None else CONFIG_CATEGORIES
    rendered_any = False
    for cat in categories:
        block = _category_block(view, cat)
        if block:
            rendered_any = True
            spans.extend(block)
    if category is None:
        spans.extend(_overrides_block(view))
    if not rendered_any:
        spans.append(Segment(text=f"    no {category} configured\n", style_token="dimmer"))
    return tuple(spans)


def config_item_spans(item: ConfigItem | None, *, category: str, name: str) -> tuple[Segment, ...]:
    """``/config <category> <name>`` -- one item's detail (or a not-found line)."""
    if item is None:
        return (Segment(text=f"  no {category} item named '{name}'\n", style_token="dimmer"),)
    spans = _header("Config", f"{category} \u00b7 {name}")
    spans.extend(_item_line(item))
    if not item.read_only:
        verb = "disable" if item.enabled else "enable"
        spans.append(Segment(text=f"    /config {category} {verb} {name}\n", style_token="dimmer"))
    return tuple(spans)


def config_diff_spans(changes: tuple[ConfigChange, ...]) -> tuple[Segment, ...]:
    """``/config diff`` -- what changed since session start (donor parity)."""
    if not changes:
        return (
            Segment(
                text="  no changes from session start \u00b7 config matches the bundle\n",
                style_token="dim",
            ),
        )
    spans = _header("Config diff", f"{len(changes)} change(s) since start")
    width = max(len(f"{c.category} {c.name}") for c in changes)
    for change in changes:
        label = f"{change.category} {change.name}"
        spans.append(Segment(text=f"    {label.ljust(width)}  ", style_token="teal"))
        spans.append(Segment(text=f"{change.action}\n", style_token="dim"))
    return tuple(spans)


def config_help_spans() -> tuple[Segment, ...]:
    """``/config`` (no args) -- a concise subcommand listing (donor parity)."""
    rows: tuple[tuple[str, str], ...] = (
        ("show", "live config tree across all categories"),
        ("<category>", "list one category (context/tools/hooks/providers/agents)"),
        ("<category> <name>", "detail for one item"),
        ("<category> disable|enable <n>", "toggle an item (hooks are read-only)"),
        ("set <path> <value>", "set a config value (session scope)"),
        ("diff", "changes since session start"),
        ("save [--scope global|project|local]", "persist to settings.yaml"),
    )
    spans = _header("Config", "live session configuration")
    width = max(len(cmd) for cmd, _ in rows)
    for cmd, desc in rows:
        spans.append(Segment(text=f"  /config {cmd.ljust(width)}  ", style_token="teal"))
        spans.append(Segment(text=f"{desc}\n", style_token="dim"))
    return tuple(spans)


__all__ = [
    "config_diff_spans",
    "config_help_spans",
    "config_item_spans",
    "config_show_spans",
]
