"""In-session ``/config`` UI controller (show/toggle/set/diff/save).

The composer posts ``/config ...`` to :func:`manage`, which parses the
argument line with the pure model router
(:func:`amplifier_app_newtui.model.config.parse_config_command`) and drives
the runtime adapter's config surface, posting an
:class:`~amplifier_app_newtui.model.blocks.Answer` (or a transient notice)
per subcommand. It mirrors :mod:`amplifier_app_newtui.ui.directory_admin`:
a fake host + adapter unit-test it with no Textual and no live session.

Round-trip (acceptance): ``toggle`` and ``set`` re-post the refreshed view
so the change is visible on screen immediately; ``diff`` reports the delta
from session start; ``save`` persists to the chosen settings scope.
"""

from __future__ import annotations

from typing import Any, Protocol

from ..model.blocks import Answer
from ..model.config import parse_config_command
from .config_view import (
    config_diff_spans,
    config_help_spans,
    config_item_spans,
    config_show_spans,
)


class ConfigAdminHost(Protocol):
    adapter: Any
    allocator: Any

    def append_block(self, block: Any) -> None: ...

    def show_notice(self, text: str, duration: float | None = None) -> None: ...


async def manage(host: ConfigAdminHost, args: str) -> None:
    inv = parse_config_command(args)

    if inv.kind == "help":
        _post(host, config_help_spans())
        return

    if inv.kind == "show":
        view = await host.adapter.config_view()
        _post(host, config_show_spans(view))
        return

    if inv.kind == "category":
        view = await host.adapter.config_view()
        _post(host, config_show_spans(view, category=inv.category))
        return

    if inv.kind == "item":
        view = await host.adapter.config_view()
        item = next((i for i in view.items_in(inv.category) if i.name == inv.name), None)
        _post(host, config_item_spans(item, category=inv.category, name=inv.name))
        return

    if inv.kind == "toggle":
        ok, message = await host.adapter.config_toggle(inv.category, inv.name, inv.enable)
        host.show_notice(message)
        if ok:
            view = await host.adapter.config_view()
            _post(host, config_show_spans(view, category=inv.category))
        return

    if inv.kind == "set":
        ok, message = await host.adapter.config_set(inv.path, inv.value)
        host.show_notice(message)
        if ok:
            view = await host.adapter.config_view()
            _post(host, config_show_spans(view))
        return

    if inv.kind == "diff":
        changes = await host.adapter.config_diff()
        _post(host, config_diff_spans(changes))
        return

    if inv.kind == "save":
        _ok, message = await host.adapter.config_save(inv.scope)
        host.show_notice(message)
        return

    # error
    host.show_notice(inv.message)


def _post(host: ConfigAdminHost, spans: Any) -> None:
    host.append_block(Answer(id=host.allocator.next_id(), spans=spans))


__all__ = ["manage"]
