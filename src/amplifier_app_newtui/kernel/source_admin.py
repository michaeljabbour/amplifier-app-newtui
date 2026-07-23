"""Module/bundle source-override administration (``amplifier-newtui source``).

amplifier-app-cli exposes a ``source`` group (add/remove/list/show) through
its own ``AppSettings``; newtui is NOT built on those classes, so this module
re-expresses the same capability over the layers that ARE shared:

- **newtui's own settings scope files** (``kernel/config.py`` +
  ``bundle_admin`` scope helpers) for the reads/writes. Overrides land in the
  exact keys the runtime already consumes:

  - module sources -> ``sources.modules.<id>`` (fed to
    ``config.build_source_resolver`` at ``Bundle.prepare()``);
  - bundle sources -> ``sources.bundles.<name>`` (bundle discovery override).

- **amplifier-foundation** naming/entry-point conventions for auto-detecting
  whether an identifier is a module or a bundle (pure inspection, no import).

Everything here is pure file/dict work over ``tmp_path``-able scope files, so
it unit-tests with no amplifier session and no real ``~/.amplifier``.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .bundle_admin import Scope, read_scope, scope_file, settings_paths, write_scope
from .config import SettingsPaths, is_bundle_uri, load_merged_settings

SourceKind = Literal["module", "bundle"]

# Identifier prefixes that name an amplifier *module* (provider/tool/hook/...).
_MODULE_PREFIXES: tuple[str, ...] = (
    "amplifier-module-",
    "provider-",
    "tool-",
    "hooks-",
    "loop-",
    "context-",
)

# Directories that mark a directory as a *bundle* rather than a module.
_BUNDLE_DIRS: tuple[str, ...] = ("agents", "context", "skills", "modules")


# --------------------------------------------------------------------------
# Auto-detection (module vs bundle) — foundation naming conventions, no import
# --------------------------------------------------------------------------


def _is_module_path(path: Path) -> bool:
    """A directory looks like a module: an ``amplifier.modules`` entry point or
    an ``amplifier-module-*`` name."""
    pyproject = path / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            data = {}
        project = data.get("project")
        entry_points = project.get("entry-points") if isinstance(project, dict) else None
        if isinstance(entry_points, dict) and "amplifier.modules" in entry_points:
            return True
    return path.name.startswith("amplifier-module-")


def _is_bundle_path(path: Path) -> bool:
    """A directory looks like a bundle: bundle resource dirs and NOT a module."""
    if _is_module_path(path):
        return False
    return any((path / dirname).is_dir() for dirname in _BUNDLE_DIRS)


def detect_source_type(identifier: str, source_uri: str) -> SourceKind:
    """Detect whether *identifier*/*source_uri* names a module or a bundle.

    Strategy (app-cli parity): inspect a local source directory first, then
    fall back to identifier naming conventions, then default to ``bundle``
    (bundles are the common case in user space).
    """
    source_path = Path(source_uri).expanduser()
    if not source_path.is_absolute():
        source_path = Path.cwd() / source_path
    if source_path.is_dir():
        if _is_module_path(source_path):
            return "module"
        if _is_bundle_path(source_path):
            return "bundle"
    if identifier.startswith(_MODULE_PREFIXES):
        return "module"
    return "bundle"


def is_local_source(source_uri: str) -> bool:
    """True when *source_uri* is a local filesystem path (not a fetchable URI).

    A source is remote when it carries a known URI scheme (``git+``,
    ``http(s)://``, ``file://``, ``zip+``); anything else is a local override.
    """
    return not is_bundle_uri(source_uri)


# --------------------------------------------------------------------------
# settings.sources.{modules,bundles} — read/write
# --------------------------------------------------------------------------


def _sources_bucket(
    data: dict[str, Any], kind: SourceKind, *, create: bool
) -> dict[str, Any] | None:
    """The ``sources.modules`` / ``sources.bundles`` dict inside *data*."""
    key = "modules" if kind == "module" else "bundles"
    sources = data.get("sources")
    if not isinstance(sources, dict):
        if not create:
            return None
        sources = {}
        data["sources"] = sources
    bucket = sources.get(key)
    if not isinstance(bucket, dict):
        if not create:
            return None
        bucket = {}
        sources[key] = bucket
    return bucket


def add_source(
    paths: SettingsPaths,
    kind: SourceKind,
    identifier: str,
    source_uri: str,
    scope: Scope,
) -> Path:
    """Write one ``sources.<modules|bundles>.<id> = uri`` override into *scope*."""
    path = scope_file(paths, scope)
    data = read_scope(path)
    bucket = _sources_bucket(data, kind, create=True)
    assert bucket is not None
    bucket[identifier] = source_uri
    write_scope(path, data)
    return path


def remove_source(
    paths: SettingsPaths,
    identifier: str,
    scope: Scope,
    *,
    module: bool = True,
    bundle: bool = True,
) -> tuple[bool, bool]:
    """Remove *identifier* from module and/or bundle overrides at *scope*.

    Returns ``(removed_module, removed_bundle)``. Empty ``modules`` /
    ``bundles`` / ``sources`` containers are pruned so a cleared scope looks
    untouched.
    """
    path = scope_file(paths, scope)
    data = read_scope(path)
    removed_module = _drop_source(data, "module", identifier) if module else False
    removed_bundle = _drop_source(data, "bundle", identifier) if bundle else False
    if removed_module or removed_bundle:
        write_scope(path, data)
    return removed_module, removed_bundle


def _drop_source(data: dict[str, Any], kind: SourceKind, identifier: str) -> bool:
    bucket = _sources_bucket(data, kind, create=False)
    if bucket is None or identifier not in bucket:
        return False
    del bucket[identifier]
    _prune_sources(data)
    return True


def _prune_sources(data: dict[str, Any]) -> None:
    sources = data.get("sources")
    if not isinstance(sources, dict):
        return
    for key in ("modules", "bundles"):
        inner = sources.get(key)
        if isinstance(inner, dict) and not inner:
            del sources[key]
    if not sources:
        data.pop("sources", None)


def cleanup_provider_config_source(paths: SettingsPaths, module_id: str, scope: Scope) -> bool:
    """Drop a local ``source`` from a ``config.providers`` entry for *module_id*.

    When a module source override is removed, a provider entry that still
    pins a *local* ``source`` path would keep resolving to the on-disk clone.
    Dropping that key lets foundation fall back to the module's default
    source. Returns True when a provider entry changed.
    """
    path = scope_file(paths, scope)
    data = read_scope(path)
    config = data.get("config")
    providers = config.get("providers") if isinstance(config, dict) else None
    if not isinstance(providers, list):
        return False
    changed = False
    for entry in providers:
        if not isinstance(entry, dict) or entry.get("module") != module_id:
            continue
        source = entry.get("source")
        if isinstance(source, str) and is_local_source(source):
            del entry["source"]
            changed = True
    if changed:
        write_scope(path, data)
    return changed


# --------------------------------------------------------------------------
# Reads for `source list` / `source show`
# --------------------------------------------------------------------------


def module_sources(settings: dict[str, Any]) -> dict[str, str]:
    """Merged ``sources.modules`` map from merged settings."""
    return _sources_map(settings, "module")


def bundle_sources(settings: dict[str, Any]) -> dict[str, str]:
    """Merged ``sources.bundles`` map from merged settings."""
    return _sources_map(settings, "bundle")


def _sources_map(settings: dict[str, Any], kind: SourceKind) -> dict[str, str]:
    bucket = _sources_bucket(settings, kind, create=False)
    if bucket is None:
        return {}
    return {str(k): str(v) for k, v in bucket.items()}


@dataclass(frozen=True)
class SourceEntry:
    name: str
    source_uri: str
    kind: SourceKind


def list_sources(
    project_dir: Path | None = None, amplifier_home: Path | None = None
) -> tuple[SourceEntry, ...]:
    """All configured source overrides (modules then bundles), name-sorted."""
    settings = load_merged_settings(settings_paths(project_dir, amplifier_home))
    entries: list[SourceEntry] = []
    for name, uri in sorted(module_sources(settings).items()):
        entries.append(SourceEntry(name=name, source_uri=uri, kind="module"))
    for name, uri in sorted(bundle_sources(settings).items()):
        entries.append(SourceEntry(name=name, source_uri=uri, kind="bundle"))
    return tuple(entries)


def _env_var_name(module_id: str) -> str:
    return f"AMPLIFIER_MODULE_{module_id.upper().replace('-', '_')}"


def effective_module_source(settings: dict[str, Any], module_id: str) -> str | None:
    """The source override newtui would apply for *module_id*.

    Precedence mirrors ``config.build_source_resolver``: ``sources.modules``
    is overridden by ``overrides.<id>.source``. ``None`` when neither is set.
    """
    result: str | None = module_sources(settings).get(module_id)
    overrides = settings.get("overrides")
    if isinstance(overrides, dict):
        override = overrides.get(module_id)
        if isinstance(override, dict) and isinstance(override.get("source"), str):
            result = override["source"]
    return result


@dataclass(frozen=True)
class ModuleResolution:
    """The layered resolution view for ``source show`` (newtui-visible layers)."""

    module_id: str
    env_var: str
    env_value: str | None
    workspace_path: str
    workspace_found: bool
    settings_source: str | None
    effective_source: str | None


def resolve_module(
    module_id: str,
    project_dir: Path | None = None,
    amplifier_home: Path | None = None,
) -> ModuleResolution:
    """Resolve where *module_id*'s source would come from (highest -> lowest).

    Reports the layers newtui itself owns: an ``AMPLIFIER_MODULE_*`` env
    override, a project ``.amplifier/modules/<id>`` workspace clone, and the
    merged settings ``sources.modules`` / ``overrides.<id>.source`` override.
    Package/bundle resolution is foundation-internal and left to prepare().
    """
    project = (project_dir or Path.cwd()).resolve()
    settings = load_merged_settings(settings_paths(project_dir, amplifier_home))
    env_var = _env_var_name(module_id)
    workspace = project / ".amplifier" / "modules" / module_id
    return ModuleResolution(
        module_id=module_id,
        env_var=env_var,
        env_value=os.environ.get(env_var),
        workspace_path=str(workspace),
        workspace_found=workspace.exists(),
        settings_source=module_sources(settings).get(module_id),
        effective_source=effective_module_source(settings, module_id),
    )


__all__ = [
    "ModuleResolution",
    "SourceEntry",
    "SourceKind",
    "add_source",
    "bundle_sources",
    "cleanup_provider_config_source",
    "detect_source_type",
    "effective_module_source",
    "is_local_source",
    "list_sources",
    "module_sources",
    "remove_source",
    "resolve_module",
]
