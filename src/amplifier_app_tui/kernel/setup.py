"""First-run setup: the logic behind ``amplifier-tui init``.

amplifier-app-cli's ``init`` is an interactive provider/routing dashboard
built on its own ``ProviderManager`` / ``KeyManager`` (app-cli-internal,
not shared). tui reuses the two shared pieces:

- **provider discovery** via ``amplifier_core.loader.ModuleLoader`` — the
  same loader app-cli's ``ProviderManager`` drives; and
- the **credential convention** the providers actually read: a
  ``provider-<x>`` module keys off ``<X>_API_KEY`` (+ optional
  ``<X>_BASE_URL``) in ``~/.amplifier/keys.env`` — verified against the
  packaged bundle (anthropic reads ``ANTHROPIC_API_KEY``) and the live
  keys.env. That is the onboarding this covers: get a provider's key
  stored so the default bundle works.

Key writing mirrors ``KeyManager.save_key`` (atomic write, ``chmod 600``,
``os.environ`` update). Pure file/dict work — unit-tested against a
``tmp_path`` keys file; only :func:`discover_providers`,
:func:`ensure_provider_available` and :func:`list_provider_models` touch
amplifier or the network.

Beyond the one-key convention above, this module also carries the pieces
``init`` needs to configure a provider the way app-cli does — from the
provider's OWN declared schema rather than a table baked in here:

- :data:`PROVIDER_SOURCES` — the module catalog, so a provider can be
  offered before it is installed (entry-point discovery alone cannot see
  an uninstalled module, which is why vLLM was missing from the picker on
  a fresh machine);
- :func:`load_provider_info` — ``get_info().config_fields`` normalized into
  :class:`ProviderConfigField`, the input to the field-driven wizard;
- :func:`list_provider_models` — the live ``list_models()`` call behind the
  ``Default Model`` picker;
- the instance-id helpers, so a second instance of the same provider type
  gets its own credential variable instead of overwriting the first's.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import unicodedata
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from filelock import FileLock

logger = logging.getLogger(__name__)


def keys_file(amplifier_home: Path | None = None) -> Path:
    return (amplifier_home or (Path.home() / ".amplifier")) / "keys.env"


def provider_env_prefix(module_id: str) -> str:
    """``provider-anthropic`` → ``ANTHROPIC`` (the provider's env prefix)."""
    name = module_id
    for lead in ("amplifier-module-", "provider-", "amplifier-provider-"):
        if name.startswith(lead):
            name = name[len(lead) :]
    return name.replace("-", "_").upper()


@dataclass(frozen=True)
class ProviderChoice:
    module_id: str
    name: str
    key_var: str
    base_url_var: str
    has_key: bool = False
    installed: bool = False
    """The module is importable in THIS process (entry-point discovered)."""
    source_uri: str | None = None
    """Where to fetch it from when it is not installed (:data:`PROVIDER_SOURCES`)."""
    display: str = ""
    """``get_info().display_name`` (e.g. ``vLLM``) when known, else empty."""


@dataclass(frozen=True)
class ProviderConfigField:
    """One ``get_info().config_fields`` entry, normalized to plain Python.

    Providers declare these as ``amplifier_core.ConfigField``; normalizing
    here keeps the wizard (and its tests, which use ``SimpleNamespace``
    fakes) independent of that class.
    """

    id: str
    display_name: str
    prompt: str
    field_type: str  # text | secret | boolean | choice
    env_var: str | None = None
    default: str | None = None
    required: bool = False
    choices: tuple[str, ...] = ()
    show_when: dict[str, Any] | None = None
    requires_model: bool = False


@dataclass(frozen=True)
class ProviderFields:
    """A provider's authoritative config schema (from ``get_info()``)."""

    module_id: str
    key_var: str | None  # secret field's env_var, e.g. ANTHROPIC_API_KEY; None if keyless
    key_field_id: str  # e.g. "api_key"
    base_url_var: str | None
    base_url_default: str | None
    has_models: bool
    display_name: str = ""
    config_fields: tuple[ProviderConfigField, ...] = ()


@dataclass(frozen=True)
class ProviderModel:
    """One model advertised by a provider's ``list_models()``."""

    id: str
    display_name: str = ""
    capabilities: tuple[str, ...] = ()


@dataclass(frozen=True)
class ModelCatalog:
    """The outcome of a ``list_models()`` probe — never an exception.

    ``error`` is a one-line reason (unreachable host, timeout, bad key) that
    the wizard prints before falling back to a free-text model prompt.
    """

    models: tuple[ProviderModel, ...] = ()
    error: str | None = None


@dataclass(frozen=True)
class ProviderAvailability:
    """Whether a provider module can be imported in this process."""

    module_id: str
    available: bool
    path: Path | None = None
    reason: str | None = None


def _load_provider_class(module_id: str) -> Any:  # duck-typed provider class
    """Import a provider module and return its ``*Provider`` class, or None."""
    import importlib
    import inspect

    name = module_id
    for lead in ("amplifier-module-", "provider-", "amplifier-provider-"):
        if name.startswith(lead):
            name = name[len(lead) :]
    try:
        module = importlib.import_module(f"amplifier_module_provider_{name.replace('-', '_')}")
    except Exception:  # noqa: BLE001 — provider not installed
        return None
    for attr in dir(module):
        obj = getattr(module, attr)
        if (
            inspect.isclass(obj)
            and attr.endswith("Provider")
            and str(getattr(obj, "__module__", "")).startswith("amplifier_module_provider")
        ):
            return obj
    return None


def resolve_placeholder(value: Any) -> Any:
    """``"${VLLM_BASE_URL}"`` → the env value; anything else unchanged.

    Config files store ``${VAR}`` placeholders, but instantiating a provider
    to ask it for models needs the REAL endpoint and key. Mirrors app-cli's
    ``_resolve_config_value``.
    """
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        return os.environ.get(value[2:-1])
    return value


def _instantiate_provider(cls: Any, collected: dict[str, Any] | None = None) -> Any:
    """Try the provider constructor signatures app-cli probes; None on failure.

    *collected* is the wizard's field map so far; its ``base_url`` / ``host`` /
    ``api_key`` (``${VAR}``-resolved) are threaded in so the instance can reach
    a real server for ``list_models()``. Without them the probe can only build
    a provider pointed at nothing.

    Rung order is load-bearing, not cosmetic: ``VLLMProvider.__init__`` raises
    ``ValueError("base_url or client must be provided")`` when rung 1 omits
    ``base_url``, and the ollama provider takes ``host=`` rather than
    ``base_url=``. ``ValueError``/``RuntimeError`` are caught alongside
    ``TypeError`` because providers signal a bad/missing argument with all
    three (an old azure-openai raises ``RuntimeError`` for a missing dep).
    """
    collected = collected or {}
    base_url = (
        resolve_placeholder(collected.get("base_url") or collected.get("azure_endpoint"))
        or "http://placeholder"
    )
    host = resolve_placeholder(collected.get("host")) or "http://localhost:11434"
    api_key = resolve_placeholder(collected.get("api_key")) or ""
    for kwargs in (
        {"api_key": api_key, "config": {}},
        {"base_url": base_url, "api_key": api_key, "config": {}},
        {"base_url": base_url, "config": {}},
        {"host": host, "config": {}},
        {"config": {}},
        {},
    ):
        try:
            return cls(**kwargs)
        except (TypeError, ValueError, RuntimeError):
            continue
        except Exception:  # noqa: BLE001 — a provider may raise anything here
            continue
    return None


def should_show_field(field_spec: ProviderConfigField, collected: dict[str, Any]) -> bool:
    """Whether a ``show_when``-conditional field applies to what's collected.

    Ported verbatim from app-cli's ``_should_show_field``: an exact
    (case-insensitive) match by default, plus the ``contains:``,
    ``not_contains:``, ``startswith:`` and ``not_startswith:`` prefixes. A
    field with no ``show_when`` always shows.
    """
    show_when = field_spec.show_when
    if not show_when:
        return True
    for key, expected in show_when.items():
        actual = str(collected.get(key, "")).lower()
        wanted = str(expected).lower()
        if wanted.startswith("not_contains:"):
            if wanted[len("not_contains:") :] in actual:
                return False
        elif wanted.startswith("contains:"):
            if wanted[len("contains:") :] not in actual:
                return False
        elif wanted.startswith("not_startswith:"):
            if actual.startswith(wanted[len("not_startswith:") :]):
                return False
        elif wanted.startswith("startswith:"):
            if not actual.startswith(wanted[len("startswith:") :]):
                return False
        elif actual != wanted:
            return False
    return True


def _normalize_config_field(raw: Any) -> ProviderConfigField | None:
    """One ``ConfigField`` (or duck-typed stand-in) → :class:`ProviderConfigField`."""
    field_id = getattr(raw, "id", None)
    if not field_id:
        return None
    choices = getattr(raw, "choices", None) or ()
    default = getattr(raw, "default", None)
    show_when = getattr(raw, "show_when", None)
    return ProviderConfigField(
        id=str(field_id),
        display_name=str(getattr(raw, "display_name", None) or field_id),
        prompt=str(getattr(raw, "prompt", None) or ""),
        field_type=str(getattr(raw, "field_type", None) or "text"),
        env_var=str(raw.env_var) if getattr(raw, "env_var", None) else None,
        default=str(default) if default not in (None, "") else None,
        required=bool(getattr(raw, "required", False)),
        choices=tuple(str(c) for c in choices),
        show_when=dict(show_when) if isinstance(show_when, dict) else None,
        requires_model=bool(getattr(raw, "requires_model", False)),
    )


_INFO_CACHE: dict[str, ProviderFields | None] = {}
"""Memo for :func:`load_provider_info`.

The picker now asks every catalog entry for its schema, and each miss costs an
import plus up to six constructor probes. Provider schemas are static for the
life of the process, so caching is free correctness-wise — except in tests,
which must call :func:`reset_provider_info_cache` between fakes.
"""


def reset_provider_info_cache() -> None:
    """Drop the :func:`load_provider_info` memo (tests swap provider fakes)."""
    _INFO_CACHE.clear()


def load_provider_info(module_id: str) -> ProviderFields | None:
    """Authoritative env-var + field schema from the provider's ``get_info()``.

    This is how app-cli learns a provider wants ``ANTHROPIC_API_KEY`` vs
    ``OPENAI_API_KEY`` vs a namespaced var — the convention guess is wrong for
    azure/gemini/copilot. Returns ``None`` when the provider can't be loaded
    (caller falls back to the convention).

    A provider with NO secret field (ollama) still returns a record, with
    ``key_var=None``: the wizard is driven by ``config_fields``, not by the
    existence of a key, and refusing to describe keyless providers is what
    made them unofferable.
    """
    if module_id in _INFO_CACHE:
        return _INFO_CACHE[module_id]
    result = _load_provider_info_uncached(module_id)
    _INFO_CACHE[module_id] = result
    return result


def _load_provider_info_uncached(module_id: str) -> ProviderFields | None:
    cls = _load_provider_class(module_id)
    if cls is None:
        return None
    inst = _instantiate_provider(cls)
    if inst is None or not hasattr(inst, "get_info"):
        return None
    try:
        info = inst.get_info()
    except Exception:  # noqa: BLE001
        return None
    key_var: str | None = None
    key_field = "api_key"
    base_url_var: str | None = None
    base_url_default: str | None = None
    fields: list[ProviderConfigField] = []
    for raw in getattr(info, "config_fields", None) or []:
        normalized = _normalize_config_field(raw)
        if normalized is not None:
            fields.append(normalized)
        ftype = getattr(raw, "field_type", None)
        env_var = getattr(raw, "env_var", None)
        fid = getattr(raw, "id", None)
        if ftype == "secret" and key_var is None and env_var:
            key_var = str(env_var)
            key_field = str(fid or "api_key")
        if fid == "base_url" or (env_var and str(env_var).endswith("_BASE_URL")):
            base_url_var = str(env_var) if env_var else None
            default = getattr(raw, "default", None)
            base_url_default = str(default) if default else None
    return ProviderFields(
        module_id=module_id,
        key_var=key_var,
        key_field_id=key_field,
        base_url_var=base_url_var,
        base_url_default=base_url_default,
        has_models=hasattr(inst, "list_models"),
        display_name=str(getattr(info, "display_name", "") or ""),
        config_fields=tuple(fields),
    )


def _choice(module_id: str, name: str, stored: set[str]) -> ProviderChoice:
    """A setup choice using the authoritative env var when discoverable."""
    info = load_provider_info(module_id)
    prefix = provider_env_prefix(module_id)
    if info is not None:
        key_var = info.key_var or f"{prefix}_API_KEY"
        base_url_var = info.base_url_var or f"{prefix}_BASE_URL"
        display = info.display_name
    else:
        key_var = f"{prefix}_API_KEY"
        base_url_var = f"{prefix}_BASE_URL"
        display = ""
    return ProviderChoice(
        module_id=module_id,
        name=name,
        key_var=key_var,
        base_url_var=base_url_var,
        has_key=key_var in stored,
        installed=info is not None,
        display=display,
    )


# -- keys.env read/write (KeyManager.save_key parity) -----------------------


def read_keys(path: Path) -> dict[str, str]:
    """Parse ``KEY=value`` lines from a keys.env file (``{}`` when absent)."""
    result: dict[str, str] = {}
    if not path.is_file():
        return result
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            result[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        return {}
    return result


def stored_key_names(path: Path) -> set[str]:
    return set(read_keys(path))


def keys_lock_path(path: Path) -> Path:
    """The advisory-lock file guarding *path* (``keys.env`` → ``keys.env.lock``)."""
    return path.with_name(path.name + ".lock")


# A crashed or slow writer must never wedge another terminal forever, so the
# advisory lock is bounded: if it can't be taken within this window a
# ``filelock.Timeout`` surfaces instead of a silent deadlock.
_KEYS_LOCK_TIMEOUT = 10.0


def _keys_lock(path: Path) -> FileLock:
    """An advisory lock serialising the read-modify-write of the key store.

    Two ordinary concurrent CLI invocations (two terminals, or a script
    racing a human) both do read-modify-write on ``keys.env``; without a
    lock that is last-writer-wins and a freshly-saved key can be silently
    dropped. The lock file sits next to the store; ``filelock`` gives a
    cross-platform (macOS + Linux) advisory lock, the bounded timeout rules
    out deadlock, and the context-manager release frees it even when the
    write raises. Mirrors ``KeyManager.save_key``'s ``FileLock`` guard.
    """
    return FileLock(str(keys_lock_path(path)), timeout=_KEYS_LOCK_TIMEOUT)


def write_key(path: Path, name: str, value: str, *, update_environ: bool = True) -> None:
    """Set ``name=value`` in the keys file (line-preserving), then ``chmod 600``.

    Existing lines for *name* are replaced in place (comments and other
    keys preserved); a new key is appended. Also updates ``os.environ`` so
    the value is live in-process (KeyManager parity)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Advisory lock the whole read-modify-write: two concurrent writers must
    # serialise or the second silently drops the first's freshly-saved key
    # (last-writer-wins). ``with`` releases the lock even if the write raises.
    with _keys_lock(path):
        lines: list[str] = []
        if path.is_file():
            lines = path.read_text(encoding="utf-8").splitlines()
        replaced = False
        for index, raw in enumerate(lines):
            stripped = raw.strip()
            if stripped.startswith("#") or "=" not in stripped:
                continue
            if stripped.split("=", 1)[0].strip() == name:
                lines[index] = f"{name}={value}"
                replaced = True
                break
        if not replaced:
            lines.append(f"{name}={value}")
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        tmp.replace(path)
        try:
            path.chmod(0o600)
        except OSError:
            pass  # best-effort on filesystems without POSIX perms
    if update_environ:
        os.environ[name] = value


# -- provider module catalog ------------------------------------------------

# Mirrors app-cli's provider_sources.DEFAULT_PROVIDER_SOURCES.
PROVIDER_SOURCES: dict[str, str] = {
    "provider-anthropic": (
        "git+https://github.com/microsoft/amplifier-module-provider-anthropic@main"
    ),
    "provider-azure-openai": (
        "git+https://github.com/microsoft/amplifier-module-provider-azure-openai@main"
    ),
    "provider-chat-completions": (
        "git+https://github.com/microsoft/amplifier-module-provider-chat-completions@main"
    ),
    "provider-gemini": "git+https://github.com/microsoft/amplifier-module-provider-gemini@main",
    "provider-github-copilot": (
        "git+https://github.com/microsoft/amplifier-module-provider-github-copilot@main"
    ),
    "provider-ollama": "git+https://github.com/microsoft/amplifier-module-provider-ollama@main",
    "provider-openai": "git+https://github.com/microsoft/amplifier-module-provider-openai@main",
    "provider-vllm": "git+https://github.com/microsoft/amplifier-module-provider-vllm@main",
}
"""Known provider modules and where to fetch them.

Entry-point discovery only sees modules already installed in the running
interpreter, and tui installs just the bundle's provider — so on a fresh
machine discovery is empty or partial and the picker had nothing to offer
beyond a hardcoded five. This catalog is the second source, matching app-cli:
a provider can be offered, configured and persisted (with its ``source:``)
before it is installed, and the next boot installs it from that source.

Deliberately separate from :data:`PROVIDER_CREDENTIAL_VARS`, which answers a
different question (which env vars mark a provider as configured for
``--from-env`` detection). app-cli keeps the same split.
"""


def effective_provider_sources(
    project_dir: Path | None = None, amplifier_home: Path | None = None
) -> dict[str, str]:
    """:data:`PROVIDER_SOURCES` overlaid with the user's own source overrides.

    Precedence, ascending: the catalog < ``sources.modules.<id>`` <
    ``config.providers[].source``. Mirrors app-cli's
    ``get_effective_provider_sources`` so a user pinning a fork or a local
    checkout gets that build everywhere, including here.
    """
    sources = dict(PROVIDER_SOURCES)
    try:
        from .bundle_admin import settings_paths
        from .config import load_merged_settings
        from .source_admin import module_sources

        paths = settings_paths(project_dir, amplifier_home)
        merged = load_merged_settings(paths)
        for module_id, uri in (module_sources(merged) or {}).items():
            if str(module_id).startswith("provider-") and uri:
                sources[str(module_id)] = str(uri)
        for scope in ("global", "project", "local"):
            for entry in _scope_providers(paths, scope):  # type: ignore[arg-type]
                module_id = str(entry.get("module") or "")
                uri = entry.get("source")
                if module_id and isinstance(uri, str) and uri:
                    sources[module_id] = uri
    except Exception:  # noqa: BLE001 — a bad settings file must not break the picker
        logger.debug("provider source overlay failed; using the catalog", exc_info=True)
    return sources


async def ensure_provider_available(
    module_id: str, source_uri: str | None, *, amplifier_home: Path | None = None
) -> ProviderAvailability:
    """Make *module_id* importable in THIS process, best effort. Never raises.

    Already importable ⇒ done. Otherwise resolve *source_uri* into
    ``~/.amplifier/cache`` (the same cache foundation populates, so an
    already-cloned module is a no-op) and put it on ``sys.path`` so
    ``get_info()`` and ``list_models()`` can be read during setup.

    Deliberately does NOT install. app-cli shells out to ``uv pip install -e``;
    persisting ``source:`` into the provider entry is enough, because the next
    session boot has foundation install it properly — which is exactly how the
    vLLM module lands in the tool venv today.

    A ``sys.path`` graft cannot satisfy the provider's own third-party imports
    (vLLM needs ``openai``); that surfaces as ``available=False`` with a reason
    and the caller degrades to the basic prompts.
    """
    if _load_provider_class(module_id) is not None:
        return ProviderAvailability(module_id, True)
    if not source_uri:
        return ProviderAvailability(module_id, False, reason="no known source")
    try:
        import importlib
        import sys

        from amplifier_foundation.paths.resolution import parse_uri
        from amplifier_foundation.sources.git import GitSourceHandler

        cache_dir = (amplifier_home or (Path.home() / ".amplifier")) / "cache"
        resolved = await GitSourceHandler().resolve(parse_uri(source_uri), cache_dir)
        path = Path(getattr(resolved, "active_path", None) or getattr(resolved, "path", ""))
        if not path.is_dir():
            return ProviderAvailability(module_id, False, reason="source resolved to no directory")
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
        importlib.invalidate_caches()
        reset_provider_info_cache()
        if _load_provider_class(module_id) is not None:
            return ProviderAvailability(module_id, True, path=path)
        return ProviderAvailability(
            module_id, False, path=path, reason="fetched, but its dependencies are not installed"
        )
    except Exception as exc:  # noqa: BLE001 — offline/no-git must degrade, not crash
        logger.debug("provider source fetch failed for %s", module_id, exc_info=True)
        return ProviderAvailability(module_id, False, reason=f"{type(exc).__name__}: {exc}")


async def list_provider_models(
    module_id: str,
    collected: dict[str, Any] | None = None,
    *,
    timeout: float = 15.0,
) -> ModelCatalog:
    """Live models from the provider's ``list_models()``. Never raises.

    *collected* supplies the endpoint and credential the wizard just gathered
    (see :func:`_instantiate_provider`). The call is bounded by *timeout* —
    app-cli has none, but a TUI must not sit on a wedged socket — and the
    provider is closed afterwards. Any failure comes back as an empty catalog
    with a one-line ``error`` for the caller to print.
    """
    cls = _load_provider_class(module_id)
    if cls is None:
        return ModelCatalog(error="provider module is not importable")
    inst = _instantiate_provider(cls, collected)
    if inst is None:
        return ModelCatalog(error="provider could not be constructed from the given settings")
    lister = getattr(inst, "list_models", None)
    if not callable(lister):
        return ModelCatalog(error="provider does not advertise models")

    async def _invoke() -> Any:
        # A sync lister must be CALLED inside the thread, not before it: calling
        # it eagerly and then wrapping the finished value in to_thread would put
        # the blocking work outside the timeout entirely.
        import inspect

        if asyncio.iscoroutinefunction(lister):
            outcome: Any = lister()
        else:
            outcome = await asyncio.to_thread(lister)
        return await outcome if inspect.isawaitable(outcome) else outcome

    raw: Any
    try:
        raw = await asyncio.wait_for(_invoke(), timeout=timeout)
    except asyncio.TimeoutError:
        return ModelCatalog(error=f"timed out after {timeout:g}s")
    except Exception as exc:  # noqa: BLE001 — unreachable host, bad key, anything
        return ModelCatalog(error=f"{type(exc).__name__}: {exc}")
    finally:
        await _close_provider(inst)
    models: list[ProviderModel] = []
    for item in raw or ():
        ident = getattr(item, "id", None) or (item if isinstance(item, str) else None)
        if not ident:
            continue
        capabilities = getattr(item, "capabilities", None) or ()
        models.append(
            ProviderModel(
                id=str(ident),
                display_name=str(getattr(item, "display_name", "") or ident),
                capabilities=tuple(str(c) for c in capabilities),
            )
        )
    return ModelCatalog(models=tuple(models))


async def _close_provider(inst: Any) -> None:
    """Best-effort ``close()`` on a throwaway probe instance."""
    closer = getattr(inst, "close", None)
    if not callable(closer):
        return
    try:
        result = closer()
        if asyncio.iscoroutine(result):
            await result
    except Exception:  # noqa: BLE001 — cleanup must never cascade
        logger.debug("provider close() failed during setup probe", exc_info=True)


# -- provider instances (a second vLLM must not clobber the first's key) ----


def normalize_id(value: str) -> str:
    """NFC-normalize so visually identical ids compare equal.

    Two ids that render the same in a terminal but differ in Unicode
    composition would otherwise defeat both the id-uniqueness and the
    credential-collision checks by construction.
    """
    return unicodedata.normalize("NFC", value)


def sanitize_env_token(value: str) -> str:
    """Collapse to an env-var token matching ``[A-Z0-9_]*``."""
    return re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_").upper()


def suggest_instance_env_var(module_id: str, instance_id: str, claimed: set[str]) -> str:
    """``(vllm, runpod)`` → ``VLLM_RUNPOD_API_KEY``, deduped against *claimed*.

    A second instance of a provider type cannot reuse the type-default
    variable — writing ``VLLM_API_KEY`` again would overwrite the first
    instance's key in keys.env and silently break it. Raises ``ValueError``
    rather than emitting a name that is empty or already taken: both cases
    would recreate the collision this exists to prevent.
    """
    display = module_id[len("provider-") :] if module_id.startswith("provider-") else module_id
    type_prefix = sanitize_env_token(display)
    # Strip a leading "<type>-" so (vllm, vllm-runpod) yields RUNPOD, not
    # VLLM_RUNPOD twice over. An id that IS just the type name consumes
    # entirely and correctly raises below — it carries no distinguishing info.
    suffix_source = re.sub(
        rf"^{re.escape(display)}[-_\s]*", "", normalize_id(instance_id), flags=re.IGNORECASE
    )
    id_suffix = sanitize_env_token(suffix_source)
    if not id_suffix:
        raise ValueError(
            f"instance id {instance_id!r} sanitizes to an empty suffix · pick a more distinct id"
        )
    suggested = f"{type_prefix}_{id_suffix}_API_KEY"
    if suggested in claimed:
        raise ValueError(
            f"instance id {instance_id!r} sanitizes to {suggested}, already in use · "
            "pick a more distinct id"
        )
    return suggested


def claimed_env_vars(
    project_dir: Path | None = None, amplifier_home: Path | None = None
) -> set[str]:
    """Env-var names already spoken for, across every settings scope and keys.env.

    A name is claimed either by a ``${VAR}`` placeholder in some scope's
    provider config, or by an actual stored secret — the latter matters when a
    key was saved moments ago and the settings write has not landed yet.
    """
    claimed: set[str] = set(stored_key_names(keys_file(amplifier_home)))
    try:
        from .bundle_admin import settings_paths

        paths = settings_paths(project_dir, amplifier_home)
        for scope in ("global", "project", "local"):
            for entry in _scope_providers(paths, scope):  # type: ignore[arg-type]
                config = entry.get("config")
                if not isinstance(config, dict):
                    continue
                for value in config.values():
                    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
                        claimed.add(value[2:-1])
    except Exception:  # noqa: BLE001
        logger.debug("claimed_env_vars scope walk failed", exc_info=True)
    return claimed


def instance_id_in_use(
    instance_id: str, project_dir: Path | None = None, amplifier_home: Path | None = None
) -> bool:
    """Whether *instance_id* already identifies a configured provider."""
    wanted = normalize_id(instance_id).lower()
    return any(
        normalize_id(configured.name).lower() == wanted
        for configured in configured_providers(project_dir, amplifier_home)
    )


# -- discovery + status -----------------------------------------------------


async def discover_providers(amplifier_home: Path | None = None) -> tuple[ProviderChoice, ...]:
    """Installed provider modules as setup choices (via ``ModuleLoader``).

    Returns ``()`` when amplifier-core is unavailable. Never raises."""
    try:
        from amplifier_core.loader import ModuleLoader  # lazy: keep --demo offline
    except Exception:  # noqa: BLE001
        return ()
    try:
        modules = await ModuleLoader().discover()
    except Exception:  # noqa: BLE001
        return ()
    stored = stored_key_names(keys_file(amplifier_home))
    choices: list[ProviderChoice] = []
    for module in modules:
        if getattr(module, "type", None) != "provider":
            continue
        module_id = str(getattr(module, "id", "") or "")
        if not module_id:
            continue
        name = str(getattr(module, "name", module_id) or module_id)
        choices.append(_choice(module_id, name, stored))
    return tuple(sorted(choices, key=lambda c: c.module_id))


async def onboarding_choices(
    amplifier_home: Path | None = None,
    project_dir: Path | None = None,
) -> tuple[ProviderChoice, ...]:
    """Providers to offer during setup — installed, catalogued, or configured.

    Three sources, unioned:

    1. **Entry-point discovery** (:func:`discover_providers`) — authoritative
       for a module that is actually installed, because its ``get_info()``
       names the real env vars (the ``<X>_API_KEY`` convention guesses wrong
       for azure/gemini/copilot).
    2. **The module catalog** (:func:`effective_provider_sources`) — so a
       provider can be offered *before* it is installed. tui only installs the
       bundle's provider, so discovery alone is empty on a fresh machine and
       partial after the first boot; that is why vLLM was absent from the
       picker even though the module exists and the user had configured it.
    3. **Anything already in ``config.providers``** — a provider the user
       configured by hand must not vanish from the list that offers to
       reconfigure it.

    Keyless providers (ollama) are included: the wizard is driven by the
    provider's ``config_fields``, not by the existence of a secret, so there is
    no longer a reason to hide them. The old known-credential table remains the
    fallback for a module that is neither installed nor introspectable.
    """
    stored = stored_key_names(keys_file(amplifier_home))
    discovered = await discover_providers(amplifier_home=amplifier_home)
    by_module: dict[str, ProviderChoice] = {c.module_id: c for c in discovered}

    sources = effective_provider_sources(project_dir, amplifier_home)
    candidates = set(sources) | set(PROVIDER_CREDENTIAL_VARS)
    candidates |= {
        configured.module_id
        for configured in configured_providers(project_dir, amplifier_home)
        if configured.module_id
    }
    for module_id in candidates:
        if module_id in by_module:
            continue
        by_module[module_id] = _catalog_choice(module_id, stored)

    return tuple(
        sorted(
            (replace(c, source_uri=sources.get(c.module_id)) for c in by_module.values()),
            key=lambda c: c.module_id,
        )
    )


def _catalog_choice(module_id: str, stored: set[str]) -> ProviderChoice:
    """A choice for a module we may not have installed.

    Prefers the provider's own ``get_info()`` when it happens to be
    importable, then the known-credential table, then the ``<X>_API_KEY``
    naming convention.
    """
    info = load_provider_info(module_id)
    prefix = provider_env_prefix(module_id)
    table = PROVIDER_CREDENTIAL_VARS.get(module_id) or []
    if info is not None and info.key_var:
        key_var = info.key_var
    elif table:
        key_var = table[0]
    else:
        key_var = f"{prefix}_API_KEY"
    base_url_var = (info.base_url_var if info else None) or f"{prefix}_BASE_URL"
    return ProviderChoice(
        module_id=module_id,
        name=_provider_display_name(module_id),
        key_var=key_var,
        base_url_var=base_url_var,
        has_key=key_var in stored,
        installed=info is not None,
        display=info.display_name if info else "",
    )


@dataclass(frozen=True)
class SetupStatus:
    keys_path: Path
    stored_keys: tuple[str, ...]
    active_bundle: str | None


def setup_status(
    project_dir: Path | None = None, amplifier_home: Path | None = None
) -> SetupStatus:
    """A snapshot of what's configured: stored key names + active bundle."""
    from .bundle_admin import current_bundle

    path = keys_file(amplifier_home)
    return SetupStatus(
        keys_path=path,
        stored_keys=tuple(sorted(stored_key_names(path))),
        active_bundle=current_bundle(project_dir, amplifier_home),
    )


# -- provider config settings writer (config.providers) ---------------------

# app-cli's detect table (provider_env_detect.PROVIDER_CREDENTIAL_VARS).
PROVIDER_CREDENTIAL_VARS: dict[str, list[str]] = {
    "provider-anthropic": ["ANTHROPIC_API_KEY"],
    "provider-openai": ["OPENAI_API_KEY"],
    "provider-azure-openai": ["AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT"],
    "provider-gemini": ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
    "provider-github-copilot": ["GITHUB_TOKEN"],
    "provider-ollama": [],
}


def provider_config_entry(
    module_id: str,
    *,
    key_var: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    base_url_var: str | None = None,
    priority: int = 1,
    config: dict[str, Any] | None = None,
    instance_id: str | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    """A ``config.providers`` entry with ``${VAR}`` placeholders (never literals).

    Two shapes, one writer:

    * *config* given — the field-driven wizard's collected map (already
      ``${VAR}``-ised by the prompter), used verbatim plus ``priority``.
    * *config* omitted — the legacy key/base-url/model shape, byte-identical
      to what this produced before, so existing callers and their exact-dict
      assertions are unaffected.

    ``instance_id`` becomes ``id:`` (what a routing matrix targets) and
    ``source`` becomes ``source:`` (what lets a not-yet-installed module be
    fetched at the next boot).
    """
    if config is not None:
        merged = dict(config)
        merged["priority"] = priority
    else:
        merged = {}
        if model:
            merged["default_model"] = model
        if key_var:
            merged["api_key"] = f"${{{key_var}}}"
        if base_url and base_url_var:
            merged["base_url"] = f"${{{base_url_var}}}"
        merged["priority"] = priority
    entry: dict[str, Any] = {"module": module_id, "config": merged}
    if instance_id:
        entry["id"] = instance_id
    if source:
        entry["source"] = source
    return entry


def write_provider_config(
    paths: Any, scope: Literal["global", "project", "local"], entry: dict[str, Any]
) -> Path:
    """Persist a provider entry into ``config.providers`` at *scope*.

    New entry goes first at priority 1 (active); the entry with the same
    IDENTITY is replaced and any other priority-1 entry is demoted to 10 —
    mirroring app-cli's ``AppSettings.set_provider_override``.

    Identity is ``id`` when present, else ``module`` (:func:`_entry_key`, the
    same key ``configured_providers`` and the mount-plan merge use). Matching
    on the module alone would make ``id: runpod`` and a plain ``provider-vllm``
    entry delete each other, which is precisely the multi-instance setup the
    instance-id support exists to allow.

    NOTE (divergence from app-cli, deliberate): app-cli's ``provider add``
    appends at ``max(priority)+1`` — newest is LAST. tui puts the newest first
    at priority 1, because "the provider I just configured is the one I want to
    use" is the right default for an interactive setup flow.
    """
    from .bundle_admin import read_scope, scope_file, write_scope

    path = scope_file(paths, scope)
    data = read_scope(path)
    config = data.get("config")
    if not isinstance(config, dict):
        config = {}
        data["config"] = config
    providers = config.get("providers")
    if not isinstance(providers, list):
        providers = []
    identity = _entry_key(entry)
    kept: list[Any] = []
    for provider in providers:
        if isinstance(provider, dict) and _entry_key(provider) == identity:
            continue  # replace the entry with the same identity
        if (
            isinstance(provider, dict)
            and isinstance(provider.get("config"), dict)
            and provider["config"].get("priority") == 1
        ):
            provider["config"]["priority"] = 10  # demote the old active
        kept.append(provider)
    config["providers"] = [entry, *kept]
    write_scope(path, data)
    return path


def detect_provider_from_env() -> str | None:
    """First provider whose credential env vars are all set (app-cli parity)."""
    for module_id, variables in PROVIDER_CREDENTIAL_VARS.items():
        if variables and all(os.environ.get(v) for v in variables):
            return module_id
    return None


async def auto_init_from_env(
    project_dir: Path | None = None, amplifier_home: Path | None = None
) -> str | None:
    """Non-interactive setup for CI/Docker: detect a provider from env and
    write its ``config.providers`` entry (the key is already exported).

    Returns the configured module id, or ``None`` when nothing was detected.
    Never raises."""
    from .bundle_admin import settings_paths

    module_id = detect_provider_from_env()
    if module_id is None:
        return None
    info = load_provider_info(module_id)
    key_var = info.key_var if info else f"{provider_env_prefix(module_id)}_API_KEY"
    entry = provider_config_entry(module_id, key_var=key_var)
    try:
        write_provider_config(settings_paths(project_dir, amplifier_home), "global", entry)
    except Exception:  # noqa: BLE001 — best-effort in headless environments
        return None
    return module_id


# -- configured providers (provider list / use / remove + first-run gate) ---
#
# app-cli reads its ``config.providers`` list through ``AppSettings``
# (``get_provider_overrides`` / ``get_scope_provider_overrides``) and drives
# ``provider list/use/remove`` + ``check_first_run`` off it. tui is not
# built on those classes, so this re-expresses the same behavioral contract
# over ``kernel/bundle_admin``'s scope files: the *primary* provider is the
# lowest ``config.priority`` (app-cli's ★ marker); a more specific scope
# (local > project > global) shadows the same identity key. Pure dict/file
# work — unit-tested against ``tmp_path`` scope files.


@dataclass(frozen=True)
class ConfiguredProvider:
    """One entry from the merged ``config.providers`` view."""

    module_id: str
    instance_id: str | None
    name: str  # display: the instance id, else the module minus ``provider-``
    model: str | None
    priority: int
    primary: bool  # lowest priority across the merged view (app-cli's ★)
    scope: str  # the most specific scope contributing this entry

    @property
    def key(self) -> str:
        """The identity key app-cli merges on (``id | module``)."""
        return self.instance_id or self.module_id


def _provider_display_name(module_id: str) -> str:
    return module_id.replace("provider-", "") if module_id else module_id


def _entry_key(entry: dict[str, Any]) -> str:
    return str(entry.get("id") or entry.get("module") or "")


def _entry_priority(entry: dict[str, Any]) -> int:
    """Selection priority of a settings entry — lower wins, absent means 100.

    Delegates to :func:`config.provider_priority` so the ★ in ``provider
    list``, the banner and the orchestrator's runtime choice can never drift
    apart.
    """
    from .config import provider_priority

    return provider_priority(entry)


def _scope_providers(
    paths: Any, scope: Literal["global", "project", "local"]
) -> list[dict[str, Any]]:
    """The raw ``config.providers`` list stored at one scope."""
    from .bundle_admin import read_scope, scope_file

    data = read_scope(scope_file(paths, scope))
    config = data.get("config")
    if isinstance(config, dict):
        providers = config.get("providers")
        if isinstance(providers, list):
            return [p for p in providers if isinstance(p, dict)]
    return []


def configured_providers(
    project_dir: Path | None = None, amplifier_home: Path | None = None
) -> tuple[ConfiguredProvider, ...]:
    """The merged ``config.providers`` view, sorted primary-first.

    Same merge rule as app-cli's ``get_provider_overrides``: keyed by
    ``id | module``, a more specific scope (local > project > global)
    shadows the same key. The lowest ``config.priority`` is the primary."""
    from .bundle_admin import SCOPES, settings_paths

    paths = settings_paths(project_dir, amplifier_home)
    merged: dict[str, tuple[str, dict[str, Any]]] = {}
    for scope in SCOPES:  # global -> project -> local: later (more specific) wins
        for entry in _scope_providers(paths, scope):
            key = _entry_key(entry)
            if key:
                merged[key] = (scope, entry)
    if not merged:
        return ()
    min_priority = min(_entry_priority(entry) for _scope, entry in merged.values())
    result: list[ConfiguredProvider] = []
    for scope, entry in merged.values():
        module_id = str(entry.get("module") or "")
        instance_id = entry.get("id")
        instance = str(instance_id) if instance_id else None
        config = entry.get("config")
        model = config.get("default_model") if isinstance(config, dict) else None
        priority = _entry_priority(entry)
        result.append(
            ConfiguredProvider(
                module_id=module_id,
                instance_id=instance,
                name=instance or _provider_display_name(module_id),
                model=str(model) if model else None,
                priority=priority,
                primary=priority == min_priority,
                scope=scope,
            )
        )
    return tuple(sorted(result, key=lambda c: (c.priority, c.name)))


def _credential_available(amplifier_home: Path | None = None) -> bool:
    """True when a known provider's credential vars are all present.

    The packaged bundle already mounts a default provider (anthropic) that
    reads ``ANTHROPIC_API_KEY``; if the credential is live in the process
    env or stored in keys.env, that default will mount without an explicit
    ``config.providers`` entry — so it is NOT a first run."""
    stored = stored_key_names(keys_file(amplifier_home))
    for variables in PROVIDER_CREDENTIAL_VARS.values():
        if variables and all(os.environ.get(v) or v in stored for v in variables):
            return True
    return False


def has_configured_provider(
    project_dir: Path | None = None, amplifier_home: Path | None = None
) -> bool:
    """Whether the app can mount a provider without onboarding.

    app-cli's ``check_first_run`` keys off configured providers only; tui
    also honours the bundle's default provider when its credential is already
    available (env or keys.env), so an exported ``ANTHROPIC_API_KEY`` alone is
    enough to boot the packaged bundle. ``not has_configured_provider()`` is
    the first-run condition the launch gate acts on."""
    if configured_providers(project_dir, amplifier_home):
        return True
    return _credential_available(amplifier_home)


def _match_configured(
    entries: tuple[ConfiguredProvider, ...], token: str
) -> ConfiguredProvider | None:
    """Resolve a user token (id / module / prefix / display) to an entry."""
    needle = token.strip().lower()
    if not needle:
        return None
    for entry in entries:
        candidates = {
            entry.module_id.lower(),
            _provider_display_name(entry.module_id).lower(),
            entry.name.lower(),
            provider_env_prefix(entry.module_id).lower(),
        }
        if entry.instance_id:
            candidates.add(entry.instance_id.lower())
        if needle in candidates:
            return entry
    return None


def use_provider(
    paths: Any,
    name: str,
    *,
    project_dir: Path | None = None,
    amplifier_home: Path | None = None,
) -> ConfiguredProvider | None:
    """Make *name* the primary provider (app-cli's ``provider use``).

    Sets the matched entry's ``config.priority`` to 1 and demotes any other
    entry that also held priority 1 to 10 — the same primary-by-priority
    mechanic as :func:`write_provider_config`. Returns the resolved entry, or
    ``None`` when nothing matched."""
    from .bundle_admin import SCOPES, read_scope, scope_file, write_scope

    entries = configured_providers(project_dir, amplifier_home)
    target = _match_configured(entries, name)
    if target is None:
        return None
    for scope in SCOPES:
        path = scope_file(paths, scope)
        data = read_scope(path)
        config = data.get("config")
        if not isinstance(config, dict):
            continue
        providers = config.get("providers")
        if not isinstance(providers, list):
            continue
        changed = False
        for entry in providers:
            if not isinstance(entry, dict):
                continue
            entry_config = entry.get("config")
            if not isinstance(entry_config, dict):
                entry_config = {}
                entry["config"] = entry_config
            if _entry_key(entry) == target.key:
                if entry_config.get("priority") != 1:
                    entry_config["priority"] = 1
                    changed = True
            elif entry_config.get("priority") == 1:
                entry_config["priority"] = 10
                changed = True
        if changed:
            write_scope(path, data)
    return target


def remove_provider(
    paths: Any,
    name: str,
    *,
    project_dir: Path | None = None,
    amplifier_home: Path | None = None,
) -> ConfiguredProvider | None:
    """Drop *name* from ``config.providers`` across every scope.

    Mirrors app-cli's ``provider remove`` (remove-from-all-scopes by identity
    key). Returns the removed entry, or ``None`` when nothing matched."""
    from .bundle_admin import SCOPES, read_scope, scope_file, write_scope

    entries = configured_providers(project_dir, amplifier_home)
    target = _match_configured(entries, name)
    if target is None:
        return None
    removed = False
    for scope in SCOPES:
        path = scope_file(paths, scope)
        data = read_scope(path)
        config = data.get("config")
        if not isinstance(config, dict):
            continue
        providers = config.get("providers")
        if not isinstance(providers, list):
            continue
        kept = [
            entry
            for entry in providers
            if not (isinstance(entry, dict) and _entry_key(entry) == target.key)
        ]
        if len(kept) == len(providers):
            continue
        if kept:
            config["providers"] = kept
        else:
            config.pop("providers", None)
            if not config:
                data.pop("config", None)
        write_scope(path, data)
        removed = True
    return target if removed else None


__all__ = [
    "PROVIDER_CREDENTIAL_VARS",
    "configured_providers",
    "ProviderChoice",
    "ProviderFields",
    "SetupStatus",
    "ConfiguredProvider",
    "auto_init_from_env",
    "detect_provider_from_env",
    "has_configured_provider",
    "discover_providers",
    "keys_file",
    "keys_lock_path",
    "load_provider_info",
    "onboarding_choices",
    "provider_config_entry",
    "provider_env_prefix",
    "remove_provider",
    "read_keys",
    "setup_status",
    "stored_key_names",
    "use_provider",
    "write_key",
    "write_provider_config",
]
