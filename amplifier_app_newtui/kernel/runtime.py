"""RealRuntime: the foundation 7-step lifecycle behind the UI event queue.

ADR-0007 §Runtimes: ``load_bundle`` → compose overlays → ``prepare()``
once → ``create_session`` → register spawn/resume capabilities (after
create, before execute) → ephemeral hooks → ``execute`` per prompt. All
amplifier-core/foundation touchpoints stay in kernel/ (no Textual); the
UI sees only the normalized ``asyncio.Queue[UIEvent]`` — exactly the
contract :class:`~amplifier_app_newtui.kernel.demo.DemoRuntime` speaks.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..model.queues import NeedsYouQueue, QueuedMessage, SteeringQueue
from ..model.trust import DenialLog
from .approval import ApprovalBroker
from .config import ResolvedConfig, resolve_config
from .cost import CostTracker, restore_session_cost
from .display import DisplaySystem
from .events import ContentBlockEnd, ContextInjected, PromptComplete, UIEvent
from .evidence import EvidenceCollector
from .git_yield import GitDiffSnapshot, capture_git_diff
from .governance_hook import GovernanceHook
from .persistence import IncrementalSaver, SessionStore
from .queue_bridge import CONSUMED_EVENTS, QueueBridge
from .turn_yield import TurnYieldTracker
from .session_factory import InitializedSession, SessionRequest, create_initialized_session
from .spawner import SessionSpawner
from .steering import StepBoundaryBridge

logger = logging.getLogger(__name__)


def _core_version() -> str:
    try:
        import amplifier_core

        return str(getattr(amplifier_core, "__version__", "unknown"))
    except Exception:  # noqa: BLE001 — banner detail only
        return "unknown"


def _provider_and_model(mount_plan: dict[str, Any]) -> tuple[str, str]:
    providers = mount_plan.get("providers") or []
    if not providers:
        return ("", "")
    entry = providers[0] if isinstance(providers[0], dict) else {}
    module_id = str(entry.get("module", entry.get("id", "")))
    provider = module_id.replace("provider-", "").replace("amplifier-module-", "")
    config = entry.get("config") if isinstance(entry.get("config"), dict) else {}
    model = str((config or {}).get("default_model", ""))
    return (provider, model)


_PRINTING_HOOKS = frozenset(
    {
        "hooks-streaming-ui",  # green "Amplifier:" line-mode streaming printer
        "hooks-todo-display",  # todo-table stdout printer
        "hooks-insight-blocks",  # insight-panel stdout printer
        "hooks-inline-blocks",  # inline-panel stdout printer
    }
)
"""Line-mode stdout printers (composed in by app-level bundle overlays).

This app owns its rendering: the packaged bundle mounts no printing
hooks (NOTES-kernel-runtime), but user ``bundle.app`` overlays can drag
them in transitively, and a hook writing raw ANSI (cursor moves, line
erases) under the full-screen TUI corrupts the Textual screen — found
live: the whole turn rendered blank in real mode. Stripped for the
headless ``run`` subcommand too, where the same printers double-echo.
"""


def restored_history(transcript: list[dict[str, Any]]) -> tuple[tuple[str, str], ...]:
    """Simplified (role, text) pairs from a stored transcript for replay.

    A resumed TUI session replays the restored conversation into the
    transcript (an empty screen over a full context reads as a fresh
    session). Tool traffic and ``<system-reminder>`` injections are
    skipped — only real user prompts and assistant prose replay.
    """
    pairs: list[tuple[str, str]] = []
    for message in transcript:
        role = message.get("role")
        if role not in ("user", "assistant"):
            continue
        if message.get("tool_call_id") or message.get("tool_calls"):
            continue
        content = message.get("content")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "\n".join(
                str(block.get("text", ""))
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
        else:
            continue
        text = text.strip()
        if not text or text.startswith("<system-reminder>"):
            continue
        pairs.append((str(role), text))
    return tuple(pairs)


def _strip_printing_hooks(mount_plan: dict[str, Any]) -> None:
    hooks = mount_plan.get("hooks")
    if isinstance(hooks, list):
        mount_plan["hooks"] = [
            h
            for h in hooks
            if not (isinstance(h, dict) and h.get("module") in _PRINTING_HOOKS)
        ]


class RealRuntime:
    """One real amplifier session driving the UI event queue."""

    def __init__(
        self,
        *,
        bundle: str | None = None,
        resume_id: str | None = None,
        queue: asyncio.Queue[UIEvent] | None = None,
        steering: SteeringQueue | None = None,
        needs_you: NeedsYouQueue | None = None,
        denial_log: DenialLog | None = None,
        mode: Callable[[], str] = lambda: "chat",
        project_dir: Path | None = None,
    ) -> None:
        self.queue: asyncio.Queue[UIEvent] = queue if queue is not None else asyncio.Queue()
        self.evidence = EvidenceCollector()
        """Derives §10 evidence links from the turn's tool calls — taps the
        bridge so it sees every normalized event before the UI consumes it."""
        self.bridge = QueueBridge(
            self.queue,
            tap=self._tap,
            # ``prompt:complete`` is NOT hook-driven here: submit()
            # synthesizes the close-out event itself AFTER the end-of-turn
            # git snapshot, so it carries the turn's yield (files/diffstat/
            # tests ✔ — DESIGN-SPEC §3) and always lands last in the queue.
            events=tuple(e for e in CONSUMED_EVENTS if e != "prompt:complete"),
        )
        self.turn_yield = TurnYieldTracker()
        """Per-turn ``tests ✔`` evidence from tool results (bridge tap)."""
        self.steering = steering or SteeringQueue()
        self.needs_you = needs_you or NeedsYouQueue()
        self.denial_log = denial_log or DenialLog()
        self.broker = ApprovalBroker(needs_you=self.needs_you, denial_log=self.denial_log)
        self.cost = CostTracker()
        self._bundle = bundle
        self._resume_id = resume_id
        self._mode = mode
        self._project_dir = project_dir
        self._initialized: InitializedSession | None = None
        self._executing = False  # a submit() turn is live (fork must refuse)
        self._resolved: ResolvedConfig | None = None
        self._store: SessionStore | None = None
        self._saver: IncrementalSaver | None = None
        self.bundle_name = ""
        self.session_short = ""
        self.banner: tuple[str, str] = ("", "")
        self.session_cost_start = Decimal("0")
        self.turn_base = 0
        """User messages restored into the live context on resume.

        Foundation's fork ``turn`` is 1-indexed over ALL user messages in
        the context (``session.messages.get_turn_boundaries``), so
        checkpoints recorded after a resume must offset past the restored
        history (DESIGN-SPEC §9)."""
        self.restored_history: tuple[tuple[str, str], ...] = ()
        """(role, text) pairs replayed into the transcript on resume."""
        self.degraded_notice: str | None = None

    async def start(self) -> None:
        """Resolve config, create the session, register every hook."""
        resolved = await resolve_config(self._bundle, project_dir=self._project_dir)
        _strip_printing_hooks(resolved.mount_plan)
        self._resolved = resolved
        store = SessionStore(project_dir=resolved.project_dir)
        self._store = store

        session_id: str | None = None
        transcript: list[dict[str, Any]] | None = None
        if self._resume_id:
            session_id = store.find_session(self._resume_id)
            transcript, _metadata = store.load(session_id)
            # Same turn semantics as foundation's fork slicing: every
            # user-role message in the restored history is one turn.
            self.turn_base = sum(1 for m in transcript if m.get("role") == "user")
            self.restored_history = restored_history(transcript)

        display = DisplaySystem(self.bridge.emit)
        spawner = SessionSpawner(
            trackers=[self.bridge],
            approval_system=self.broker,
            display_system=display,
        )
        initialized = await create_initialized_session(
            SessionRequest(
                resolved=resolved,
                session_id=session_id,
                approval_system=self.broker,
                display_system=display,
                initial_transcript=transcript,
                spawn_capability=spawner.spawn,
            )
        )
        self._initialized = initialized
        hooks = initialized.coordinator.hooks
        initialized.unregister_handles.append(self.bridge.register_hooks(hooks))
        governance = GovernanceHook(
            initialized.session_id,
            mode=self._mode,
            denial_log=self.denial_log,
            broker=self.broker,
            needs_you=self.needs_you,
        )
        initialized.unregister_handles.append(governance.register_hooks(hooks))
        boundary = StepBoundaryBridge(
            initialized.session_id,
            self.steering,
            needs_you=self.needs_you,
            on_applied=self._steer_applied,
            # Each applied injection is one more persistent user-role
            # message in the live context; the reducer shifts checkpoint
            # turn ids past it so rewind forks at the true turn boundary
            # (DESIGN-SPEC §9).
            on_inject=lambda: self.bridge.emit(
                ContextInjected(session_id=initialized.session_id)
            ),
        )
        initialized.unregister_handles.append(boundary.register_hooks(hooks))
        saver = IncrementalSaver(
            store,
            initialized.session_id,
            session=initialized.session,
            base_metadata={"bundle": resolved.bundle_name},
        )
        initialized.unregister_handles.append(saver.register(hooks))
        self._saver = saver

        if self._resume_id:
            restore_session_cost(self.cost, store.events_path(initialized.session_id))
            self.session_cost_start = self.cost.session_cost

        self.bundle_name = resolved.bundle_name
        self.session_short = initialized.session_id[:6]
        self.degraded_notice = initialized.degraded_notice
        provider, model = _provider_and_model(resolved.mount_plan)
        from .. import __version__

        identity = " | ".join(
            part
            for part in (
                f"Bundle: {resolved.bundle_name}",
                f"Provider: {provider}" if provider else "",
                f"{model} · session {self.session_short}" if model else f"session {self.session_short}",
            )
            if part
        )
        self.banner = (f"Amplifier {__version__} · core {_core_version()}", identity)

    def _tap(self, event: UIEvent) -> None:
        """Bridge tap: evidence derivation + append-only events.jsonl.

        events.jsonl is the append-only normalized UIEvent log
        (persistence module contract / ADR-0007 resolution 9); it powers
        the resume cost re-seed (``restore_session_cost``), so every
        emitted event is appended once the session identity exists.
        Both halves are best-effort and never block the queue.
        """
        self.evidence.observe(event)
        self.turn_yield.observe(event)
        if self._store is not None and self._initialized is not None:
            self._store.append_event(self._initialized.session_id, event)

    def _steer_applied(self, steer: QueuedMessage) -> None:
        """Narrate a steer consumed at a step boundary (DESIGN-SPEC §5).

        Mockup ``runTurn`` logs ``● Applying steer: <text>`` when the
        queued steer is applied (design-v3-cohesive.html L327); emitted
        as the same durable narration text block the demo runtime uses
        (``ContentBlockEnd`` with a ``narration`` role marker).
        """
        session_id = self._initialized.session_id if self._initialized else ""
        self.bridge.emit(
            ContentBlockEnd(
                session_id=session_id,
                block_type="text",
                block={
                    "type": "text",
                    "text": f"Applying steer: {steer.text}",
                    "demo_role": "narration",
                },
            )
        )

    async def submit(self, text: str) -> str:
        """Execute one user turn; returns the final response text.

        Git-yield capture (reference: amplifier-app-cli
        ``runtime/interactive_turn.py``): a diff snapshot is taken before
        and after ``execute``; the delta rides on the synthesized
        ``PromptComplete`` close-out so the reducer can label the rule
        ``N files · +A/−D · tests ✔`` and mark the turn shipped.
        """
        if self._initialized is None:
            raise RuntimeError("RealRuntime.start() has not completed")
        self.turn_yield.start_turn()
        starting_diff = await self._capture_diff()
        self._executing = True
        response: Any = ""
        try:
            response = await self._initialized.session.execute(text)
        finally:
            self._executing = False
            # End-of-turn save (reference: amplifier-app-cli persists after
            # every turn) — the incremental tool:post save misses the final
            # assistant message, which lands in the context only after the
            # last tool call.
            if self._saver is not None:
                try:
                    await self._saver.maybe_save()
                except Exception:  # noqa: BLE001 — persistence is best-effort
                    logger.warning("end-of-turn save failed", exc_info=True)
            # The close-out event is emitted here — never from the raw
            # ``prompt:complete`` hook — so it is guaranteed to (a) follow
            # every turn event and (b) carry the end-of-turn yield.
            await self._emit_close_out(str(response or ""), starting_diff)
        return str(response or "")

    def _turn_cwd(self) -> Path:
        resolved = self._resolved
        if resolved is not None and resolved.project_dir is not None:
            return Path(resolved.project_dir)
        return self._project_dir or Path.cwd()

    async def _capture_diff(self) -> GitDiffSnapshot:
        try:
            return await capture_git_diff(self._turn_cwd())
        except Exception:  # noqa: BLE001 — yield capture must never kill a turn
            logger.debug("git diff snapshot failed", exc_info=True)
            return GitDiffSnapshot(False)

    async def _emit_close_out(self, response: str, starting_diff: GitDiffSnapshot) -> None:
        """Synthesize the enriched ``PromptComplete`` (files/diffstat/tests)."""
        ending_diff = await self._capture_diff()
        delta = ending_diff.delta_from(starting_diff)
        self.bridge.emit(
            PromptComplete(
                session_id=self._initialized.session_id if self._initialized else "",
                response=response,
                files_changed=delta.files if delta else 0,
                diffstat=delta.diff_label if delta and delta.files else "",
                tests_ok=self.turn_yield.tests_ok,
            )
        )

    async def interrupt(self) -> bool:
        """Best-effort graceful cancellation at the next step boundary.

        Real API surface (amplifier-core ``CancellationToken``):
        ``coordinator.cancellation.request_graceful()`` — the same call
        amplifier-app-cli's esc-interrupt path makes. Falls back to
        ``coordinator.request_cancel(immediate=False)`` (the coordinator
        convenience wrapper) for duck-typed test doubles.
        """
        initialized = self._initialized
        if initialized is None:
            return False
        coordinator = initialized.coordinator
        cancellation = getattr(coordinator, "cancellation", None)
        candidates: tuple[tuple[Any, str], ...] = (
            (cancellation, "request_graceful"),
            (coordinator, "request_cancel"),
        )
        for owner, method in candidates:
            if owner is None:
                continue
            request = getattr(owner, method, None)
            if not callable(request):
                continue
            try:
                result = request()
                if asyncio.iscoroutine(result):
                    await result
                return True
            except Exception:  # noqa: BLE001 — cancellation is best-effort
                logger.debug("cancellation request failed", exc_info=True)
        return False

    async def fork(self, checkpoint_id: str, ledger: Any) -> Any:
        """Rewind the live session to *checkpoint_id* (ADR-0007 §Rewind).

        In-memory fork via :class:`~amplifier_app_newtui.kernel.rewind.
        RewindController`: foundation's ``fork_session_in_memory`` slices
        the live context's messages at the checkpoint's turn,
        ``context.set_messages()`` commits them, and *ledger* trims only
        after the context confirms (confirm-then-trim). Raises
        :class:`~amplifier_app_newtui.kernel.rewind.RewindError` on any
        failure, leaving context and ledger untouched.
        """
        from .rewind import RewindController, RewindError

        initialized = self._initialized
        if initialized is None:
            raise RewindError("RealRuntime.start() has not completed")
        if self._executing:
            # ``context.set_messages()`` under a live provider loop corrupts
            # turn numbering — the UI interrupts and awaits close-out first
            # (interrupt-then-fork); refuse if a caller ever bypasses that.
            raise RewindError("turn still running — interrupt it first")
        context = initialized.coordinator.get("context")
        if context is None or not hasattr(context, "set_messages"):
            raise RewindError("context module lacks set_messages — cannot fork")
        messages: list[dict[str, Any]] = []
        if hasattr(context, "get_messages"):
            messages = list(await context.get_messages())
        controller = RewindController(ledger)
        return await controller.fork_in_memory(
            checkpoint_id,
            messages=messages,
            set_messages=context.set_messages,
            parent_id=initialized.session_id,
        )

    async def cleanup(self) -> None:
        if self._initialized is not None:
            await self._initialized.cleanup()
            self._initialized = None


def list_sessions(project_dir: Path | None = None) -> list[str]:
    """Session ids stored for this project (newest last)."""
    return SessionStore(project_dir=project_dir).list_sessions()


__all__ = ["RealRuntime", "list_sessions"]
