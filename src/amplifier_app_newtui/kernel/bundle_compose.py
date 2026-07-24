"""In-session overlay composition: mount a deferred bundle on demand.

Fast boot (RCA): a user's ``bundle.app`` list can carry ~18 overlays, each
composed on EVERY session boot; ``bundle.deferred`` (kernel/config.py) holds
the heavy ones back so boot stays quick, and this module composes one of them
into the ALREADY-RUNNING session when the user asks (``/bundle load <name>``).

What newtui controls vs foundation (honest boundary):

- Foundation composes a bundle's full module stack (providers, orchestrator,
  context, tools, hooks, agents) inside ``AmplifierSession.initialize()`` — a
  one-shot step; there is no supported public API to re-run it for an extra
  bundle against a live session.
- What IS supported live is the coordinator's ``loader.load(module_id, …)``
  seam (the same one ``initialize`` drives per module): it returns a mount
  function that instantiates a module and mounts it onto the running
  coordinator. This module drives that seam for the *additive* mount points
  only — ``tools`` / ``hooks`` / ``agents`` — the ones a behavior overlay
  actually contributes.
- Single-slot points (``providers`` / ``orchestrator`` / ``context`` /
  ``module-source-resolver``) are deliberately NOT hot-swapped: replacing the
  live provider or context mid-conversation is not composition, it is a
  session identity change. An overlay that carries them is reported as
  partially composed so the boundary is never hidden — the user can move it
  back to the boot set (undefer) to get it fully at the next session start.

Everything here is duck-typed over the coordinator (``loader.load`` +
``mount``/``hooks``), so it unit-tests with a plain fake — no real session,
no amplifier-core import at module load.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Additive, multi-slot mount points a behavior overlay contributes — safe to
# mount onto a live coordinator. Order is load order (tools before hooks
# before agents mirrors foundation's own mount ordering closely enough for the
# additive set). Single-slot points are intentionally excluded (see module doc).
COMPOSABLE_SECTIONS: tuple[str, ...] = ("tools", "hooks", "agents")

# Mount points an overlay may carry that cannot be hot-swapped into a live
# session — reported, never mounted.
_NON_COMPOSABLE_SECTIONS: tuple[str, ...] = (
    "providers",
    "orchestrator",
    "context",
)


@dataclass
class ComposeResult:
    """Outcome of composing one overlay's additive modules into a session.

    ``cleanups`` are the per-module teardown callables the loader handed back;
    the runtime keeps them so the mounted overlay unwinds with the session
    (mirrors ``InitializedSession.unregister_handles``)."""

    ok: bool
    mounted: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    """Module ids that could not mount (per-module failure) — best-effort
    composition never aborts the whole overlay for one bad module."""
    deferred_sections: tuple[str, ...] = ()
    """Non-composable mount points the overlay carried (providers/context/…);
    named so the "attaches fully at next boot" boundary is explicit."""
    message: str = ""
    cleanups: list[Callable[..., Any]] = field(default_factory=list)

    def summary(self, name: str) -> str:
        """One-line user-facing summary for the load command notice."""
        if self.message:
            return self.message
        parts: list[str] = []
        if self.mounted:
            parts.append(f"{len(self.mounted)} module(s) mounted")
        if self.skipped:
            parts.append(f"{len(self.skipped)} failed")
        if self.deferred_sections:
            parts.append(f"{', '.join(self.deferred_sections)} attach at next session start")
        detail = " · ".join(parts) if parts else "nothing to mount"
        verb = "loaded" if self.ok else "load incomplete"
        return f"{verb} · {name} · {detail}"


def _module_entries(mount_plan: dict[str, Any], section: str) -> list[dict[str, Any]]:
    """The dict-shaped module entries under *section* (junk entries dropped)."""
    raw = mount_plan.get(section)
    if not isinstance(raw, list):
        return []
    return [entry for entry in raw if isinstance(entry, dict) and entry.get("module")]


def _non_composable_present(mount_plan: dict[str, Any]) -> tuple[str, ...]:
    """Non-composable sections the overlay actually carries."""
    return tuple(
        section for section in _NON_COMPOSABLE_SECTIONS if _module_entries(mount_plan, section)
    )


async def _mount_one(
    coordinator: Any, section: str, entry: dict[str, Any]
) -> Callable[..., Any] | None:
    """Instantiate + mount a single overlay module via the loader seam.

    Returns the module's cleanup callable (or ``None`` when it exposes none).
    Raises on failure so the caller can record the module as skipped without
    aborting the rest of the overlay."""
    loader = getattr(coordinator, "loader", None)
    if loader is None or not callable(getattr(loader, "load", None)):
        raise RuntimeError("coordinator exposes no module loader")
    module_id = str(entry["module"])
    config = entry.get("config") if isinstance(entry.get("config"), dict) else {}
    source_hint = entry.get("source")
    # loader.load(...) returns a mount function; awaiting it against the live
    # coordinator performs the actual mount and yields a cleanup callable —
    # the exact contract AmplifierSession.initialize() drives per module.
    mount_fn = loader.load(
        module_id, config=config, source_hint=source_hint, coordinator=coordinator
    )
    result = mount_fn(coordinator)
    if isinstance(result, Awaitable):
        cleanup = await result
    else:
        cleanup = result
    del section  # the loader keys off the module's own declared mount point
    return cleanup if callable(cleanup) else None


async def mount_overlay_modules(coordinator: Any, mount_plan: dict[str, Any]) -> ComposeResult:
    """Mount an overlay's additive modules onto a live coordinator.

    Iterates :data:`COMPOSABLE_SECTIONS` and mounts each module through the
    loader seam (:func:`_mount_one`). Best-effort per module: one module that
    fails to mount is recorded in ``skipped`` and never aborts the rest.
    Non-composable sections the overlay carries are reported in
    ``deferred_sections`` (honest boundary — they attach fully at the next
    boot). ``ok`` is True when at least one module mounted or the overlay had
    nothing composable to mount and nothing failed."""
    mounted: list[str] = []
    skipped: list[str] = []
    cleanups: list[Callable[..., Any]] = []
    for section in COMPOSABLE_SECTIONS:
        for entry in _module_entries(mount_plan, section):
            module_id = str(entry["module"])
            try:
                cleanup = await _mount_one(coordinator, section, entry)
            except Exception:  # noqa: BLE001 — one bad module never aborts the overlay
                logger.warning("overlay module %s failed to mount", module_id, exc_info=True)
                skipped.append(module_id)
                continue
            mounted.append(module_id)
            if cleanup is not None:
                cleanups.append(cleanup)
    deferred_sections = _non_composable_present(mount_plan)
    ok = bool(mounted) or (not skipped and not deferred_sections)
    return ComposeResult(
        ok=ok,
        mounted=tuple(mounted),
        skipped=tuple(skipped),
        deferred_sections=deferred_sections,
        cleanups=cleanups,
    )


__all__ = ["COMPOSABLE_SECTIONS", "ComposeResult", "mount_overlay_modules"]
