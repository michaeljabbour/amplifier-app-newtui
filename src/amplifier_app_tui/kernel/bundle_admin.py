"""Bundle management: the logic behind ``amplifier-tui bundle …``.

amplifier-app-cli exposes ``bundle list/show/use/clear/current/add/
remove/update`` through its own ``AppSettings`` + ``AppBundleDiscovery``.
tui is NOT built on app-cli's classes, so this module reuses the two
layers that ARE shared:

- **tui's own settings/discovery** (``kernel/config.py``) for the
  scope files and local bundle search — canonical ``tui.bundle.active``
  (with legacy ``bundle.active`` fallback), plus the shared
  ``bundle.added`` / ``bundle.app`` keys the runtime already reads.
- **amplifier-foundation** (``load_bundle`` / ``check_bundle_status`` /
  ``update_bundle`` / ``walk_include_chains``) for URI resolution,
  include chains and remote status — imported lazily so ``--demo`` and
  the offline tests never touch the network.

Everything except the foundation-backed calls is pure file/dict work, so
it unit-tests against a ``tmp_path`` with no amplifier session.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from .config import (
    SettingsPaths,
    TUI_SETTINGS_NAMESPACE,
    active_bundle_name,
    added_bundle_uris,
    bundle_search_paths,
    discover_bundle,
    is_bundle_uri,
    list_available_bundles,
    load_merged_settings,
    overlay_uris,
)

Scope = Literal["global", "project", "local"]
SCOPES: tuple[Scope, ...] = ("global", "project", "local")


def _amplifier_home(amplifier_home: Path | None) -> Path:
    """Resolve the amplifier home dir.

    An explicit argument always wins (tests). Otherwise honor the
    foundation-native ``AMPLIFIER_HOME`` env var (same resolution as
    ``amplifier_foundation.paths.resolution.get_amplifier_home``), then
    fall back to ``~/.amplifier``.
    """
    if amplifier_home is not None:
        return amplifier_home
    env_home = os.environ.get("AMPLIFIER_HOME")
    if env_home:
        return Path(env_home).expanduser()
    return Path.home() / ".amplifier"


def settings_paths(project_dir: Path | None, amplifier_home: Path | None) -> SettingsPaths:
    return SettingsPaths.default(
        (project_dir or Path.cwd()).resolve(), _amplifier_home(amplifier_home)
    )


def scope_file(paths: SettingsPaths, scope: Scope) -> Path:
    return {
        "global": paths.global_settings,
        "project": paths.project_settings,
        "local": paths.local_settings,
    }[scope]


def read_scope(path: Path) -> dict[str, Any]:
    """One scope's raw settings dict (``{}`` when missing/malformed)."""
    if not path.is_file():
        return {}
    try:
        content = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return content if isinstance(content, dict) else {}


def write_scope(path: Path, data: dict[str, Any]) -> None:
    """Persist a scope dict atomically (tmp-file → replace), mkdir parents.

    An empty dict removes the file rather than leaving a stray ``{}`` — a
    cleared scope should look untouched."""
    if not data:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    tmp.replace(path)


def _bundle_section(data: dict[str, Any]) -> dict[str, Any]:
    section = data.get("bundle")
    if not isinstance(section, dict):
        section = {}
        data["bundle"] = section
    return section


def _tui_bundle_section(data: dict[str, Any]) -> dict[str, Any]:
    """Canonical app-owned ``tui.bundle`` section, created as needed."""
    namespace = data.get(TUI_SETTINGS_NAMESPACE)
    if not isinstance(namespace, dict):
        namespace = {}
        data[TUI_SETTINGS_NAMESPACE] = namespace
    section = namespace.get("bundle")
    if not isinstance(section, dict):
        section = {}
        namespace["bundle"] = section
    return section


def _prune_tui_bundle(data: dict[str, Any]) -> None:
    """Drop empty canonical containers after an app preference is removed."""
    namespace = data.get(TUI_SETTINGS_NAMESPACE)
    if not isinstance(namespace, dict):
        return
    section = namespace.get("bundle")
    if isinstance(section, dict) and not section:
        namespace.pop("bundle", None)
    if not namespace:
        data.pop(TUI_SETTINGS_NAMESPACE, None)


# -- active bundle (bundle use / clear / current) ---------------------------


def set_active_bundle(paths: SettingsPaths, name: str, scope: Scope) -> Path:
    """Write ``tui.bundle.active: <name>`` into *scope*.

    The legacy top-level ``bundle.active`` is preserved for migration and for
    other Amplifier apps.  TUI reads prefer this canonical namespaced value.
    """
    path = scope_file(paths, scope)
    data = read_scope(path)
    _tui_bundle_section(data)["active"] = name
    write_scope(path, data)
    return path


def clear_active_bundle(paths: SettingsPaths, scope: Scope) -> bool:
    """Clear this app's active bundle at *scope* without clobbering legacy data.

    When a legacy ``bundle.active`` remains, a namespaced ``null`` tombstone
    masks it so ``bundle clear`` does not make the old value reappear.  With no
    legacy fallback the canonical key is removed and empty containers are
    pruned normally.
    """
    path = scope_file(paths, scope)
    data = read_scope(path)
    namespace = data.get(TUI_SETTINGS_NAMESPACE)
    tui_bundle = namespace.get("bundle") if isinstance(namespace, dict) else None
    namespaced_present = isinstance(tui_bundle, dict) and "active" in tui_bundle
    namespaced_value = tui_bundle.get("active") if isinstance(tui_bundle, dict) else None
    legacy_bundle = data.get("bundle")
    legacy_present = isinstance(legacy_bundle, dict) and "active" in legacy_bundle

    # A canonical null already masks any legacy fallback: effectively clear.
    if namespaced_present and namespaced_value is None:
        return False
    if not namespaced_present and not legacy_present:
        return False

    if legacy_present:
        _tui_bundle_section(data)["active"] = None
    else:
        assert isinstance(tui_bundle, dict)
        tui_bundle.pop("active", None)
        _prune_tui_bundle(data)
    write_scope(path, data)
    return True


# -- bundle registry (bundle add / remove) ----------------------------------


def added_bundles(settings: dict[str, Any]) -> dict[str, str]:
    """``bundle.added`` name→URI registry from merged settings.

    Thin alias for :func:`kernel.config.added_bundle_uris` — the boot path
    (:func:`resolve_bundle_source`) and this CLI read the registry from one
    home, so ``bundle add`` / ``bundle list`` / ``bundle use`` can never
    disagree about what a name resolves to."""
    return added_bundle_uris(settings)


def add_bundle(
    paths: SettingsPaths, name: str, uri: str, scope: Scope, *, as_app: bool = False
) -> Path:
    """Register a bundle for discovery (``bundle.added``); ``as_app`` also
    composes it onto every session (``bundle.app`` overlay, app-cli ``--app``)."""
    path = scope_file(paths, scope)
    data = read_scope(path)
    section = _bundle_section(data)
    added = section.get("added")
    if not isinstance(added, dict):
        added = {}
        section["added"] = added
    added[name] = uri
    if as_app:
        app = section.get("app")
        if not isinstance(app, list):
            app = []
            section["app"] = app
        if uri not in app:
            app.append(uri)
    write_scope(path, data)
    return path


def remove_bundle(paths: SettingsPaths, name: str, scope: Scope) -> bool:
    """Drop *name* from ``bundle.added`` (+ its URI from ``bundle.app``)."""
    path = scope_file(paths, scope)
    data = read_scope(path)
    section = data.get("bundle")
    if not isinstance(section, dict):
        return False
    added = section.get("added")
    removed = False
    uri = ""
    if isinstance(added, dict) and name in added:
        uri = str(added.pop(name))
        removed = True
        if not added:
            section.pop("added", None)
    app = section.get("app")
    if isinstance(app, list) and uri and uri in app:
        app.remove(uri)
        if not app:
            section.pop("app", None)
    if removed:
        if not section:
            data.pop("bundle", None)
        write_scope(path, data)
    return removed


# -- discovery (bundle list / current) --------------------------------------


@dataclass(frozen=True)
class BundleEntry:
    name: str
    active: bool
    source: str  # "local" | "registry" | "app" | "added"
    uri: str = ""


def _registry_entries(
    amplifier_home: Path | None, active: str | None, app_uris: set[str], all_bundles: bool
) -> dict[str, BundleEntry]:
    """User-selectable bundles from the shared foundation ``BundleRegistry``.

    This is the same registry amplifier-app-cli lists (well-known + fetched
    bundles). Default hides nested dependency bundles (only root /
    explicitly-requested); ``all_bundles`` includes every registered name.
    Degrades to ``{}`` if foundation is unavailable — never raises."""
    try:
        from amplifier_foundation import BundleRegistry  # lazy: offline stays offline
    except Exception:  # noqa: BLE001
        return {}
    try:
        registry = BundleRegistry(home=_amplifier_home(amplifier_home))
        names = registry.list_registered()
    except Exception:  # noqa: BLE001
        return {}
    entries: dict[str, BundleEntry] = {}
    for name in names:
        try:
            state = registry.get_state(name)
        except Exception:  # noqa: BLE001
            state = None
        if not all_bundles and state is not None:
            selectable = getattr(state, "is_root", False) or getattr(
                state, "explicitly_requested", False
            )
            if not selectable:
                continue
        uri = ""
        try:
            uri = registry.find(name) or ""
        except Exception:  # noqa: BLE001
            uri = ""
        source = "app" if uri and uri in app_uris else "registry"
        entries[name] = BundleEntry(name=name, active=name == active, source=source, uri=uri)
    return entries


def list_bundles(
    project_dir: Path | None = None,
    amplifier_home: Path | None = None,
    *,
    all_bundles: bool = False,
) -> tuple[BundleEntry, ...]:
    """All bundles a user can select: locally-discovered on disk, the shared
    foundation registry (well-known + fetched), settings ``bundle.app``
    overlays and ``bundle.added`` registrations — each flagged active/source.

    ``all_bundles`` also surfaces nested dependency bundles from the
    registry (app-cli's ``--all``)."""
    paths = settings_paths(project_dir, amplifier_home)
    settings = load_merged_settings(paths)
    active = active_bundle_name(settings)
    app_uris = set(overlay_uris(settings))
    search = bundle_search_paths(
        paths.project_settings.parent.parent, _amplifier_home(amplifier_home)
    )

    # Foundation registry first, then local on-disk + settings registrations
    # override its entry (local/added is more specific for this project).
    entries: dict[str, BundleEntry] = _registry_entries(
        amplifier_home, active, app_uris, all_bundles
    )
    for name in list_available_bundles(search):
        location = discover_bundle(name, search) or ""
        entries[name] = BundleEntry(name=name, active=name == active, source="local", uri=location)
    for name, uri in added_bundles(settings).items():
        if name not in entries:
            entries[name] = BundleEntry(name=name, active=name == active, source="added", uri=uri)
    return tuple(sorted(entries.values(), key=lambda entry: entry.name))


def current_bundle(
    project_dir: Path | None = None, amplifier_home: Path | None = None
) -> str | None:
    """The active bundle name from merged settings (``None`` → the default)."""
    paths = settings_paths(project_dir, amplifier_home)
    return active_bundle_name(load_merged_settings(paths))


# -- foundation-backed (bundle show / update / add validation) ---------------


@dataclass(frozen=True)
class BundleInfo:
    name: str
    version: str = ""
    description: str = ""
    uri: str = ""
    includes: tuple[str, ...] = field(default_factory=tuple)
    providers: int = 0
    tools: int = 0
    hooks: int = 0
    agents: int = 0


async def load_bundle_info(uri: str) -> BundleInfo | None:
    """Resolve a bundle URI/name via foundation and summarize it.

    Returns ``None`` when foundation cannot load it (bad URI, offline for
    a remote source, …). Never raises — the CLI reports the miss."""
    try:
        from amplifier_foundation import load_bundle  # lazy: offline stays offline
    except Exception:  # noqa: BLE001
        return None
    # Resolve a bare local name (e.g. the packaged ``tui``) to its
    # on-disk path via tui's own discovery; URIs pass straight through.
    from .config import bundle_search_paths, discover_bundle

    paths = settings_paths(None, None)
    search = bundle_search_paths(paths.project_settings.parent.parent, _amplifier_home(None))
    target = discover_bundle(uri, search) or uri
    try:
        bundle = await load_bundle(target, auto_include=False)
    except Exception:  # noqa: BLE001 — surfaced as "could not load" by the caller
        return None
    plan: dict[str, Any] = {}
    to_plan = getattr(bundle, "to_mount_plan", None)
    if callable(to_plan):
        try:
            produced = to_plan()
            if isinstance(produced, dict):
                plan = produced
        except Exception:  # noqa: BLE001
            plan = {}

    def _count(section: str) -> int:
        value = plan.get(section)
        return len(value) if isinstance(value, list) else 0

    includes = getattr(bundle, "includes", ()) or ()
    return BundleInfo(
        name=str(getattr(bundle, "name", uri) or uri),
        version=str(getattr(bundle, "version", "") or ""),
        description=str(getattr(bundle, "description", "") or ""),
        uri=str(getattr(bundle, "uri", uri) or uri),
        includes=tuple(str(i) for i in includes),
        providers=_count("providers"),
        tools=_count("tools"),
        hooks=_count("hooks"),
        agents=_count("agents"),
    )


async def check_updates(name: str) -> str | None:
    """A one-line update-status summary for *name* via foundation.

    ``None`` when foundation/status is unavailable; never raises."""
    try:
        from amplifier_foundation import BundleRegistry, check_bundle_status
    except Exception:  # noqa: BLE001
        return None
    try:
        registry = BundleRegistry(home=_amplifier_home(None))
        loaded = await registry.load(name)
        bundle = next(iter(loaded.values())) if isinstance(loaded, dict) else loaded
        status = await check_bundle_status(bundle)
    except Exception:  # noqa: BLE001
        return None
    summary = getattr(status, "summary", None)
    if summary:
        return str(summary)
    return "updates available" if getattr(status, "has_updates", False) else "up to date"


@dataclass(frozen=True)
class WarmResult:
    """Outcome of warming (pre-installing) a bundle's modules."""

    ok: bool
    name: str
    message: str


async def warm_bundle(
    uri: str,
    *,
    project_dir: Path | None = None,
    amplifier_home: Path | None = None,
    progress: Any = None,
) -> WarmResult:
    """Pre-install a bundle's modules ONCE, out of the boot install burst.

    The tui-side mitigation for foundation's fragile mass install (RCA: a
    cold boot runs ``activate_all`` across every overlay's modules at once and
    one intermittently gets killed). Warming a bundle at ``bundle add`` time —
    or explicitly via ``bundle warm`` — prepares it with ``install_deps=True``
    so its modules land in the module cache; a later boot that composes it then
    only ever *skips* the install. Foundation owns the actual install; tui
    only controls *when* it runs (add-time, not the boot path).

    Never raises: a load/prepare failure comes back as ``ok=False`` with the
    reason so the CLI reports the miss and exits cleanly (offline stays offline
    — foundation is imported lazily inside :func:`config.prepare_overlay_bundle`)."""
    from .config import prepare_overlay_bundle

    paths = settings_paths(project_dir, amplifier_home)
    settings = load_merged_settings(paths)
    home = _amplifier_home(amplifier_home)
    target = discover_bundle(uri, bundle_search_paths(paths.project_settings.parent.parent, home))
    resolved_uri = target or uri
    try:
        mount_plan = await prepare_overlay_bundle(
            resolved_uri, settings, amplifier_home=home, install_deps=True, progress=progress
        )
    except Exception as error:  # noqa: BLE001 — surfaced as a CLI miss, never a traceback
        return WarmResult(ok=False, name=uri, message=str(error) or type(error).__name__)

    def _count(section: str) -> int:
        value = mount_plan.get(section)
        return len(value) if isinstance(value, list) else 0

    summary = (
        f"{_count('providers')} providers · {_count('tools')} tools · "
        f"{_count('hooks')} hooks · {_count('agents')} agents"
    )
    return WarmResult(ok=True, name=uri, message=f"modules ready ({summary})")


__all__ = [
    "SCOPES",
    "BundleEntry",
    "BundleInfo",
    "Scope",
    "WarmResult",
    "add_bundle",
    "added_bundles",
    "check_updates",
    "warm_bundle",
    "clear_active_bundle",
    "current_bundle",
    "is_bundle_uri",
    "list_bundles",
    "load_bundle_info",
    "read_scope",
    "remove_bundle",
    "scope_file",
    "set_active_bundle",
    "settings_paths",
    "write_scope",
]
