"""Pre-takeover mount/provider preflight for the interactive run path (S4/AC4).

Before Textual takes the alternate screen, the interactive launch path
(``amplifier-tui`` bare, and ``run`` with no prompt on a TTY) must know
whether the resolved bundle/provider will actually mount -- a failure
discovered only *after* takeover renders as a corrupted or blank screen with
an error the user cannot read (main.py's ``_interactive_launch`` is the
caller; see ``_run_preflight`` there).

:func:`run_preflight` calls the SAME ``resolve_config`` the real boot calls
(``kernel/runtime.py`` ``RealRuntime.start``): it does not create a
SESSION (``prepared.create_session()``) -- a second, real resolution still
happens for the actual launch -- but it now goes one step further than the
bare mount PLAN and proves the priority provider (the one that will
actually serve the first turn) really works. That mirrors the ``reset``
command's own ``--dry-run`` shape, which always computes the plan via
``run_reset(dry_run=True)`` before deciding whether to act on it.

One deliberate difference from the real boot's call: ``install_deps=False``.
Measured against a realistic, fully-populated bundle (the shared ``anchors``
roster tui composes by default), ``resolve_config(..., install_deps=True)``
(the default) costs ~0.6-0.9s PER MODULE -- foundation's ``ModuleActivator``
shells out to install/verify each module's Python dependencies even when
already satisfied -- which on a real bundle totals tens of seconds. Preflight
running an EXTRA full ``install_deps=True`` pass before the real boot's own
would roughly double that cost, squarely violating "preflight must not
meaningfully delay normal startup". ``install_deps=False`` skips only that
per-module dependency install/verify step; module SOURCE resolution
(``self._resolver.resolve(source_uri)`` -- the part that actually fails for a
bad ``--bundle``/unreachable module source) still runs, so the bundle/mount-plan
composition this preflight exists to validate is unchanged. It also adds no
NEW network calls beyond what a normal boot already makes: the real launch's
own ``resolve_config`` call, right after, still installs deps for real with
its default ``install_deps=True`` -- this preflight only skips redoing that
specific (already redundant, since the previous boot most likely satisfied it)
work a second time.

Scope: the PLAN half (bundle/mount-plan composition, the "zero providers
configured" hard-fail) catches a bad ``--bundle``, an unreachable module
source, or an unknown ``--provider`` override -- the same condition
``session_factory.MountReport.no_provider`` would raise
``ProviderMountError`` for once mounting is attempted for real.

The REALITY half (``kernel/preflight_verify.py``, AC4's follow-up) closes
the gap that scope note used to describe: a plan that *resolves* but whose
priority provider cannot actually mount, whose credentials are missing, or
whose selected model does not exist. It runs three checks against the
priority provider -- real mounting, credential viability, and
selected-model availability -- and is deliberately scoped to that ONE
provider (the one that determines whether the first turn works at all);
see ``preflight_verify``'s module docstring for what each check does, the
offline/network boundary, and why an import failure can degrade instead of
blocking. Provider/credential UX at CONFIGURATION time remains covered by
``init`` (#180/#184/#186); this is the launch-time proof that what ``init``
configured still actually works.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import (
    BundleNotFoundError,
    ProviderNotConfiguredError,
    provider_priority,
    resolve_config,
    routing_enabled,
)
from .preflight_verify import DEFAULT_LIVE_TIMEOUT, verify_provider


@dataclass(frozen=True)
class PreflightReport:
    """What preflight learned about the mounts/providers a launch would use.

    ``ok`` is the single gate the CLI acts on. The rest is display data: the
    ``bundle_*``/``provider``/``model``/``*_count``/``routing_enabled`` fields
    feed the ``--dry-run`` "would launch" table on success; ``error`` +
    ``remediation`` feed the plain-terminal failure notice on either path.
    """

    ok: bool
    bundle_name: str = ""
    bundle_uri: str = ""
    provider: str = ""
    model: str = ""
    provider_count: int = 0
    tool_count: int = 0
    routing_enabled: bool = False
    error: str | None = None
    remediation: str | None = None


def _priority_provider_entry(mount_plan: dict[str, Any]) -> dict[str, Any] | None:
    """The RAW mount-plan entry for the provider that would serve the turn.

    Same LOWEST-``config.priority`` rule as ``runtime._provider_and_model``
    (kept as a small local copy rather than a cross-module private import --
    see :func:`_priority_provider` for the full rationale); list position is
    not it. Returns the entry dict itself (not display strings) so callers
    can read its real ``module``/``config`` fields -- see
    :func:`_priority_provider` for the derived-display-string version.
    """
    entries = [entry for entry in (mount_plan.get("providers") or []) if isinstance(entry, dict)]
    if not entries:
        return None
    return min(entries, key=provider_priority)


def _priority_provider(mount_plan: dict[str, Any]) -> tuple[str, str]:
    """The provider (and its model) that would actually serve the turn.

    Display-string form of :func:`_priority_provider_entry` for the
    ``--dry-run`` table / report fields.
    """
    entry = _priority_provider_entry(mount_plan)
    if entry is None:
        return ("", "")
    module_id = str(entry.get("id") or entry.get("module") or "")
    provider = module_id.replace("provider-", "").replace("amplifier-module-", "")
    config = entry.get("config") if isinstance(entry.get("config"), dict) else {}
    model = str((config or {}).get("default_model", ""))
    return (provider, model)


def _preflight_settings(settings: dict[str, Any]) -> tuple[bool, bool]:
    """``(verify_provider, verify_live)`` from the ``preflight`` settings block.

    Both default to this feature's own defaults (fast checks on, live
    models-list probe off) so an unconfigured app gets exactly the
    behaviour described in ``preflight_verify``'s module docstring. Junk
    shapes fall back to the defaults -- never raises, never blocks startup
    on a settings typo. ``verify_provider`` is an escape hatch for the real-
    mount/credential checks (both fast/offline by design, but a kill switch
    costs nothing and this codebase already favours them, e.g.
    ``routing.enabled``, ``hooks.suppress``); ``verify_live`` opts an
    installation into the networked models-list probe on EVERY launch,
    not just ``--dry-run``.
    """
    section = settings.get("preflight")
    if not isinstance(section, dict):
        return True, False
    verify_provider_setting = section.get("verify_provider", True)
    verify_live_setting = section.get("verify_live", False)
    return (
        verify_provider_setting if isinstance(verify_provider_setting, bool) else True,
        verify_live_setting if isinstance(verify_live_setting, bool) else False,
    )


async def run_preflight(
    bundle: str | None,
    *,
    project_dir: Path | None = None,
    provider_override: str | None = None,
    model_override: str | None = None,
    verify_live: bool = False,
    strict: bool = False,
) -> PreflightReport:
    """Resolve mounts/providers for *bundle* and prove the priority provider works.

    Never raises: every failure mode ``resolve_config`` can hit today (and
    would otherwise surface mid- or post-takeover) comes back as a
    :class:`PreflightReport` with ``ok=False`` and an actionable remediation.
    Does NOT create a session (``prepared.create_session()``); it mounts
    only the ONE priority provider, into a disposable coordinator, purely to
    prove it works (see ``preflight_verify``), and always cleans up after
    itself.

    ``verify_live`` opts into the networked models-list check (see
    ``preflight_verify`` module docstring). A ``preflight.verify_live: true``
    setting has the same effect on every launch. ``strict`` is the bounded,
    fail-closed diagnostic tier used for an explicit model override,
    ``--dry-run``, and ``doctor``: it forces that live check, refuses an
    inconclusive dependency import, and cannot be disabled by the normal
    ``preflight.verify_provider`` startup escape hatch.
    """
    try:
        resolved = await resolve_config(
            bundle,
            project_dir=project_dir,
            provider_override=provider_override,
            model_override=model_override,
            # See module docstring: skips only the per-module dependency
            # install/verify step (measured ~0.6-0.9s/module on a realistic
            # bundle) -- module source resolution still runs, so a bad
            # --bundle or unreachable module source still fails here.
            install_deps=False,
        )
    except BundleNotFoundError as error:
        return PreflightReport(
            ok=False,
            error=f"bundle not found: {error}",
            remediation="check --bundle name/path, or run `amplifier-tui bundle list`",
        )
    except ProviderNotConfiguredError as error:
        return PreflightReport(
            ok=False,
            error=str(error),
            remediation="run `amplifier-tui provider list` to see configured providers",
        )
    except Exception as error:  # noqa: BLE001 -- fail closed, pre-takeover beats a raw traceback after
        return PreflightReport(
            ok=False,
            error=f"failed to resolve mounts: {error}",
            remediation="run `amplifier-tui doctor` for a full diagnosis",
        )

    providers = [p for p in (resolved.mount_plan.get("providers") or []) if isinstance(p, dict)]
    tools = [t for t in (resolved.mount_plan.get("tools") or []) if isinstance(t, dict)]
    if not providers:
        return PreflightReport(
            ok=False,
            bundle_name=resolved.bundle_name,
            bundle_uri=resolved.bundle_uri,
            error="no provider configured",
            remediation="run `amplifier-tui init` to configure a provider",
        )

    provider_name, model_name = _priority_provider(resolved.mount_plan)

    # AC4 follow-up: the PLAN resolving above proves the bundle/provider
    # COULD mount; this proves the priority provider (the one that will
    # actually serve the first turn) really does -- see
    # preflight_verify's module docstring for what each check covers, the
    # offline/network boundary, and the import-failure degrade rule.
    settings_verify_provider, settings_verify_live = _preflight_settings(resolved.settings)
    entry = _priority_provider_entry(resolved.mount_plan)
    if entry is not None and (settings_verify_provider or strict):
        module_id = str(entry.get("module") or "")
        entry_config = entry.get("config") if isinstance(entry.get("config"), dict) else {}
        verification = await verify_provider(
            module_id=module_id,
            config=dict(entry_config or {}),
            model=model_name,
            live_verify=strict or verify_live or settings_verify_live,
            strict=strict,
            live_timeout=DEFAULT_LIVE_TIMEOUT,
        )
        if not verification.ok:
            return PreflightReport(
                ok=False,
                bundle_name=resolved.bundle_name,
                bundle_uri=resolved.bundle_uri,
                error=verification.error,
                remediation=verification.remediation,
            )

    return PreflightReport(
        ok=True,
        bundle_name=resolved.bundle_name,
        bundle_uri=resolved.bundle_uri,
        provider=provider_name,
        model=model_name,
        provider_count=len(providers),
        tool_count=len(tools),
        routing_enabled=routing_enabled(resolved.settings),
    )


__all__ = ["PreflightReport", "run_preflight"]
