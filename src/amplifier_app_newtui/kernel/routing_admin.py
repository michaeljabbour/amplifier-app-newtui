"""Routing-matrix discovery + selection (``amplifier-newtui routing list/use``).

amplifier-app-cli exposes ``routing list/use/show/...`` through its own
``AppSettings`` plus a routing-matrix bundle cache; newtui re-expresses the
inspect/choose surface over the SAME data the runtime already reads:

- **discovered matrices** — the composed ``routing-matrix`` bundle's
  ``routing/*.yaml`` in the shared foundation cache
  (``<home>/cache/amplifier-bundle-routing-matrix-*/routing/``) plus user
  matrices in ``<home>/routing/`` — exactly the ``custom_routing_dirs`` that
  ``config.inject_routing_config`` feeds to ``hooks-routing``.
- **active matrix + selection** — settings ``routing.matrix``, the very key
  ``config.inject_routing_config`` bridges into ``hooks-routing``'s
  ``default_matrix``.
- **compatibility** — the configured providers in settings ``config.providers``
  (same identity rule the spawner routes by: bare module type + instance id).

Discovery is pure filesystem work over a scoped ``amplifier_home``, so it
unit-tests against ``tmp_path`` with no session and no network. The optional
lazy bundle fetch is best-effort and offline-safe (foundation imported lazily).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .bundle_admin import Scope, read_scope, scope_file, settings_paths, write_scope
from .config import SettingsPaths, load_merged_settings

logger = logging.getLogger(__name__)

DEFAULT_MATRIX = "balanced"
"""Matrix name assumed active when settings pick none (app-cli parity)."""

_ROUTING_BUNDLE_GLOB = "amplifier-bundle-routing-matrix-*"


def _amplifier_home(amplifier_home: Path | None) -> Path:
    # Mirror bundle_admin's resolution (AMPLIFIER_HOME-aware) so every admin
    # surface agrees on where config/cache live.
    return settings_paths(None, amplifier_home).global_settings.parent


def custom_routing_dir(amplifier_home: Path | None = None) -> Path:
    """Where user-authored matrices live (``<home>/routing``)."""
    return _amplifier_home(amplifier_home) / "routing"


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


def _ensure_routing_bundle_cached(amplifier_home: Path) -> None:
    """Best-effort fetch of the ``routing-matrix`` bundle into the cache.

    Called only when no bundle-cache matrices exist yet, so ``routing list``
    can work on a clean install. Offline-safe: any failure is logged and
    swallowed (the caller then simply reports no matrices).
    """
    try:
        from amplifier_foundation import BundleRegistry
    except Exception:  # noqa: BLE001 — foundation optional/offline
        return
    try:
        registry = BundleRegistry(home=amplifier_home)
        if "routing-matrix" not in registry.list_registered():
            return
        import asyncio

        asyncio.run(registry.load("routing-matrix"))
    except Exception as exc:  # noqa: BLE001 — network/registry best-effort
        logger.warning("Could not fetch routing-matrix bundle: %s", exc)


def discover_matrix_files(
    amplifier_home: Path | None = None, *, fetch: bool = False
) -> list[Path]:
    """Discover routing-matrix YAML files (bundle cache + user dir), sorted.

    Looks in ``<home>/cache/amplifier-bundle-routing-matrix-*/routing/*.yaml``
    then ``<home>/routing/*.yaml``. When *fetch* is set and no bundle-cache
    matrices are present, lazily fetches the bundle first (best-effort).
    """
    home = _amplifier_home(amplifier_home)
    files: list[Path] = []

    cache_base = home / "cache"
    bundle_dirs = (
        sorted(cache_base.glob(_ROUTING_BUNDLE_GLOB)) if cache_base.is_dir() else []
    )
    if not bundle_dirs and fetch:
        _ensure_routing_bundle_cached(home)
        bundle_dirs = (
            sorted(cache_base.glob(_ROUTING_BUNDLE_GLOB)) if cache_base.is_dir() else []
        )
    for bundle_dir in bundle_dirs:
        routing_dir = bundle_dir / "routing"
        if routing_dir.is_dir():
            files.extend(routing_dir.glob("*.yaml"))

    user_dir = home / "routing"
    if user_dir.is_dir():
        files.extend(user_dir.glob("*.yaml"))

    return sorted(files)


def load_matrix(path: Path) -> dict[str, Any] | None:
    """Load one matrix YAML file (``None`` when missing/malformed)."""
    try:
        content = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return None
    return content if isinstance(content, dict) else None


def load_all_matrices(matrix_files: list[Path]) -> dict[str, dict[str, Any]]:
    """Load matrix files into a ``name -> data`` map (skips nameless/broken)."""
    matrices: dict[str, dict[str, Any]] = {}
    for path in matrix_files:
        data = load_matrix(path)
        if data and isinstance(data.get("name"), str):
            matrices[data["name"]] = data
    return matrices


# --------------------------------------------------------------------------
# Compatibility / resolution against configured providers
# --------------------------------------------------------------------------


def configured_provider_types(settings: dict[str, Any]) -> set[str]:
    """Provider identifiers a matrix candidate may reference.

    Includes each provider's bare module type (without the ``provider-``
    prefix) AND its instance ``id`` when set — both forms are valid candidate
    references, matching how the spawner resolves providers at routing time.
    """
    config = settings.get("config")
    providers = config.get("providers") if isinstance(config, dict) else None
    types: set[str] = set()
    if not isinstance(providers, list):
        return types
    for entry in providers:
        if not isinstance(entry, dict):
            continue
        module = str(entry.get("module", ""))
        if module.startswith("provider-"):
            types.add(module.removeprefix("provider-"))
        elif module:
            types.add(module)
        instance_id = entry.get("id")
        if isinstance(instance_id, str) and instance_id:
            types.add(instance_id)
    return types


def _roles(matrix_data: dict[str, Any]) -> dict[str, Any]:
    roles = matrix_data.get("roles")
    return roles if isinstance(roles, dict) else {}


def check_compatibility(
    matrix_data: dict[str, Any], provider_types: set[str]
) -> tuple[int, int]:
    """Count roles with at least one configured provider: ``(covered, total)``."""
    roles = _roles(matrix_data)
    covered = 0
    for role_config in roles.values():
        if not isinstance(role_config, dict):
            continue
        candidates = role_config.get("candidates")
        if not isinstance(candidates, list):
            continue
        if any(
            isinstance(c, dict) and c.get("provider") in provider_types
            for c in candidates
        ):
            covered += 1
    return covered, len(roles)


@dataclass(frozen=True)
class RoleResolution:
    role: str
    model: str | None
    provider: str | None


def resolve_matrix(
    matrix_data: dict[str, Any], provider_types: set[str]
) -> tuple[RoleResolution, ...]:
    """Resolve each role to its first candidate served by a configured provider."""
    rows: list[RoleResolution] = []
    for role_name, role_config in _roles(matrix_data).items():
        model: str | None = None
        provider: str | None = None
        candidates = (
            role_config.get("candidates") if isinstance(role_config, dict) else None
        )
        if isinstance(candidates, list):
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                if candidate.get("provider") in provider_types:
                    provider = str(candidate.get("provider"))
                    model = str(candidate.get("model", "?"))
                    break
        rows.append(RoleResolution(role=str(role_name), model=model, provider=provider))
    return tuple(rows)


# --------------------------------------------------------------------------
# Active matrix (routing.matrix) — read/write
# --------------------------------------------------------------------------


def active_matrix(settings: dict[str, Any]) -> str:
    """The active matrix name from settings ``routing.matrix`` (or the default)."""
    routing = settings.get("routing")
    if isinstance(routing, dict):
        name = routing.get("matrix")
        if isinstance(name, str) and name:
            return name
    return DEFAULT_MATRIX


def set_active_matrix(paths: SettingsPaths, name: str, scope: Scope) -> Path:
    """Write ``routing.matrix: <name>`` into *scope* (preserves other routing keys)."""
    path = scope_file(paths, scope)
    data = read_scope(path)
    routing = data.get("routing")
    if not isinstance(routing, dict):
        routing = {}
        data["routing"] = routing
    routing["matrix"] = name
    write_scope(path, data)
    return path


# --------------------------------------------------------------------------
# `routing list`
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class MatrixEntry:
    name: str
    active: bool
    description: str
    updated: str
    covered: int
    total: int
    has_providers: bool


def list_matrices(
    project_dir: Path | None = None,
    amplifier_home: Path | None = None,
    *,
    fetch: bool = False,
) -> tuple[MatrixEntry, ...]:
    """Discovered matrices with active/compatibility flags, name-sorted."""
    settings = load_merged_settings(settings_paths(project_dir, amplifier_home))
    matrices = load_all_matrices(
        discover_matrix_files(amplifier_home, fetch=fetch)
    )
    active = active_matrix(settings)
    provider_types = configured_provider_types(settings)
    entries: list[MatrixEntry] = []
    for name, data in sorted(matrices.items()):
        covered, total = check_compatibility(data, provider_types)
        entries.append(
            MatrixEntry(
                name=name,
                active=name == active,
                description=str(data.get("description", "")),
                updated=str(data.get("updated", "")),
                covered=covered,
                total=total,
                has_providers=bool(provider_types),
            )
        )
    return tuple(entries)


__all__ = [
    "DEFAULT_MATRIX",
    "MatrixEntry",
    "RoleResolution",
    "active_matrix",
    "check_compatibility",
    "configured_provider_types",
    "custom_routing_dir",
    "discover_matrix_files",
    "list_matrices",
    "load_all_matrices",
    "load_matrix",
    "resolve_matrix",
    "set_active_matrix",
]
