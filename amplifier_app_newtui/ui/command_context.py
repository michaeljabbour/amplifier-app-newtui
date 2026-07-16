"""The app's CommandContext implementation (commands ↔ app boundary).

Command handlers act on the app exclusively through
:class:`~amplifier_app_newtui.commands.registry.CommandContext`; this
adapter satisfies that protocol by delegating to the composition root's
public surface — no widget objects cross the boundary.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from ..commands.context import ContextUsage
from ..model.queues import NeedsYouQueue, SteeringQueue
from ..model.trust import DenialLog
from ..model.turn import OutcomeLedger

if TYPE_CHECKING:
    from .app import NewTuiApp


class AppCommandContext:
    """CommandContext over the running :class:`NewTuiApp`."""

    def __init__(self, app: NewTuiApp) -> None:
        self._app = app

    # -- data surfaces --------------------------------------------------------

    @property
    def ledger(self) -> OutcomeLedger:
        return self._app.ledger

    @property
    def denial_log(self) -> DenialLog:
        return self._app.adapter.denial_log

    @property
    def steering(self) -> SteeringQueue:
        return self._app.adapter.steering

    @property
    def needs_you(self) -> NeedsYouQueue:
        return self._app.adapter.needs_you

    @property
    def session_cost(self) -> Decimal:
        return self._app.reducer.session_cost

    @property
    def session_short(self) -> str:
        return self._app.adapter.session_short

    @property
    def bundle_name(self) -> str:
        return self._app.adapter.bundle_name

    def next_block_id(self) -> str:
        return self._app.allocator.next_id()

    def context_usage(self) -> ContextUsage:
        return self._app.context_usage()

    def approval_tallies(self) -> tuple[object, ...]:
        return tuple(self._app.journal.tallies())

    def overridden_denials(self) -> tuple[object, ...]:
        return tuple(self._app.journal.overrides(self._app.adapter.denial_log))

    def mcp_server_stats(self) -> tuple[object, ...]:
        return ()

    # -- actions ------------------------------------------------------------------

    def echo_user_line(self, text: str) -> None:
        self._app.echo_user_line(text)

    def post_block(self, block: object) -> None:
        self._app.append_block(block)  # type: ignore[arg-type]

    def show_notice(self, text: str) -> None:
        self._app.show_notice(text)

    def cycle_mode(self) -> None:
        self._app.action_cycle_mode()

    def set_mode(self, mode_id: str) -> None:
        self._app.set_mode_by_id(mode_id)

    def set_theme(self, name: str) -> None:
        self._app.set_theme_by_name(name)

    def toggle_lanes(self) -> None:
        self._app.action_toggle_lanes()

    def open_rewind(self) -> None:
        self._app.action_open_rewind()

    def open_permissions(self) -> None:
        self._app.open_permissions()

    def quit_app(self) -> None:
        self._app.exit()


__all__ = ["AppCommandContext"]
