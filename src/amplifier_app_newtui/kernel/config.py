"""The single configuration golden path: ``resolve_config()``.

One function resolves everything the app needs to create sessions
(ADR-0007 §Runtimes, RESEARCH-BRIEF risk #9 "config/bundle
dual-representation"):

1. **Settings** — three-scope deep merge: global
   (``~/.amplifier/settings.yaml``) → project
   (``<project>/.amplifier/settings.yaml``) → local
   (``<project>/.amplifier/settings.local.yaml``); most specific wins.
2. **Bundle discovery** — project bundles → user bundles → packaged
   bundles (``amplifier_app_newtui/data/bundles``). URIs pass through.
3. **Foundation lifecycle** — ``load_bundle`` → ``compose`` overlay
   bundles (settings ``bundle.app`` list) → ``prepare()`` exactly ONCE.
4. **Overrides** — settings module overrides applied to the prepared
   mount plan *in place* so ``prepared.mount_plan`` and the returned
   plan can never drift apart.

Everything except :func:`resolve_config` itself is pure and offline;
foundation is imported lazily inside the async body only.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable, Mapping, MutableMapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .compaction import apply_compaction_settings

logger = logging.getLogger(__name__)

DEFAULT_BUNDLE = "newtui"
"""Bundle name used when neither the caller nor settings pick one."""

_URI_PREFIXES = ("git+", "file://", "http://", "https://", "zip+")
_BUNDLE_FILE_CANDIDATES = ("{name}.md", "{name}.yaml", "{name}/bundle.md", "{name}/bundle.yaml")


# --------------------------------------------------------------------------
# Settings — three-scope deep merge
# --------------------------------------------------------------------------


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge *overlay* onto *base*; overlay wins on conflicts."""
    result = dict(base)
    for key, value in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


_UNION_TOOL_CONFIG_FIELDS = frozenset(
    {"allowed_write_paths", "allowed_read_paths", "denied_write_paths"}
)
"""Tool permission lists extend across scopes instead of replacing each other."""


def merge_tool_configs(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Merge one tool config with stable union semantics for path policy.

    Amplifier's directory settings are additive capabilities: adding a project-
    scoped path must not erase a global path (or the implicit project root).
    Other config values retain normal overlay-wins semantics.
    """
    merged = deep_merge(base, overlay)
    for config_key in _UNION_TOOL_CONFIG_FIELDS:
        if config_key not in base and config_key not in overlay:
            continue
        combined: list[Any] = []
        for source in (base.get(config_key, []), overlay.get(config_key, [])):
            if not isinstance(source, list):
                continue
            for value in source:
                if value not in combined:
                    combined.append(value)
        merged[config_key] = combined
    return merged


def _merge_tool_lists(base: list[Any], overlay: list[Any]) -> list[Any]:
    """Merge settings ``modules.tools`` by identity, preserving order."""
    result = [dict(item) if isinstance(item, dict) else item for item in base]
    index = {
        str(item.get("id") or item.get("instance_id") or item.get("module")): offset
        for offset, item in enumerate(result)
        if isinstance(item, dict)
        and (item.get("id") or item.get("instance_id") or item.get("module"))
    }
    for raw in overlay:
        if not isinstance(raw, dict):
            continue
        item = dict(raw)
        key = str(item.get("id") or item.get("instance_id") or item.get("module") or "")
        if key and key in index:
            existing = result[index[key]]
            assert isinstance(existing, dict)
            merged = {**existing, **item}
            merged["config"] = merge_tool_configs(
                existing.get("config") or {}, item.get("config") or {}
            )
            result[index[key]] = merged
        else:
            result.append(item)
            if key:
                index[key] = len(result) - 1
    return result


def merge_settings(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Settings merge with Amplifier-native module/path semantics."""
    merged = deep_merge(base, overlay)
    base_tools = (base.get("modules") or {}).get("tools")
    overlay_tools = (overlay.get("modules") or {}).get("tools")
    if isinstance(base_tools, list) and isinstance(overlay_tools, list):
        modules = merged.setdefault("modules", {})
        if isinstance(modules, dict):
            modules["tools"] = _merge_tool_lists(base_tools, overlay_tools)
    return merged


@dataclass(frozen=True)
class SettingsPaths:
    """The three settings scopes, least → most specific."""

    global_settings: Path
    project_settings: Path
    local_settings: Path

    @classmethod
    def default(cls, project_dir: Path, amplifier_home: Path) -> SettingsPaths:
        return cls(
            global_settings=amplifier_home / "settings.yaml",
            project_settings=project_dir / ".amplifier" / "settings.yaml",
            local_settings=project_dir / ".amplifier" / "settings.local.yaml",
        )

    def in_merge_order(self) -> tuple[Path, ...]:
        return (self.global_settings, self.project_settings, self.local_settings)


def load_merged_settings_reporting(
    paths: SettingsPaths,
) -> tuple[dict[str, Any], tuple[Path, ...]]:
    """Load and deep-merge the three settings scopes, reporting skips.

    Returns ``(merged, malformed_paths)``. A malformed file is skipped so
    settings never prevent startup (amplifier-app-cli ``AppSettings``
    parity), but the skipped path is reported so the caller can surface a
    user-facing notice — the whole scope's settings are being ignored, and
    a silent ``logger.warning`` is the wrong place to bury that (the
    analogous bundle fallback notifies loudly).
    """
    merged: dict[str, Any] = {}
    malformed: list[Path] = []
    for path in paths.in_merge_order():
        if not path.is_file():
            continue
        try:
            content = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            logger.warning("Skipping malformed settings file: %s", path)
            malformed.append(path)
            continue
        if isinstance(content, dict):
            merged = merge_settings(merged, content)
    return merged, tuple(malformed)


def load_merged_settings(paths: SettingsPaths) -> dict[str, Any]:
    """Load and deep-merge all three settings scopes.

    Missing or malformed files are skipped — settings must never prevent
    startup. This thin wrapper drops the malformed-path report; callers on
    the boot path use :func:`load_merged_settings_reporting` to surface it.
    """
    merged, _malformed = load_merged_settings_reporting(paths)
    return merged


def malformed_settings_notice(malformed: tuple[Path, ...]) -> str | None:
    """Build the user-facing notice for skipped settings scopes, or ``None``."""
    if not malformed:
        return None
    names = ", ".join(path.name for path in malformed)
    return (
        f"Skipped malformed settings file(s): {names} — defaults applied "
        "for those scopes (run doctor for the parse error)."
    )


def map_provider_ids_to_instance_ids(mount_plan: dict[str, Any]) -> None:
    """Copy each provider's settings ``id`` to the kernel's ``instance_id``.

    settings.yaml identifies a provider instance by ``id`` (e.g.
    ``id: openmj`` for a second provider-vllm); amplifier-core mounts a
    provider under its ``instance_id`` and only falls back to the module
    name when none is given. Without this map a provider with an ``id``
    mounts under its module-derived name (``vllm``), so a mount check that
    looks for the configured ``id`` (``openmj``) reports it missing even
    though it is live — a false 'degraded start' notice. The reference CLI
    does exactly this (``runtime/config._map_id_to_instance_id``).
    """
    for provider in mount_plan.get("providers") or []:
        if isinstance(provider, dict) and "id" in provider and "instance_id" not in provider:
            provider["instance_id"] = provider["id"]


def load_keys_env(amplifier_home: Path | None = None) -> None:
    """Load ``~/.amplifier/keys.env`` into the process environment.

    The amplifier system stores provider credentials and endpoints
    (``VLLM_BASE_URL``, ``ANTHROPIC_PROVIDER_ANTHROPIC_API_KEY``, …) in
    ``keys.env``, and the reference CLI sources it at startup (app-cli
    ``KeyManager._load_keys``). newtui expands ``${VAR}`` placeholders in
    the mount plan from ``os.environ`` — so without this load those
    placeholders resolve to nothing and every provider whose creds live
    only in ``keys.env`` fails to mount. Existing env wins (never clobber
    a value the user exported), matching app-cli exactly.
    """
    keys_file = (amplifier_home or (Path.home() / ".amplifier")) / "keys.env"
    if not keys_file.exists():
        return
    try:
        for raw in keys_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key and key not in os.environ:
                os.environ[key] = value.strip().strip('"').strip("'")
    except OSError:
        pass  # manual env vars still work; never fail startup on this


def active_bundle_name(settings: dict[str, Any]) -> str | None:
    """Read ``bundle.active`` from merged settings."""
    bundle_section = settings.get("bundle")
    if isinstance(bundle_section, dict):
        active = bundle_section.get("active")
        if isinstance(active, str) and active:
            return active
    return None


def added_bundle_uris(settings: dict[str, Any]) -> dict[str, str]:
    """The ``bundle.added`` name->URI registry from merged settings.

    ``bundle add <name> <uri>`` writes ``bundle.added.<name> = uri`` (see
    ``kernel/bundle_admin.add_bundle``); this reader is the single home the
    boot path and the ``bundle_admin`` CLI both consult, so a registered name
    resolves to the URI it was registered with. Any non-dict / junk shape
    yields ``{}``; empty values are dropped."""
    bundle_section = settings.get("bundle")
    if not isinstance(bundle_section, dict):
        return {}
    added = bundle_section.get("added")
    if not isinstance(added, dict):
        return {}
    return {str(name): str(uri) for name, uri in added.items()}


def overlay_uris(settings: dict[str, Any]) -> tuple[str, ...]:
    """App/behavior bundles (``bundle.app``) composed onto every session."""
    bundle_section = settings.get("bundle")
    if not isinstance(bundle_section, dict):
        return ()
    app_bundles = bundle_section.get("app")
    if not isinstance(app_bundles, list):
        return ()
    return tuple(str(uri) for uri in app_bundles if uri)


ROUTING_MATRIX_BUNDLE_URI = "git+https://github.com/microsoft/amplifier-bundle-routing-matrix@main"
"""The curated routing-matrix bundle (donor parity: amplifier-app-cli's
``WELL_KNOWN_BUNDLES['routing-matrix']['remote']``). Its ``bundle.md``
includes ``behaviors/routing.yaml``, which mounts ``hooks-routing`` (with a
pinned ``source``), a routing-instructions context file, and the routing
skills. Composed as an overlay only when routing is opted in."""

_ROUTING_BUNDLE_MARKER = "amplifier-bundle-routing-matrix"
"""Substring identifying the routing-matrix bundle in any overlay URI
(remote git URL, subdirectory variant, or local dev-checkout path)."""


def routing_enabled(settings: dict[str, Any]) -> bool:
    """Whether the user opted into model routing (mount ``hooks-routing``).

    Routing is opt-in (anchors parity: ``hooks-routing`` is not mounted by
    the base bundle). ``routing.enabled`` is the explicit switch and always
    wins; absent an explicit boolean, naming a matrix (``routing.matrix``)
    opts in — picking a matrix is an unambiguous request for routing. Any
    other shape ⇒ off. Never raises."""
    routing = settings.get("routing")
    if not isinstance(routing, dict):
        return False
    enabled = routing.get("enabled")
    if isinstance(enabled, bool):
        return enabled
    matrix = routing.get("matrix")
    return isinstance(matrix, str) and bool(matrix)


def composed_overlay_uris(settings: dict[str, Any]) -> tuple[str, ...]:
    """All overlay bundle URIs composed onto the session, in compose order.

    User ``bundle.app`` overlays come first; the routing-matrix bundle is
    appended last when routing is opted in (:func:`routing_enabled`) so the
    settings→hook bridge (:func:`inject_routing_config`) has a mounted
    ``hooks-routing`` to patch. Deduped by bundle identity: a user who
    already lists the routing-matrix bundle in ``bundle.app`` never composes
    it twice."""
    overlays = list(overlay_uris(settings))
    if routing_enabled(settings) and not any(_ROUTING_BUNDLE_MARKER in uri for uri in overlays):
        overlays.append(ROUTING_MATRIX_BUNDLE_URI)
    return tuple(overlays)


def build_source_resolver(settings: dict[str, Any]) -> Callable[[str, str], str]:
    """Module-source override resolver for ``Bundle.prepare()``.

    Precedence (least → most specific): ``sources.modules`` <
    ``overrides.<id>.source``. Unknown modules fall through unchanged.
    """
    combined: dict[str, str] = {}

    sources_section = settings.get("sources")
    if isinstance(sources_section, dict):
        modules = sources_section.get("modules")
        if isinstance(modules, dict):
            combined.update({str(k): str(v) for k, v in modules.items()})

    overrides_section = settings.get("overrides")
    if isinstance(overrides_section, dict):
        for module_id, override in overrides_section.items():
            if isinstance(override, dict) and isinstance(override.get("source"), str):
                combined[str(module_id)] = override["source"]

    def resolve(module_id: str, source: str) -> str:
        return combined.get(module_id, source)

    return resolve


# A ``namespace:path`` include (e.g. ``foundation:behaviors/streaming-ui``)
# resolves through the bundle registry's namespace lookup, NOT as a fetchable
# URI, so it must never be redirected by a bundle-source override: a substring
# match there would rewrite the include to the override URI and trip
# foundation's cycle detector, silently dropping the sub-bundle (app-cli issue
# #257). A namespace identifier is a letter followed by 1+ word/hyphen chars
# then ``:``; a URI is distinguished by containing ``://`` or a ``git+``/``zip+``
# prefix.
_NAMESPACE_PATH_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]+:")


def _is_namespace_path(source: str) -> bool:
    """True when *source* is a ``namespace:path`` registry reference, not a URI."""
    if "://" in source or source.startswith(("git+", "zip+")):
        return False
    return bool(_NAMESPACE_PATH_PATTERN.match(source))


def bundle_source_overrides(settings: dict[str, Any]) -> dict[str, str]:
    """The merged ``sources.bundles`` map (written by ``source add --bundles``).

    Keys are substrings matched against a bundle *include* URI; values are the
    override URIs the include is redirected to. Empty when nothing is set.
    """
    sources_section = settings.get("sources")
    if not isinstance(sources_section, dict):
        return {}
    bundles = sources_section.get("bundles")
    if not isinstance(bundles, dict):
        return {}
    return {str(k): str(v) for k, v in bundles.items()}


def build_bundle_include_resolver(
    settings: dict[str, Any],
) -> Callable[[str], str | None] | None:
    """Include-source resolver from ``sources.bundles``, or ``None`` when unset.

    ``source add --bundles`` writes ``sources.bundles.<key> = uri``; those
    redirect the URIs a bundle *includes* (composes) before foundation resolves
    them, so a user can point a bundle include at a local checkout or a fork.
    Returns ``None`` when no bundle sources are configured, so the default load
    path (no registry override) is left untouched.

    Behavioral contract mirrors amplifier-app-cli
    (``lib/bundle_loader/prepare._build_include_source_resolver``): each key is
    matched as a *substring* of an include's source URI; the first match wins
    and its override replaces the source, preserving the original ``#fragment``
    (e.g. ``#subdirectory=...``) when the override carries none. A
    ``namespace:path`` include is never redirected (:func:`_is_namespace_path`).
    """
    overrides = bundle_source_overrides(settings)
    if not overrides:
        return None

    def resolve(source: str) -> str | None:
        if _is_namespace_path(source):
            return None
        for key, override in overrides.items():
            if key in source:
                # Preserve the original include's fragment when the override
                # carries none; an override's own fragment always wins.
                if "#" in source and "#" not in override:
                    return f"{override}#{source.split('#', 1)[1]}"
                return override
        return None

    return resolve


def _bundle_registry_for(settings: dict[str, Any], amplifier_home: Path) -> Any:
    """A ``BundleRegistry`` carrying the ``sources.bundles`` include resolver.

    Returns ``None`` when no bundle-source overrides are configured, so
    ``load_bundle`` keeps building its own default registry (behavior
    unchanged). Otherwise a registry scoped to *amplifier_home* is created with
    the include resolver attached, so bundle-include redirects reach
    foundation's include resolution during load/compose (the seam app-cli uses:
    ``registry.set_include_source_resolver`` + ``load_bundle(uri, registry=...)``).
    """
    include_resolver = build_bundle_include_resolver(settings)
    if include_resolver is None:
        return None
    from amplifier_foundation import BundleRegistry  # lazy: keep module import light

    registry = BundleRegistry(home=amplifier_home)
    registry.set_include_source_resolver(include_resolver)
    return registry


def apply_module_overrides(mount_plan: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    """Merge settings ``config.providers`` / ``modules.tools`` /
    ``overrides.<id>.config`` into *mount_plan* **in place**.

    Mutating the prepared bundle's own mount plan (instead of copying)
    is deliberate: RESEARCH-BRIEF risk #9 — the plan the session mounts
    and the plan child sessions inherit must be the same object.
    """
    # overrides.<id>.config — generic, applied first so specific sections win.
    overrides_section = settings.get("overrides")
    if isinstance(overrides_section, dict):
        generic = {
            str(module_id): override["config"]
            for module_id, override in overrides_section.items()
            if isinstance(override, dict) and isinstance(override.get("config"), dict)
        }
        if generic:
            for section in ("providers", "tools", "hooks"):
                for entry in mount_plan.get(section) or []:
                    if not isinstance(entry, dict):
                        continue
                    override_config = generic.get(str(entry.get("module")))
                    if override_config:
                        merge = merge_tool_configs if section == "tools" else deep_merge
                        entry["config"] = merge(entry.get("config") or {}, override_config)

    # config.providers — provider entries merged by identity (id | module).
    provider_overrides = (settings.get("config") or {}).get("providers")
    if isinstance(provider_overrides, list) and provider_overrides:
        _merge_module_entries(mount_plan, "providers", provider_overrides)

    # modules.tools — tool config overrides merged by module id.
    tool_overrides = (settings.get("modules") or {}).get("tools")
    if isinstance(tool_overrides, list) and tool_overrides:
        _merge_module_entries(mount_plan, "tools", tool_overrides)

    return mount_plan


class ProviderNotConfiguredError(ValueError):
    """A ``--provider`` override named a provider no configured entry matches.

    Distinct from :class:`BundleNotFoundError`: the bundle resolved fine, but
    the per-invocation provider override cannot be honored. The CLI surfaces the
    message and exits nonzero instead of dumping a traceback.
    """


def _match_provider_index(providers: list[Any], provider: str) -> int | None:
    """Locate *provider* in *providers* (reference ``run.py`` two-pass search).

    First pass matches an explicit ``id`` / ``instance_id`` (multi-instance
    setups); the second matches the module id with the ``provider-`` prefix
    convention (``-p anthropic`` -> ``provider-anthropic``). Returns the list
    index, or ``None`` when nothing matches.
    """
    module_id = provider if provider.startswith("provider-") else f"provider-{provider}"
    for index, entry in enumerate(providers):
        if isinstance(entry, dict) and provider in (entry.get("id"), entry.get("instance_id")):
            return index
    for index, entry in enumerate(providers):
        if isinstance(entry, dict) and entry.get("module") in (provider, module_id):
            return index
    return None


def _provider_labels(providers: list[Any]) -> list[str]:
    """Human-facing provider labels for a ``--provider`` error message."""
    labels: list[str] = []
    for entry in providers:
        if not isinstance(entry, dict):
            continue
        label = (
            entry.get("id")
            or entry.get("instance_id")
            or str(entry.get("module", "")).replace("provider-", "")
        )
        if label and str(label) not in labels:
            labels.append(str(label))
    return labels


def apply_run_overrides(
    mount_plan: dict[str, Any],
    *,
    provider: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """Apply per-invocation ``--provider`` / ``--model`` overrides IN PLACE.

    Ephemeral to a single ``run`` invocation: unlike
    :func:`apply_module_overrides` (which folds in the *persisted* settings
    scopes), this touches only the in-memory ``mount_plan`` and never writes a
    settings file. Mirrors amplifier-app-cli ``run`` override handling
    (``commands/run.py``): the named provider is promoted to highest priority
    (front of ``providers``) and, when *model* is given, its
    ``config.default_model`` is set for THIS boot only.

    An unknown ``--provider`` raises :class:`ProviderNotConfiguredError` naming
    the configured providers. A bare ``--model`` (no provider) retargets the
    priority provider -- the CLI already refuses ``--model`` without
    ``--provider``, so this is only the defensive kernel-side fallback.

    Mutates ``providers`` in place (never a copy) so the plan the session mounts
    and the plan child sessions inherit stay one object (RESEARCH-BRIEF risk #9).
    """
    if provider is None and model is None:
        return mount_plan
    providers = mount_plan.get("providers")
    if not isinstance(providers, list) or not providers:
        raise ProviderNotConfiguredError(
            "no providers are configured — run `amplifier-newtui init` before "
            "overriding --provider/--model"
        )
    if provider is not None:
        matched = _match_provider_index(providers, provider)
        if matched is None:
            available = ", ".join(_provider_labels(providers)) or "none"
            raise ProviderNotConfiguredError(
                f"provider '{provider}' is not configured · available: {available}"
            )
        if matched != 0:
            # Promote to highest priority (front) for THIS boot, keeping the
            # rest of a multi-provider setup intact (reference parity).
            providers.insert(0, providers.pop(matched))
    if model is not None:
        entry = providers[0]
        if isinstance(entry, dict):
            config = entry.get("config")
            if not isinstance(config, dict):
                config = {}
                entry["config"] = config
            config["default_model"] = model
    return mount_plan


_ENV_PATTERN = re.compile(r"\$\{([^}:]+)(?::([^}]*))?\}")
"""``${VAR}`` / ``${VAR:default}`` placeholders in config string values."""


def expand_env_placeholders(config: dict[str, Any]) -> dict[str, Any]:
    """Expand ``${VAR}``/``${VAR:default}`` in every config string, IN PLACE.

    Ported from amplifier-app-cli ``runtime/config_merge.expand_env_vars``
    (applied to the effective bundle config there), with one fail-safe
    refinement: a dict value that is EXACTLY one unset ``${VAR}`` with no
    default is *dropped* rather than expanded to ``""`` — providers treat
    an absent key as "use my default" but pass ``""`` straight to their
    SDK (e.g. ``AsyncAnthropic(base_url="")`` → invalid request URL,
    surfaced as a bare "Connection error"). This mirrors the reference's
    ``_resolve_env_placeholder(...) or <default>`` pattern in
    ``provider_loader.py``.

    In place (mutating dicts/lists) because ``mount_plan`` must remain
    ``prepared.mount_plan`` itself — never a copy (risk #9).
    """

    def _replace_match(match: re.Match[str]) -> str:
        default = match.group(2)
        return os.environ.get(match.group(1), default if default is not None else "")

    def _is_unset_placeholder(value: str) -> bool:
        match = _ENV_PATTERN.fullmatch(value)
        return (
            match is not None and match.group(2) is None and os.environ.get(match.group(1)) is None
        )

    def _walk(value: Any) -> Any:
        if isinstance(value, str):
            return _ENV_PATTERN.sub(_replace_match, value)
        if isinstance(value, dict):
            for key in [
                k for k, v in value.items() if isinstance(v, str) and _is_unset_placeholder(v)
            ]:
                del value[key]
            for key in value:
                value[key] = _walk(value[key])
            return value
        if isinstance(value, list):
            for index in range(len(value)):
                value[index] = _walk(value[index])
            return value
        return value

    return _walk(config)


def _entry_key(entry: dict[str, Any]) -> str:
    return str(entry.get("id") or entry.get("instance_id") or entry.get("module") or "")


def _merge_module_entries(mount_plan: dict[str, Any], section: str, overlay: list[Any]) -> None:
    """Merge *overlay* module entries into ``mount_plan[section]`` by identity."""
    entries = mount_plan.setdefault(section, [])
    index_by_key = {
        _entry_key(entry): i for i, entry in enumerate(entries) if isinstance(entry, dict)
    }
    for item in overlay:
        if not isinstance(item, dict):
            continue
        key = _entry_key(item)
        if key and key in index_by_key:
            existing = entries[index_by_key[key]]
            merged = {**existing, **item}
            merge = merge_tool_configs if section == "tools" else deep_merge
            merged["config"] = merge(existing.get("config") or {}, item.get("config") or {})
            entries[index_by_key[key]] = merged
        else:
            entries.append(item)
            if key:
                index_by_key[key] = len(entries) - 1


# --------------------------------------------------------------------------
# Bundle discovery — project → user → packaged
# --------------------------------------------------------------------------


def packaged_bundles_dir() -> Path:
    """The bundles shipped inside this package (lowest precedence)."""
    return Path(__file__).resolve().parent.parent / "data" / "bundles"


def packaged_modes_dir() -> Path:
    """Native mode definitions shipped with this app (plan/brainstorm/careful).

    Fed into the mounted ``hooks-mode`` search_paths so the app's postures
    activate self-contained modes even on a clean install with no bundle
    overlays composed in."""
    return Path(__file__).resolve().parent.parent / "data" / "modes"


def inject_routing_config(
    mount_plan: dict[str, Any], settings: dict[str, Any], amplifier_home: Path
) -> None:
    """Bridge ``settings.routing`` into the mounted ``hooks-routing`` config.

    - ``routing.matrix`` → ``default_matrix`` (user picks the matrix);
    - ``routing.overrides`` → ``overrides``;
    - ``~/.amplifier/routing`` added to ``custom_routing_dirs`` (so user
      matrices there are visible to the hook, not just to a lister).
    No-op when ``hooks-routing`` isn't mounted. Never raises."""
    entry = None
    for hook in mount_plan.get("hooks") or []:
        if isinstance(hook, dict) and hook.get("module") == "hooks-routing":
            entry = hook
            break
    if entry is None:
        return
    config = entry.get("config")
    if not isinstance(config, dict):
        config = {}
        entry["config"] = config
    routing = settings.get("routing")
    if isinstance(routing, dict):
        if isinstance(routing.get("matrix"), str) and routing["matrix"]:
            config["default_matrix"] = routing["matrix"]
        if isinstance(routing.get("overrides"), dict):
            config["overrides"] = routing["overrides"]
    user_dir = amplifier_home / "routing"
    if user_dir.is_dir():
        dirs = config.get("custom_routing_dirs")
        if not isinstance(dirs, list):
            dirs = []
            config["custom_routing_dirs"] = dirs
        if str(user_dir) not in dirs:
            dirs.append(str(user_dir))


CONTEXT_INTELLIGENCE_HOOK = "hook-context-intelligence"
"""Module id of the upstream context-intelligence-logging telemetry hook.

Mounted only when the ``context-intelligence-logging`` behavior is composed
in (via a ``bundle.app`` overlay); this app never vendors a telemetry sink
of its own (issue #51 — mount upstream, don't reimplement)."""

_TELEMETRY_SCALAR_KEYS: dict[str, str] = {
    # settings ``telemetry.<key>`` -> hook config key (scalars only;
    # ``destinations`` / ``*_events`` are handled specially below).
    "server_url": "context_intelligence_server_url",
    "api_key": "context_intelligence_api_key",
    "workspace": "workspace",
    "log_level": "log_level",
    "base_path": "base_path",
    "project_slug": "project_slug",
    "dispatch_timeout": "dispatch_timeout",
    "dispatch_failure_threshold": "dispatch_failure_threshold",
    "dispatch_queue_capacity": "dispatch_queue_capacity",
    "close_drain_timeout": "close_drain_timeout",
}
"""Single-destination + dispatch-tuning knobs bridged straight through by name."""


def _context_intelligence_hook(mount_plan: dict[str, Any]) -> dict[str, Any] | None:
    """The mounted ``hook-context-intelligence`` entry, or ``None``."""
    for hook in mount_plan.get("hooks") or []:
        if isinstance(hook, dict) and hook.get("module") == CONTEXT_INTELLIGENCE_HOOK:
            return hook
    return None


def inject_telemetry_config(mount_plan: dict[str, Any], settings: dict[str, Any]) -> None:
    """Bridge ``settings.telemetry`` onto the mounted context-intelligence hook.

    The ``context-intelligence-logging`` behavior (module
    ``hook-context-intelligence``) is composed in through a ``bundle.app``
    overlay; this bridge maps the app's ``telemetry`` settings section onto
    that hook's config so custom telemetry destinations — and the legacy
    single-destination keys — are configurable through the same three-scope
    settings path as everything else. The app mounts the upstream sink and
    never reimplements one (issue #51).

    Mapped:

    - ``telemetry.destinations`` -> hook ``destinations`` (the upstream
      multi-destination map: per-destination ``url`` / ``api_key`` /
      ``.gitignore``-style ``include`` / ``exclude`` session routing /
      ``auth_mode: static|entra`` / dispatch tuning);
    - ``telemetry.server_url`` / ``api_key`` / ``workspace`` -> the legacy
      single-destination keys (older module builds without a map);
    - dispatch tuning + ``log_level`` scalars pass through by name
      (:data:`_TELEMETRY_SCALAR_KEYS`);
    - ``telemetry.exclude_events`` -> hook ``exclude_events`` (a ``[]`` value
      opts back in to every event, including the streaming deltas);
    - ``telemetry.additional_events`` is UNIONED onto whatever the behavior
      already ships, so its ``delegate:*`` coverage is never clobbered.

    A no-op when the hook is not mounted (the behavior wasn't composed) or
    when ``telemetry`` is absent/junk-shaped — so *no destinations configured
    = local JSONL capture only*, and an unconfigured app is untouched.
    ``${VAR}`` secrets in any injected value are expanded afterwards by
    :func:`expand_env_placeholders` (from ``keys.env``). Never raises.
    """
    entry = _context_intelligence_hook(mount_plan)
    if entry is None:
        return
    telemetry = settings.get("telemetry")
    if not isinstance(telemetry, dict):
        return
    config = entry.get("config")
    if not isinstance(config, dict):
        config = {}
        entry["config"] = config

    for settings_key, config_key in _TELEMETRY_SCALAR_KEYS.items():
        value = telemetry.get(settings_key)
        if value is not None:
            config[config_key] = value

    destinations = telemetry.get("destinations")
    if isinstance(destinations, dict) and destinations:
        config["destinations"] = destinations

    exclude_events = telemetry.get("exclude_events")
    if isinstance(exclude_events, list):
        config["exclude_events"] = list(exclude_events)

    extra_events = telemetry.get("additional_events")
    if isinstance(extra_events, list) and extra_events:
        existing = config.get("additional_events")
        merged = list(existing) if isinstance(existing, list) else []
        for name in extra_events:
            if name not in merged:
                merged.append(name)
        config["additional_events"] = merged


# --------------------------------------------------------------------------
# Notifications bridge (config.notifications.*) -- desktop ladder + ntfy push
# --------------------------------------------------------------------------

NOTIFY_PUSH_HOOK = "hooks-notify-push"
"""Module id of the mounted off-machine ntfy push hook (amplifier-bundle-notify).

The wrapper bundle mounts it inert (no-op unless ``AMPLIFIER_NTFY_TOPIC`` is
set); :func:`inject_notifications_config` bridges the non-secret ntfy knobs
from settings onto its config, mirroring :func:`inject_telemetry_config`."""

# The attention-ladder env vars the native OSC 777 path (``ui/notifications``)
# already reads. Mirrored here as literals (kernel must not import ``ui`` --
# ADR-0007 layering) so settings can be lowered onto the same contract.
_NOTIFY_ENV = "AMPLIFIER_NOTIFY"
_NOTIFY_TERMINAL_ENV = "AMPLIFIER_TERMINAL_NOTIFICATIONS"

# ntfy push env vars owned by the hooks-notify-push module. The topic is a
# secret (public ntfy topics are world-readable) and is read ONLY from the
# environment/keys.env -- never bridged from a settings scope.
_NTFY_TOPIC_ENV = "AMPLIFIER_NTFY_TOPIC"
_NTFY_SERVER_ENV = "AMPLIFIER_NTFY_SERVER"
_NOTIFY_PUSH_ENABLED_ENV = "AMPLIFIER_NOTIFY_PUSH_ENABLED"

_PUSH_PASSTHROUGH_KEYS = ("priority", "tags", "debug")
"""ntfy push config keys with no env equivalent -- taken from settings as-is."""

_TRUE_STRINGS = frozenset({"true", "1", "yes", "on"})
_FALSE_STRINGS = frozenset({"false", "0", "no", "off"})


def _coerce_bool(value: Any) -> bool | None:
    """A real bool, a YAML-ish bool string, or ``None`` when unrecognized."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in _TRUE_STRINGS:
            return True
        if low in _FALSE_STRINGS:
            return False
    return None


def notification_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """The ``config.notifications`` section of merged settings (``{}`` if absent).

    Donor parity: amplifier-app-cli keeps notification config under
    ``config.notifications`` (``AppSettings.get_notification_config``); newtui
    reads the same key so a settings file shared between the two apps agrees.
    Any non-dict shape yields ``{}`` so an unconfigured/junk section is inert.
    """
    config = settings.get("config")
    if not isinstance(config, dict):
        return {}
    notifications = config.get("notifications")
    return notifications if isinstance(notifications, dict) else {}


def merged_push_settings(notifications: dict[str, Any]) -> dict[str, Any]:
    """Merge the ``push`` + ``ntfy`` blocks into one dict (ntfy wins on conflict).

    ntfy is the only push transport today, so the two blocks are aliases; the
    more specific ``ntfy`` key wins for field-level values (donor parity:
    ``get_notification_hook_overrides`` merges the same way). Non-dict blocks
    are ignored. The secret ``topic`` is intentionally left in place here but
    is never lowered onto the hook config by :func:`inject_notifications_config`.
    """
    merged: dict[str, Any] = {}
    for block in (notifications.get("push"), notifications.get("ntfy")):
        if isinstance(block, dict):
            merged.update(block)
    return merged


def apply_notification_ladder_env(
    settings: dict[str, Any], environ: MutableMapping[str, str] | None = None
) -> None:
    """Lower ``config.notifications`` desktop/suppress keys onto the ladder env.

    The native attention ladder (``ui/notifications``) is driven by two env
    vars: ``AMPLIFIER_NOTIFY`` (off/bell/desktop ceiling) and
    ``AMPLIFIER_TERMINAL_NOTIFICATIONS`` (desktop-rung gate). Rather than teach
    the pure ladder about settings, the two honored keys are lowered onto those
    vars here -- the same "settings seed the environment, an explicit env var
    always wins" contract as :func:`load_keys_env`:

    - ``notifications.suppress: true`` -> ``AMPLIFIER_NOTIFY=off`` (silence the
      whole local ladder: bell + desktop);
    - ``notifications.desktop.enabled: false`` ->
      ``AMPLIFIER_TERMINAL_NOTIFICATIONS=off`` (drop the desktop rung, keep the
      bell); ``true`` -> ``=force`` (desktop on any terminal, bypassing the
      render allowlist -- the user opted in explicitly).

    Precedence: an already-set env var is never overwritten (explicit env
    wins over settings). A no-op when ``config.notifications`` is absent/junk,
    so an unconfigured app's environment is byte-identical to today. Never
    raises. Idempotent.
    """
    env = os.environ if environ is None else environ
    notifications = notification_settings(settings)
    if not notifications:
        return
    if _coerce_bool(notifications.get("suppress")) and _NOTIFY_ENV not in env:
        env[_NOTIFY_ENV] = "off"
    desktop = notifications.get("desktop")
    if isinstance(desktop, dict) and _NOTIFY_TERMINAL_ENV not in env:
        enabled = _coerce_bool(desktop.get("enabled"))
        if enabled is False:
            env[_NOTIFY_TERMINAL_ENV] = "off"
        elif enabled is True:
            env[_NOTIFY_TERMINAL_ENV] = "force"


def inject_notifications_config(
    mount_plan: dict[str, Any],
    settings: dict[str, Any],
    environ: Mapping[str, str] | None = None,
) -> None:
    """Bridge ``config.notifications.push``/``ntfy`` onto the ntfy push hook.

    Mirrors :func:`inject_telemetry_config`: finds the mounted
    ``hooks-notify-push`` entry and folds the non-secret ntfy knobs a user can
    safely keep in a settings scope onto its config, preserving the mounted
    ``listen_event`` (and anything else already there):

    - ``enabled`` / ``server`` -- env-aware: the ``AMPLIFIER_NOTIFY_PUSH_ENABLED``
      / ``AMPLIFIER_NTFY_SERVER`` vars win, so settings only fill an unset var
      (consistent "explicit env wins" precedence);
    - ``priority`` / ``tags`` / ``debug`` -- no env equivalent, taken from
      settings as-is.

    The ntfy *topic* is deliberately NOT bridged: the push module treats it as
    a secret and reads it only from ``AMPLIFIER_NTFY_TOPIC`` (keys.env), so
    ``notify set topic`` writes there, never a settings scope. A no-op when the
    hook isn't mounted or ``config.notifications`` is absent/junk -- so an
    unconfigured app is byte-identical to today. Never raises.
    """
    entry = None
    for hook in mount_plan.get("hooks") or []:
        if isinstance(hook, dict) and hook.get("module") == NOTIFY_PUSH_HOOK:
            entry = hook
            break
    if entry is None:
        return
    push = merged_push_settings(notification_settings(settings))
    if not push:
        return
    env = os.environ if environ is None else environ
    config = entry.get("config")
    if not isinstance(config, dict):
        config = {}
        entry["config"] = config
    enabled = _coerce_bool(push.get("enabled"))
    if enabled is not None and not env.get(_NOTIFY_PUSH_ENABLED_ENV):
        config["enabled"] = enabled
    server = push.get("server")
    if isinstance(server, str) and server and not env.get(_NTFY_SERVER_ENV):
        config["server"] = server
    for key in _PUSH_PASSTHROUGH_KEYS:
        if key in push:
            config[key] = push[key]


def inject_mode_search_paths(mount_plan: dict[str, Any], modes_dir: Path) -> None:
    """Add *modes_dir* to the mounted ``hooks-mode`` config's search_paths.

    A no-op when ``hooks-mode`` is not mounted. Idempotent — the dir is
    only appended once."""
    for hook in mount_plan.get("hooks") or []:
        if not isinstance(hook, dict) or hook.get("module") != "hooks-mode":
            continue
        config = hook.get("config")
        if not isinstance(config, dict):
            config = {}
            hook["config"] = config
        paths = config.get("search_paths")
        if not isinstance(paths, list):
            paths = []
            config["search_paths"] = paths
        target = str(modes_dir)
        if target not in paths:
            paths.append(target)


def ensure_project_write_path(mount_plan: dict[str, Any], project_dir: Path) -> None:
    """Keep the session project writable when users add extra directories.

    ``tool-filesystem`` defaults to the session working directory only while
    ``allowed_write_paths`` is absent. Once a user supplies that list, its
    default disappears, so stamp the resolved project root explicitly. This
    mirrors amplifier-app-cli's ``'.'`` policy without depending on process cwd.
    """
    project = str(project_dir.resolve())
    for tool in mount_plan.get("tools") or []:
        if not isinstance(tool, dict) or tool.get("module") != "tool-filesystem":
            continue
        config = tool.get("config")
        if not isinstance(config, dict):
            config = {}
            tool["config"] = config
        paths = config.get("allowed_write_paths")
        if not isinstance(paths, list):
            paths = []
        config["allowed_write_paths"] = [project, *(p for p in paths if p != project)]


def bundle_search_paths(project_dir: Path, amplifier_home: Path) -> tuple[Path, ...]:
    """Search order (highest precedence first): project → user → packaged."""
    return (
        project_dir / ".amplifier" / "bundles",
        amplifier_home / "bundles",
        packaged_bundles_dir(),
    )


def is_bundle_uri(name: str) -> bool:
    return name.startswith(_URI_PREFIXES)


def discover_bundle(name: str, search_paths: tuple[Path, ...] | list[Path]) -> str | None:
    """Resolve a bundle *name* to a loadable URI.

    URIs pass straight through, as do plain local paths that point at an
    existing bundle file (``./bundles/dev.md``, ``/abs/bundle.md``) or a
    directory holding a ``bundle.md`` / ``bundle.yaml``. Bare names are
    looked up in each search path as ``<name>.md`` / ``<name>.yaml`` /
    ``<name>/bundle.md`` / ``<name>/bundle.yaml``; first hit wins.
    """
    if is_bundle_uri(name):
        return name
    # A plain filesystem path (relative or absolute) that resolves to a
    # bundle file/dir is a valid source — foundation's load_bundle takes
    # local paths directly (URI_FORMATS.md), so don't force a URI prefix.
    if any(sep in name for sep in ("/", "\\")) or name.endswith((".md", ".yaml")):
        path = Path(name).expanduser()
        if path.is_file():
            return str(path)
        for candidate in ("bundle.md", "bundle.yaml"):
            if (path / candidate).is_file():
                return str(path / candidate)
    for base in search_paths:
        for pattern in _BUNDLE_FILE_CANDIDATES:
            candidate = base / pattern.format(name=name)
            if candidate.is_file():
                return str(candidate)
    return None


def resolve_bundle_name(
    name: str,
    settings: dict[str, Any],
    search_paths: tuple[Path, ...] | list[Path],
) -> str | None:
    """Resolve a bundle *name* to a loadable URI, consulting ``bundle.added``.

    Precedence (first hit wins):

    1. **Local / packaged discovery** (:func:`discover_bundle`) — a URI, a
       filesystem path, or a bare name found in the project/user/packaged
       search paths. This keeps the builtin :data:`DEFAULT_BUNDLE` and any
       on-disk project bundle authoritative and matches ``list_bundles``,
       where a local bundle overrides a same-named ``bundle.added`` entry.
    2. **The ``bundle.added`` registry** (:func:`added_bundle_uris`) — what
       ``bundle add <name> <uri>`` persisted. Without this step a
       ``bundle use <added-name>`` (which writes ``bundle.active: <name>``)
       resolves to nothing at boot and the caller silently falls back to the
       default; consulting it here is the fix for that redirect being ignored.
       A registered value is itself run back through :func:`discover_bundle`
       so a URI, a local path, or a bare name all load uniformly.

    Returns ``None`` when neither resolves, so the default-bundle fallback path
    is left unchanged when no added bundle matches.
    """
    uri = discover_bundle(name, search_paths)
    if uri is not None:
        return uri
    registered = added_bundle_uris(settings).get(name)
    if registered:
        return discover_bundle(registered, search_paths) or registered
    return None


def resolve_bundle_source(
    bundle: str | None,
    settings: dict[str, Any],
    search_paths: tuple[Path, ...] | list[Path],
) -> tuple[str, str, str | None]:
    """Resolve which bundle to boot: explicit arg → settings → default.

    The chosen name is resolved through :func:`resolve_bundle_name`, so a
    ``bundle.added`` registration (``bundle add <name> <uri>``) is honored —
    ``bundle use <added-name>`` loads that bundle instead of silently falling
    back to the default (issue #105). Local/packaged bundles keep precedence
    over a same-named added entry.

    An explicit *bundle* argument that can't resolve raises — the caller
    asked for it by name. A settings-configured bundle that can't resolve
    degrades to :data:`DEFAULT_BUNDLE` with a notice (third element) so a
    settings file shared with another amplifier app never kills the boot
    (field report: ``bundle.active: anchors`` → "session failed to start").
    """
    name = bundle or active_bundle_name(settings) or DEFAULT_BUNDLE
    uri = resolve_bundle_name(name, settings, search_paths)
    notice: str | None = None
    if uri is None and bundle is None and name != DEFAULT_BUNDLE:
        notice = (
            f"bundle '{name}' not found — started '{DEFAULT_BUNDLE}' instead "
            f"(amplifier-newtui bundle list shows options)"
        )
        name = DEFAULT_BUNDLE
        uri = resolve_bundle_name(name, settings, search_paths)
    if uri is None:
        available = ", ".join(list_available_bundles(search_paths)) or "none"
        raise BundleNotFoundError(
            f"Bundle '{name}' not found in project, user, or packaged "
            f"bundle paths. Available bundles: {available}"
        )
    return name, uri, notice


def list_available_bundles(search_paths: tuple[Path, ...] | list[Path]) -> tuple[str, ...]:
    """Names discoverable across all search paths (for error messages)."""
    names: list[str] = []
    seen: set[str] = set()
    for base in search_paths:
        if not base.is_dir():
            continue
        for entry in sorted(base.iterdir()):
            if entry.is_file() and entry.suffix in (".md", ".yaml"):
                name = entry.stem
            elif entry.is_dir() and (
                (entry / "bundle.md").is_file() or (entry / "bundle.yaml").is_file()
            ):
                name = entry.name
            else:
                continue
            if name not in seen:
                seen.add(name)
                names.append(name)
    return tuple(names)


# --------------------------------------------------------------------------
# The golden path
# --------------------------------------------------------------------------


class BundleNotFoundError(FileNotFoundError):
    """The requested bundle name resolved to nothing in any search path."""


@dataclass(frozen=True)
class ResolvedConfig:
    """Everything session creation needs, resolved exactly once.

    ``mount_plan`` is ``prepared.mount_plan`` itself (post-overrides) —
    the same dict object, never a copy.
    """

    bundle_name: str
    bundle_uri: str
    settings: dict[str, Any]
    prepared: Any  # amplifier_foundation.bundle.PreparedBundle
    mount_plan: dict[str, Any]
    overlays: tuple[str, ...] = field(default=())
    project_dir: Path = field(default_factory=Path.cwd)
    fallback_notice: str | None = None
    """Set when a settings-configured bundle failed discovery and the app
    default was booted instead — the runtime surfaces it as a Notification."""
    settings_notice: str | None = None
    """Set when a settings.yaml scope was malformed and skipped — the
    runtime surfaces it as a Notification, so a whole ignored scope is not
    buried in a silent ``logger.warning`` (doctor also flags it at rest)."""


async def resolve_config(
    bundle: str | None = None,
    *,
    project_dir: Path | None = None,
    amplifier_home: Path | None = None,
    install_deps: bool = True,
    progress: Callable[[str, str], None] | None = None,
    provider_override: str | None = None,
    model_override: str | None = None,
) -> ResolvedConfig:
    """The single configuration golden path (see module docstring).

    Args:
        bundle: Bundle name or URI. Falls back to settings
            ``bundle.active``, then :data:`DEFAULT_BUNDLE`.
        project_dir: Project root (default: cwd).
        amplifier_home: Amplifier home dir (default: ``~/.amplifier``).
        install_deps: Passed to ``Bundle.prepare()``.
        progress: Optional ``(action, detail)`` progress callback.

    Raises:
        BundleNotFoundError: When the bundle cannot be discovered.
    """
    project_dir = (project_dir or Path.cwd()).resolve()
    amplifier_home = amplifier_home or (Path.home() / ".amplifier")

    # 0. Provider creds/endpoints live in ~/.amplifier/keys.env; load them
    #    before ${VAR} expansion so settings placeholders resolve like the
    #    reference CLI (else keys.env-only providers fail to mount).
    load_keys_env(amplifier_home)

    # 1. Settings: three-scope deep merge. A malformed scope is skipped
    #    (settings must never block startup) but reported so the boot can
    #    say so out loud instead of silently dropping the whole scope.
    settings, malformed_settings = load_merged_settings_reporting(
        SettingsPaths.default(project_dir, amplifier_home)
    )
    settings_notice = malformed_settings_notice(malformed_settings)

    # 2. Bundle discovery.
    search_paths = bundle_search_paths(project_dir, amplifier_home)
    bundle_name, uri, fallback_notice = resolve_bundle_source(bundle, settings, search_paths)

    # 3. Foundation lifecycle: load → compose overlays → prepare() ONCE.
    from amplifier_foundation import load_bundle  # lazy: keep module import light

    # sources.bundles (source add --bundles) redirects bundle *include* URIs.
    # A registry carrying that include-source resolver must back every
    # load_bundle call so the redirect reaches foundation's include resolution;
    # with no bundle-source override this is None and load_bundle keeps its own
    # default registry (behavior unchanged).
    registry = _bundle_registry_for(settings, amplifier_home)

    if progress:
        progress("loading", bundle_name)
    root = await load_bundle(uri, registry=registry)

    overlays = composed_overlay_uris(settings)
    if overlays:
        if progress:
            progress("composing", f"{len(overlays)} overlay bundle(s)")
        overlay_bundles = [
            await load_bundle(overlay_uri, registry=registry) for overlay_uri in overlays
        ]
        composed = root.compose(*overlay_bundles)
    else:
        composed = root

    if progress:
        progress("preparing", bundle_name)
    prepared = await composed.prepare(
        install_deps=install_deps,
        source_resolver=build_source_resolver(settings),
        progress_callback=progress,
    )

    # 4. Settings overrides — applied to prepared.mount_plan in place —
    #    then ${VAR} placeholder expansion (reference: amplifier-app-cli
    #    expands the effective bundle config before session creation).
    mount_plan = apply_module_overrides(prepared.mount_plan, settings)
    # Per-invocation ``run --provider/--model`` overrides — ephemeral to THIS
    # boot (they mutate the in-memory plan, never a settings scope file).
    apply_run_overrides(mount_plan, provider=provider_override, model=model_override)
    apply_compaction_settings(mount_plan, settings)
    ensure_project_write_path(mount_plan, project_dir)
    # settings ``id`` → kernel ``instance_id`` so a provider mounts under
    # its configured id (reference CLI parity); prevents a false
    # 'provider unavailable' when the config names an instance.
    map_provider_ids_to_instance_ids(mount_plan)
    # Point the mounted mode system at the app's own mode definitions so the
    # plan/brainstorm/careful postures work self-contained (approvals stay
    # OFF until a posture activates one of these — feature-mapping.md).
    inject_mode_search_paths(mount_plan, packaged_modes_dir())
    inject_routing_config(mount_plan, settings, amplifier_home)
    # Bridge settings.telemetry -> the composed context-intelligence-logging
    # hook (custom destinations + legacy single-destination keys); a no-op
    # unless that behavior is composed in via a bundle.app overlay (issue #51).
    inject_telemetry_config(mount_plan, settings)
    # Bridge settings.config.notifications -> the mounted ntfy push hook
    # (non-secret knobs) and lower the desktop/suppress keys onto the
    # attention-ladder env vars the native OSC 777 path reads; both no-op
    # (byte-identical) unless config.notifications is configured (issue #106).
    inject_notifications_config(mount_plan, settings)
    apply_notification_ladder_env(settings)
    expand_env_placeholders(mount_plan)

    return ResolvedConfig(
        bundle_name=bundle_name,
        bundle_uri=uri,
        settings=settings,
        prepared=prepared,
        mount_plan=mount_plan,
        overlays=overlays,
        project_dir=project_dir,
        fallback_notice=fallback_notice,
        settings_notice=settings_notice,
    )


def get_project_slug(project_dir: Path | None = None) -> str:
    """Deterministic project slug from the project directory path.

    ``/Users/me/dev/proj`` → ``-Users-me-dev-proj`` (matches the
    amplifier-app-cli convention so session storage is shared).
    """
    resolved = (project_dir or Path.cwd()).resolve()
    slug = str(resolved).replace("/", "-").replace("\\", "-").replace(":", "")
    if not slug.startswith("-"):
        slug = "-" + slug
    return slug


__all__ = [
    "DEFAULT_BUNDLE",
    "BundleNotFoundError",
    "ResolvedConfig",
    "SettingsPaths",
    "ProviderNotConfiguredError",
    "active_bundle_name",
    "added_bundle_uris",
    "apply_module_overrides",
    "apply_run_overrides",
    "build_bundle_include_resolver",
    "build_source_resolver",
    "bundle_search_paths",
    "bundle_source_overrides",
    "deep_merge",
    "discover_bundle",
    "expand_env_placeholders",
    "ensure_project_write_path",
    "inject_mode_search_paths",
    "inject_routing_config",
    "inject_telemetry_config",
    "inject_notifications_config",
    "apply_notification_ladder_env",
    "notification_settings",
    "merged_push_settings",
    "NOTIFY_PUSH_HOOK",
    "packaged_modes_dir",
    "get_project_slug",
    "is_bundle_uri",
    "list_available_bundles",
    "load_keys_env",
    "load_merged_settings",
    "load_merged_settings_reporting",
    "malformed_settings_notice",
    "merge_settings",
    "merge_tool_configs",
    "map_provider_ids_to_instance_ids",
    "overlay_uris",
    "composed_overlay_uris",
    "routing_enabled",
    "ROUTING_MATRIX_BUNDLE_URI",
    "packaged_bundles_dir",
    "resolve_bundle_name",
    "resolve_bundle_source",
    "resolve_config",
]
