"""Real provider verification for preflight (S4 AC4 follow-up).

:mod:`kernel.preflight` proves the mount/provider PLAN resolves. This module
proves the PRIORITY provider (the one that will actually serve the first
turn -- ``preflight._priority_provider_entry``) does three additional,
concrete things before Textual takes the screen:

1. **Real provider mounting** -- the module actually imports and its
   ``mount()`` produces an object satisfying ``amplifier_core.interfaces
   .Provider`` (:func:`verify_provider`, via the mount step below).
2. **Credential viability** -- every environment variable the provider's
   OWN ``get_info()`` declares (``ProviderInfo.credential_env_vars`` --
   authoritative, not a guessed ``<X>_API_KEY`` convention) is present and
   non-blank (:func:`_check_credentials`).
3. **Selected-model availability** -- the configured/default model is at
   least a real, non-blank selection by default; whether it actually
   EXISTS for the provider is verified only when asked
   (:func:`_check_model`).

Offline/network boundary (read this before changing the defaults)
-------------------------------------------------------------------
Checks 1 and 2 are **always on and offline**: importing a module and
calling its ``mount()`` touches no network (providers construct SDK
clients lazily -- the wire is only touched on the first real
``complete()``/``list_models()`` call), and checking ``os.environ`` for a
variable NAME is a dict lookup. Both run on every launch without
measurably slowing it down.

Check 3 has a genuine offline/online split. Confirming a model STRING
was selected is free; confirming it EXISTS for the provider means asking
the provider (``Provider.list_models()``, whose own docstring says the
provider may implement it via "API query, hardcoded list, cached
response, etc." -- so from preflight's vantage point it must be treated
as potentially networked). ``kernel.setup.list_provider_models`` already
treats it exactly this way (a mandatory timeout, "app-cli has none, but a
TUI must not sit on a wedged socket"). So: the static/default tier never
calls it; the live tier does, opt-in only (``live_verify=True``, wired to
``--dry-run`` -- see ``kernel/preflight.py``), bounded by ``live_timeout``.
A live check also doubles as an authentic credential-acceptance probe (an
invalid key surfaces as a real 401 from ``list_models()``), so there is
no separate networked "is this key accepted" probe for check 2 -- it
would just re-pay the same round trip for the same signal.

The import-failure boundary
----------------------------
A provider module can fail to import for two very different reasons that
look identical from the outside:

* its OWN third-party dependency (the SDK) is not pip-installed yet,
  because this preflight -- like the real boot's OWN prior resolution
  step -- deliberately runs with ``install_deps=False`` (see
  ``kernel/preflight.py`` module docstring); the very next thing that
  happens on a successful preflight is the real launch's
  ``resolve_config(..., install_deps=True)``, which installs it for real.
  Hard-failing here would block a launch that was about to self-heal.
* the module (or its bundle source) is simply broken/misconfigured --
  exactly the silent, post-takeover failure this whole feature exists to
  catch.

:func:`_degrades_gracefully` tells these apart using
``ModuleNotFoundError.name``: only a MISSING TRANSITIVE dependency (not
the bundle module's own top-level package) degrades. This mirrors
``kernel.setup.ensure_provider_available``, which resolves the identical
ambiguity in the onboarding wizard by *not* installing and tolerating
"not available" rather than blocking ("app-cli shells out to
``uv pip install -e``; persisting ``source:`` ... is enough, because the
next session boot has foundation install it properly").

Never leak a secret
--------------------
This module never logs or echoes a credential VALUE. Credential checks
report only environment variable NAMES + a presence boolean
(:func:`_check_credentials`). Any arbitrary text from provider-raised
exceptions is scrubbed of config values that look like secrets
(:func:`_secret_values` / :func:`_scrub`) before it can reach a
:class:`ProviderVerification.error`, which is the only thing that ever
reaches a log line, the plain-terminal failure notice, or the
``--dry-run`` report.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import os
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_LIVE_TIMEOUT = 15.0
"""Seconds before a live ``list_models()`` probe gives up (matches
``kernel.setup.list_provider_models``'s own bound for the identical
provider call, same rationale: never sit on a wedged socket)."""

_SECRET_KEY_HINTS = ("key", "token", "secret", "password", "credential")
"""Config-field NAME hints treated as secret-valued for scrubbing.

Keyed on name shape, not a fixed field list -- providers vary
(``api_key``, ``subscription_key``, ``token``, ...) and a false positive
here (scrubbing an ordinary value) is far cheaper than a false negative
(a real credential leaking into an error message)."""


@dataclass(frozen=True)
class ProviderVerification:
    """Outcome of verifying ONE real provider mount + its credentials/model.

    Deliberately the same shape as ``PreflightReport``'s failure fields
    (``error``/``remediation``) so ``run_preflight`` can plug a failure in
    directly -- no new user-facing surface, per AC4's "keep the existing
    behaviour on failure".
    """

    ok: bool
    error: str | None = None
    remediation: str | None = None


_DIAGNOSE_REMEDIATION = "run `amplifier-tui doctor` for a full diagnosis"


def _secret_values(config: dict[str, Any]) -> tuple[str, ...]:
    """Config values that look like secrets (see module docstring)."""
    values: list[str] = []
    for key, value in config.items():
        if (
            isinstance(value, str)
            and value.strip()
            and any(hint in key.lower() for hint in _SECRET_KEY_HINTS)
        ):
            values.append(value)
    return tuple(values)


def _scrub(text: str, secrets: tuple[str, ...]) -> str:
    """Redact any known secret VALUE out of arbitrary (provider-raised) text."""
    for secret in secrets:
        text = text.replace(secret, "***")
    return text


def _implements_provider_protocol(obj: Any) -> bool:
    """Structural check for ``amplifier_core.interfaces.Provider``.

    ``Provider`` is a ``Protocol``, not ``@runtime_checkable``, so
    ``isinstance()`` cannot be used here -- mirrors the exact hasattr/
    callable check ``amplifier_core.validation.provider`` uses for the
    same cross-Python-version-safety reason.
    """
    return (
        hasattr(obj, "name")
        and callable(getattr(obj, "get_info", None))
        and callable(getattr(obj, "list_models", None))
        and callable(getattr(obj, "complete", None))
        and callable(getattr(obj, "parse_tool_calls", None))
    )


def _import_provider_module(module_id: str) -> Any:
    """Import an already-source-resolved bundle module.

    Uses the SAME filesystem-import convention amplifier_core's own
    loader uses for a bundle-sourced module
    (``amplifier_core.loader.ModuleLoader._load_filesystem``):
    ``amplifier_module_<id-with-underscores>``, relying on the module's
    directory already being on ``sys.path`` -- ``resolve_config()``'s
    ``ModuleActivator.activate()`` inserts it unconditionally, even with
    ``install_deps=False`` (see ``kernel/preflight.py``). This is not a
    parallel/guessed mechanism; it is the exact one the real boot falls
    back to for filesystem-sourced modules, so success here means the
    real launch would import it the identical way.
    """
    name = f"amplifier_module_{module_id.replace('-', '_')}"
    return importlib.import_module(name)


def _degrades_gracefully(module_id: str, error: BaseException) -> bool:
    """See module docstring "The import-failure boundary"."""
    if not isinstance(error, ImportError):
        return False
    expected = f"amplifier_module_{module_id.replace('-', '_')}"
    missing = getattr(error, "name", None)
    if not missing:
        return False  # can't attribute the failure -- surface it, don't guess
    is_the_bundle_module_itself = missing == expected or missing.startswith(f"{expected}.")
    return not is_the_bundle_module_itself


def _mounted_provider_instance(coordinator: Any, mount_result: Any) -> Any | None:
    """The provider instance a ``mount()`` call produced, if any.

    Two shapes a module may use (mirrors ``amplifier_core.validation
    .provider``'s own tolerance): registered onto the coordinator's
    ``providers`` mount point, or returned directly from ``mount()``.
    """
    providers = coordinator.mount_points.get("providers") or {}
    if providers:
        return next(iter(providers.values()))
    if _implements_provider_protocol(mount_result):
        return mount_result
    return None


async def _call_maybe_async(fn: Any, *args: Any) -> Any:
    """Call *fn* and await the result if it's awaitable.

    Provider mount()/cleanup callables are conventionally async, but this
    tolerates a sync one too -- the same call-then-check-awaitable idiom
    ``kernel.setup.list_provider_models`` already uses for a provider's
    (possibly sync) ``list_models``. Routing the call through a plain
    ``Any``-typed parameter also sidesteps a pyright quirk: narrowing a
    value via ``callable()`` synthesizes a ``(...) -> object`` signature
    whose return isn't recognized as awaitable, but that narrowing does
    not cross this function boundary.
    """
    result = fn(*args)
    if inspect.isawaitable(result):
        return await result
    return result


async def _cleanup(coordinator: Any, mount_result: Any) -> None:
    """Tear down whatever mounting created. Never raises."""
    if callable(mount_result):
        try:
            await _call_maybe_async(mount_result)
        except Exception:  # noqa: BLE001 -- cleanup must never mask the real verdict
            logger.debug("preflight: provider cleanup callable failed", exc_info=True)
    try:
        await coordinator.cleanup()
    except Exception:  # noqa: BLE001 -- same as above
        logger.debug("preflight: coordinator cleanup failed", exc_info=True)


def _check_credentials(module_id: str, instance: Any) -> ProviderVerification:
    """Check 2: credential presence + well-formed (offline, always on).

    "Well-formed" here means non-blank -- see module docstring for why a
    deeper per-provider format check (key prefixes/lengths) is out of
    scope: it is provider-specific, brittle, and the question that
    actually matters ("does the server accept it?") is inherently a
    network question, already covered by check 3's opt-in live tier.
    """
    try:
        info = instance.get_info()
    except Exception:  # noqa: BLE001 -- can't introspect; degrade, don't block
        logger.debug(
            "preflight: %s get_info() failed; skipping credential check",
            module_id,
            exc_info=True,
        )
        return ProviderVerification(ok=True)

    required = [str(v) for v in (getattr(info, "credential_env_vars", None) or []) if v]
    missing = [v for v in required if not os.environ.get(v, "").strip()]
    if not missing:
        return ProviderVerification(ok=True)
    return ProviderVerification(
        ok=False,
        error=f"provider '{module_id}' is missing credentials: {', '.join(missing)} not set",
        remediation="run `amplifier-tui init` to configure a provider, or set the variable(s) named above",
    )


async def _check_model(
    module_id: str,
    instance: Any,
    model: str,
    *,
    live_verify: bool,
    live_timeout: float,
    secrets: tuple[str, ...],
) -> ProviderVerification:
    """Check 3: selected-model availability -- see module docstring boundary.

    Static/default tier: a blank ``model`` means the provider's own
    default applies (nothing to check); a non-blank one is, by
    definition, a real selection -- confirming it EXISTS requires asking
    the provider, which only the live tier does.
    """
    if not model or not live_verify:
        return ProviderVerification(ok=True)

    try:
        models = await asyncio.wait_for(instance.list_models(), timeout=live_timeout)
    except TimeoutError:
        return ProviderVerification(
            ok=False,
            error=f"provider '{module_id}' timed out listing models after {live_timeout:g}s",
            remediation="check network connectivity to the provider, or drop --dry-run to skip this check",
        )
    except Exception as error:  # noqa: BLE001 -- report it; the user explicitly opted into this probe
        return ProviderVerification(
            ok=False,
            error=f"provider '{module_id}' could not list models: {_scrub(str(error), secrets)}",
            remediation="verify the credential is valid and the endpoint is reachable",
        )

    known = {str(getattr(item, "id", "")) for item in (models or [])}
    known.discard("")
    if known and model not in known:
        preview = ", ".join(sorted(known)[:8]) + ("..." if len(known) > 8 else "")
        return ProviderVerification(
            ok=False,
            error=f"model '{model}' is not available for provider '{module_id}' (known: {preview})",
            remediation="run `amplifier-tui provider list`, or pick one of the models named above",
        )
    return ProviderVerification(ok=True)


async def verify_provider(
    *,
    module_id: str,
    config: dict[str, Any],
    model: str,
    live_verify: bool = False,
    live_timeout: float = DEFAULT_LIVE_TIMEOUT,
) -> ProviderVerification:
    """Mount *module_id* for real and run all three checks against it.

    Never raises: every failure mode comes back as a
    :class:`ProviderVerification` with ``ok=False`` and an actionable
    remediation, or ``ok=True`` when the check is inconclusive and
    degrades (see module docstring). Whatever mounting creates is always
    cleaned up (:func:`_cleanup`), even on failure.
    """
    try:
        module = _import_provider_module(module_id)
    except Exception as error:  # noqa: BLE001 -- classify below, never propagate
        if _degrades_gracefully(module_id, error):
            logger.debug(
                "preflight: %s not importable yet (%s); deferring to the real launch's install pass",
                module_id,
                error,
            )
            return ProviderVerification(ok=True)
        return ProviderVerification(
            ok=False,
            error=f"provider '{module_id}' module failed to import: {error}",
            remediation=_DIAGNOSE_REMEDIATION,
        )

    mount_fn = getattr(module, "mount", None)
    if not callable(mount_fn):
        return ProviderVerification(
            ok=False,
            error=f"provider '{module_id}' has no mount() function",
            remediation=_DIAGNOSE_REMEDIATION,
        )

    from amplifier_core.testing import MockCoordinator  # lazy: offline-safe import graph

    secrets = _secret_values(config)
    coordinator = MockCoordinator()
    mount_result: Any = None
    try:
        mount_result = await _call_maybe_async(mount_fn, coordinator, dict(config))
        instance = _mounted_provider_instance(coordinator, mount_result)
        if instance is None or not _implements_provider_protocol(instance):
            return ProviderVerification(
                ok=False,
                error=f"provider '{module_id}' mounted but does not satisfy the Provider protocol",
                remediation=_DIAGNOSE_REMEDIATION,
            )

        credential_check = _check_credentials(module_id, instance)
        if not credential_check.ok:
            return credential_check

        return await _check_model(
            module_id,
            instance,
            model,
            live_verify=live_verify,
            live_timeout=live_timeout,
            secrets=secrets,
        )
    except Exception as error:  # noqa: BLE001 -- fail closed with a sanitized message
        return ProviderVerification(
            ok=False,
            error=f"provider '{module_id}' failed to mount: {_scrub(str(error), secrets)}",
            remediation=_DIAGNOSE_REMEDIATION,
        )
    finally:
        await _cleanup(coordinator, mount_result)


__all__ = [
    "DEFAULT_LIVE_TIMEOUT",
    "ProviderVerification",
    "verify_provider",
]
