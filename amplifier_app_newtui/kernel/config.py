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
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

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


def load_merged_settings(paths: SettingsPaths) -> dict[str, Any]:
    """Load and deep-merge all three settings scopes.

    Missing or malformed files are skipped silently — settings must never
    prevent startup (matching amplifier-app-cli ``AppSettings`` behavior).
    """
    merged: dict[str, Any] = {}
    for path in paths.in_merge_order():
        if not path.is_file():
            continue
        try:
            content = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            logger.warning("Skipping malformed settings file: %s", path)
            continue
        if isinstance(content, dict):
            merged = deep_merge(merged, content)
    return merged


def active_bundle_name(settings: dict[str, Any]) -> str | None:
    """Read ``bundle.active`` from merged settings."""
    bundle_section = settings.get("bundle")
    if isinstance(bundle_section, dict):
        active = bundle_section.get("active")
        if isinstance(active, str) and active:
            return active
    return None


def overlay_uris(settings: dict[str, Any]) -> tuple[str, ...]:
    """App/behavior bundles (``bundle.app``) composed onto every session."""
    bundle_section = settings.get("bundle")
    if not isinstance(bundle_section, dict):
        return ()
    app_bundles = bundle_section.get("app")
    if not isinstance(app_bundles, list):
        return ()
    return tuple(str(uri) for uri in app_bundles if uri)


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


def apply_module_overrides(
    mount_plan: dict[str, Any], settings: dict[str, Any]
) -> dict[str, Any]:
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
                        entry["config"] = deep_merge(entry.get("config") or {}, override_config)

    # config.providers — provider entries merged by identity (id | module).
    provider_overrides = (settings.get("config") or {}).get("providers")
    if isinstance(provider_overrides, list) and provider_overrides:
        _merge_module_entries(mount_plan, "providers", provider_overrides)

    # modules.tools — tool config overrides merged by module id.
    tool_overrides = (settings.get("modules") or {}).get("tools")
    if isinstance(tool_overrides, list) and tool_overrides:
        _merge_module_entries(mount_plan, "tools", tool_overrides)

    return mount_plan


def _entry_key(entry: dict[str, Any]) -> str:
    return str(entry.get("id") or entry.get("instance_id") or entry.get("module") or "")


def _merge_module_entries(
    mount_plan: dict[str, Any], section: str, overlay: list[Any]
) -> None:
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
            merged["config"] = deep_merge(existing.get("config") or {}, item.get("config") or {})
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

    URIs pass straight through. Names are looked up in each search path
    as ``<name>.md`` / ``<name>.yaml`` / ``<name>/bundle.md`` /
    ``<name>/bundle.yaml``; first hit wins.
    """
    if is_bundle_uri(name):
        return name
    for base in search_paths:
        for pattern in _BUNDLE_FILE_CANDIDATES:
            candidate = base / pattern.format(name=name)
            if candidate.is_file():
                return str(candidate)
    return None


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


async def resolve_config(
    bundle: str | None = None,
    *,
    project_dir: Path | None = None,
    amplifier_home: Path | None = None,
    install_deps: bool = True,
    progress: Callable[[str, str], None] | None = None,
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

    # 1. Settings: three-scope deep merge.
    settings = load_merged_settings(SettingsPaths.default(project_dir, amplifier_home))

    # 2. Bundle discovery.
    bundle_name = bundle or active_bundle_name(settings) or DEFAULT_BUNDLE
    search_paths = bundle_search_paths(project_dir, amplifier_home)
    uri = discover_bundle(bundle_name, search_paths)
    if uri is None:
        available = ", ".join(list_available_bundles(search_paths)) or "none"
        raise BundleNotFoundError(
            f"Bundle '{bundle_name}' not found in project, user, or packaged "
            f"bundle paths. Available bundles: {available}"
        )

    # 3. Foundation lifecycle: load → compose overlays → prepare() ONCE.
    from amplifier_foundation import load_bundle  # lazy: keep module import light

    if progress:
        progress("loading", bundle_name)
    root = await load_bundle(uri)

    overlays = overlay_uris(settings)
    if overlays:
        if progress:
            progress("composing", f"{len(overlays)} overlay bundle(s)")
        overlay_bundles = [await load_bundle(overlay_uri) for overlay_uri in overlays]
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

    # 4. Settings overrides — applied to prepared.mount_plan in place.
    mount_plan = apply_module_overrides(prepared.mount_plan, settings)

    return ResolvedConfig(
        bundle_name=bundle_name,
        bundle_uri=uri,
        settings=settings,
        prepared=prepared,
        mount_plan=mount_plan,
        overlays=overlays,
        project_dir=project_dir,
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
    "active_bundle_name",
    "apply_module_overrides",
    "build_source_resolver",
    "bundle_search_paths",
    "deep_merge",
    "discover_bundle",
    "get_project_slug",
    "is_bundle_uri",
    "list_available_bundles",
    "load_merged_settings",
    "overlay_uris",
    "packaged_bundles_dir",
    "resolve_config",
]
