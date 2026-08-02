"""Routing-matrix discovery + selection (``amplifier-tui routing list/use``).

amplifier-app-cli exposes ``routing list/use/show/...`` through its own
``AppSettings`` plus a routing-matrix bundle cache; tui re-expresses the
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

import datetime
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from . import bundle_admin
from .bundle_admin import Scope, read_scope, scope_file, write_scope
from .config import ROUTING_MATRIX_BUNDLE_URI, SettingsPaths, load_merged_settings

logger = logging.getLogger(__name__)

DEFAULT_MATRIX = "balanced"
"""Matrix name assumed active when settings pick none (app-cli parity)."""

_ROUTING_BUNDLE_GLOB = "amplifier-bundle-routing-matrix-*"


def _amplifier_home(amplifier_home: Path | None) -> Path:
    # Mirror bundle_admin's resolution (AMPLIFIER_HOME-aware) so every admin
    # surface agrees on where config/cache live. Late-bound module attribute
    # (not a from-import): this module is often imported lazily mid-session,
    # and a from-import would permanently snapshot whatever settings_paths
    # was at that moment — including a test's monkeypatch (real order-
    # dependent failures in test_routing_cli).
    return bundle_admin.settings_paths(None, amplifier_home).global_settings.parent


def custom_routing_dir(amplifier_home: Path | None = None) -> Path:
    """Where user-authored matrices live (``<home>/routing``)."""
    return _amplifier_home(amplifier_home) / "routing"


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


def _run_coro(coro):  # noqa: ANN001, ANN202 — any coroutine/result
    """Run a coroutine from sync code, loop-safe.

    ``routing list``/``use`` are plain sync click commands (no loop —
    ``asyncio.run`` is fine), but interactive setup can call this from inside
    an ``asyncio.run(...)`` body, where a bare ``asyncio.run`` raises
    ``RuntimeError``. In that case run it on a worker thread's own loop.
    """
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _ensure_routing_bundle_cached(amplifier_home: Path) -> None:
    """Best-effort fetch of the ``routing-matrix`` bundle into the cache.

    Called only when no bundle-cache matrices exist yet, so ``routing list``
    can work on a clean install. A pre-existing ``routing-matrix``
    registration wins; otherwise the well-known curated bundle
    (:data:`~amplifier_app_tui.kernel.config.ROUTING_MATRIX_BUNDLE_URI`) is
    fetched — loading by URI auto-registers it for next time. Offline-safe:
    any failure is logged and swallowed (the caller then simply reports no
    matrices).
    """
    try:
        from amplifier_foundation import BundleRegistry
    except Exception:  # noqa: BLE001 — foundation optional/offline
        return
    try:
        registry = BundleRegistry(home=amplifier_home)
        target = registry.find("routing-matrix") or ROUTING_MATRIX_BUNDLE_URI
        _run_coro(registry.load(target))
    except Exception as exc:  # noqa: BLE001 — network/registry best-effort
        logger.warning("Could not fetch routing-matrix bundle: %s", exc)


def discover_matrix_files(amplifier_home: Path | None = None, *, fetch: bool = False) -> list[Path]:
    """Discover routing-matrix YAML files (bundle cache + user dir), sorted.

    Looks in ``<home>/cache/amplifier-bundle-routing-matrix-*/routing/*.yaml``
    then ``<home>/routing/*.yaml``. When *fetch* is set and no bundle-cache
    matrices are present, lazily fetches the bundle first (best-effort).
    """
    home = _amplifier_home(amplifier_home)
    files: list[Path] = []

    cache_base = home / "cache"
    bundle_dirs = sorted(cache_base.glob(_ROUTING_BUNDLE_GLOB)) if cache_base.is_dir() else []
    if not bundle_dirs and fetch:
        _ensure_routing_bundle_cached(home)
        bundle_dirs = sorted(cache_base.glob(_ROUTING_BUNDLE_GLOB)) if cache_base.is_dir() else []
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


def check_compatibility(matrix_data: dict[str, Any], provider_types: set[str]) -> tuple[int, int]:
    """Count roles with at least one configured provider: ``(covered, total)``."""
    roles = _roles(matrix_data)
    covered = 0
    for role_config in roles.values():
        if not isinstance(role_config, dict):
            continue
        candidates = role_config.get("candidates")
        if not isinstance(candidates, list):
            continue
        if any(isinstance(c, dict) and c.get("provider") in provider_types for c in candidates):
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
        candidates = role_config.get("candidates") if isinstance(role_config, dict) else None
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
    settings = load_merged_settings(bundle_admin.settings_paths(project_dir, amplifier_home))
    matrices = load_all_matrices(discover_matrix_files(amplifier_home, fetch=fetch))
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


# --------------------------------------------------------------------------
# Provider selectors + effective resolution (`routing show`)
# --------------------------------------------------------------------------


def _provider_type_name(entry: dict[str, Any]) -> str:
    """Bare module type of a provider entry (``provider-anthropic`` -> ``anthropic``)."""
    module = str(entry.get("module", ""))
    return module.removeprefix("provider-") if module.startswith("provider-") else module


def _provider_entries(settings: dict[str, Any]) -> list[dict[str, Any]]:
    config = settings.get("config")
    providers = config.get("providers") if isinstance(config, dict) else None
    if not isinstance(providers, list):
        return []
    return [entry for entry in providers if isinstance(entry, dict)]


def provider_selectors(settings: dict[str, Any]) -> list[str]:
    """Ordered provider selectors a matrix candidate may target.

    Returns the bare module type (e.g. ``"anthropic"``) for single-instance
    modules — what the spawner's type-name resolution expects. When multiple
    instances of one module are configured (e.g. two ``provider-chat-completions``
    with distinct ``id:`` values) the bare type is ambiguous, so each instance's
    ``id`` is returned instead, mirroring app-cli's ``_get_provider_names``.
    """
    providers = _provider_entries(settings)
    type_counts: dict[str, int] = {}
    for entry in providers:
        name = _provider_type_name(entry)
        if name:
            type_counts[name] = type_counts.get(name, 0) + 1
    seen: set[str] = set()
    selectors: list[str] = []
    for entry in providers:
        name = _provider_type_name(entry)
        if not name:
            continue
        instance_id = entry.get("id")
        selector = (
            (instance_id if isinstance(instance_id, str) and instance_id else name)
            if type_counts[name] > 1
            else name
        )
        if selector not in seen:
            seen.add(selector)
            selectors.append(selector)
    return selectors


def provider_default_model(settings: dict[str, Any], selector: str) -> str | None:
    """The configured ``default_model`` for a provider selector, if any.

    ``selector`` may be a bare module type or an instance ``id``. Used by
    ``routing show`` to display the model a role actually resolves to (the
    provider's configured default) rather than the matrix candidate's pattern.
    """
    for entry in _provider_entries(settings):
        if entry.get("id") == selector or _provider_type_name(entry) == selector:
            cfg = entry.get("config")
            if isinstance(cfg, dict):
                default_model = cfg.get("default_model")
                if isinstance(default_model, str) and default_model:
                    return default_model
    return None


def primary_provider_type(settings: dict[str, Any]) -> str | None:
    """The bare type of the first configured provider (the routing default)."""
    providers = _provider_entries(settings)
    if not providers:
        return None
    return _provider_type_name(providers[0]) or None


@dataclass(frozen=True)
class ResolvedRole:
    role: str
    model: str | None
    provider: str | None


def resolve_effective(
    matrix_data: dict[str, Any], settings: dict[str, Any]
) -> tuple[ResolvedRole, ...]:
    """Role -> effective (model, provider), with configured default_model applied.

    Each role resolves to its first candidate served by a configured provider
    (see ``resolve_matrix``); when that provider pins a ``default_model`` the
    displayed model reflects it, matching what the runtime would actually use.
    """
    provider_types = configured_provider_types(settings)
    rows: list[ResolvedRole] = []
    for row in resolve_matrix(matrix_data, provider_types):
        model = row.model
        if row.provider:
            pinned = provider_default_model(settings, row.provider)
            if pinned:
                model = pinned
        rows.append(ResolvedRole(role=row.role, model=model, provider=row.provider))
    return tuple(rows)


@dataclass(frozen=True)
class CandidateView:
    provider: str
    model: str
    config: dict[str, Any]
    configured: bool
    active: bool


@dataclass(frozen=True)
class RoleWaterfall:
    role: str
    description: str
    candidates: tuple[CandidateView, ...]
    servable: bool


def matrix_waterfall(
    matrix_data: dict[str, Any], provider_types: set[str]
) -> tuple[RoleWaterfall, ...]:
    """Full candidate waterfall per role for the detailed ``routing show`` view.

    Each candidate is flagged ``configured`` (its provider is available) and
    the first configured candidate per role is flagged ``active`` (the winner).
    """
    out: list[RoleWaterfall] = []
    for role_name, role_config in _roles(matrix_data).items():
        description = ""
        raw_candidates: Any = None
        if isinstance(role_config, dict):
            description = str(role_config.get("description", ""))
            raw_candidates = role_config.get("candidates")
        views: list[CandidateView] = []
        winner_found = False
        if isinstance(raw_candidates, list):
            for candidate in raw_candidates:
                if not isinstance(candidate, dict):
                    continue
                provider = str(candidate.get("provider", ""))
                model = str(candidate.get("model", "?"))
                cfg = candidate.get("config")
                cfg = dict(cfg) if isinstance(cfg, dict) else {}
                configured = provider in provider_types
                active = configured and not winner_found
                if active:
                    winner_found = True
                views.append(CandidateView(provider, model, cfg, configured, active))
        out.append(RoleWaterfall(str(role_name), description, tuple(views), winner_found))
    return tuple(out)


# --------------------------------------------------------------------------
# Role discovery + custom-matrix authoring (`routing create` / `manage`)
# --------------------------------------------------------------------------


_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")


def matrix_name_valid(name: str) -> bool:
    """Whether *name* is a legal matrix (and filename) identifier."""
    return bool(_NAME_RE.match(name))


def discover_roles(matrix_files: list[Path]) -> dict[str, str]:
    """Unique ``role -> description`` across all matrices (first description wins)."""
    roles: dict[str, str] = {}
    for path in matrix_files:
        data = load_matrix(path)
        if not data:
            continue
        for role_name, role_config in _roles(data).items():
            if role_name not in roles:
                desc = role_config.get("description", "") if isinstance(role_config, dict) else ""
                roles[str(role_name)] = str(desc)
    return roles


def build_custom_matrix(
    name: str,
    assignments: dict[str, dict[str, str]],
    *,
    updated: str | None = None,
) -> dict[str, Any]:
    """Assemble a custom-matrix dict from ``role -> {description, provider, model}``."""
    roles: dict[str, Any] = {}
    for role_name, info in assignments.items():
        roles[role_name] = {
            "description": info.get("description", ""),
            "candidates": [{"provider": info["provider"], "model": info["model"]}],
        }
    return {
        "name": name,
        "description": f"Custom matrix: {name}",
        "updated": updated or datetime.date.today().isoformat(),
        "roles": roles,
    }


def save_matrix(matrix_data: dict[str, Any], output_dir: Path) -> Path:
    """Write *matrix_data* to ``<output_dir>/<name>.yaml`` and return the path."""
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / f"{matrix_data['name']}.yaml"
    output_path.write_text(
        yaml.safe_dump(matrix_data, default_flow_style=False, sort_keys=False),
        encoding="utf-8",
    )
    return output_path


__all__ = [
    "DEFAULT_MATRIX",
    "CandidateView",
    "MatrixEntry",
    "ResolvedRole",
    "RoleResolution",
    "RoleWaterfall",
    "active_matrix",
    "build_custom_matrix",
    "check_compatibility",
    "configured_provider_types",
    "custom_routing_dir",
    "discover_matrix_files",
    "discover_roles",
    "list_matrices",
    "load_all_matrices",
    "load_matrix",
    "matrix_name_valid",
    "matrix_waterfall",
    "primary_provider_type",
    "provider_default_model",
    "provider_selectors",
    "resolve_effective",
    "resolve_matrix",
    "save_matrix",
    "set_active_matrix",
]
