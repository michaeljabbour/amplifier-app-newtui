"""Single entry point for session creation: ``create_initialized_session``.

Canonical initialization order (ported from amplifier-app-cli-flagship
``session_runner.py``, the cleanest reference implementation):

1. Mint the session id (or accept the caller's, e.g. on resume).
2. Stamp root session metadata into the mount plan (guards: fill only
   missing keys, so child sessions inherit parent values untouched).
3. ``prepared.create_session(...)`` — foundation handles module mounting.
4. Register ``session.spawn`` / ``session.resume`` capabilities —
   **after** ``create_session``, **before** ``execute`` (the timing bug
   documented in foundation's APPLICATION_INTEGRATION_GUIDE).
5. Verify mounted providers/tools against the mount plan
   (ADR-0007 resolution 12): missing provider → hard fail with a doctor
   pointer; missing tools → start degraded with a notice.
6. Restore the transcript on resume (preserving the fresh system prompt
   that ``create_session`` already injected).

This module imports amplifier-core types only under ``TYPE_CHECKING`` so
its logic is unit-testable with fakes — no API keys, no network.
"""

from __future__ import annotations

import logging
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import ResolvedConfig, get_project_slug

if TYPE_CHECKING:
    from amplifier_core import AmplifierSession

logger = logging.getLogger(__name__)

APPLICATION_HOST = "Amplifier NewTUI"

SPAWN_CAPABILITY = "session.spawn"
RESUME_CAPABILITY = "session.resume"


class ProviderMountError(RuntimeError):
    """A configured provider failed to mount — the session cannot run.

    Hard-fail policy per ADR-0007 resolution 12: a session with no
    working provider silently does nothing, so failing loudly with a
    doctor pointer beats limping.
    """


# --------------------------------------------------------------------------
# Request / result dataclasses
# --------------------------------------------------------------------------


@dataclass
class SessionRequest:
    """Everything :func:`create_initialized_session` needs.

    ``spawn_capability`` / ``resume_capability`` are the app's subagent
    entry points (see ``kernel/spawner.py``); they are registered on the
    coordinator in step 4. ``initial_transcript`` present ⇒ resume.
    """

    resolved: ResolvedConfig
    session_id: str | None = None
    approval_system: Any = None
    display_system: Any = None
    session_cwd: Path | None = None
    initial_transcript: list[dict[str, Any]] | None = None
    spawn_capability: Callable[..., Awaitable[dict[str, Any]]] | None = None
    resume_capability: Callable[..., Awaitable[dict[str, Any]]] | None = None

    @property
    def is_resume(self) -> bool:
        return self.initial_transcript is not None


@dataclass(frozen=True)
class MountReport:
    """Result of verifying mounted modules against the mount plan."""

    missing_providers: tuple[str, ...] = ()
    mounted_provider_count: int = 0
    missing_tools: tuple[str, ...] = ()
    configured_tool_count: int = 0
    mounted_tool_count: int = 0

    @property
    def no_provider(self) -> bool:
        """True only when NOT ONE provider mounted — the fatal case. A
        partial failure (some providers up, some down — e.g. a local vLLM
        endpoint offline while Anthropic is fine) degrades, never blocks."""
        return self.mounted_provider_count == 0

    @property
    def tools_degraded(self) -> bool:
        return bool(self.missing_tools) or self.mounted_tool_count < self.configured_tool_count

    def degraded_notice(self) -> str | None:
        """The blocking transcript notice for a degraded start (a provider
        or tool module that failed to mount while the session still ran)."""
        parts: list[str] = []
        if self.missing_providers:
            parts.append(f"provider(s) unavailable: {', '.join(self.missing_providers)}")
        if self.missing_tools:
            parts.append(f"tool modules failed to mount: {', '.join(self.missing_tools)}")
        elif self.mounted_tool_count < self.configured_tool_count:
            parts.append(
                f"{self.mounted_tool_count}/{self.configured_tool_count} tool modules mounted"
            )
        if not parts:
            return None
        return f"degraded start · {' · '.join(parts)} · run doctor for details"


@dataclass
class InitializedSession:
    """A fully initialized session, ready for ``execute()``."""

    session: "AmplifierSession"
    session_id: str
    resolved: ResolvedConfig
    mount_report: MountReport
    unregister_handles: list[Callable[[], None]] = field(default_factory=list)

    @property
    def coordinator(self) -> Any:
        return self.session.coordinator

    @property
    def degraded_notice(self) -> str | None:
        return self.mount_report.degraded_notice()

    async def cleanup(self) -> None:
        """Unregister app hooks, then tear the session down."""
        for unregister in reversed(self.unregister_handles):
            try:
                unregister()
            except Exception:  # noqa: BLE001 — cleanup must never cascade
                logger.debug("unregister handle failed during cleanup", exc_info=True)
        self.unregister_handles.clear()
        await self.session.cleanup()


# --------------------------------------------------------------------------
# Pure helpers (unit-tested directly)
# --------------------------------------------------------------------------


def stamp_root_metadata(
    config: dict[str, Any],
    *,
    session_id: str,
    bundle_name: str,
    project_dir: Path,
) -> None:
    """Stamp root session metadata into *config* (fill-only guards).

    Child sessions inherit these via config deep-merge; the guards
    ensure a child never overwrites its root's values.
    """
    cwd = str(project_dir.resolve())
    config["working_dir"] = cwd
    config.setdefault("root_session_id", session_id)
    config.setdefault("application_host", APPLICATION_HOST)
    config.setdefault("bundle_name", bundle_name)
    config.setdefault("project_slug", get_project_slug(project_dir))
    config.setdefault("project_dir", cwd)
    config.setdefault("project_name", project_dir.name)


def _normalize_provider_name(module_id: str) -> str:
    """``provider-anthropic`` → ``anthropic`` (mounted-key convention)."""
    return module_id.removeprefix("provider-")


def _configured_ids(mount_plan: dict[str, Any], section: str) -> list[str]:
    ids: list[str] = []
    for entry in mount_plan.get(section) or []:
        if isinstance(entry, dict):
            ids.append(
                str(entry.get("instance_id") or entry.get("id") or entry.get("module") or "")
            )
        else:
            ids.append(str(entry))
    return [i for i in ids if i]


def verify_mounts(mount_plan: dict[str, Any], coordinator: Any) -> MountReport:
    """Compare mounted providers/tools against the mount plan.

    Providers are matched by normalized name with multiplicity (Counter,
    so multi-instance configs are counted accurately). Tool modules
    cannot be mapped 1:1 to mounted tool names (one module registers
    many tools), so tools report *complete* failure by module list and
    partial failure by count only.
    """
    configured_providers = _configured_ids(mount_plan, "providers")
    mounted_providers: dict[str, Any] = coordinator.get("providers") or {}
    configured_counts = Counter(_normalize_provider_name(p) for p in configured_providers)
    mounted_counts = Counter(_normalize_provider_name(str(k)) for k in mounted_providers)
    missing_providers = tuple(sorted((configured_counts - mounted_counts).keys()))

    configured_tools = _configured_ids(mount_plan, "tools")
    mounted_tools: dict[str, Any] = coordinator.get("tools") or {}
    missing_tools: tuple[str, ...] = ()
    if configured_tools and not mounted_tools:
        missing_tools = tuple(configured_tools)

    return MountReport(
        missing_providers=missing_providers,
        mounted_provider_count=len(mounted_providers),
        missing_tools=missing_tools,
        configured_tool_count=len(configured_tools),
        mounted_tool_count=len(mounted_tools),
    )


async def _restore_transcript(session: Any, transcript: list[dict[str, Any]]) -> None:
    """Restore a resumed transcript, preserving the fresh system prompt.

    ``create_session()`` already injected a fresh system prompt from the
    bundle; old transcripts may have lost theirs to compaction, so if the
    restored messages carry no system message we re-inject the fresh one.
    """
    context = session.coordinator.get("context")
    if context is None or not hasattr(context, "set_messages"):
        logger.warning("Context module lacks set_messages — transcript NOT restored")
        return

    fresh_system_msg: dict[str, Any] | None = None
    if hasattr(context, "get_messages"):
        current = await context.get_messages()
        for message in current:
            if message.get("role") == "system":
                fresh_system_msg = message
                break

    await context.set_messages(transcript)
    logger.info("Restored %d messages from transcript", len(transcript))

    if fresh_system_msg is not None:
        restored = await context.get_messages()
        if not any(m.get("role") == "system" for m in restored):
            logger.warning("Transcript missing system prompt — re-injecting from bundle")
            await context.set_messages([fresh_system_msg, *restored])


# --------------------------------------------------------------------------
# The factory
# --------------------------------------------------------------------------


async def create_initialized_session(request: SessionRequest) -> InitializedSession:
    """Create and fully initialize a session (see module docstring).

    Raises:
        ProviderMountError: If any configured provider failed to mount.
    """
    resolved = request.resolved
    prepared = resolved.prepared

    # Step 1: session identity.
    session_id = request.session_id or str(uuid.uuid4())

    # Step 2: root metadata into the mount plan (pre-create so foundation
    # copies it into the coordinator config).
    stamp_root_metadata(
        resolved.mount_plan,
        session_id=session_id,
        bundle_name=resolved.bundle_name,
        project_dir=resolved.project_dir,
    )

    # Step 3: create the session (foundation mounts modules + resolver).
    session = await prepared.create_session(
        session_id=session_id,
        approval_system=request.approval_system,
        display_system=request.display_system,
        session_cwd=request.session_cwd or resolved.project_dir,
        is_resumed=request.is_resume,
    )

    # Belt-and-suspenders: foundation may copy the config into a fresh
    # dict for the coordinator; hooks read coordinator.config, so stamp
    # there too (fill-only, same guards).
    stamp_root_metadata(
        session.config,
        session_id=resolved.mount_plan.get("root_session_id", session_id),
        bundle_name=resolved.bundle_name,
        project_dir=resolved.project_dir,
    )

    # Step 4: spawn/resume capabilities — AFTER create_session, BEFORE
    # any execute() (integration-guide timing contract).
    if request.spawn_capability is not None:
        session.coordinator.register_capability(SPAWN_CAPABILITY, request.spawn_capability)
    if request.resume_capability is not None:
        session.coordinator.register_capability(RESUME_CAPABILITY, request.resume_capability)

    # Step 5: verify mounted modules vs the plan. A session needs at least
    # ONE working provider — but a partial failure (a configured provider
    # down while another is up, e.g. an offline local vLLM endpoint next to
    # Anthropic) must degrade, not block. Only zero mounted providers is
    # fatal; missing providers otherwise ride the degraded notice.
    report = verify_mounts(resolved.mount_plan, session.coordinator)
    if report.no_provider:
        try:
            await session.cleanup()
        except Exception:  # noqa: BLE001 — surface the mount error, not cleanup noise
            logger.debug("session cleanup failed after provider mount failure", exc_info=True)
        detail = (
            f"Provider module(s) failed to mount: {', '.join(report.missing_providers)}. "
            if report.missing_providers
            else "No provider configured. "
        )
        raise ProviderMountError(
            detail + "The session cannot run without a provider — check credentials "
            "and module install state (run `amplifier-newtui doctor`)."
        )
    if report.missing_providers or report.tools_degraded:
        logger.warning("%s", report.degraded_notice())

    # Step 6: restore transcript on resume.
    if request.is_resume and request.initial_transcript:
        await _restore_transcript(session, request.initial_transcript)

    return InitializedSession(
        session=session,
        session_id=session_id,
        resolved=resolved,
        mount_report=report,
    )


__all__ = [
    "APPLICATION_HOST",
    "RESUME_CAPABILITY",
    "SPAWN_CAPABILITY",
    "InitializedSession",
    "MountReport",
    "ProviderMountError",
    "SessionRequest",
    "create_initialized_session",
    "stamp_root_metadata",
    "verify_mounts",
]
