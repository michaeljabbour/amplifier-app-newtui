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
from collections.abc import Callable, Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

from ..model.queues import NeedsYouQueue, QueuedMessage, SteeringQueue
from ..model.trust import CapabilityClass, DenialLog, TrustDecision
from .approval import ApprovalBroker
from .bundle_admin import read_scope, settings_paths
from .config import ResolvedConfig, resolve_config
from .clipboard import ClipboardImageInjector, ImageAttachment
from .compaction import CompactionConfig, CompactionRuntimeBinding, compaction_config
from .cost import CostTracker, restore_session_cost, start_live_pricing
from .display import DisplaySystem
from .events import (
    ApprovalDenied,
    ContentBlockEnd,
    ContextInjected,
    Notification,
    PromptComplete,
    PromptSubmit,
    ProviderResponseUsage,
    UIEvent,
)
from .evidence import EvidenceCollector
from .directory_permissions import (
    DirectoryEntry,
    DirectoryKind,
    DirectoryPolicy,
    apply_policy_to_mount_plan,
    configured_entries,
    policy_from_mount_plan,
    settings_path_values,
    update_settings_path,
    write_boundary_setting,
)
from .governance_hook import GovernanceHook
from . import session_ops
from .git_yield import GitDiffSnapshot, capture_git_diff, capture_git_patch
from .persistence import IncrementalSaver, SessionStore
from .queue_bridge import CONSUMED_EVENTS, QueueBridge
from .turn_yield import TurnYieldTracker
from .session_factory import InitializedSession, SessionRequest, create_initialized_session
from .spawner import SessionSpawner
from .steering import StepBoundaryBridge

logger = logging.getLogger(__name__)

TURN_ABORTED_MARKER = """<turn_aborted>
The user intentionally interrupted the previous turn. Any in-flight tools may
have partially completed; verify current state before retrying unfinished work.
</turn_aborted>"""
"""Model-visible, persisted boundary after an accepted Esc interrupt."""


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
    }
)
"""Line-mode stdout printers (composed in by app-level bundle overlays).

This app owns its rendering: the packaged bundle mounts no printing
hooks (NOTES-kernel-runtime), but user ``bundle.app`` overlays can drag
them in transitively, and a hook writing raw ANSI (cursor moves, line
erases) under the full-screen TUI corrupts the Textual screen — found
live: the whole turn rendered blank in real mode. Stripped for the
headless ``run`` subcommand too, where the same printers double-echo.

``hooks-insight-blocks`` / ``hooks-inline-blocks`` used to be listed
here as "panel stdout printers" — that was wrong. Reading the cached
modules: both are pure ``inject_context`` instruction hooks
(``session:start`` / ``prompt:submit``) that teach the model to emit
★ insight / ✂ MJ callouts as Markdown blockquotes in its OWN prose;
they write nothing to stdout. Suppressing them only severed the callout
channel. They now mount normally, and the transcript renders their
blockquote callouts behind a ``▌`` gutter (``ui/live_tail.answer_spans``).
"""

_SUPPRESSED_HOOKS_DEFAULT = _PRINTING_HOOKS | frozenset({"hooks-logging", "hooks-notify"})
"""Built-in default set of hook module ids suppressed at mount time.

The line-mode printers write raw ANSI (cursor moves, line erases)
that corrupts the full-screen TUI; ``hooks-logging`` (composed in
transitively via an anchors ``include``) double-writes the app-owned
``events.jsonl``; ``hooks-notify`` writes raw OSC-777/BEL escape
sequences straight to stdout (or the TTY device), which corrupts the
full-screen Textual TUI the same way the printers do — the app rings
Textual's own driver-safe bell instead (``ui/app_support``
attention-bell policy). Settings-extensible via
``suppressed_hooks_setting`` below — user ``hooks.suppress`` entries
are unioned in, never replace this baseline.
"""


def suppressed_hooks_setting(settings: dict[str, Any]) -> frozenset[str]:
    """Resolve the suppressed-hooks set from merged settings.

    Copies the ``write_boundary_setting`` resolver pattern
    (``kernel/directory_permissions.py``): the built-in default is always
    present, and a well-shaped ``hooks.suppress`` list is unioned in.
    Junk shapes (missing/non-dict ``hooks``, non-list ``suppress``) fall
    back to the default set alone; blank entries are stripped.
    """
    hooks = settings.get("hooks")
    raw = hooks.get("suppress") if isinstance(hooks, dict) else None
    if not isinstance(raw, list):
        return _SUPPRESSED_HOOKS_DEFAULT
    return _SUPPRESSED_HOOKS_DEFAULT | {str(item).strip() for item in raw if str(item).strip()}


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
        if not text or text.startswith("<system-reminder>") or text.startswith("<turn_aborted>"):
            continue
        pairs.append((str(role), text))
    return tuple(pairs)


def _apply_hook_suppression(
    mount_plan: dict[str, Any],
    notify: Callable[[Any], None],
    suppressed: frozenset[str] | None = None,
) -> list[str]:
    """Strip suppressed hooks from the mount plan; notify what was removed.

    Replaces the old silent ``_strip_printing_hooks``: stripping hooks
    behind the user's back (even for good reasons \u2014 corrupted-screen
    printers, double-logging) is a surprise waiting to happen. One
    ``Notification`` names every removed module id so it never is.
    """
    suppress_set = _SUPPRESSED_HOOKS_DEFAULT if suppressed is None else suppressed
    hooks = mount_plan.get("hooks", [])
    kept: list[Any] = []
    removed: list[str] = []
    if isinstance(hooks, list):
        for entry in hooks:
            if isinstance(entry, dict) and entry.get("module") in suppress_set:
                removed.append(str(entry.get("module")))
            else:
                kept.append(entry)
    mount_plan["hooks"] = kept
    removed_sorted = sorted(removed)
    if removed_sorted:
        notify(Notification(message=f"suppressed hooks: {', '.join(removed_sorted)}"))
    return removed_sorted


def _resume_bundle_notice(
    metadata: dict[str, Any],
    current_bundle: str,
    notify: Callable[[Any], None],
) -> None:
    """Notice when a resumed session's stored bundle differs from the one
    currently resolved.

    Resuming under a different bundle than the session was created with can
    silently change which modules/tools/hooks govern the turn - one
    ``Notification`` surfaces the mismatch instead of reattaching quietly.
    """
    stored_bundle = metadata.get("bundle")
    if stored_bundle and stored_bundle != current_bundle:
        notify(
            Notification(
                message=(
                    f"resuming session from '{stored_bundle}' bundle "
                    f"under '{current_bundle}' bundle"
                )
            )
        )


class _BrokerApprovalProvider:
    """Kernel ``ApprovalProvider`` protocol over the app's ApprovalBroker.

    Registered through hooks-approval's ``approval.register_provider``
    capability — the native module decides WHEN to ask (mode confirm
    lists, its policy rules) and owns allow-always persistence via
    ``ApprovalResponse.remember``; this adapter only presents the ask.
    """

    def __init__(self, broker: ApprovalBroker) -> None:
        self._broker = broker

    async def request_approval(self, request: Any) -> Any:
        from amplifier_core import ApprovalResponse

        from .approval import ALLOW_ALWAYS, STANDARD_OPTIONS, ApprovalDetail, is_allow

        action = str(getattr(request, "action", "") or getattr(request, "tool_name", ""))
        prompt = f"Allow {action}?"
        details = getattr(request, "details", None)
        self._broker.stage_detail(
            prompt,
            ApprovalDetail(
                command=action,
                rule=str(getattr(request, "risk_level", "") or ""),
                tool_name=str(getattr(request, "tool_name", "") or ""),
                tool_input=dict(details) if isinstance(details, Mapping) else {},
            ),
        )
        choice = await self._broker.request_approval(
            prompt,
            list(STANDARD_OPTIONS),
            timeout=float(getattr(request, "timeout", None) or 3600.0),
            default="deny",
        )
        return ApprovalResponse(
            approved=is_allow(choice),
            reason=f"user chose {choice}",
            remember=choice == ALLOW_ALWAYS,
        )


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
        mode: Callable[[], str] = lambda: "auto",
        permission_resolver: Callable[[str, Mapping[str, object] | None], TrustDecision]
        | None = None,
        capability_resolver: Callable[[CapabilityClass], TrustDecision] | None = None,
        project_dir: Path | None = None,
        on_progress: Callable[[str, str], None] | None = None,
    ) -> None:
        self._on_progress = on_progress
        """Boot-phase feedback ``(action, detail)`` — module prepare can
        run for minutes; the TUI shows each phase instead of a blank
        screen. Defensive arity: foundation's ``progress_callback``
        consumers vary, so :meth:`_progress` tolerates 1–2 args."""
        self.queue: asyncio.Queue[UIEvent] = queue if queue is not None else asyncio.Queue()
        self.evidence = EvidenceCollector()
        """Derives §10 evidence links from the turn's tool calls — taps the
        bridge so it sees every normalized event before the UI consumes it."""
        self.bridge = QueueBridge(
            self.queue,
            tap=self._tap,
            # Neither ``prompt:submit`` nor ``prompt:complete`` is
            # hook-driven here: submit() emits the open itself BEFORE
            # ``session.execute`` (overlay hooks can grind for seconds
            # before the raw hook fires — the user's echo and the working
            # line must not wait for them), and synthesizes the close-out
            # AFTER the end-of-turn git snapshot so it carries the turn's
            # yield (files/diffstat/tests ✔ — DESIGN-SPEC §3) and always
            # lands last in the queue.
            events=tuple(
                e for e in CONSUMED_EVENTS if e not in ("prompt:submit", "prompt:complete")
            ),
        )
        self.turn_yield = TurnYieldTracker()
        """Per-turn ``tests ✔`` evidence from tool results (bridge tap)."""
        self.steering = steering or SteeringQueue()
        self.needs_you = needs_you or NeedsYouQueue()
        self.denial_log = denial_log or DenialLog()
        self.broker = ApprovalBroker(
            needs_you=self.needs_you,
            denial_log=self.denial_log,
            # The supervisor is present at the bar — approvals must wait
            # for them, not time out to deny mid-plan-reading (1 hour;
            # esc denies deliberately, ctrl-y defers to needs-you).
            min_timeout=3600.0,
        )
        self.cost = CostTracker()
        self._bundle = bundle
        self._resume_id = resume_id
        self._mode = mode
        self._permission_resolver = permission_resolver
        self._capability_resolver = capability_resolver
        self._project_dir = project_dir
        self._initialized: InitializedSession | None = None
        self._executing = False  # a submit() turn is live (fork must refuse)
        self._interrupt_requested = False
        self._resolved: ResolvedConfig | None = None
        self._store: SessionStore | None = None
        self._saver: IncrementalSaver | None = None
        self._image_injector: ClipboardImageInjector | None = None
        self.directory_policy: DirectoryPolicy | None = None
        self._session_settings_path: Path | None = None
        self.bundle_name = ""
        self.model_name = ""
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
        self.compaction = CompactionConfig()
        self._compaction_binding: CompactionRuntimeBinding | None = None

    def _progress(self, action: str = "", detail: str = "", *rest: object) -> None:
        del rest
        self._report_progress(str(action), str(detail))

    def _report_progress(self, action: str, detail: str) -> None:
        if self._on_progress is None:
            return
        try:
            self._on_progress(action, detail)
        except Exception:  # noqa: BLE001 — progress display is best-effort
            logger.debug("boot progress callback failed", exc_info=True)

    async def start(self) -> None:
        """Resolve config, create the session, register every hook."""
        resolved = await resolve_config(
            self._bundle, project_dir=self._project_dir, progress=self._progress
        )
        _apply_hook_suppression(
            resolved.mount_plan, self.bridge.emit, suppressed_hooks_setting(resolved.settings)
        )
        if resolved.fallback_notice:
            # A settings-configured bundle failed discovery — the boot
            # continued on the app default; tell the user loudly.
            self.bridge.emit(Notification(message=resolved.fallback_notice))
        self._resolved = resolved
        self.compaction = compaction_config(resolved.mount_plan)
        # Live pricing (BACKLOG item 1, behind settings ``pricing.live``,
        # default on): fresh disk cache applies immediately; otherwise a
        # daemon background fetch swaps the table for NEW turns only.
        # Never raises — failure keeps the offline fallback silently.
        start_live_pricing(resolved.settings)
        self._report_progress("creating", "session")
        store = SessionStore(project_dir=resolved.project_dir)
        self._store = store

        session_id: str | None = None
        transcript: list[dict[str, Any]] | None = None
        if self._resume_id:
            session_id = store.find_session(self._resume_id)
            transcript, metadata = store.load(session_id)
            _resume_bundle_notice(metadata, resolved.bundle_name, self.bridge.emit)
            # Same turn semantics as foundation's fork slicing: every
            # user-role message in the restored history is one turn.
            self.turn_base = sum(1 for m in transcript if m.get("role") == "user")
            self.restored_history = restored_history(transcript)

        # Directory policy is derived from the prepared mount plan so the
        # filesystem tool, child sessions, CLI administration and shell
        # governance all consult one effective source. Session-scoped paths
        # are folded in before a resumed session mounts its tools.
        directory_policy = policy_from_mount_plan(
            resolved.mount_plan,
            resolved.project_dir,
            write_boundary=write_boundary_setting(resolved.settings),
        )
        if session_id is not None:
            self._session_settings_path = store.session_dir(session_id) / "settings.yaml"
            session_settings = read_scope(self._session_settings_path)
            for kind in ("allowed", "denied"):
                directory_policy.set_session(kind, settings_path_values(session_settings, kind))
        apply_policy_to_mount_plan(resolved.mount_plan, directory_policy)
        self.directory_policy = directory_policy

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
        if self._session_settings_path is None:
            self._session_settings_path = (
                store.session_dir(initialized.session_id) / "settings.yaml"
            )
        self._sync_directory_tools()
        hooks = initialized.coordinator.hooks
        initialized.unregister_handles.append(self.bridge.register_hooks(hooks))
        # App posture and outside-project gating is an ephemeral Amplifier
        # hook over the same tool:pre contract as native hooks-mode. Mounted
        # hooks still own bundle-defined modes; this hook owns only the TUI's
        # five trust postures and directory boundary.
        governance = GovernanceHook(
            initialized.session_id,
            mode=self._mode,
            denial_log=self.denial_log,
            broker=self.broker,
            needs_you=self.needs_you,
            directory_policy=directory_policy,
            permission_resolver=self._permission_resolver,
            capability_resolver=self._capability_resolver,
            on_blocked=self._governance_blocked,
        )
        initialized.unregister_handles.append(governance.register_hooks(hooks))
        # hooks-approval owns bundle-mode ask/allow-always policy. The app's
        # broker is its presentation provider as well as governance's asker.
        self._register_approval_provider(initialized)
        boundary = StepBoundaryBridge(
            initialized.session_id,
            self.steering,
            needs_you=self.needs_you,
            on_applied=self._steer_applied,
            # Each applied injection is one more persistent user-role
            # message in the live context; the reducer shifts checkpoint
            # turn ids past it so rewind forks at the true turn boundary
            # (DESIGN-SPEC §9).
            on_inject=lambda: self.bridge.emit(ContextInjected(session_id=initialized.session_id)),
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

        # Clipboard images: execute() stays text-only; a provider:request
        # hook rewrites the just-submitted user message to multimodal
        # content right before the provider call (amplifier-app-cli parity).
        context = initialized.coordinator.get("context")
        if context is not None:
            binding = CompactionRuntimeBinding(context, self.compaction)
            self.compaction = binding.apply()
            self._compaction_binding = binding
            injector = ClipboardImageInjector(context)
            unregister = hooks.register(
                "provider:request",
                injector.handle_provider_request,
                priority=900,
                name="newtui-clipboard-images",
            )
            if callable(unregister):

                def _drop_injector() -> None:
                    unregister()

                initialized.unregister_handles.append(_drop_injector)
            self._image_injector = injector

        if self._resume_id:
            restore_session_cost(self.cost, store.events_path(initialized.session_id))
            self.session_cost_start = self.cost.session_cost

        self.bundle_name = resolved.bundle_name
        self.session_short = initialized.session_id[:6]
        self.degraded_notice = initialized.degraded_notice
        provider, model = _provider_and_model(resolved.mount_plan)
        self.model_name = "/".join(part for part in (provider, model) if part)
        from .. import __version__

        identity = " | ".join(
            part
            for part in (
                f"Bundle: {resolved.bundle_name}",
                f"Provider: {provider}" if provider else "",
                f"{model} · session {self.session_short}"
                if model
                else f"session {self.session_short}",
            )
            if part
        )
        self.banner = (f"Amplifier {__version__} · core {_core_version()}", identity)

    @property
    def session_id(self) -> str:
        return self._initialized.session_id if self._initialized is not None else ""

    def _governance_blocked(self, action: str, reason: str) -> None:
        session_id = self._initialized.session_id if self._initialized else ""
        self.bridge.emit(
            ApprovalDenied(
                session_id=session_id,
                prompt=f"Allow {action}?",
                command=action,
                reason=reason,
                continuation=f"continuing without {action}",
            )
        )

    def _sync_directory_tools(self) -> None:
        """Apply the current path lists to mounted filesystem tool objects."""
        if self._initialized is None or self.directory_policy is None:
            return
        tools = self._initialized.coordinator.get("tools") or {}
        values = tools.values() if isinstance(tools, Mapping) else ()
        for tool in values:
            if hasattr(tool, "allowed_write_paths"):
                tool.allowed_write_paths = list(self.directory_policy.allowed)
            if hasattr(tool, "denied_write_paths"):
                tool.denied_write_paths = list(self.directory_policy.denied)

    def directory_entries(self, kind: DirectoryKind) -> tuple[DirectoryEntry, ...]:
        """Effective paths with scope provenance for TUI display."""
        if self._resolved is None or self.directory_policy is None:
            return ()
        result = list(configured_entries(settings_paths(self._resolved.project_dir, None), kind))
        session_values = (
            self.directory_policy.session_allowed
            if kind == "allowed"
            else self.directory_policy.session_denied
        )
        result = [DirectoryEntry(path, "session") for path in session_values] + result
        if kind == "allowed":
            project = str(self._resolved.project_dir)
            if not any(entry.path == project for entry in result):
                result.append(DirectoryEntry(project, "project-default"))
        elif self.directory_policy is not None:
            configured_paths = {entry.path for entry in result}
            result.extend(
                DirectoryEntry(path, "protected-default")
                for path in self.directory_policy.protected
                if path not in configured_paths
            )
        seen: set[str] = set()
        unique: list[DirectoryEntry] = []
        for entry in result:
            if entry.path in seen:
                continue
            seen.add(entry.path)
            unique.append(entry)
        return tuple(unique)

    async def update_session_directory(
        self,
        kind: DirectoryKind,
        operation: str,
        path: str,
    ) -> tuple[bool, str]:
        """Persist and activate a session-scoped directory capability."""
        if operation not in ("add", "remove"):
            return (False, "operation must be add or remove")
        if self.directory_policy is None or self._session_settings_path is None:
            return (False, "session still starting")
        if operation == "add":
            changed, resolved = update_settings_path(self._session_settings_path, kind, "add", path)
        else:
            changed, resolved = update_settings_path(
                self._session_settings_path, kind, "remove", path
            )
        if not changed:
            return (False, f"path not found in session scope · {resolved}")
        if operation == "add":
            self.directory_policy.add_session(kind, resolved)
        else:
            self.directory_policy.remove_session(kind, resolved)
        if self._resolved is not None:
            apply_policy_to_mount_plan(self._resolved.mount_plan, self.directory_policy)
        self._sync_directory_tools()
        verb = "allowed" if kind == "allowed" else "denied"
        return (True, f"{verb} · {resolved} · session scope")

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
        if (
            isinstance(event, ProviderResponseUsage)
            and self._compaction_binding is not None
            and event.input_tokens > 0
        ):
            asyncio.create_task(self._compaction_binding.observe_input_tokens(event.input_tokens))
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

    async def submit(self, text: str, attachments: tuple[ImageAttachment, ...] = ()) -> str:
        """Execute one user turn; returns the final response text.

        Git-yield capture (reference: amplifier-app-cli
        ``runtime/interactive_turn.py``): a diff snapshot is taken before
        and after ``execute``; the delta rides on the synthesized
        ``PromptComplete`` close-out so the reducer can label the rule
        ``N files · +A/−D · tests ✔`` and mark the turn shipped.

        Clipboard images ride ``attachments``: ``execute`` stays text-only
        and the injector's ``provider:request`` hook upgrades the pending
        user message to multimodal content just before the provider call.
        """
        if self._initialized is None:
            raise RuntimeError("RealRuntime.start() has not completed")
        if attachments:
            if self._image_injector is None:
                raise RuntimeError("session context cannot accept image attachments")
            self._image_injector.prepare(text, attachments)
        self._interrupt_requested = False
        self._executing = True
        response: Any = ""
        starting_diff = GitDiffSnapshot(False)
        try:
            # Turn-open first: the user's echo + working line paint NOW, not
            # after the pre-prompt hook work inside ``session.execute``.
            self.bridge.emit(PromptSubmit(session_id=self._initialized.session_id, prompt=text))
            self.turn_yield.start_turn()
            starting_diff = await self._capture_diff()
            response = await self._initialized.session.execute(text)
        finally:
            self._executing = False
            if self._image_injector is not None:
                self._image_injector.clear()
            if self._interrupt_requested:
                await self._append_turn_aborted_marker()
                self._interrupt_requested = False
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

    async def _append_turn_aborted_marker(self) -> bool:
        """Append the durable model-only boundary for an interrupted turn."""
        initialized = self._initialized
        if initialized is None:
            return False
        context = initialized.coordinator.get("context")
        add_message = getattr(context, "add_message", None)
        if not callable(add_message):
            logger.warning("context cannot persist the turn-aborted marker")
            return False
        try:
            result = add_message({"role": "assistant", "content": TURN_ABORTED_MARKER})
            if asyncio.iscoroutine(result):
                await result
            return True
        except Exception:  # noqa: BLE001 — interruption must still close cleanly
            logger.warning("turn-aborted marker persistence failed", exc_info=True)
            return False

    def _turn_cwd(self) -> Path:
        resolved = self._resolved
        if resolved is not None and resolved.project_dir is not None:
            return Path(resolved.project_dir)
        return self._project_dir or Path.cwd()

    async def workspace_files(self) -> tuple[str, ...]:
        """Discover files for composer autocomplete without blocking a loop."""
        from .file_mentions import discover_workspace_files

        return await asyncio.to_thread(discover_workspace_files, self._turn_cwd())

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

    def _register_approval_provider(self, initialized: InitializedSession) -> None:
        """Hand the broker to hooks-approval via its registration capability.

        The native module asks its registered ApprovalProvider and owns
        allow-always persistence itself (ApprovalResponse.remember) — the
        app supplies presentation only. Best-effort: sessions without
        hooks-approval simply have no native asker.
        """
        try:
            register = initialized.coordinator.get_capability("approval.register_provider")
        except Exception:  # noqa: BLE001 — capability registry variance
            register = None
        if callable(register):
            register(_BrokerApprovalProvider(self.broker))

    def _mode_tool(self) -> Any | None:
        """The bundle-mounted ``mode`` tool (tool-mode), when composed in."""
        if self._initialized is None:
            return None
        tools = self._initialized.coordinator.get("tools") or {}
        return tools.get("mode")

    async def list_native_modes(self) -> Any:
        """Native mode catalog via the mounted mode tool (``operation=list``).

        Modes are dynamically composed through the bundle system
        (superpowers, modes, occams-machete, …) — the app never hardcodes
        them. Returns the tool's raw output (typically a mapping with a
        ``modes`` list of ``{name, description, source}``); "" when no
        mode system is mounted.
        """
        tool = self._mode_tool()
        if tool is None:
            return ""
        try:
            result = await tool.execute({"operation": "list"})
        except Exception:  # noqa: BLE001 — a broken mode tool must not kill the UI
            logger.warning("mode list failed", exc_info=True)
            return ""
        output = getattr(result, "output", None)
        return output if getattr(result, "success", False) and output else ""

    async def set_native_mode(self, name: str | None) -> tuple[bool, str]:
        """Activate (or clear, ``name=None``) a bundle-provided mode.

        Transitions can be gate-confirmed (hooks-mode ``warn`` policy
        denies the first ``set`` so agents confirm intent) — one retry
        covers the confirm handshake.
        """
        tool = self._mode_tool()
        if tool is None:
            return (False, "no native mode system mounted")
        payload: dict[str, Any] = (
            {"operation": "clear"} if name is None else {"operation": "set", "name": name}
        )
        try:
            result = await tool.execute(payload)
            if not getattr(result, "success", False):
                result = await tool.execute(payload)  # gate confirm
        except Exception as error:  # noqa: BLE001
            return (False, str(error))
        ok = bool(getattr(result, "success", False))
        output: Any = getattr(result, "output", None) or getattr(result, "error", None)
        if isinstance(output, Mapping):
            output = output.get("message") or output.get("error") or str(dict(output))
        return (ok, str(output) if output else "")

    def _coordinator(self) -> Any | None:
        """The live amplifier coordinator, or ``None`` before ``start()``."""
        return self._initialized.coordinator if self._initialized else None

    # -- in-session ops (/model /effort /compact /clear /status /tools) ------
    # All run on the runtime loop (the coordinator is thread-owned here); the
    # adapter marshals each call in via ``run_coroutine_threadsafe``.

    async def list_models(self) -> session_ops.ModelListing:
        coord = self._coordinator()
        if coord is None:
            return session_ops.ModelListing(provider="", current="")
        return await session_ops.list_models(coord)

    async def set_model(self, model: str) -> tuple[bool, str]:
        coord = self._coordinator()
        if coord is None:
            return (False, "session still starting")
        return await session_ops.set_model(coord, model)

    async def get_effort(self) -> str | None:
        coord = self._coordinator()
        return session_ops.get_effort(coord) if coord is not None else None

    async def set_effort(self, level: str) -> tuple[bool, str]:
        coord = self._coordinator()
        if coord is None:
            return (False, "session still starting")
        return session_ops.set_effort(coord, level)

    async def compact(self, focus: str = "") -> tuple[bool, str]:
        coord = self._coordinator()
        if coord is None:
            return (False, "session still starting")
        return await session_ops.compact_context(coord, focus)

    async def clear_context(self) -> tuple[bool, int]:
        coord = self._coordinator()
        if coord is None:
            return (False, 0)
        return await session_ops.clear_context(coord)

    async def status(self) -> session_ops.StatusInfo:
        coord = self._coordinator()
        if coord is None:
            return session_ops.StatusInfo()
        return await session_ops.status_snapshot(coord)

    async def list_tools(self) -> tuple[str, ...]:
        coord = self._coordinator()
        return await session_ops.list_tools(coord) if coord is not None else ()

    async def list_agents(self) -> tuple[str, ...]:
        coord = self._coordinator()
        return await session_ops.list_agents(coord) if coord is not None else ()

    async def diff(self, staged: bool = False) -> str | None:
        return await capture_git_patch(self._turn_cwd(), staged=staged)

    async def list_skills(self) -> tuple[session_ops.SkillInfo, ...]:
        coord = self._coordinator()
        return await session_ops.list_skills(coord) if coord is not None else ()

    async def load_skill(self, name: str) -> tuple[bool, str]:
        coord = self._coordinator()
        if coord is None:
            return (False, "session still starting")
        return await session_ops.load_skill(coord, name)

    async def mcp_tools(self) -> tuple[str, ...]:
        coord = self._coordinator()
        return await session_ops.list_mcp_tools(coord) if coord is not None else ()

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
                if self._executing:
                    self._interrupt_requested = True
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
