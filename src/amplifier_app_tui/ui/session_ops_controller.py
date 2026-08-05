"""SessionOpsController: the live in-session op surface (ADR-0007 seam).

The ``/status /model /effort /compact /clear /tools /agents /diff /skills
/skill /mcp /bundle /module`` handlers used to live directly on
:class:`~amplifier_app_tui.ui.app.TuiApp`; this controller owns them
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

import asyncio
import shlex
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol

from ..kernel.goal import goal_action, parse_goal_max_turns
from ..kernel.session_ops import EFFORT_LEVELS
from ..model.blocks import Answer, Segment, TranscriptBlock
from .session_ops_view import (
    diff_spans,
    mcp_spans,
    model_listing_spans,
    names_spans,
    skill_loaded_spans,
    skills_spans,
    status_spans,
)

CLEAR_INTERRUPT_TIMEOUT_S = 30.0
CONTEXT_MUTATION_TIMEOUT_S = 30.0

if TYPE_CHECKING:
    from ..model.blocks import BlockIdAllocator
    from .runtime_adapter import RuntimeAdapter


def _next_effort(current: str | None) -> str:
    """Next reasoning-effort tier in the canonical ring, wrapping ``xhigh`` -> ``none``.

    The client-side twin of ``kernel.serve._next_effort`` (the Python client cycles
    in-process, not over serve): an unset/unknown current enters the ring at the first
    tier; otherwise advance one and wrap. There is no Default(unset) slot because
    ``set_effort`` has no clear path (documented divergence from the donor).
    """
    if current is None or current not in EFFORT_LEVELS:
        return EFFORT_LEVELS[0]
    return EFFORT_LEVELS[(EFFORT_LEVELS.index(current) + 1) % len(EFFORT_LEVELS)]


class SessionOpsHost(Protocol):
    """The narrow app surface :class:`SessionOpsController` drives.

    Implemented by :class:`~amplifier_app_tui.ui.app.TuiApp` (the
    real host) and by plain fakes in tests -- no widget objects cross the
    boundary.
    """

    adapter: RuntimeAdapter
    allocator: BlockIdAllocator
    turn_active: bool

    @property
    def submit_pending(self) -> bool:
        """A prompt is awaiting its runtime PromptSubmit acknowledgement."""
        ...

    @property
    def context_restore_pending(self) -> bool:
        """A checkpoint fork/restore already owns the mutable context."""
        ...

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

    def clear_transcript_view(self) -> None:
        """Unmount every transcript row and fence stale pre-clear events.

        The view-only half of ``/clear`` (D3): pairs with the adapter's
        ``clear_context()`` model-level clear so the rendered transcript
        and the live conversation context empty together.
        """
        ...

    async def wait_for_turn_idle(self) -> None:
        """Wait until a requested active-turn interrupt has fully closed."""
        ...

    def refresh_status(self) -> None:
        """Repaint the title/footer after adapter-derived state changes."""
        ...

    def refresh_skill_commands(self, skills: tuple[Any, ...]) -> None:
        """Replace dynamic skill slash aliases with the live catalog."""
        ...

    def refresh_mcp_prompt_commands(self, prompts: tuple[Any, ...]) -> None:
        """Replace dynamic MCP prompt slash commands with the live catalog."""
        ...

    def submit_or_queue_generated_prompt(self, text: str) -> None:
        """Run an idle generated prompt or queue it behind an active turn."""
        ...

    def set_effort_indicator(self, level: str | None) -> None:
        """Cache the reasoning-effort tier and repaint the footer indicator."""
        ...


class SessionOpsController:
    """In-session ops over the live amplifier coordinator (ADR-0007 seam).

    Owns ``/status /model /effort /compact /clear /tools /agents /diff
    /skills /skill /mcp``. Behavior is identical to the app's prior inline
    handlers; only the host reference is indirected.
    """

    def __init__(self, host: SessionOpsHost) -> None:
        self._host = host
        self._clear_pending = False
        self._compact_pending = False
        self._snapshot_pending = False
        self._goal_pending = False

    @property
    def clear_pending(self) -> bool:
        """Whether an accepted backend context clear is still in flight."""
        return self._clear_pending

    @property
    def compact_pending(self) -> bool:
        """Whether a manual context compaction is still in flight."""
        return self._compact_pending

    @property
    def context_mutation_pending(self) -> bool:
        """Whether clear or manual compaction currently owns the context."""
        return self._clear_pending or self._compact_pending

    @property
    def context_snapshot_pending(self) -> bool:
        """Whether branch/fork is taking a consistent context snapshot."""
        return self._snapshot_pending

    @property
    def context_operation_pending(self) -> bool:
        """Whether any serialized context read or mutation is in flight."""
        return self.context_mutation_pending or self._snapshot_pending or self._goal_pending

    @property
    def context_operation_label(self) -> str:
        """User-facing name for the current serialized context operation."""
        if self._clear_pending:
            return "context clear"
        if self._compact_pending:
            return "context compaction"
        if self._snapshot_pending:
            return "session snapshot"
        if self._goal_pending:
            return "goal submission"
        return "context operation"

    def goal_admitted(self) -> None:
        """Release the pre-PromptSubmit fence for a native goal turn."""

        self._goal_pending = False

    def begin_context_snapshot(self) -> bool:
        """Claim the context long enough for /branch or /fork to copy it."""
        if self._ops_starting():
            return False
        if self.context_mutation_pending:
            self._host.show_notice(
                f"{self.context_operation_label} in progress · snapshot unavailable"
            )
            return False
        if self._snapshot_pending:
            self._host.show_notice("session snapshot already in progress")
            return False
        if (
            self._host.submit_pending
            or self._host.turn_active
            or self._host.context_restore_pending
        ):
            self._host.show_notice("session snapshot requires an idle session")
            return False
        self._snapshot_pending = True
        return True

    def finish_context_snapshot(self) -> None:
        """Release a branch/fork snapshot claim after its worker settles."""
        self._snapshot_pending = False

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
                    # The FULL resolved bundle URI/path, untruncated (D4 gap 1) --
                    # status_spans never truncates its bundle row.
                    bundle=self._host.adapter.bundle_uri,
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
            if ok:
                self._host.set_effort_indicator(detail)  # footer indicator stays honest
            self._host.show_notice(f"effort · {detail}" if ok else detail)
            return
        current = await self._host.adapter.get_effort()
        self._host.set_effort_indicator(current)  # sync the indicator on a bare /effort
        self._host.show_notice(f"effort · {current or '(default)'} · /effort <level> to set")

    def cycle_effort(self) -> None:
        """ctrl+b: advance the reasoning-effort tier one step (donor variant.cycle)."""
        if self._ops_starting():
            return
        self._host.run_worker(self._cycle_effort(), exclusive=False)

    async def _cycle_effort(self) -> None:
        nxt = _next_effort(await self._host.adapter.get_effort())
        ok, detail = await self._host.adapter.set_effort(nxt)
        if ok:
            self._host.set_effort_indicator(detail)
        self._host.show_notice(f"effort · {detail}" if ok else detail)

    def compact_context(self, focus: str) -> None:
        if self._ops_starting():
            return
        if self._clear_pending:
            self._host.show_notice("context clear in progress · compact unavailable")
            return
        if self._compact_pending:
            self._host.show_notice("context compaction already in progress")
            return
        if self._snapshot_pending:
            self._host.show_notice("session snapshot in progress · compact unavailable")
            return
        if (
            self._host.submit_pending
            or self._host.turn_active
            or self._host.context_restore_pending
        ):
            self._host.show_notice("compact requires an idle session")
            return
        self._compact_pending = True
        self._host.run_worker(self._compact_context(focus), exclusive=False)

    async def _compact_context(self, focus: str) -> None:
        try:
            try:
                async with asyncio.timeout(CONTEXT_MUTATION_TIMEOUT_S):
                    ok, detail = await self._host.adapter.compact(focus)
            except TimeoutError:
                self._host.show_notice(
                    "compact timed out · context state uncertain; retry or restart"
                )
                return
            self._host.show_notice(f"compacted · {detail}" if ok else detail)
        except Exception as error:  # noqa: BLE001 — an op failure must not tear down the TUI
            self._host.show_notice(f"compact failed · {error}")
        finally:
            self._compact_pending = False

    def manage_goal(self, args: str) -> None:
        """Inspect, clear, or start Amplifier's mounted native goal loop."""

        action = goal_action(args)
        if action == "set":
            if self._ops_starting():
                return
            try:
                cap, condition = parse_goal_max_turns(args)
            except ValueError as error:
                self._host.show_notice(f"goal not set · {error}")
                return
            if not condition:
                self._host.show_notice("usage: /goal [--max-turns N] <condition>")
                return
            if self.context_operation_pending:
                self._host.show_notice(
                    f"{self.context_operation_label} in progress · goal not started"
                )
                return
            if (
                self._host.submit_pending
                or self._host.turn_active
                or self._host.context_restore_pending
            ):
                self._host.show_notice("goal requires an idle session")
                return
            # Claim the same narrow pre-admission window as an ordinary prompt.
            # PromptSubmit releases it through TuiApp.turn_started().
            self._goal_pending = True
            turns = f"max {cap} turns" if cap else "unlimited turns"
            self._host.show_notice(f"goal starting · {turns} · /goal stop to clear")
        elif self._ops_starting():
            return
        self._host.run_worker(self._manage_goal(args, action), exclusive=False)

    async def _manage_goal(self, args: str, action: str) -> None:
        try:
            result = await self._host.adapter.manage_goal(args)
            if not result.ok:
                self._host.show_notice(result.detail)
                return
            if result.action == "status":
                lines = result.detail.splitlines() or [result.detail]
                spans: list[Segment] = [
                    Segment(text="· ", style_token="blue"),
                    Segment(text="Native goal", style_token="bright", bold=True),
                    Segment(text="  loop-streaming\n", style_token="dim"),
                ]
                for line in lines:
                    spans.append(Segment(text=f"  {line}\n", style_token="dim"))
                self._host.append_block(
                    Answer(id=self._host.allocator.next_id(), spans=tuple(spans))
                )
            elif result.action == "cleared":
                self._host.show_notice(result.detail)
            # ``set`` returns after the native loop ends. Progress/terminal
            # state is rendered from orchestrator:goal_progress, so adding a
            # second app-authored completion here would be duplicate truth.
        except Exception as error:  # noqa: BLE001 — keep the TUI alive on runtime failure
            self._host.show_notice(f"goal failed · {error}")
        finally:
            if action == "set":
                self._goal_pending = False

    def clear_context(self) -> None:
        if self._ops_starting():
            return
        if self._clear_pending:
            self._host.show_notice("context clear already in progress")
            return
        if self._host.submit_pending:
            self._host.show_notice("clear requires an idle session · esc to interrupt, then retry")
            return
        if self._host.context_restore_pending:
            self._host.show_notice("checkpoint restore in progress · clear unavailable")
            return
        if self._compact_pending:
            self._host.show_notice("context compaction in progress · clear unavailable")
            return
        if self._snapshot_pending:
            self._host.show_notice("session snapshot in progress · clear unavailable")
            return
        # Set synchronously before scheduling the worker. A prompt submitted
        # on the next UI tick can now be retained instead of racing the async
        # context mutation and disappearing when its late result clears view
        # and checkpoint state.
        self._clear_pending = True
        interrupt_active = self._host.turn_active
        if interrupt_active:
            self._host.show_notice("interrupting turn to clear context …")
        self._host.run_worker(self._clear_context(interrupt_active), exclusive=False)

    async def _clear_context(self, interrupt_active: bool = False) -> None:
        try:
            if interrupt_active and self._host.turn_active:
                try:
                    async with asyncio.timeout(CLEAR_INTERRUPT_TIMEOUT_S):
                        interrupted = await self._host.adapter.interrupt()
                        if not interrupted and self._host.turn_active:
                            self._host.show_notice(
                                "clear could not interrupt the active turn · retry"
                            )
                            return
                        if self._host.turn_active:
                            await self._host.wait_for_turn_idle()
                except TimeoutError:
                    self._host.show_notice(
                        "clear timed out waiting for the active turn · context unchanged"
                    )
                    return
            try:
                async with asyncio.timeout(CONTEXT_MUTATION_TIMEOUT_S):
                    ok, count = await self._host.adapter.clear_context()
            except TimeoutError:
                self._host.show_notice("clear timed out · view kept; context state uncertain")
                return
            if ok:
                # View-only reset happens ONLY on a confirmed context clear --
                # a failed/unavailable clear must leave the transcript exactly
                # as it was (D3: make the visible result primary, but never
                # wipe the view for a no-op).
                self._host.clear_transcript_view()
            self._host.show_notice(
                f"view cleared · {count} messages dropped"
                if ok
                else "clear unavailable in this session"
            )
        except Exception as error:  # noqa: BLE001 — an op failure must not tear down the TUI
            self._host.show_notice(f"clear failed · {error}")
        finally:
            self._clear_pending = False

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
            # Keep execution native: the TUI activates the mounted skill,
            # then schedules an ordinary full turn so user-invoked shortcuts
            # such as /goalify do not sit inert until a second manual prompt.
            # The real skill procedure remains owned by tool-skills.
            skill_name = name.strip().split(maxsplit=1)[0]
            self._host.submit_or_queue_generated_prompt(
                f"Apply the active /{skill_name} skill now and complete its requested output."
            )
        else:
            self._host.show_notice(payload or f"no such skill · {name}")

    def manage_mcp(self, args: str) -> None:
        self._host.run_worker(self._manage_mcp(args), exclusive=False)

    def run_mcp_prompt(self, server: str, prompt: str, args: str) -> None:
        """Execute a native MCP prompt wrapper and schedule its returned body."""
        if self._ops_starting():
            return
        self._host.run_worker(
            self._run_mcp_prompt(server, prompt, args),
            exclusive=False,
        )

    async def _run_mcp_prompt(self, server: str, prompt: str, args: str) -> None:
        ok, payload = await self._host.adapter.execute_mcp_prompt(server, prompt, args)
        if not ok:
            self._host.show_notice(payload)
            return
        self._host.submit_or_queue_generated_prompt(payload)

    async def _refresh_mcp_prompt_commands(self) -> None:
        prompts = await self._host.adapter.mcp_prompts()
        self._host.refresh_mcp_prompt_commands(prompts)

    async def _manage_mcp(self, args: str) -> None:
        try:
            parts = shlex.split(args)
        except ValueError as error:
            self._host.show_notice(f"invalid /mcp arguments · {error}")
            return
        sub = parts[0].lower() if parts else "list"
        if sub in ("", "list"):
            servers = await self._host.adapter.mcp_servers()
            live = await self._host.adapter.mcp_tools()
            await self._refresh_mcp_prompt_commands()
            self._host.append_block(
                Answer(id=self._host.allocator.next_id(), spans=mcp_spans(servers, live))
            )
        elif sub == "add":
            if len(parts) < 3:
                self._host.show_notice("usage: /mcp add <name> <command> [args…]")
                return
            _ok, detail = await self._host.adapter.add_mcp_server(
                parts[1], parts[2], tuple(parts[3:])
            )
            await self._refresh_mcp_prompt_commands()
            self._host.show_notice(detail)
        elif sub == "reload":
            if len(parts) != 2:
                self._host.show_notice("usage: /mcp reload <name>")
                return
            _ok, detail = await self._host.adapter.reload_mcp_server(parts[1])
            await self._refresh_mcp_prompt_commands()
            self._host.show_notice(detail)
        elif sub == "remove":
            if len(parts) != 2:
                self._host.show_notice("usage: /mcp remove <name>")
                return
            _ok, detail = await self._host.adapter.remove_mcp_server(parts[1])
            await self._refresh_mcp_prompt_commands()
            self._host.show_notice(detail)
        else:
            self._host.show_notice(
                f"unknown /mcp subcommand · {sub} (list | add | reload | remove)"
            )

    def load_bundle(self, args: str) -> None:
        """``/bundle`` — list/load same-session additive bundle modules.

        Deferred overlays, registered bundles, and local/direct bundle URIs
        all resolve here. Preparing a bundle can install modules, so compose
        runs on a worker (never blocks the UI); singleton sections are reported
        as next-session-only by the kernel."""
        try:
            parts = shlex.split(args)
        except ValueError as error:
            self._host.show_notice(f"invalid /bundle arguments · {error}")
            return
        sub = parts[0].lower() if parts else "list"
        if sub in ("", "list"):
            self._host.run_worker(self._list_deferred_bundles(), exclusive=False)
            return
        if sub == "load":
            name = parts[1] if len(parts) > 1 else ""
            if not name or len(parts) > 2:
                self._host.show_notice(
                    "usage: /bundle load <name-or-uri> · /bundle lists available targets"
                )
                return
            if self._ops_starting():
                return
            self._host.run_worker(self._load_bundle(name), exclusive=False)
            return
        # A bare `/bundle <name>` is the natural shorthand for `load <name>`.
        if len(parts) > 1:
            self._host.show_notice(
                "usage: /bundle load <name-or-uri> · quote local paths containing spaces"
            )
            return
        if self._ops_starting():
            return
        self._host.run_worker(self._load_bundle(parts[0]), exclusive=False)

    async def _list_deferred_bundles(self) -> None:
        names = await self._host.adapter.deferred_bundles()
        self._host.append_block(
            Answer(
                id=self._host.allocator.next_id(),
                spans=names_spans(
                    "Live-loadable bundles",
                    names,
                    "none discovered · use amplifier-tui bundle add or pass a local URI",
                ),
            )
        )

    async def _load_bundle(self, name: str) -> None:
        ok, detail = await self._host.adapter.load_deferred_bundle(name)
        if ok:
            self._host.refresh_status()  # mounted tools/agents change the roster
            self._host.refresh_skill_commands(await self._host.adapter.list_skills())
            await self._refresh_mcp_prompt_commands()
        self._host.show_notice(f"bundle · {detail}" if ok else detail)

    def load_module(self, args: str) -> None:
        """``/module load ID [SOURCE]`` — mount an additive provider/tool/hook now."""
        try:
            parts = shlex.split(args)
        except ValueError as error:
            self._host.show_notice(f"invalid /module arguments · {error}")
            return
        if parts and parts[0].lower() == "load":
            parts = parts[1:]
        if not parts or len(parts) > 2:
            self._host.show_notice(
                "usage: /module load <provider-, tool-, or hook-module> [source-uri]"
            )
            return
        if self._ops_starting():
            return
        module_id = parts[0]
        source_hint = parts[1] if len(parts) == 2 else ""
        self._host.run_worker(
            self._load_module(module_id, source_hint),
            exclusive=False,
        )

    async def _load_module(self, module_id: str, source_hint: str) -> None:
        ok, detail = await self._host.adapter.load_module(module_id, source_hint)
        if ok:
            self._host.refresh_status()
            self._host.refresh_skill_commands(await self._host.adapter.list_skills())
            await self._refresh_mcp_prompt_commands()
        self._host.show_notice(f"module · {detail}" if ok else detail)


__all__ = ["SessionOpsController", "SessionOpsHost"]
