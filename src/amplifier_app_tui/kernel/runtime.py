"""RealRuntime: the foundation 7-step lifecycle behind the UI event queue.

ADR-0007 §Runtimes: ``load_bundle`` → compose overlays → ``prepare()``
once → ``create_session`` → register spawn/resume capabilities (after
create, before execute) → ephemeral hooks → ``execute`` per prompt. All
amplifier-core/foundation touchpoints stay in kernel/ (no Textual); the
UI sees only the normalized ``asyncio.Queue[UIEvent]`` — exactly the
contract :class:`~amplifier_app_tui.kernel.demo.DemoRuntime` speaks.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable, Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, cast

from ..model.blocks import UnsupportedBlock
from ..model.config import SessionConfigState
from ..model.queues import (
    LaneSteeringQueue,
    NeedsYouItem,
    NeedsYouQueue,
    QueuedMessage,
    SteeringQueue,
)
from ..model.terminal import TerminalSurface
from ..model.trust import CapabilityClass, DenialLog, TrustDecision
from .approval import ApprovalBroker
from .attention_push import NtfyAttentionDestination, resolve_ntfy_attention_config
from .bundle_admin import list_bundles as list_known_bundles
from .bundle_admin import read_scope, settings_paths
from .bundle_summon import (
    LOAD_BUNDLE_TOOL_NAME,
    DeferredCatalogInjector,
    LoadBundleTool,
    build_deferred_catalog,
    catalog_instruction_text,
)
from .config import (
    DEFAULT_BUNDLE,
    NOTIFY_PUSH_HOOK,
    BundleNotFoundError,
    ResolvedConfig,
    SettingsPaths,
    active_bundle_name,
    added_bundle_uris,
    bundle_search_paths,
    deferred_overlay_uris,
    inject_mode_search_paths,
    inject_notifications_config,
    inject_routing_config,
    inject_telemetry_config,
    load_merged_settings,
    packaged_modes_dir,
    prepare_live_overlay_bundle,
    provider_priority,
    resolve_config,
    resolve_deferred_bundle,
    resolve_bundle_name,
)
from .clipboard import (
    ClipboardImageInjector,
    ImageAttachment,
    image_attachments_from_message,
)
from .compaction import CompactionConfig, CompactionRuntimeBinding, compaction_config
from .cost import CostTracker, restore_session_cost, start_live_pricing
from .display import DisplaySystem
from .events import (
    ApprovalDenied,
    ContentBlockEnd,
    ContextInjected,
    Notification,
    ParsedEvent,
    PromptComplete,
    PromptSubmit,
    ProviderResponseUsage,
    RewindMarker,
    UIEvent,
    drop_rewound_events,
    parse_event,
)
from .evidence import EvidenceCollector
from .directory_permissions import (
    DirectoryEntry,
    DirectoryKind,
    DirectoryPolicy,
    apply_policy_to_mount_plan,
    configured_entries,
    governance_setting,
    policy_from_mount_plan,
    resolve_write_boundary,
    settings_path_values,
    update_settings_path,
)
from .governance_hook import GovernanceHook
from .mention_expansion import MentionBudget, expand_mentions
from . import goal as goal_bridge
from . import session_manager, session_ops, tool_cli
from .git_yield import GitDiffSnapshot, capture_git_diff, capture_git_patch
from .persistence import IncrementalSaver, SessionStore
from .queue_bridge import CONSUMED_EVENTS, QueueBridge
from .recipes import RecipeApprovalBridge
from .reminder_trust import (
    has_concealment_directive,
    is_injected_reminder,
    reminder_source,
)
from .turn_yield import TurnYieldTracker
from .session_factory import InitializedSession, SessionRequest, create_initialized_session
from .session_integrity import complete_orphaned_tool_results
from .spawner import SessionSpawner
from .steering import StepBoundaryBridge
from .surface_hint import SurfaceHintInjector

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
    """The provider (and its model) that will actually serve the turn.

    Selected by LOWEST ``config.priority`` — the same rule the orchestrator
    applies at call time (``loop-streaming::_select_provider`` sorts mounted
    providers by priority, lower wins, defaulting to 100). List position is
    NOT the rule: ``_merge_module_entries`` merges a settings entry onto the
    bundle-declared provider *in place* and appends new ones, so index 0 is
    pinned to whatever the bundle declared. Reading index 0 made the banner,
    the footer and the cost estimator name ``anthropic`` while every request
    went to a higher-priority vLLM instance — and priced the tokens wrong.
    """
    entries = [entry for entry in (mount_plan.get("providers") or []) if isinstance(entry, dict)]
    if not entries:
        return ("", "")
    entry = min(entries, key=provider_priority)
    module_id = str(entry.get("id") or entry.get("module") or "")
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

_SUPPRESSED_HOOKS_DEFAULT = _PRINTING_HOOKS | frozenset({"hooks-notify", NOTIFY_PUSH_HOOK})
"""Built-in default set of hook module ids suppressed at mount time.

The line-mode printers write raw ANSI (cursor moves, line erases)
that corrupts the full-screen TUI; ``hooks-notify`` writes raw
OSC-777/BEL escape sequences straight to stdout (or the TTY device),
which corrupts the full-screen Textual TUI the same way the printers
do — the app rings Textual's own driver-safe bell instead
(``ui/app_support`` attention-bell policy).

``hooks-notify-push`` is also always suppressed: the app-owned ntfy sink
consumes normalized attention records and acknowledgements. Allowing a user
or deferred overlay to re-mount the legacy raw-completion producer would
duplicate notifications and discard the record correlation boundary.

``hooks-logging`` used to be listed here as a double-writer of the
app-owned ``events.jsonl`` — that conflict is gone: the app's UIEvent
log moved to ``ui-events.jsonl`` (kernel/persistence.py), so
hooks-logging mounts natively and owns the canonical ``events.jsonl``
(file-only writer, no stdout). Settings-extensible via
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
    baseline = _SUPPRESSED_HOOKS_DEFAULT
    hooks = settings.get("hooks")
    raw = hooks.get("suppress") if isinstance(hooks, dict) else None
    if not isinstance(raw, list):
        return baseline
    return baseline | {str(item).strip() for item in raw if str(item).strip()}


def restored_history(transcript: list[dict[str, Any]]) -> tuple[tuple[str, str], ...]:
    """Simplified (role, text) pairs from a stored transcript for replay.

    A resumed TUI session replays the restored conversation into the
    transcript (an empty screen over a full context reads as a fresh
    session). Tool traffic and ``<system-reminder>`` injections are
    skipped — only real user prompts and assistant prose replay.

    The reminder filter is attribute-tolerant
    (:func:`~amplifier_app_tui.kernel.reminder_trust.is_injected_reminder`):
    every reminder a real hook emits is tagged ``<system-reminder
    source="...">``, which a bare ``<system-reminder>`` prefix test would
    miss — replaying an injected "process silently / do not mention this to
    the user" block as a fake user turn. Dropped concealment directives are
    logged (never silenced) so the trust event stays observable.
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
        if not text or text.startswith("<turn_aborted>"):
            continue
        if is_injected_reminder(text):
            if has_concealment_directive(text):
                logger.info(
                    "dropped injected reminder from replay (source=%s): "
                    "concealment directive not replayed as a user turn",
                    reminder_source(text) or "unknown",
                )
            continue
        pairs.append((str(role), text))
    return tuple(pairs)


_REPLAY_STREAM_KINDS = frozenset(
    {"stream_block_start", "stream_block_delta", "stream_block_end", "stream_aborted"}
)
"""Channel A stream kinds that never belong in ``ui-events.jsonl``.

The transcript replay renders from Channel B's durable
``content_block_end`` records only, and cost re-seed reads
``provider_response_usage`` / ``content_block_end`` (kernel/cost.py) — so
nothing that resume or re-seed reads is a stream kind. That makes these
kinds pure write-side noise: ``stream_block_delta`` fires **per token**,
turning the hottest path into an unbounded per-token open/write/close on
the log.

So they are skipped at **write** time (:meth:`RealRuntime._tap`) rather
than merely filtered at load time. The load-time filter in
:func:`restored_ui_events` is retained for backward compatibility with
logs written by older builds that still recorded every delta."""


def restored_ui_events(store: SessionStore, session_id: str) -> tuple[ParsedEvent, ...]:
    """The session's persisted UIEvents, typed, for resume transcript replay.

    Read through the store's own reader (never a hardcoded filename — the
    event-log path is the store's contract) and re-typed via
    :func:`~amplifier_app_tui.kernel.events.parse_event`; Channel A stream
    kinds are skipped (see :data:`_REPLAY_STREAM_KINDS`). Records this build
    cannot type — a foreign writer's line, an unknown/removed ``kind``, or
    schema drift — degrade to a redacted
    :class:`~amplifier_app_tui.model.blocks.UnsupportedBlock` placeholder
    (S5) rather than being dropped. Each placeholder carries a safe
    RECOVERY REFERENCE (S5 AC2) — the log path plus its own 1-based line,
    read via :meth:`~amplifier_app_tui.kernel.persistence.SessionStore.read_events_located`
    — so a user/support engineer can find the exact persisted line later;
    it is also logged immediately with a redacted (6-char) session id, the
    record's own type name, and that same path/line — never the raw
    payload — so a resumed session stays visible and diagnosable instead of
    silently losing the line.

    Post-rewind ghost turns are filtered out here (issue #40): the log is
    append-only, so a confirmed rewind leaves its discarded turns in the
    file. :func:`~amplifier_app_tui.kernel.events.drop_rewound_events`
    honors the :class:`~amplifier_app_tui.kernel.events.RewindMarker`
    records written at fork time, so the reducer replays only the turns
    that were still on screen — the read-side half of the append-only
    contract.
    """
    events: list[ParsedEvent] = []
    for path, line_no, record in store.read_events_located(session_id):
        if record.get("kind") in _REPLAY_STREAM_KINDS:
            continue
        event = parse_event(record, source_path=str(path), source_line=line_no)
        if isinstance(event, UnsupportedBlock):
            logger.warning(
                "resume: unsupported persisted record · session=%s type=%s source=%s:%s",
                session_id[:6],
                event.type_name,
                path,
                line_no,
            )
        events.append(event)
    return tuple(drop_rewound_events(events))


def _kept_turns_for(ledger: Any, checkpoint_id: str) -> int:
    """1-indexed ledger position of *checkpoint_id* (0 when unknown).

    One checkpoint is cut per completed turn, so the target's position is
    the number of prompt-delimited turns that survive the rewind — the
    kept-turns count the resume-side :func:`drop_rewound_events` truncates
    the replay to.
    """
    for index, turn in enumerate(ledger.turns):
        if turn.checkpoint.id == checkpoint_id:
            return index + 1
    if ledger.checkpoint_by_id(checkpoint_id) is not None:
        return len(ledger.turns) + 1
    return 0


def _kept_turns_before(ledger: Any, checkpoint_id: str) -> int | None:
    """Number of ledger prompt turns before a pre-prompt checkpoint."""
    explicit = getattr(ledger, "kept_turns_before", None)
    if isinstance(explicit, int) and ledger.checkpoint_by_id(checkpoint_id) is not None:
        return explicit
    # ``ledger.checkpoints`` is intentionally capped to the latest 100 for
    # the picker, whereas a rewind marker records the absolute surviving
    # turn count. Walk the complete turn ledger so selecting visible t151
    # after a long session persists ``kept_turns=150``, not zero.
    turns = ledger.turns
    for index, turn in enumerate(turns):
        if turn.checkpoint.id == checkpoint_id:
            return index
    # If it resolves but is not among completed turns, it is the one active
    # pre-prompt checkpoint.
    if ledger.checkpoint_by_id(checkpoint_id) is not None:
        return len(turns)
    return None


def resume_use_active_bundle(settings: dict[str, Any]) -> bool:
    """Resolve ``resume.use_active_bundle`` from merged settings.

    ``False`` (the default) resumes a session under the bundle it was
    created with; ``True`` opts back into attaching under the currently
    active bundle. Same defensive shape as :func:`suppressed_hooks_setting`:
    junk-shaped settings fall back to the default.
    """
    resume = settings.get("resume")
    value = resume.get("use_active_bundle") if isinstance(resume, dict) else None
    return value is True


def _plan_resume_bundle(
    stored_bundle: str | None,
    explicit_bundle: str | None,
    *,
    use_active: bool,
) -> tuple[str | None, str]:
    """Decide which bundle a resumed session boots under.

    Returns ``(bundle argument for resolve_config, reason)`` where reason
    is one of ``explicit`` / ``active`` / ``stored``. A session's module
    stack is part of its identity — resuming under whatever bundle is
    currently active silently swaps orchestrator/tools/hooks out from
    under the stored conversation, so the stored bundle wins by default.
    Overrides: an explicit ``--bundle`` argument (the caller asked for it
    by name) or settings ``resume.use_active_bundle: true``.
    """
    if explicit_bundle is not None:
        return (explicit_bundle, "explicit")
    if stored_bundle is None or use_active:
        return (None, "active")
    return (stored_bundle, "stored")


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
    stored_bundle: str | None,
    reason: str,
    resolved_bundle: str,
    active_bundle: str,
    notify: Callable[[Any], None],
) -> None:
    """One ``Notification`` saying which bundle a resumed session attached
    under — and why — whenever stored and attached bundles could diverge.

    Resuming under a different bundle than the session was created with
    silently changes which modules/tools/hooks govern the turn, so every
    non-default outcome is said out loud; the common case (stored bundle
    honored and identical to the active one) stays quiet.
    """
    if not stored_bundle:
        return
    if reason == "stored":
        if stored_bundle != active_bundle:
            notify(
                Notification(
                    message=(
                        f"resumed under stored bundle '{stored_bundle}' · active bundle "
                        f"'{active_bundle}' not attached (resume.use_active_bundle overrides)"
                    )
                )
            )
    elif reason == "stored-missing":
        notify(
            Notification(
                message=(
                    f"stored bundle '{stored_bundle}' not found — resumed under "
                    f"'{resolved_bundle}' bundle instead"
                )
            )
        )
    elif stored_bundle != resolved_bundle:
        cause = "--bundle" if reason == "explicit" else "resume.use_active_bundle"
        notify(
            Notification(
                message=(
                    f"session stored under '{stored_bundle}' bundle · resumed under "
                    f"'{resolved_bundle}' bundle ({cause})"
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
        lane_steering: LaneSteeringQueue | None = None,
        needs_you: NeedsYouQueue | None = None,
        denial_log: DenialLog | None = None,
        surface: TerminalSurface | None = None,
        mode: Callable[[], str] = lambda: "auto",
        model_override: str | None = None,
        provider_override: str | None = None,
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
            # tool-delegate's delegate:agent_completed payload has no result
            # field — the spawner records each child's final output and the
            # bridge fills AgentCompleted.result from it (lane recap +
            # delegate-summary snippets).
            agent_result_lookup=self._spawn_result,
        )
        self.turn_yield = TurnYieldTracker()
        """Per-turn ``tests ✔`` evidence from tool results (bridge tap)."""
        self.steering = steering or SteeringQueue()
        self.surface = surface or TerminalSurface()
        """Live terminal width for the width-aware surface hint (#35);
        the UI updates it on resize, the surface-hint hook reads it."""
        self.lane_steering = lane_steering or LaneSteeringQueue()
        """Per-lane steer FIFOs (issue #39): steers aimed at a running
        delegate, delivered at that child's next provider:request boundary
        by the shared StepBoundaryBridge."""
        self.needs_you = needs_you or NeedsYouQueue()
        # Every kernel-side deferral (broker ctrl-y park, auto-classifier
        # deny, escalation) becomes ONE decision Notification carrying the
        # queue item's id — the UI resolves that item (bell, badge, turn
        # deferred-marking) instead of re-deriving data from message text.
        self.needs_you.add_defer_listener(self._decision_deferred)
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
        self._mention_budget = MentionBudget()
        """Per-turn @mention expansion budget (issue #48; kernel/mention_expansion)."""
        self._bundle = bundle
        self._resume_id = resume_id
        self._mode = mode
        self._model_override = model_override
        self._provider_override = provider_override
        self._permission_resolver = permission_resolver
        self._capability_resolver = capability_resolver
        self._project_dir = project_dir
        self._spawner: SessionSpawner | None = None
        self._initialized: InitializedSession | None = None
        self._live_mcp: Any | None = None
        # Same-session extension ledger.  It is seeded from the boot mount
        # plan, then shared by /bundle and /module so a live module instance is
        # mounted once and contributes at most one teardown handle.
        self._live_module_keys: set[str] = set()
        self._live_bundle_ledger: dict[str, tuple[bool, str]] = {}
        self._live_load_lock = asyncio.Lock()
        self._executing = False  # a submit() turn is live (fork must refuse)
        self._interrupt_requested = False
        self._resolved: ResolvedConfig | None = None
        self._store: SessionStore | None = None
        self._saver: IncrementalSaver | None = None
        self._checkpoint_store: Any | None = None
        self._restoring_checkpoint = False
        self._rewind_recovery_pending = False
        self._rewind_recovery_disk_reconciled = False
        self._workspace_reconcile_pending = False
        self._image_injector: ClipboardImageInjector | None = None
        self._attention_push: NtfyAttentionDestination | None = None
        self.directory_policy: DirectoryPolicy | None = None
        self._session_settings_path: Path | None = None
        self.bundle_name = ""
        self.bundle_uri = ""
        """The actually-resolved bundle URI/path (``ResolvedConfig.bundle_uri``,
        ``kernel/config.resolve_bundle_source``) — distinct from
        :attr:`bundle_name`, which is the short name/argument a bundle was
        *requested* by (e.g. ``anchors``) and can differ from where it was
        actually loaded from (a packaged file path, a fetched git URI, …).
        Set once :meth:`start` resolves config; this is the value that must
        reach the UI's one persistent bundle display (D4 AC1) so "the full
        active bundle path" claim is actually true."""
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
        self.restored_events: tuple[ParsedEvent, ...] = ()
        """The session's persisted UIEvents on resume (Channel A stream
        kinds already filtered) — the reducer replays them so the
        transcript rebuilds exactly as it rendered live (DESIGN-SPEC
        §3/§11); empty for fresh sessions and for stored sessions with no
        usable event log (prose fallback). Records this build could not
        type degrade to a redacted ``UnsupportedBlock`` placeholder rather
        than being dropped (S5) — see :func:`restored_ui_events`."""
        self.degraded_notice: str | None = None
        self.gated_auto = False
        """Whether ``permissions.governance: gated`` armed auto-mode gating
        this boot — the UI shows auto's posture string truthfully from this."""
        self.mount_report: Any = None
        """The boot :class:`~.session_factory.MountReport`, kept past startup so
        ``/doctor`` can report WHICH module failed — the degraded notice sends
        the user to doctor, so doctor has to be able to answer."""
        self.pending_directive = ""
        """A resumed fork child's primed starting directive (``/fork`` /
        ``session fork``): set from stored metadata in :meth:`start` and
        consumed once by the app, which auto-runs it as the first turn. Empty
        for fresh sessions and ordinary resumes."""
        self.compaction = CompactionConfig()
        self._compaction_binding: CompactionRuntimeBinding | None = None
        self._background_tasks: set[asyncio.Task[Any]] = set()
        """Strong refs for fire-and-forget tasks spawned off the bridge tap
        (e.g. compaction token observation). A bare ``create_task`` result
        may be garbage-collected mid-flight, silently stopping the work
        (mirrors ``recipes`` ``self._tasks`` + ``add_done_callback``)."""

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
        # Resume loads the stored session BEFORE config resolution: the
        # bundle a session was created under (metadata["bundle"]) is part
        # of its identity, and the boot must resolve THAT bundle through
        # the normal resolve_config/foundation path — attaching under
        # whatever bundle is currently active would silently swap the
        # module stack out from under the stored conversation.
        store = SessionStore(project_dir=self._project_dir)
        self._store = store
        session_id: str | None = None
        transcript: list[dict[str, Any]] | None = None
        stored_bundle: str | None = None
        resume_reason = "active"
        boot_bundle = self._bundle
        if self._resume_id:
            session_id = store.find_session(self._resume_id)
            transcript, metadata = store.load(session_id)
            transcript, tool_repairs = complete_orphaned_tool_results(transcript)
            if tool_repairs:
                # Persist before the first resumed model request. Provider-side
                # request repair is necessarily ephemeral; without this write,
                # a second request can lose the same synthetic results and be
                # rejected even though the first repaired request succeeded.
                try:
                    store.save(session_id, transcript, metadata)
                except OSError:
                    logger.warning(
                        "Could not persist interrupted-tool resume repair for %s",
                        session_id,
                        exc_info=True,
                    )
                self.bridge.emit(
                    Notification(
                        message=(
                            f"Resume repaired {len(tool_repairs)} interrupted tool "
                            "result(s) before model execution. The tools may have "
                            "executed; inspect actual state before retrying."
                        ),
                        level="warning",
                    )
                )
            stored_bundle = str(metadata.get("bundle") or "") or None
            # A resumed fork child (/fork, session fork) is primed with a
            # starting directive; surface it for the app to auto-run as the
            # first turn. Consume-once: the store copy is cleared here so a
            # later resume of the same child never replays the instruction.
            self.pending_directive = session_manager.take_pending_directive(store, session_id)
            # resolve_config re-reads settings itself; this early read
            # exists only because the resume-policy knob must be known
            # BEFORE the golden path takes its bundle argument.
            project_dir = (self._project_dir or Path.cwd()).resolve()
            settings = load_merged_settings(
                SettingsPaths.default(project_dir, Path.home() / ".amplifier")
            )
            boot_bundle, resume_reason = _plan_resume_bundle(
                stored_bundle,
                self._bundle,
                use_active=resume_use_active_bundle(settings),
            )
        try:
            resolved = await resolve_config(
                boot_bundle,
                project_dir=self._project_dir,
                progress=self._progress,
                provider_override=self._provider_override,
                model_override=self._model_override,
            )
        except BundleNotFoundError:
            if resume_reason != "stored":
                raise
            # The stored bundle is no longer discoverable: resume on the
            # active bundle rather than refusing the session — the notice
            # below says so out loud.
            resume_reason = "stored-missing"
            resolved = await resolve_config(
                None,
                project_dir=self._project_dir,
                progress=self._progress,
                provider_override=self._provider_override,
                model_override=self._model_override,
            )
        _apply_hook_suppression(
            resolved.mount_plan, self.bridge.emit, suppressed_hooks_setting(resolved.settings)
        )
        if resolved.fallback_notice:
            # A settings-configured bundle failed discovery — the boot
            # continued on the app default; tell the user loudly.
            self.bridge.emit(Notification(message=resolved.fallback_notice))
        if resolved.settings_notice:
            # A settings.yaml scope was malformed and skipped — surface it
            # loudly rather than silently dropping the whole scope (the
            # analogous bundle fallback above already speaks up).
            self.bridge.emit(Notification(message=resolved.settings_notice, level="warning"))
        if resolved.deferred_notice:
            # bundle.deferred held overlays back from this boot for speed —
            # say so out loud (never a silent drop); /bundle load composes them.
            self.bridge.emit(Notification(message=resolved.deferred_notice))
        self._resolved = resolved
        self._live_module_keys.clear()
        self._live_bundle_ledger.clear()

        self.compaction = compaction_config(resolved.mount_plan)
        # Live pricing (BACKLOG item 1, behind settings ``pricing.live``,
        # default on): fresh disk cache applies immediately; otherwise a
        # daemon background fetch swaps the table for NEW turns only.
        # Never raises — failure keeps the offline fallback silently.
        start_live_pricing(resolved.settings)
        self._report_progress("creating", "session")

        if session_id is not None and transcript is not None:
            _resume_bundle_notice(
                stored_bundle,
                resume_reason,
                resolved.bundle_name,
                active_bundle_name(resolved.settings) or DEFAULT_BUNDLE,
                self.bridge.emit,
            )
            if store.transcript_recovery_failed:
                # The stored transcript existed but neither it nor its
                # .backup parsed: the resumed conversation lost its history.
                # Say so loudly rather than silently resuming empty (the
                # metadata path already flags this with a `recovered` marker).
                self.bridge.emit(
                    Notification(
                        message=(
                            "Resumed session transcript was unreadable — prior "
                            "history could not be recovered."
                        ),
                        level="warning",
                    )
                )
            if store.rewind_recovery_failed:
                self._rewind_recovery_pending = True
                self.bridge.emit(
                    Notification(
                        message=(
                            "A pending checkpoint restore could not be fully reconciled — "
                            "conversation history and scrollback may differ; inspect the "
                            "session restore record before continuing."
                        ),
                        level="warning",
                    )
                )
            if store.rewind_recovery_interrupted:
                self.bridge.emit(
                    Notification(
                        message=(
                            "A combined checkpoint restore was interrupted before code "
                            "completed. Conversation history was kept; inspect the workspace "
                            "restore journal and retry the checkpoint."
                        ),
                        level="warning",
                    )
                )

            # Same turn semantics as foundation's fork slicing: every
            # user-role message in the restored history is one turn.
            self.turn_base = sum(1 for m in transcript if m.get("role") == "user")
            self.restored_history = restored_history(transcript)
            self.restored_events = restored_ui_events(store, session_id)
            # Rebuild the per-answer evidence map from the same stored
            # stream (keyed by exact answer text, so replaying the whole
            # log in order restores links for EVERY turn's answer) — the
            # collector otherwise starts empty and every restored answer
            # would render unclickable. UnsupportedBlock placeholders (S5)
            # carry no event fields to observe and are skipped here — the
            # reducer still renders them in place during transcript replay.
            for event in self.restored_events:
                if isinstance(event, UnsupportedBlock):
                    continue
                self.evidence.observe(event)

        # Directory policy is derived from the prepared mount plan so the
        # filesystem tool, child sessions, CLI administration and shell
        # governance all consult one effective source. Session-scoped paths
        # are folded in before a resumed session mounts its tools.
        # Audit H2: ``open`` (app-cli parity) delegates outside-project write
        # enforcement to the mounted filesystem tool. Assert that enforcer is
        # actually planned; when it is not, degrade to the app-level ``guarded``
        # gate and announce it (never a silent trust of a non-existent tool).
        write_boundary, write_boundary_notice = resolve_write_boundary(
            resolved.settings, resolved.mount_plan
        )
        if write_boundary_notice is not None:
            self.bridge.emit(Notification(message=write_boundary_notice))
        directory_policy = policy_from_mount_plan(
            resolved.mount_plan,
            resolved.project_dir,
            write_boundary=write_boundary,
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
        self._spawner = spawner
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
        # MCP config changes are reconciled against this exact coordinator.
        # The helper prefers an upstream public capability and otherwise uses
        # the audited single-server seam from the pinned tool-mcp version. Its
        # async close handle is owned by the same session cleanup stack as
        # every other live-mounted extension.
        from .live_mcp import LiveMCPReconciler

        self._live_mcp = LiveMCPReconciler(initialized.coordinator)
        initialized.unregister_handles.append(self._live_mcp.close)
        from .bundle_compose import boot_module_identities

        # A boot plan records intent, while the coordinator/mount report prove
        # what actually attached. Keep failed provider and tool identities out
        # of the live ledger so a degraded module remains retryable through
        # /module or /bundle instead of being trapped as "already active".
        self._live_module_keys = boot_module_identities(
            resolved.mount_plan,
            initialized.coordinator,
            missing_tools=initialized.mount_report.missing_tools,
        )
        if self._session_settings_path is None:
            self._session_settings_path = (
                store.session_dir(initialized.session_id) / "settings.yaml"
            )
        self._sync_directory_tools()
        hooks = initialized.coordinator.hooks
        # B7: off-machine delivery consumes the same durable record/ack
        # events as every other destination.  The app-owned adapter uses
        # ntfy sequence IDs for destination dedupe + exact clear and never
        # listens to raw orchestrator completion (which would recreate a
        # second, uncorrelated notification source).
        attention_push = NtfyAttentionDestination(resolve_ntfy_attention_config(resolved.settings))
        initialized.unregister_handles.append(attention_push.register_hooks(hooks))
        self._attention_push = attention_push
        initialized.unregister_handles.append(self.bridge.register_hooks(hooks))
        # Filesystem undo is TUI-owned because Amplifier Foundation exposes
        # conversation forks but mounted file tools retain no preimages. The
        # store is private to this durable session and registers only on the
        # ROOT hook bus (it is deliberately not inherited by SessionSpawner),
        # matching the documented limitation that subagent/bash edits are not
        # code-restorable.
        from .checkpoints import (
            WorkspaceCheckpointStore,
            WorkspaceCheckpointUnavailableError,
        )

        try:
            checkpoint_store = WorkspaceCheckpointStore(
                store.session_dir(initialized.session_id),
                Path(resolved.project_dir),
                initialized.session_id,
            )
        except WorkspaceCheckpointUnavailableError:
            # Two independent runtimes writing/restoring one session cannot
            # uphold compare-and-swap. Refuse the duplicate owner instead of
            # silently running an uncheckpointed competing turn.
            await initialized.cleanup()
            self._initialized = None
            raise
        except (OSError, ValueError):
            logger.warning("workspace checkpoint store unavailable", exc_info=True)
            self.bridge.emit(
                Notification(
                    message=(
                        "Code checkpoints are unavailable because private checkpoint "
                        "storage could not be secured; conversation restore still works."
                    ),
                    level="warning",
                )
            )
        else:
            initialized.unregister_handles.append(checkpoint_store.close)
            unregister_checkpoint_store = checkpoint_store.register_hooks(hooks)
            if callable(unregister_checkpoint_store):
                initialized.unregister_handles.append(unregister_checkpoint_store)
            self._checkpoint_store = checkpoint_store
            self._workspace_reconcile_pending = bool(
                getattr(checkpoint_store, "pending_visible_reconcile", False)
            )
            if self._workspace_reconcile_pending and not self._rewind_recovery_pending:
                # A staged workspace branch without a conversation intent is
                # never allowed to race a new turn. Keep the pre-submit gate
                # closed and surface the missing marker on retry.
                self._rewind_recovery_pending = True
                self._rewind_recovery_disk_reconciled = True
            recovery_required = tuple(getattr(checkpoint_store, "recovery_required", ()))
            if recovery_required:
                self.bridge.emit(
                    Notification(
                        message=(
                            "An interrupted code restore needs attention before another "
                            "turn. Retry checkpoint " + ", ".join(recovery_required) + "."
                        ),
                        level="warning",
                    )
                )
        # Drift canary: hook kinds the engine publishes (core ALL_EVENTS +
        # observability.events contributions) that the bridge neither
        # consumes nor deliberately ignores surface once per session
        # instead of silently disappearing.
        initialized.unregister_handles.append(
            await self.bridge.register_canary(initialized.coordinator)
        )
        # App posture and outside-project gating is an ephemeral Amplifier
        # hook over the same tool:pre contract as native hooks-mode. Mounted
        # hooks still own bundle-defined modes; this hook owns only the TUI's
        # five trust postures and directory boundary.
        self.gated_auto = governance_setting(resolved.settings) == "gated"
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
            native_tools=self._native_safe_tools,
            gate_auto=self.gated_auto,
        )
        initialized.unregister_handles.append(governance.register_hooks(hooks))
        # Child lanes inherit the SAME governance instance so a gated posture
        # (plan/careful) blocks the same actions in a lane as in the root
        # (issue #38: children previously bypassed TUI posture gating). One
        # live mode() source — no per-child teardown on a mode change.
        spawner.set_governance_hook(governance)
        # hooks-approval owns bundle-mode ask/allow-always policy. The app's
        # broker is its presentation provider as well as governance's asker.
        self._register_approval_provider(initialized)
        # tool-recipes gates bypass the approval:* path entirely (custom
        # recipe:approval event + tool-operation resume) — bridge them onto
        # the same broker so a paused recipe raises the approval bar instead
        # of hanging invisibly (contract details: kernel/recipes.py).
        recipes_bridge = RecipeApprovalBridge(
            broker=self.broker,
            tools=lambda: (
                self._initialized.coordinator.get("tools")
                if self._initialized is not None
                else None
            ),
            emit=self.bridge.emit,
            is_executing=lambda: self._executing,
        )
        initialized.unregister_handles.append(recipes_bridge.register_hooks(hooks))
        boundary = StepBoundaryBridge(
            initialized.session_id,
            self.steering,
            needs_you=self.needs_you,
            # Same hook, keyed by child session id: a delegate's own
            # provider:request drains its lane queue (issue #39).
            lane_steering=self.lane_steering,
            on_lane_applied=self._lane_steer_applied,
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
            # Width-aware surface hint (issue #35 / docs/BACKLOG.md
            # section 2): an app-level provider:request hook telling the
            # model the live terminal width + supported Markdown subset,
            # so it survives any bundle override that drops the packaged
            # static contract. It edits the context directly and returns
            # continue (like the clipboard injector) rather than returning
            # inject_context -- a second inject_context on provider:request
            # would merge with the steering bridge's persistent steer under
            # one ephemeral flag and break rewind turn accounting.
            surface_hint = SurfaceHintInjector(initialized.session_id, self.surface, context)
            initialized.unregister_handles.append(surface_hint.register_hooks(hooks))
            injector = ClipboardImageInjector(context)
            unregister = hooks.register(
                "provider:request",
                injector.handle_provider_request,
                priority=900,
                name="tui-clipboard-images",
            )
            if callable(unregister):

                def _drop_injector() -> None:
                    unregister()

                initialized.unregister_handles.append(_drop_injector)
            self._image_injector = injector

        # Agent-summonable deferred bundles: when bundle.deferred held overlays
        # back for fast boot, tell the model what it can summon (catalog in
        # context) AND give it a host-provided load_bundle tool routing to the
        # same load_deferred_bundle seam /bundle load drives. A no-op unless
        # something was actually deferred (backward compatible).
        await self._install_deferred_summon(initialized, context)

        # Host-provided `question` tool: routes model question calls through
        # the shared NeedsYouQueue so BOTH clients answer via the existing
        # decision path (kernel/serve.py {"op":"decision"} / ui apply_decision).
        # Mounted onto the live coordinator like the load_bundle summon above.
        await self._install_question_tool(initialized)

        if self._resume_id:
            # Both files: a pre-rename session resumed under this build has
            # UIEvents split across events.jsonl and ui-events.jsonl.
            restore_session_cost(self.cost, *store.events_read_paths(initialized.session_id))
            self.session_cost_start = self.cost.session_cost

        self.bundle_name = resolved.bundle_name
        self.bundle_uri = resolved.bundle_uri
        self.session_short = initialized.session_id[:6]
        self.degraded_notice = initialized.degraded_notice
        self.mount_report = initialized.mount_report
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

    def session_dir(self) -> Path | None:
        """The live session's durable directory, once started (else ``None``).

        The SAME directory ``kernel/session_control.py`` keys its
        ``control.json`` off (``store.session_dir(session_id)``) -- attention
        durability (B7 gap 1) is deliberately kept beside it, not in a new
        location, so a controller or a resume already knows where to look.
        """
        if self._store is None or self._initialized is None:
            return None
        return self._store.session_dir(self._initialized.session_id)

    async def publish_attention(self, payload: dict[str, Any]) -> None:
        """Best-effort: publish one normalized attention transition.

        *payload* is the record-derived shape from ``ui.notifications.
        attention_push_payload`` -- carrying the attention ``event_id`` so a
        listener can retain the durable producer-side dedupe key.  The one
        canonical ``attention:recorded`` event is emitted unchanged for every
        record-aware consumer, including the app-owned ntfy destination.
        There is deliberately no raw-completion or compatibility projection:
        one persisted record remains the only producer.

        Emission never raises: a listener or hooks-bus problem must not block
        the live session.
        """
        initialized = self._initialized
        if initialized is None:
            return
        try:
            await initialized.coordinator.hooks.emit("attention:recorded", dict(payload))
        except Exception:  # noqa: BLE001 -- a destination failure must never block the session
            logger.debug("attention:recorded hook emission failed", exc_info=True)

    async def publish_attention_acknowledged(self, payload: dict[str, Any]) -> None:
        """Best-effort mirror of a durable acknowledgement onto the hooks bus.

        The normalized ``attention:acknowledged`` event gives every destination
        that supports clearing an explicit, event-id-correlated signal.  The
        app-owned ntfy destination maps it to the same deterministic sequence
        ID used for publish, then issues ntfy's clear operation.
        """
        initialized = self._initialized
        if initialized is None:
            return
        try:
            await initialized.coordinator.hooks.emit("attention:acknowledged", payload)
        except Exception:  # noqa: BLE001 -- a destination failure must never block the session
            logger.debug("attention:acknowledged hook emission failed", exc_info=True)

    def _spawn_result(self, sub_session_id: str) -> str:
        """Child final-output summary for AgentCompleted.result synthesis."""
        return self._spawner.result_for(sub_session_id) if self._spawner is not None else ""

    def agent_brief(self, agent_name: str) -> str:
        """Latest delegate brief for *agent_name* — the real lane seed.

        Read cross-thread by the adapter's ``lane_seed`` (a plain dict get
        under the GIL); "" until the agent's first spawn this session.
        """
        return self._spawner.brief_for(agent_name) if self._spawner is not None else ""

    def _decision_deferred(self, item: NeedsYouItem) -> None:
        """Surface a needs-you deferral to the UI (demo-contract parity:
        DemoRuntime emits the same ``level="decision"`` notification)."""
        self.bridge.emit(
            Notification(
                session_id=self.session_id,
                message=f"decision deferred to queue · {item.question}",
                level="decision",
                source="needs_you",
                decision_id=item.decision_id,
                # Full deferral detail (additive): a protocol client has no
                # shared queue to read the parked item from — without these
                # the wire lacked the WHY and the actionable choices.
                question=item.question,
                reason=item.reason,
                choices=item.choices,
                descriptions=item.descriptions,
                multiple=item.multiple,
                custom=item.custom,
                highlight=item.highlight,
                action=item.action,
            )
        )

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
        """Bridge tap: evidence derivation + append-only ui-events.jsonl.

        ui-events.jsonl is the append-only normalized UIEvent log
        (persistence module contract / ADR-0007 resolution 9); it powers
        the resume cost re-seed (``restore_session_cost``), so every
        durable event is appended once the session identity exists.

        Channel A stream kinds (:data:`_REPLAY_STREAM_KINDS`) are skipped:
        ``stream_block_delta`` fires per token, and appending it would open
        /write/close the log on every token while nothing that resume or
        re-seed reads is a stream kind (they render from Channel B's
        durable records). Everything resume/cost re-seed reads still lands
        on disk. Both halves are best-effort and never block the queue.
        """
        self.evidence.observe(event)
        self.turn_yield.observe(event)
        if (
            isinstance(event, ProviderResponseUsage)
            and self._compaction_binding is not None
            and event.input_tokens > 0
        ):
            # Keep a strong ref until done: a bare create_task result can be
            # GC'd mid-flight, silently stopping token observation until the
            # context overflows (contrast recipes.py's self._tasks set).
            task = asyncio.create_task(
                self._compaction_binding.observe_input_tokens(event.input_tokens)
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

        if (
            self._store is not None
            and self._initialized is not None
            and event.kind not in _REPLAY_STREAM_KINDS
        ):
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

    def _lane_steer_applied(self, session_id: str, steer: QueuedMessage) -> None:
        """Narrate a per-lane steer delivered to a delegate (issue #39).

        Symmetric with :meth:`_steer_applied`, but stamped with the CHILD
        ``session_id`` so the reducer diverts the ``Applying steer: <text>``
        narration into that lane's focused transcript (DESIGN-SPEC §8) —
        the delivery echo the acceptance calls for.
        """
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

    async def submit(
        self,
        text: str,
        attachments: tuple[ImageAttachment, ...] = (),
        *,
        _expanded_prompt: str | None = None,
        _on_admitted: Callable[[], None] | None = None,
    ) -> str:
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
        if self._rewind_recovery_pending:
            await self._retry_rewind_recovery()
        if self._restoring_checkpoint:
            raise RuntimeError("checkpoint restore in progress")
        if self._executing:
            raise RuntimeError("another turn is already running")
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
        workspace_checkpoint_id = ""
        turn_started = False
        try:
            # Cut the durable file checkpoint BEFORE the UI echo and before
            # session.execute can emit a write-tool pre hook. Its opaque id is
            # carried by PromptSubmit into the model ledger; display tN ids can
            # be reused after rewind, filesystem ids never are.
            if self._checkpoint_store is not None:
                candidate_checkpoint_id = uuid.uuid4().hex
                try:
                    await asyncio.to_thread(
                        self._checkpoint_store.begin,
                        candidate_checkpoint_id,
                        text,
                    )
                except Exception as exc:  # noqa: BLE001 — reject unsafe untracked turn
                    from .checkpoints import WorkspaceCheckpointUnavailableError

                    logger.warning("workspace checkpoint begin failed", exc_info=True)
                    raise WorkspaceCheckpointUnavailableError(
                        "workspace checkpoint could not be created; message was not sent"
                    ) from exc
                workspace_checkpoint_id = candidate_checkpoint_id
            # Turn-open first: the user's echo + working line paint NOW, not
            # after the pre-prompt hook work inside ``session.execute``.
            # Stamp the live app posture so the durable ui-events.jsonl log
            # (and thus resume replay) records which mode this turn ran under
            # — historical mode badges (BACKLOG parity). ``self._mode`` is the
            # app-supplied posture callable (lambda -> mode id).
            self.bridge.emit(
                PromptSubmit(
                    session_id=self._initialized.session_id,
                    prompt=text,
                    mode=self._mode(),
                    workspace_checkpoint_id=workspace_checkpoint_id,
                )
            )
            if _on_admitted is not None:
                _on_admitted()
            self.turn_yield.start_turn()
            turn_started = True
            starting_diff = await self._capture_diff()
            prompt_for_model = (
                _expanded_prompt
                if _expanded_prompt is not None
                else await self._expand_mentions(text)
            )
            response = await self._initialized.session.execute(prompt_for_model)
        finally:
            self._executing = False
            if workspace_checkpoint_id and self._checkpoint_store is not None:
                try:
                    await asyncio.to_thread(
                        self._checkpoint_store.finish,
                        workspace_checkpoint_id,
                    )
                except Exception:  # noqa: BLE001 — preserve the turn; surface in logs
                    logger.warning("workspace checkpoint finish failed", exc_info=True)
            if self._image_injector is not None:
                self._image_injector.clear()
            if turn_started and self._interrupt_requested:
                await self._append_turn_aborted_marker()
                self._interrupt_requested = False
            # End-of-turn save (reference: amplifier-app-cli persists after
            # every turn) — the incremental tool:post save misses the final
            # assistant message, which lands in the context only after the
            # last tool call.
            if turn_started and self._saver is not None:
                try:
                    await self._saver.maybe_save()
                except Exception:  # noqa: BLE001 — persistence is best-effort
                    logger.warning("end-of-turn save failed", exc_info=True)
            # The close-out event is emitted here — never from the raw
            # ``prompt:complete`` hook — so it is guaranteed to (a) follow
            # every turn event and (b) carry the end-of-turn yield.
            if turn_started:
                await self._emit_close_out(str(response or ""), starting_diff)
        return str(response or "")

    async def _retry_rewind_recovery(self) -> None:
        """Reconcile disk and live context before accepting a later prompt."""
        from .rewind import RewindRecoveryPendingError

        if self._store is None or self._initialized is None:
            raise RewindRecoveryPendingError(
                "checkpoint restore recovery is pending; message was not sent"
            )
        try:
            if not self._rewind_recovery_disk_reconciled:
                reconciled = await asyncio.to_thread(
                    self._store.reconcile_rewind_intent,
                    self._initialized.session_id,
                )
                if not reconciled:
                    raise RuntimeError("durable checkpoint restore intent is missing")
                self._rewind_recovery_disk_reconciled = True
            # Startup may have built Foundation's context from the old
            # transcript after reconciliation failed. Reload the durable
            # result and replace that already-live context before a later
            # save can overwrite the recovered history.
            transcript, _ = await asyncio.to_thread(
                self._store.load,
                self._initialized.session_id,
            )
            context = self._initialized.coordinator.get("context")
            if context is None or not hasattr(context, "set_messages"):
                raise RuntimeError("context module lacks set_messages")
            await context.set_messages([dict(message) for message in transcript])
            if self._workspace_reconcile_pending and self._checkpoint_store is not None:
                reconcile_staged = getattr(
                    self._checkpoint_store,
                    "reconcile_staged_visible",
                    None,
                )
                if not callable(reconcile_staged):
                    raise RuntimeError("workspace branch recovery is unavailable")
                workspace_reconciled = await asyncio.to_thread(reconcile_staged)
                if not workspace_reconciled:
                    raise RuntimeError("workspace branch rewind marker is not durable")
                self._workspace_reconcile_pending = False
            if self._saver is not None:
                mark_saved = getattr(self._saver, "mark_saved_message_count", None)
                if callable(mark_saved):
                    mark_saved(len(transcript))
        except Exception as exc:  # noqa: BLE001 — keep the send gate closed
            raise RewindRecoveryPendingError(
                f"checkpoint restore recovery is still pending: {exc}; message was not sent"
            ) from exc
        self._rewind_recovery_pending = False
        self._rewind_recovery_disk_reconciled = False

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

    def _mention_resolver(self) -> Any | None:
        """The session's foundation-registered ``@mention`` resolver.

        ``PreparedBundle.create_session`` registers a ``BaseMentionResolver``
        (base_path = session cwd, all composed bundle namespaces) under the
        ``mention_resolver`` capability -- the same capability the donor's
        ``_process_runtime_mentions`` reads. Absent (or a broken registry)
        simply means expansion no-ops, like a session with no mentions.
        """
        if self._initialized is None:
            return None
        try:
            return self._initialized.coordinator.get_capability("mention_resolver")
        except Exception:  # noqa: BLE001 -- capability registry variance
            return None

    async def _expand_mentions(self, text: str) -> str:
        """Inline resolved ``@mention`` content ahead of *text* (issue #48).

        Reference: amplifier-app-cli ``main.py:_process_runtime_mentions``.
        The raw *text* (mentions intact) is what the user echo shows; only
        the model-bound copy carries the prepended ``<context_file>`` blocks.
        Best-effort -- a resolver failure returns the text unexpanded rather
        than dropping the turn. Mentions dropped by the size budget surface
        as one Notification.
        """
        resolver = self._mention_resolver()
        if resolver is None:
            return text
        try:
            expansion = await expand_mentions(
                text,
                resolver=resolver,
                relative_to=self._turn_cwd(),
                budget=self._mention_budget,
            )
        except Exception:  # noqa: BLE001 -- expansion must never kill a turn
            logger.debug("mention expansion failed", exc_info=True)
            return text
        if expansion.skipped:
            names = ", ".join(mention for mention, _ in expansion.skipped)
            self.bridge.emit(
                Notification(message=f"@mention expansion skipped (size bounds): {names}")
            )
        return expansion.text

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

    def _native_safe_tools(self) -> frozenset[str]:
        """Tool names the ACTIVE native mode declares ``safe`` (hooks-mode).

        The governance hook lets these survive a tool-restrictive posture
        (tool-policy precedence). Reads the single upstream-enforced mode
        (``session_state["active_mode"]``) from the mounted hooks-mode
        discovery — consistent with the single-slot mode system. Best-effort:
        any missing/broken mode system yields the empty set (posture governs).
        """
        init = self._initialized
        if init is None:
            return frozenset()
        try:
            state = getattr(init.coordinator, "session_state", None) or {}
            active = state.get("active_mode")
            discovery = state.get("mode_discovery")
            if not active or discovery is None:
                return frozenset()
            mode_def = discovery.find(active)
            safe = getattr(mode_def, "safe_tools", None) or ()
            return frozenset(str(name) for name in safe)
        except Exception:  # noqa: BLE001 — a broken mode system must not gate tools
            logger.debug("native safe-tools lookup failed", exc_info=True)
            return frozenset()

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

    @property
    def project_dir(self) -> Path:
        """Session project root — the settings-scope target for /config save."""
        return self._turn_cwd()

    def config_state(self) -> SessionConfigState:
        """A fresh ``/config`` state seeded from this session's mount plan.

        The app builds this ONCE at start and mutates the adapter's copy;
        toggles/sets live in the session, and ``/config save`` persists
        them to the chosen settings scope (see ``kernel/config_ops``).
        """
        from .config_ops import state_from_plan

        if self._resolved is None:
            from ..model.config import default_config_state

            return default_config_state(self.bundle_name)
        return state_from_plan(self._resolved.mount_plan, bundle=self.bundle_name)

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
        ok, detail = await session_ops.set_model(coord, model)
        if ok:
            # ``model_name`` feeds the footer; without this a switch kept
            # showing the boot-time model until restart. Read the structured
            # session selection rather than parsing presentation text, which
            # may also carry an independent routing-matrix status suffix.
            state = getattr(coord, "session_state", None)
            selection = state.get("ui.model_override") if isinstance(state, dict) else None
            if isinstance(selection, dict):
                provider_name = str(selection.get("provider") or "")
                selected_model = str(selection.get("model") or "")
            else:
                parts = detail.split(" · ", 2)
                provider_name = parts[0]
                selected_model = parts[1] if len(parts) > 1 else ""
            self.model_name = "/".join(part for part in (provider_name, selected_model) if part)
        return (ok, detail)

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

    async def manage_goal(self, args: str) -> goal_bridge.GoalCommandResult:
        """Bridge ``/goal`` into the mounted orchestrator's native loop.

        The TUI owns only command parsing and first-turn admission.  Goal
        evaluation, continuation, stall detection, cancellation, and progress
        events remain inside ``loop-streaming``.  Mention expansion is snapped
        once so the evaluator condition and the first model turn see identical
        content while the transcript keeps the user's original text.
        """

        coord = self._coordinator()
        if coord is None:
            return goal_bridge.GoalCommandResult(
                False,
                "error",
                "Goal unavailable: session still starting.",
            )
        result = await goal_bridge.configure_goal(
            coord,
            args,
            expand_mentions=self._expand_mentions,
        )
        if not result.ok or result.action != "set":
            return result

        admitted = False

        def mark_admitted() -> None:
            nonlocal admitted
            admitted = True

        try:
            await self.submit(
                result.raw_condition,
                _expanded_prompt=result.condition,
                _on_admitted=mark_admitted,
            )
        except BaseException:
            # A checkpoint/preflight rejection happened before PromptSubmit;
            # do not leave an invisible armed goal behind.  Once admitted,
            # native state is authoritative and can be inspected/cleared with
            # a later bare ``/goal`` or ``/goal stop``.
            if not admitted:
                goal_bridge.clear_matching_goal(coord, result)
            raise
        return result

    async def status(self) -> session_ops.StatusInfo:
        coord = self._coordinator()
        if coord is None:
            return session_ops.StatusInfo()
        return await session_ops.status_snapshot(coord)

    async def list_tools(self) -> tuple[str, ...]:
        coord = self._coordinator()
        return await session_ops.list_tools(coord) if coord is not None else ()

    async def describe_tools(self) -> tuple[session_ops.ToolDescriptor, ...]:
        coord = self._coordinator()
        return await session_ops.describe_tools(coord) if coord is not None else ()

    async def invoke_tool(
        self, name: str, args: dict[str, Any], *, allow_writes: bool = False
    ) -> session_ops.ToolInvocation:
        """Invoke a mounted tool from the CLI, honoring the trust gate.

        A one-shot CLI cannot answer an interactive approval, so the same
        posture the TUI would apply is resolved up front (kernel/tool_cli):
        reads/tests run, mutations are refused unless *allow_writes* opts into
        in-project writes (still boundary-checked against this session's
        directory policy). A missing tool comes back ``found=False`` (clear
        error + nonzero exit) rather than a governance block, so the
        unknown-tool path stays unambiguous.
        """
        coord = self._coordinator()
        if coord is None:
            return session_ops.ToolInvocation(found=False, ok=False, error="session still starting")
        names = await session_ops.list_tools(coord)
        if name not in names:
            return session_ops.ToolInvocation(
                found=False, ok=False, error=f"no tool named '{name}' is mounted"
            )
        gate = tool_cli.gate_invocation(
            name, args, allow_writes=allow_writes, directory_policy=self.directory_policy
        )
        if not gate.allowed:
            return session_ops.ToolInvocation(
                found=True, ok=False, error=gate.reason, blocked=True, capability=gate.capability
            )
        return await session_ops.invoke_tool(coord, name, args)

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

    async def mcp_prompts(self) -> tuple[Any, ...]:
        """Live prompt descriptors from native tool-mcp wrappers."""
        from .mcp_prompts import discover_mcp_prompts

        coord = self._coordinator()
        return discover_mcp_prompts(coord) if coord is not None else ()

    async def execute_mcp_prompt(
        self, server: str, prompt: str, args: str = ""
    ) -> tuple[bool, str]:
        """Fetch a prompt body through the currently mounted native wrapper."""
        from .mcp_prompts import execute_mcp_prompt

        coord = self._coordinator()
        if coord is None:
            return (False, "session still starting")
        return await execute_mcp_prompt(coord, server, prompt, args)

    def _inline_mcp_config(self) -> dict[str, Any]:
        """Inline config on the boot-plan's tool-mcp entry, if any."""
        resolved = self._resolved
        if resolved is None:
            return {}
        for entry in resolved.mount_plan.get("tools") or ():
            if not isinstance(entry, dict):
                continue
            module = str(entry.get("module") or "")
            canonical = module.removeprefix("amplifier-module-")
            if canonical == "tool-mcp":
                config = entry.get("config")
                return dict(config) if isinstance(config, dict) else {}
        return {}

    def _effective_mcp_servers(self) -> dict[str, Any]:
        """Effective MCP config using tool-mcp's actual precedence."""
        from .mcp_config import read_effective_servers

        return read_effective_servers(
            project_dir=self.project_dir,
            inline=self._inline_mcp_config(),
        )

    async def mcp_servers(self) -> dict[str, str]:
        """Configured server summaries across user/project/env/inline scopes."""
        from .mcp_config import describe_server

        return {name: describe_server(spec) for name, spec in self._effective_mcp_servers().items()}

    async def add_mcp_server(
        self,
        name: str,
        command: str,
        args: tuple[str, ...] = (),
    ) -> tuple[bool, str]:
        """Persist a stdio server and connect it to this session when safe."""
        if self._initialized is None or self._live_mcp is None:
            return (False, "session still starting")
        server = name.strip()
        executable = command.strip()
        if not server or not executable:
            return (False, "usage: /mcp add <name> <command> [args…]")

        from . import mcp_config

        before = self._effective_mcp_servers()
        path = mcp_config.mcp_config_path()
        mcp_config.add_stdio_server(path, server, executable, args)
        desired: dict[str, Any] = {"command": executable}
        if args:
            desired["args"] = list(args)
        after = self._effective_mcp_servers()
        effective = after.get(server)
        if effective != desired:
            return (
                False,
                f"configuration saved globally for '{server}', but a project, environment, "
                "or inline definition still overrides it; live connection unchanged",
            )
        result = await self._live_mcp.add(
            server,
            desired,
            configured=True,
            previously_configured=server in before,
        )
        return (result.ok, f"mcp {server} · {result.message}")

    async def reload_mcp_server(self, name: str) -> tuple[bool, str]:
        """Reconnect a TUI-owned server; preserve boot-owned connections."""
        if self._initialized is None or self._live_mcp is None:
            return (False, "session still starting")
        server = name.strip()
        spec = self._effective_mcp_servers().get(server)
        if not isinstance(spec, Mapping):
            return (False, f"no configured MCP server · {server}")
        result = await self._live_mcp.reload(server, dict(spec), configured=True)
        return (result.ok, f"mcp {server} · {result.message}")

    async def remove_mcp_server(self, name: str) -> tuple[bool, str]:
        """Remove a user-scope server and disconnect it when TUI-owned."""
        if self._initialized is None or self._live_mcp is None:
            return (False, "session still starting")
        server = name.strip()
        if not server:
            return (False, "usage: /mcp remove <name>")

        from . import mcp_config

        before = self._effective_mcp_servers()
        removed = mcp_config.remove_server(mcp_config.mcp_config_path(), server)
        if not removed:
            if server in before:
                return (
                    False,
                    f"'{server}' is configured by project, environment, or bundle scope; "
                    "remove it at that source",
                )
            return (False, f"no such server · {server}")
        after = self._effective_mcp_servers()
        if server in after:
            return (
                False,
                f"global configuration removed for '{server}', but a higher-priority "
                "definition remains active; live connection unchanged",
            )
        result = await self._live_mcp.remove(
            server,
            configured=False,
            previously_configured=server in before,
        )
        return (result.ok, f"mcp {server} · {result.message}")

    async def _install_deferred_summon(self, initialized: InitializedSession, context: Any) -> None:
        """Make deferred bundles discoverable + summonable by the model.

        Two app-level attachments, both skipped entirely when nothing was
        deferred (backward compatible):

        - **Discovery**: a catalog of every held-back overlay (name +
          one-line description) injected into the root context as one system
          message via :class:`DeferredCatalogInjector` (the same direct-edit
          seam as the surface hint), so the model knows what it can summon.
        - **Summon**: a host-provided ``load_bundle`` tool mounted onto the
          live coordinator's ``tools`` point (foundation's own mount seam),
          whose ``execute`` routes to :meth:`load_deferred_bundle` — the same
          path ``/bundle load`` drives, honest single-slot boundary included.

        Best-effort: a mount/registration failure degrades the session (the
        manual ``/bundle load`` command still works) rather than blocking boot.
        """
        resolved = self._resolved
        if resolved is None or not resolved.deferred_overlays:
            return
        catalog = build_deferred_catalog(
            resolved.deferred_overlays,
            resolved.settings,
            bundle_search_paths(resolved.project_dir, Path.home() / ".amplifier"),
        )
        # Summon tool: mounted directly onto the coordinator (the seam
        # foundation itself uses for a Python tool — loader_grpc mounts a
        # bridge the same way), so the model sees it from turn one.
        tool = LoadBundleTool(self.load_deferred_bundle, catalog)
        try:
            await initialized.coordinator.mount("tools", tool, name=LOAD_BUNDLE_TOOL_NAME)
        except Exception:  # noqa: BLE001 — summon degrades to /bundle load, never blocks boot
            logger.warning("could not mount load_bundle summon tool", exc_info=True)
        # Discovery catalog: one system message reconciled at each root
        # provider:request (survives /clear + compaction). Needs an editable
        # context; without one the tool's own description still lists options.
        if context is not None:
            injector = DeferredCatalogInjector(
                initialized.session_id, catalog_instruction_text(catalog), context
            )
            initialized.unregister_handles.append(
                injector.register_hooks(initialized.coordinator.hooks)
            )

    async def _install_question_tool(self, initialized: InitializedSession) -> None:
        """Mount the host-provided ``question`` tool onto the live coordinator.

        The model calls ``question`` to ask the user a structured question;
        the tool defers it onto the shared
        :class:`NeedsYouQueue` (surfaced to both clients via the existing
        ``level=\"decision\"`` notification). Interactive modes wait for the
        same ``needs_you.answer`` seam the serve ``decision`` op and TUI use;
        Auto returns immediately so unrelated work continues and the answer is
        injected when available. Same mount seam as
        :meth:`_install_deferred_summon`'s ``load_bundle``. Best-effort: a mount
        failure degrades to no question tool rather than blocking boot.
        """
        from .question import QUESTION_TOOL_NAME, QuestionTool

        tool = QuestionTool(self.needs_you, mode=self._mode)
        try:
            await initialized.coordinator.mount("tools", tool, name=QUESTION_TOOL_NAME)
        except Exception:  # noqa: BLE001 — degrade to no question tool, never block boot
            logger.warning("could not mount question tool", exc_info=True)

    # -- in-session bundle/module composition (/bundle, /module) ------------

    def deferred_bundles(self) -> tuple[str, ...]:
        """Names/URIs the live ``/bundle load`` path can resolve.

        The legacy method name remains part of the adapter contract, but the
        catalog is no longer deferred-only: it includes held-back overlays,
        ``bundle.added`` registrations, locally discovered bundles, and the
        shared Foundation registry.  A bundle already active at boot remains
        visible and reports an idempotent no-op if selected.
        """
        if self._resolved is None:
            return ()
        settings = self._resolved.settings
        names: list[str] = list(deferred_overlay_uris(settings))
        names.extend(added_bundle_uris(settings))
        try:
            entries = list_known_bundles(
                self._resolved.project_dir,
                Path.home() / ".amplifier",
            )
        except Exception:  # noqa: BLE001 — the deferred/added catalog still works
            entries = ()
        names.extend(entry.name for entry in entries)
        return tuple(dict.fromkeys(name for name in names if name))

    def _resolve_live_bundle_uri(self, target: str) -> str | None:
        """Resolve a deferred, registered, local, or direct bundle target."""
        resolved = self._resolved
        if resolved is None:
            return None
        settings = resolved.settings
        # Preserve the original deferred resolver's name -> URI semantics,
        # then broaden through the same local/added discovery boot uses.
        uri = resolve_deferred_bundle(target, settings)
        if uri is not None:
            return uri
        search = bundle_search_paths(resolved.project_dir, Path.home() / ".amplifier")
        uri = resolve_bundle_name(target, settings, search)
        if uri is not None:
            return uri
        # Foundation's registry can contain names that are neither on disk nor
        # in bundle.added.  Resolve from its already-known entries without
        # changing settings or composing anything yet.
        try:
            entries = list_known_bundles(
                resolved.project_dir,
                Path.home() / ".amplifier",
            )
        except Exception:  # noqa: BLE001 — an unknown target is reported below
            return None
        for entry in entries:
            if entry.name == target and entry.uri:
                return entry.uri
        return None

    @staticmethod
    def _live_bundle_key(uri: str) -> str:
        """Canonical ledger key for a bundle URI or local path."""
        path = Path(uri).expanduser()
        if path.exists():
            try:
                return str(path.resolve())
            except OSError:
                pass
        return uri

    async def load_deferred_bundle(self, name: str) -> tuple[bool, str]:
        """Compose a bundle's additive modules into the LIVE session.

        Resolves *name* as a deferred overlay, a ``bundle.added`` or Foundation
        registry name, a locally discovered bundle, or a direct URI/path;
        prepares it (installing missing modules), then mounts only its additive
        provider/tool/hook/agent modules onto the running coordinator via
        :func:`kernel.bundle_compose.mount_overlay_modules`.
        Settings bridges (mode search paths, routing, telemetry, notifications)
        are applied to the overlay plan first so a deferred behavior bundle
        still gets its config lowered exactly as it would at boot.

        Returns ``(ok, detail)``; never raises into the UI.  A newly mounted
        provider stays idle until the user selects its model; orchestrator and
        context replacement remain an explicit next-session boundary.
        The URI ledger and shared module ledger make retries idempotent and
        guarantee that each newly mounted instance registers one cleanup.
        """
        target = name.strip()
        if self._initialized is None or self._resolved is None:
            return (False, "session still starting")
        if not target:
            available = ", ".join(self.deferred_bundles()) or "none"
            return (False, f"usage: /bundle load <name-or-uri> · available: {available}")
        async with self._live_load_lock:
            return await self._load_bundle_live(target)

    async def _load_bundle_live(self, target: str) -> tuple[bool, str]:
        """Locked implementation for :meth:`load_deferred_bundle`."""
        assert self._initialized is not None
        assert self._resolved is not None
        settings = self._resolved.settings
        uri = self._resolve_live_bundle_uri(target)
        if uri is None:
            available = ", ".join(self.deferred_bundles()) or "none"
            return (False, f"bundle '{target}' not found · available: {available}")
        key = self._live_bundle_key(uri)
        active_keys = {
            self._live_bundle_key(active)
            for active in (self._resolved.bundle_uri, *self._resolved.overlays)
            if active
        }
        if key in active_keys:
            return (True, f"already active from session start · {target}")
        previous = self._live_bundle_ledger.get(key)
        if previous is not None:
            ok, detail = previous
            state = "already loaded this session" if ok else "already attempted this session"
            return (ok, f"{state} · {target} · {detail}")
        try:
            prepared = await prepare_live_overlay_bundle(uri, settings, progress=self._progress)
            mount_plan = prepared.mount_plan
        except Exception as error:  # noqa: BLE001 — surfaced as a load miss, never a traceback
            logger.warning("deferred bundle prepare failed: %s", uri, exc_info=True)
            return (False, f"could not prepare '{target}': {error or type(error).__name__}")
        # Bridge the same settings sections the boot path bridges, and strip
        # any TUI-corrupting printing hooks the overlay drags in, BEFORE mount.
        inject_mode_search_paths(mount_plan, packaged_modes_dir())
        inject_routing_config(mount_plan, settings, Path.home() / ".amplifier")
        inject_telemetry_config(mount_plan, settings)
        inject_notifications_config(mount_plan, settings)
        _apply_hook_suppression(mount_plan, self.bridge.emit, suppressed_hooks_setting(settings))
        from .bundle_compose import mount_overlay_modules
        from .bundle_content import activate_bundle_content

        content = await activate_bundle_content(
            prepared,
            self._initialized.coordinator,
            self._initialized.session,
            self.project_dir,
        )

        result = await mount_overlay_modules(
            self._initialized.coordinator,
            mount_plan,
            seen=self._live_module_keys,
            bundle_content_deferred=not content.ok,
            parent_config=getattr(self._initialized.session, "config", None),
        )
        # Mounted modules unwind with the session (parity with the boot hooks'
        # unregister_handles) — a bare cleanup would otherwise leak on exit.
        self._initialized.unregister_handles.extend(result.cleanups)
        if content.cleanup is not None:
            self._initialized.unregister_handles.append(content.cleanup)
        detail = result.summary(target)
        if content.added:
            detail += " · instructions/context active for next turn"
        elif not content.ok and content.reason:
            detail += f" · content activation failed: {content.reason}"
        self._live_bundle_ledger[key] = (result.ok, detail)
        return (result.ok, detail)

    async def load_module(self, module_id: str, source_hint: str = "") -> tuple[bool, str]:
        """Mount one additive provider/tool/hook module into the current session.

        Explicit module loading accepts named multi-slot providers, tools, and
        hooks. It intentionally rejects orchestrators, contexts, agents, and
        unknown module kinds; those are singleton/config-identity surfaces and
        attach at the next session start. TUI-suppressed printing/notification
        hooks are rejected too.
        """
        initialized = self._initialized
        if initialized is None or self._resolved is None:
            return (False, "session still starting")
        target = module_id.strip()
        if not target:
            return (False, "usage: /module load <provider-, tool-, or hook-module> [source-uri]")
        async with self._live_load_lock:
            return await self._load_module_live(target, source_hint)

    async def _load_module_live(self, target: str, source_hint: str) -> tuple[bool, str]:
        """Locked implementation for :meth:`load_module`."""
        assert self._initialized is not None
        assert self._resolved is not None
        canonical = target.removeprefix("amplifier-module-")
        suppressed = suppressed_hooks_setting(self._resolved.settings)
        if target in suppressed or canonical in suppressed:
            return (
                False,
                f"module '{target}' is suppressed because it bypasses TUI rendering",
            )
        from .bundle_compose import mount_additive_module

        result = await mount_additive_module(
            self._initialized.coordinator,
            target,
            source_hint=source_hint.strip() or None,
            seen=self._live_module_keys,
            parent_config=getattr(self._initialized.session, "config", None),
        )
        self._initialized.unregister_handles.extend(result.cleanups)
        return (result.ok, result.summary(target))

    # -- stored-session lifecycle (/rename /sessions /branch) ---------------

    def session_summaries(self) -> tuple[session_manager.SessionSummary, ...]:
        """Newest-first summaries of this project's stored sessions."""
        if self._store is None:
            return ()
        return tuple(session_manager.list_summaries(self._store, limit=20))

    @property
    def store(self) -> SessionStore | None:
        """The live session's persistence store (``None`` before boot).

        Exposed for the ``serve`` protocol's additive tag ops, which read and
        write session tags directly on the store (kernel/serve._serve_store).
        """
        return self._store

    def session_tags(self) -> tuple[str, ...]:
        """The live session's tags (sorted; empty before boot or when none)."""
        if self._store is None or self._initialized is None:
            return ()
        return session_manager.read_tags(self._store, self._initialized.session_id)

    def sessions_by_tag(self, tag: str) -> tuple[session_manager.SessionSummary, ...]:
        """Newest-first summaries of stored sessions carrying *tag*."""
        if self._store is None:
            return ()
        return tuple(session_manager.sessions_by_tag(self._store, tag))

    async def add_session_tags(self, tags: tuple[str, ...]) -> tuple[bool, str]:
        """Attach *tags* to the live session (persisted metadata ``tags``).

        Like ``/rename``, the lazily-persisted live session is materialized
        first so the tag always lands. Returns ``(ok, human_message)``.
        """
        if self._store is None or self._initialized is None:
            return (False, "session still starting")
        session_id = self._initialized.session_id
        if not session_manager.ensure_session_dir(self._store, session_id, bundle=self.bundle_name):
            return (False, "could not persist session to tag")
        outcome = session_manager.add_tags(self._store, session_id, list(tags))
        return (outcome.ok, _tag_message(outcome, verb="add"))

    async def remove_session_tags(self, tags: tuple[str, ...]) -> tuple[bool, str]:
        """Detach *tags* from the live session. Returns ``(ok, human_message)``."""
        if self._store is None or self._initialized is None:
            return (False, "session still starting")
        outcome = session_manager.remove_tags(self._store, self._initialized.session_id, list(tags))
        return (outcome.ok, _tag_message(outcome, verb="remove"))

    async def rename_session(self, name: str) -> tuple[bool, str]:
        """Label the live session (persisted metadata ``name``).

        A fresh session persists lazily (``tool:post`` / end-of-turn), so
        its directory may not exist yet; a minimal metadata save is written
        first so ``/rename`` always lands.
        """
        if self._store is None or self._initialized is None:
            return (False, "session still starting")
        session_id = self._initialized.session_id
        if not self._store.exists(session_id):
            try:
                self._store.save(
                    session_id, [], {"session_id": session_id, "bundle": self.bundle_name}
                )
            except (OSError, ValueError):
                return (False, "could not persist session to rename")
        return session_manager.rename(self._store, session_id, name)

    async def branch_session(self, name: str = "") -> tuple[bool, str]:
        """Snapshot the live conversation into a new stored session.

        The persisted-fork analog of the in-memory ``/rewind``: the current
        context messages are written under a fresh id carrying this
        session's id as ``parent_id`` (kernel/session_manager.branch).
        """
        if self._store is None or self._initialized is None:
            return (False, "session still starting")
        context = self._initialized.coordinator.get("context")
        messages: list[dict[str, Any]] = []
        if context is not None and hasattr(context, "get_messages"):
            messages = list(await context.get_messages())
        return session_manager.branch(
            self._store,
            self._initialized.session_id,
            messages,
            name=name,
            bundle=self.bundle_name,
        )

    async def fork_session(self, directive: str) -> tuple[bool, str]:
        """Snapshot the live conversation into a new session PRIMED with *directive*.

        The directive-seeded sibling of :meth:`branch_session`: the current
        context messages are copied into a fresh id carrying this session's id
        as ``parent_id``, and *directive* is stored so a later
        ``amplifier-tui resume <child>`` runs it first
        (kernel/session_manager.fork). The re-expression of app-cli's ``/fork
        <directive>`` background self-delegation over tui's persisted store —
        a primed, resumable child, not a detached daemon (see the module note).
        """
        if self._store is None or self._initialized is None:
            return (False, "session still starting")
        context = self._initialized.coordinator.get("context")
        messages: list[dict[str, Any]] = []
        if context is not None and hasattr(context, "get_messages"):
            messages = list(await context.get_messages())
        return session_manager.fork(
            self._store,
            self._initialized.session_id,
            messages,
            directive,
            bundle=self.bundle_name,
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
                if self._executing:
                    self._interrupt_requested = True
                return True
            except Exception:  # noqa: BLE001 — cancellation is best-effort
                logger.debug("cancellation request failed", exc_info=True)
        return False

    async def fork(self, checkpoint_id: str, ledger: Any) -> Any:
        """Rewind the live session to *checkpoint_id* (ADR-0007 §Rewind).

        In-memory fork via :class:`~amplifier_app_tui.kernel.rewind.
        RewindController`: foundation's ``fork_session_in_memory`` slices
        the live context's messages at the checkpoint's turn,
        ``context.set_messages()`` commits them, and *ledger* trims only
        after the context confirms (confirm-then-trim). Raises
        :class:`~amplifier_app_tui.kernel.rewind.RewindError` on any
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
        # Count the surviving turns BEFORE the fork trims the ledger: the
        # rewind marker records how many prompt-delimited turns replay must
        # keep on resume (issue #40). One checkpoint per completed turn, so
        # the target's 1-indexed ledger position IS that count.
        kept_turns = _kept_turns_for(ledger, checkpoint_id)
        controller = RewindController(ledger)
        outcome = await controller.fork_in_memory(
            checkpoint_id,
            messages=messages,
            set_messages=context.set_messages,
            parent_id=initialized.session_id,
        )
        # Backend + ledger both confirmed the trim — now stamp the boundary
        # into the append-only log so a later resume drops the turns this
        # fork discarded instead of replaying them as ghost turns.
        if kept_turns > 0 and self._store is not None:
            self._store.append_event(
                initialized.session_id,
                RewindMarker(
                    session_id=initialized.session_id,
                    checkpoint_id=checkpoint_id,
                    kept_turns=kept_turns,
                ),
            )
        return outcome

    async def restore_checkpoint(self, checkpoint_id: str, ledger: Any, *, scope: str) -> Any:
        """Serialize one checkpoint restore against every new user turn."""
        from .rewind import RewindError

        if self._rewind_recovery_pending:
            await self._retry_rewind_recovery()
        if self._restoring_checkpoint:
            raise RewindError("another checkpoint restore is already in progress")
        if self._executing:
            raise RewindError("turn still running — interrupt it first")
        self._restoring_checkpoint = True
        try:
            return await self._restore_checkpoint_impl(checkpoint_id, ledger, scope=scope)
        finally:
            self._restoring_checkpoint = False

    async def _restore_checkpoint_impl(self, checkpoint_id: str, ledger: Any, *, scope: str) -> Any:
        """Restore a pre-prompt checkpoint's code, conversation, or both.

        Conversation restoration delegates slicing to Amplifier Foundation
        and commits through the native context module. Code restoration uses
        the TUI's private, compare-and-swap workspace store because Core and
        Foundation do not retain file preimages. Direct root file-tool edits
        are covered; bash, subagent, and external mutations intentionally are
        not, and a later manual edit becomes an explicit conflict rather than
        being overwritten.
        """
        from .rewind import CheckpointRestoreOutcome, RewindController, RewindError

        if scope not in {"both", "conversation", "code"}:
            raise RewindError(f"unknown restore scope: {scope}")
        validated_scope = cast(Literal["both", "conversation", "code"], scope)
        initialized = self._initialized
        if initialized is None:
            raise RewindError("RealRuntime.start() has not completed")
        target = ledger.checkpoint_by_id(checkpoint_id)
        if target is None:
            raise RewindError(f"unknown checkpoint: {checkpoint_id}")

        summaries: list[str] = []
        code_status = "not_requested"
        partial = False
        controller = RewindController(ledger)
        context: Any | None = None
        conversation_plan: Any | None = None
        kept_turns: int | None = None
        visible_workspace_ids_before: tuple[str, ...] = ()
        original_messages: list[dict[str, Any]] = []
        rewind_marker: RewindMarker | None = None
        rewind_intent_started = False
        visible_intent_staged = False
        conversation_committed = False
        prompt_attachments: tuple[ImageAttachment, ...] = ()
        if scope in {"both", "conversation"}:
            # Validate the native context seam and Foundation slice before a
            # combined restore mutates any files. A bad turn boundary should
            # leave both surfaces untouched.
            context = initialized.coordinator.get("context")
            if context is None or not hasattr(context, "set_messages"):
                raise RewindError("context module lacks set_messages — cannot restore")
            if hasattr(context, "get_messages"):
                original_messages = list(await context.get_messages())
            user_messages = [
                message for message in original_messages if message.get("role") == "user"
            ]
            if target.before_turn_id < len(user_messages):
                prompt_attachments = image_attachments_from_message(
                    user_messages[target.before_turn_id]
                )
            kept_turns = _kept_turns_before(ledger, checkpoint_id)
            if kept_turns is None:
                raise RewindError(f"unknown checkpoint: {checkpoint_id}")
            snapshot_ids = getattr(ledger, "visible_workspace_ids_before", None)
            if isinstance(snapshot_ids, tuple):
                visible_workspace_ids_before = tuple(
                    item for item in snapshot_ids if isinstance(item, str) and item
                )
            else:
                turns = tuple(getattr(ledger, "turns", ()))
                visible_workspace_ids_before = tuple(
                    turn.checkpoint.workspace_id
                    for turn in turns[:kept_turns]
                    if getattr(turn.checkpoint, "workspace_id", "")
                )
            conversation_plan = controller.prepare_restore_before_in_memory(
                checkpoint_id,
                messages=original_messages,
                parent_id=initialized.session_id,
            )
            rewind_marker = RewindMarker(
                event_id=f"rewind-{uuid.uuid4().hex}",
                session_id=initialized.session_id,
                checkpoint_id=checkpoint_id,
                kept_turns=kept_turns,
            )
            if self._store is not None:
                try:
                    self._store.begin_rewind_intent(
                        initialized.session_id,
                        marker=rewind_marker,
                        messages=[dict(message) for message in conversation_plan.messages],
                        ready=scope != "both",
                    )
                except (OSError, TypeError, ValueError) as exc:
                    raise RewindError(f"could not stage durable restore: {exc}") from exc
                rewind_intent_started = True
                if scope == "conversation" and self._checkpoint_store is not None:
                    stage_visible = getattr(
                        self._checkpoint_store,
                        "stage_visible_reconcile",
                        None,
                    )
                    if callable(stage_visible):
                        try:
                            await asyncio.to_thread(
                                stage_visible,
                                visible_workspace_ids_before,
                                rewind_marker.event_id,
                            )
                        except (OSError, RuntimeError, ValueError) as exc:
                            self._store.cancel_rewind_intent(initialized.session_id)
                            raise RewindError(
                                f"could not stage workspace branch restore: {exc}"
                            ) from exc
                        visible_intent_staged = True
                        self._workspace_reconcile_pending = True

        # For a combined restore, commit the reversible conversation slice
        # first but defer ledger trimming. If code infrastructure then fails
        # before a result, the original messages can be put back and the UI
        # timeline/ledger remain untouched.
        conversation_applied = False
        if scope == "both":
            assert context is not None
            assert conversation_plan is not None
            try:
                await controller.apply_prepared_restore(
                    conversation_plan,
                    set_messages=context.set_messages,
                )
            except RewindError:
                if rewind_intent_started and self._store is not None:
                    self._store.cancel_rewind_intent(initialized.session_id)
                raise
            conversation_applied = True

        async def rollback_combined_conversation(
            reason: str,
            cause: BaseException | None = None,
        ) -> None:
            """Restore original context and neutralize its durable intent."""
            nonlocal conversation_applied, rewind_intent_started
            if not conversation_applied:
                return
            assert context is not None
            try:
                await context.set_messages([dict(message) for message in original_messages])
            except Exception as rollback_error:  # noqa: BLE001 — prevent split state
                source = cause if cause is not None else rollback_error
                raise RewindError(
                    f"{reason}; conversation rollback failed: {rollback_error}"
                ) from source
            if rewind_intent_started and self._store is not None:
                try:
                    self._store.cancel_rewind_intent(initialized.session_id)
                except (OSError, TypeError, ValueError) as cancel_error:
                    source = cause if cause is not None else cancel_error
                    raise RewindError(
                        f"{reason}; conversation rolled back, but the durable restore "
                        f"intent could not be cancelled: {cancel_error}"
                    ) from source
            rewind_intent_started = False
            conversation_applied = False

        if scope in {"both", "code"}:
            if self._checkpoint_store is None or not target.workspace_id:
                summaries.append("no tracked code checkpoint")
                code_status = "unavailable"
                partial = True
            else:
                checkpoint_status = "active"
                status_fn = getattr(self._checkpoint_store, "checkpoint_status", None)
                try:
                    if callable(status_fn):
                        checkpoint_status = await asyncio.to_thread(
                            status_fn,
                            target.workspace_id,
                        )
                except (KeyError, OSError, RuntimeError, ValueError) as exc:
                    await rollback_combined_conversation(
                        "code checkpoint status failed",
                        exc,
                    )
                    raise RewindError(f"code checkpoint status failed: {exc}") from exc
                if checkpoint_status == "retired":
                    summaries.append("code checkpoint already restored")
                    code_status = "already_restored"
                    file_outcome = None
                elif checkpoint_status == "expired":
                    summaries.append("code checkpoint expired")
                    code_status = "unavailable"
                    partial = True
                    file_outcome = None
                else:
                    try:
                        file_outcome = await asyncio.to_thread(
                            self._checkpoint_store.restore,
                            target.workspace_id,
                            include_target=True,
                            retain_target=scope == "code",
                        )
                    except (KeyError, OSError, RuntimeError, ValueError) as exc:
                        await rollback_combined_conversation("code restore failed", exc)
                        raise RewindError(f"code restore failed: {exc}") from exc
                if file_outcome is not None:
                    description = getattr(file_outcome, "summary", "")
                    file_summary = str(description()) if callable(description) else str(description)
                    warnings = tuple(getattr(file_outcome, "warnings", ()))
                    skipped = tuple(getattr(file_outcome, "skipped_paths", ()))
                    if warnings:
                        first = " ".join(str(warnings[0]).split())[:180]
                        more = f" (+{len(warnings) - 1} more)" if len(warnings) > 1 else ""
                        file_summary = f"{file_summary} · {first}{more}"
                    if skipped or warnings:
                        code_status = "partial"
                        partial = True
                    elif tuple(getattr(file_outcome, "restored_paths", ())):
                        code_status = "restored"
                    else:
                        code_status = "unchanged"
                    summaries.append(file_summary)

        combined_code_incomplete = scope == "both" and code_status in {
            "partial",
            "unavailable",
        }
        if combined_code_incomplete and conversation_applied:
            # Keep the conversation and its ledger target intact when code
            # restoration needs intervention. The workspace store retains
            # only unresolved paths, so the same visible checkpoint can be
            # retried after conflicts are fixed. Successfully restored files
            # remain restored and are reported as a partial result.
            await rollback_combined_conversation("code restore was partial")
            summaries.append("conversation kept for retry")

        if (
            scope == "both"
            and not combined_code_incomplete
            and rewind_intent_started
            and self._store is not None
        ):
            try:
                self._store.arm_rewind_intent(initialized.session_id)
            except (OSError, TypeError, ValueError) as exc:
                await rollback_combined_conversation("could not arm durable restore", exc)
                raise RewindError(f"could not arm durable restore: {exc}") from exc

        if scope in {"both", "conversation"} and not combined_code_incomplete:
            assert context is not None
            assert conversation_plan is not None
            assert kept_turns is not None
            if scope == "conversation":
                try:
                    await controller.commit_prepared_restore(
                        conversation_plan,
                        set_messages=context.set_messages,
                    )
                except RewindError:
                    if rewind_intent_started and self._store is not None:
                        self._store.cancel_rewind_intent(initialized.session_id)
                    if visible_intent_staged and self._checkpoint_store is not None:
                        cancel_visible = getattr(
                            self._checkpoint_store,
                            "cancel_visible_reconcile",
                            None,
                        )
                        if callable(cancel_visible):
                            await asyncio.to_thread(cancel_visible)
                        self._workspace_reconcile_pending = False
                    raise
            else:
                controller.confirm_prepared_restore(conversation_plan)
            conversation_committed = True
            # A private intent was staged before context mutation. Save the
            # live context, then reconcile transcript + unique rewind marker
            # as one restart-completable transaction. The intent is removed
            # only after both durable sides succeed.
            incremental_saved = False
            if self._saver is not None:
                try:
                    incremental_saved = bool(await self._saver.force_save())
                except Exception:  # noqa: BLE001 — live restore already committed
                    logger.warning("restored conversation save failed", exc_info=True)
            reconciled = False
            if self._store is not None and rewind_intent_started:
                try:
                    reconciled = self._store.reconcile_rewind_intent(initialized.session_id)
                except (OSError, TypeError, ValueError) as exc:
                    logger.warning("restored conversation reconciliation deferred", exc_info=True)
                    summaries.append(f"persistence warning: {exc} · recovery queued")
                    partial = True
                    self._rewind_recovery_pending = True
                    self._rewind_recovery_disk_reconciled = False
            elif self._store is None and self._saver is not None and not incremental_saved:
                summaries.append("persistence warning: transcript save was not confirmed")
                partial = True
            if reconciled and self._saver is not None:
                # A real IncrementalSaver already advanced through force_save;
                # this assignment also covers a force-save failure followed by
                # successful intent reconciliation without another disk write.
                mark_saved = getattr(self._saver, "mark_saved_message_count", None)
                if callable(mark_saved):
                    mark_saved(len(conversation_plan.messages))
            if reconciled:
                self._rewind_recovery_pending = False
                self._rewind_recovery_disk_reconciled = False
            reconcile_staged = (
                getattr(self._checkpoint_store, "reconcile_staged_visible", None)
                if visible_intent_staged and self._checkpoint_store is not None
                else None
            )
            reconcile_visible = (
                getattr(self._checkpoint_store, "reconcile_visible", None)
                if not visible_intent_staged and self._checkpoint_store is not None
                else None
            )
            if callable(reconcile_staged) and reconciled:
                try:
                    if not await asyncio.to_thread(reconcile_staged):
                        raise RuntimeError("rewind marker is not durable")
                    self._workspace_reconcile_pending = False
                except (OSError, RuntimeError, ValueError) as exc:
                    summaries.append(f"workspace history warning: {exc} · branch cleanup queued")
                    partial = True
                    self._rewind_recovery_pending = True
                    self._rewind_recovery_disk_reconciled = True
            elif callable(reconcile_visible):
                try:
                    await asyncio.to_thread(
                        reconcile_visible,
                        visible_workspace_ids_before,
                    )
                except (OSError, RuntimeError, ValueError) as exc:
                    summaries.append(f"workspace history warning: {exc} · branch cleanup queued")
                    partial = True
            summaries.append(f"conversation before turn {target.turn_id}")

        return CheckpointRestoreOutcome(
            scope=validated_scope,
            summary=" · ".join(part for part in summaries if part) or "nothing changed",
            conversation_restored=conversation_committed,
            code_status=code_status,
            partial=partial,
            prompt_attachments=prompt_attachments if conversation_committed else (),
        )

    async def cleanup(self) -> None:
        if self._attention_push is not None:
            await self._attention_push.cleanup()
            self._attention_push = None
        if self._initialized is not None:
            await self._initialized.cleanup()
            self._initialized = None
        self._live_mcp = None


def _tag_message(outcome: session_manager.TagOutcome, *, verb: str) -> str:
    """Human one-liner for a tag add/remove outcome (the /tag notice text)."""
    if not outcome.ok:
        return outcome.error or "could not update tags"
    now = ", ".join(outcome.tags) if outcome.tags else "(none)"
    if outcome.changed:
        head = "tagged" if verb == "add" else "untagged"
        line = f"{head} · {', '.join(outcome.changed)} · now: {now}"
    elif verb == "add":
        line = f"no new tags · now: {now}"
    else:
        line = f"no matching tags · now: {now}"
    if outcome.rejected:
        line += f" · rejected: {', '.join(outcome.rejected)}"
    return line


def list_sessions(project_dir: Path | None = None) -> list[str]:
    """Session ids stored for this project (newest last)."""
    return SessionStore(project_dir=project_dir).list_sessions()


__all__ = ["RealRuntime", "list_sessions"]
