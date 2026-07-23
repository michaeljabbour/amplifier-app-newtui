"""SessionOpsController: the live in-session op surface (ADR-0007 seam).

The ``/status /model /effort /compact /clear /tools /agents /diff /skills
/skill /mcp`` handlers used to live directly on
:class:`~amplifier_app_newtui.ui.app.NewTuiApp`; this controller owns them
as a single-purpose unit so the composition root stays a thin shell
(ADR-0007's <500-line budget). Each public method is the sync trigger the
command handler calls; the async body runs on a worker so the coordinator
call marshals through the adapter to the runtime loop without blocking the
UI (mirrors the app's ``_show_native_modes`` pattern).

The controller touches the app only through the narrow
:class:`SessionOpsHost` protocol, so it is unit-testable without a full
Textual App -- a plain fake host satisfies it (mirrors how command tests
drive ``FakeCommandContext``).
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol

from ..model.blocks import Answer, TranscriptBlock
from .session_ops_view import (
    diff_spans,
    mcp_spans,
    model_listing_spans,
    names_spans,
    skill_loaded_spans,
    skills_spans,
    status_spans,
)

if TYPE_CHECKING:
    from ..model.blocks import BlockIdAllocator
    from .runtime_adapter import RuntimeAdapter


class SessionOpsHost(Protocol):
    """The narrow app surface :class:`SessionOpsController` drives.

    Implemented by :class:`~amplifier_app_newtui.ui.app.NewTuiApp` (the
    real host) and by plain fakes in tests -- no widget objects cross the
    boundary.
    """

    adapter: RuntimeAdapter
    allocator: BlockIdAllocator

    @property
    def mode_id(self) -> str:
        """Current interaction-mode id (status/footer field)."""
        ...

    @property
    def session_cost(self) -> Decimal:
        """Cumulative session cost shown in ``/status``."""
        ...

    @property
    def splash_active(self) -> bool:
        """True while the boot splash is up (session not ready yet)."""
        ...

    def run_worker(self, work: Any, *, exclusive: bool = ...) -> Any:
        """Schedule an async body on the app's event loop."""
        ...

    def append_block(self, block: TranscriptBlock) -> None:
        """Append a transcript block."""
        ...

    def show_notice(self, text: str, duration: float | None = ...) -> None:
        """Show a transient right-aligned dim notice."""
        ...

    def refresh_status(self) -> None:
        """Repaint the title/footer after adapter-derived state changes."""
        ...


class SessionOpsController:
    """In-session ops over the live amplifier coordinator (ADR-0007 seam).

    Owns ``/status /model /effort /compact /clear /tools /agents /diff
    /skills /skill /mcp``. Behavior is identical to the app's prior inline
    handlers; only the host reference is indirected.
    """

    def __init__(self, host: SessionOpsHost) -> None:
        self._host = host

    def _ops_starting(self) -> bool:
        """True (and notices) when the session banner has not landed yet."""
        if self._host.splash_active:
            self._host.show_notice("session still starting · try again once the banner lands")
            return True
        return False

    def show_status(self) -> None:
        self._host.run_worker(self._show_status(), exclusive=False)

    async def _show_status(self) -> None:
        info = await self._host.adapter.status()
        self._host.append_block(
            Answer(
                id=self._host.allocator.next_id(),
                spans=status_spans(
                    info,
                    mode=self._host.mode_id,
                    bundle=self._host.adapter.bundle_name,
                    session_short=self._host.adapter.session_short,
                    cost=self._host.session_cost,
                    compaction=self._host.adapter.compaction,
                ),
            )
        )

    def show_model(self, arg: str) -> None:
        if arg and self._ops_starting():
            return
        self._host.run_worker(self._show_model(arg), exclusive=False)

    async def _show_model(self, arg: str) -> None:
        if arg:
            ok, detail = await self._host.adapter.set_model(arg)
            if ok:
                self._host.refresh_status()  # footer model field is adapter-derived
            self._host.show_notice(f"model · {detail}" if ok else detail)
            return
        listing = await self._host.adapter.list_models()
        self._host.append_block(
            Answer(id=self._host.allocator.next_id(), spans=model_listing_spans(listing))
        )

    def apply_effort(self, arg: str) -> None:
        if arg and self._ops_starting():
            return
        self._host.run_worker(self._apply_effort(arg), exclusive=False)

    async def _apply_effort(self, arg: str) -> None:
        if arg:
            ok, detail = await self._host.adapter.set_effort(arg)
            self._host.show_notice(f"effort · {detail}" if ok else detail)
            return
        current = await self._host.adapter.get_effort()
        self._host.show_notice(f"effort · {current or '(default)'} · /effort <level> to set")

    def compact_context(self, focus: str) -> None:
        if self._ops_starting():
            return
        self._host.run_worker(self._compact_context(focus), exclusive=False)

    async def _compact_context(self, focus: str) -> None:
        ok, detail = await self._host.adapter.compact(focus)
        self._host.show_notice(f"compacted · {detail}" if ok else detail)

    def clear_context(self) -> None:
        if self._ops_starting():
            return
        self._host.run_worker(self._clear_context(), exclusive=False)

    async def _clear_context(self) -> None:
        ok, count = await self._host.adapter.clear_context()
        self._host.show_notice(
            f"context cleared · {count} messages dropped"
            if ok
            else "clear unavailable in this session"
        )

    def show_tools(self) -> None:
        self._host.run_worker(self._show_tools(), exclusive=False)

    async def _show_tools(self) -> None:
        names = await self._host.adapter.list_tools()
        self._host.append_block(
            Answer(
                id=self._host.allocator.next_id(),
                spans=names_spans("Tools", names, "no tools mounted"),
            )
        )

    def show_agents(self) -> None:
        self._host.run_worker(self._show_agents(), exclusive=False)

    async def _show_agents(self) -> None:
        names = await self._host.adapter.list_agents()
        self._host.append_block(
            Answer(
                id=self._host.allocator.next_id(),
                spans=names_spans(
                    "Agents", names, "no agents · bundle has no agents: include: block"
                ),
            )
        )

    _DIFF_STAGED_ARGS = frozenset({"staged", "cached", "--staged", "--cached"})

    def show_diff(self, arg: str) -> None:
        self._host.run_worker(self._show_diff(arg), exclusive=False)

    async def _show_diff(self, arg: str) -> None:
        staged = arg.strip().lower() in self._DIFF_STAGED_ARGS
        patch = await self._host.adapter.diff(staged)
        self._host.append_block(
            Answer(id=self._host.allocator.next_id(), spans=diff_spans(patch, staged=staged))
        )

    def show_skills(self) -> None:
        self._host.run_worker(self._show_skills(), exclusive=False)

    async def _show_skills(self) -> None:
        skills = await self._host.adapter.list_skills()
        self._host.append_block(
            Answer(id=self._host.allocator.next_id(), spans=skills_spans(skills))
        )

    def load_skill(self, name: str) -> None:
        if not name:
            self._host.show_notice("usage: /skill <name> · /skills lists them")
            return
        if self._ops_starting():
            return
        self._host.run_worker(self._load_skill(name), exclusive=False)

    async def _load_skill(self, name: str) -> None:
        ok, payload = await self._host.adapter.load_skill(name)
        if ok:
            self._host.append_block(
                Answer(id=self._host.allocator.next_id(), spans=skill_loaded_spans(name, payload))
            )
            self._host.show_notice(f"skill loaded · {name}")
        else:
            self._host.show_notice(payload or f"no such skill · {name}")

    def manage_mcp(self, args: str) -> None:
        self._host.run_worker(self._manage_mcp(args), exclusive=False)

    async def _manage_mcp(self, args: str) -> None:
        from ..kernel import mcp_config

        parts = args.split()
        sub = parts[0].lower() if parts else "list"
        path = mcp_config.mcp_config_path()
        if sub in ("", "list"):
            servers = {
                name: mcp_config.describe_server(spec)
                for name, spec in mcp_config.read_servers(path).items()
            }
            live = await self._host.adapter.mcp_tools()
            self._host.append_block(
                Answer(id=self._host.allocator.next_id(), spans=mcp_spans(servers, live))
            )
        elif sub == "add":
            if len(parts) < 3:
                self._host.show_notice("usage: /mcp add <name> <command> [args…]")
                return
            mcp_config.add_stdio_server(path, parts[1], parts[2], tuple(parts[3:]))
            self._host.show_notice(
                f"mcp server added · {parts[1]} · restart the session to connect"
            )
        elif sub == "remove":
            if len(parts) < 2:
                self._host.show_notice("usage: /mcp remove <name>")
                return
            removed = mcp_config.remove_server(path, parts[1])
            self._host.show_notice(
                f"mcp server removed · {parts[1]} · restart to apply"
                if removed
                else f"no such server · {parts[1]}"
            )
        else:
            self._host.show_notice(f"unknown /mcp subcommand · {sub} (list | add | remove)")


__all__ = ["SessionOpsController", "SessionOpsHost"]
