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

from ..model.queues import NeedsYouQueue, SteeringQueue
from ..model.trust import DenialLog
from .approval import ApprovalBroker
from .config import ResolvedConfig, resolve_config
from .cost import CostTracker, restore_session_cost
from .display import DisplaySystem
from .events import UIEvent
from .governance_hook import GovernanceHook
from .persistence import IncrementalSaver, SessionStore
from .queue_bridge import QueueBridge
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
        self.bridge = QueueBridge(self.queue)
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
        self._resolved: ResolvedConfig | None = None
        self._store: SessionStore | None = None
        self.bundle_name = ""
        self.session_short = ""
        self.banner: tuple[str, str] = ("", "")
        self.session_cost_start = Decimal("0")
        self.degraded_notice: str | None = None

    async def start(self) -> None:
        """Resolve config, create the session, register every hook."""
        resolved = await resolve_config(self._bundle, project_dir=self._project_dir)
        self._resolved = resolved
        store = SessionStore(project_dir=resolved.project_dir)
        self._store = store

        session_id: str | None = None
        transcript: list[dict[str, Any]] | None = None
        if self._resume_id:
            session_id = store.find_session(self._resume_id)
            transcript, _metadata = store.load(session_id)

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
            initialized.session_id, self.steering, needs_you=self.needs_you
        )
        initialized.unregister_handles.append(boundary.register_hooks(hooks))
        saver = IncrementalSaver(
            store,
            initialized.session_id,
            session=initialized.session,
            base_metadata={"bundle": resolved.bundle_name},
        )
        initialized.unregister_handles.append(saver.register(hooks))

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

    async def submit(self, text: str) -> str:
        """Execute one user turn; returns the final response text."""
        if self._initialized is None:
            raise RuntimeError("RealRuntime.start() has not completed")
        response = await self._initialized.session.execute(text)
        return str(response or "")

    async def interrupt(self) -> bool:
        """Best-effort cancellation at the next step boundary."""
        initialized = self._initialized
        if initialized is None:
            return False
        for owner in (initialized.session, initialized.coordinator):
            cancellation = getattr(owner, "cancellation", None)
            if cancellation is None:
                continue
            for method in ("cancel", "request_cancel", "request"):
                request = getattr(cancellation, method, None)
                if callable(request):
                    try:
                        result = request()
                        if asyncio.iscoroutine(result):
                            await result
                        return True
                    except Exception:  # noqa: BLE001 — cancellation is best-effort
                        logger.debug("cancellation request failed", exc_info=True)
        return False

    async def cleanup(self) -> None:
        if self._initialized is not None:
            await self._initialized.cleanup()
            self._initialized = None


def list_sessions(project_dir: Path | None = None) -> list[str]:
    """Session ids stored for this project (newest last)."""
    return SessionStore(project_dir=project_dir).list_sessions()


__all__ = ["RealRuntime", "list_sessions"]
