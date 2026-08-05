"""Keep the root model choice and delegated-model routing in sync.

The model a user chooses is the root/orchestrating model.  Routing matrices
answer a different question: which provider/model should a *delegated role*
use?  The two controls therefore move together without allowing the matrix to
replace the exact root model:

* setup persists the provider's matching matrix next to its default model;
* ``--provider`` + ``--model`` applies the same hint to the in-memory settings
  for that launch only; and
* ``/model`` retargets the live matrix resolver, agent defaults, and routing
  context while leaving the selected provider's ``default_model`` untouched.

Only a matrix that is known to ship with the routing bundle or exists on disk
is selected.  A custom provider without a same-named matrix keeps the current
matrix rather than activating a name the hook cannot load.
"""

from __future__ import annotations

import copy
import inspect
from importlib import import_module
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml

SHIPPED_PROVIDER_MATRICES: frozenset[str] = frozenset(
    {"anthropic", "copilot", "gemini", "ollama", "openai"}
)
"""Provider-family matrices guaranteed by the pinned routing bundle."""

_PROVIDER_ALIASES = {
    "github-copilot": "copilot",
    "provider-github-copilot": "copilot",
}


def amplifier_home(path: Path | None = None) -> Path:
    """Resolve the shared Amplifier home without inventing a second TUI home."""
    if path is not None:
        return path.expanduser()
    configured = os.environ.get("AMPLIFIER_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".amplifier"


def _matrix_paths(home: Path) -> tuple[Path, ...]:
    """Matrix files in hook precedence order: user first, then bundle cache."""
    paths: list[Path] = []
    user_dir = home / "routing"
    if user_dir.is_dir():
        paths.extend(sorted(user_dir.glob("*.yaml")))
    cache = home / "cache"
    if cache.is_dir():
        for bundle_dir in sorted(cache.glob("amplifier-bundle-routing-matrix-*"), reverse=True):
            routing_dir = bundle_dir / "routing"
            if routing_dir.is_dir():
                paths.extend(sorted(routing_dir.glob("*.yaml")))
    return tuple(paths)


def available_matrix_names(home: Path | None = None) -> frozenset[str]:
    """Names safe to auto-select without a network call."""
    root = amplifier_home(home)
    return frozenset({*SHIPPED_PROVIDER_MATRICES, *(path.stem for path in _matrix_paths(root))})


def _provider_token(value: str | None) -> str:
    token = str(value or "").strip()
    if token.startswith("provider-"):
        token = token.removeprefix("provider-")
    return _PROVIDER_ALIASES.get(token, token)


def provider_matrix_candidates(provider_name: str, module_id: str | None = None) -> tuple[str, ...]:
    """Safe matrix candidates for one provider selection.

    A known provider module family wins over a colliding instance nickname:
    an Anthropic provider called ``economy`` still selects ``anthropic``, not
    the unrelated curated economy strategy. Unknown/custom provider families
    remain instance-first so a named endpoint such as ``runpod`` can use its
    user-authored ``runpod.yaml`` matrix.
    """
    candidates: list[str] = []
    family = _provider_token(module_id)
    ordered = (
        (module_id, provider_name)
        if family in SHIPPED_PROVIDER_MATRICES
        else (provider_name, module_id)
    )
    for raw in ordered:
        candidate = _provider_token(raw)
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    return tuple(candidates)


def matching_matrix(
    provider_name: str,
    module_id: str | None = None,
    *,
    home: Path | None = None,
) -> str | None:
    """Return the same-named provider matrix, or ``None`` when none exists."""
    names = available_matrix_names(home)
    folded: dict[str, list[str]] = {}
    for name in names:
        folded.setdefault(name.casefold(), []).append(name)
    for candidate in provider_matrix_candidates(provider_name, module_id):
        if candidate in names:
            return candidate
        matches = folded.get(candidate.casefold(), ())
        if len(matches) == 1:
            return matches[0]
    return None


def _matching_matrix_from_identities(
    provider_name: str,
    identities: tuple[str, ...],
    *,
    home: Path | None = None,
) -> str | None:
    """Match a proven shipped family, then exact instance and family tokens."""
    shipped_families: set[str] = set()
    for identity in identities:
        normalized = _provider_token(identity)
        if normalized in SHIPPED_PROVIDER_MATRICES:
            shipped_families.add(normalized)
        tokens = {
            _PROVIDER_ALIASES.get(token.casefold(), token.casefold())
            for token in re.split(r"[^A-Za-z0-9]+", identity)
            if token
        }
        shipped_families.update(tokens.intersection(SHIPPED_PROVIDER_MATRICES))
    if len(shipped_families) == 1:
        family = next(iter(shipped_families))
        direct_family = matching_matrix(family, home=home)
        if direct_family is not None:
            return direct_family

    direct = matching_matrix(provider_name, home=home)
    if direct is not None:
        return direct
    for identity in identities:
        matrix = matching_matrix(provider_name, identity, home=home)
        if matrix is not None:
            return matrix

    names = available_matrix_names(home)
    matches: set[str] = set()
    for identity in (provider_name, *identities):
        tokens = {token.casefold() for token in re.split(r"[^A-Za-z0-9]+", identity) if token}
        for name in names:
            if name.casefold() in tokens:
                matches.add(name)
    return next(iter(matches)) if len(matches) == 1 else None


def _provider_entries(settings: dict[str, Any]) -> list[dict[str, Any]]:
    config = settings.get("config")
    providers = config.get("providers") if isinstance(config, dict) else None
    return [entry for entry in providers or () if isinstance(entry, dict)]


def _provider_priority(entry: dict[str, Any]) -> int:
    config = entry.get("config")
    value = config.get("priority", 100) if isinstance(config, dict) else 100
    return value if isinstance(value, int) and not isinstance(value, bool) else 100


def _entry_matches(entry: dict[str, Any], provider: str) -> bool:
    module = str(entry.get("module") or "")
    return provider in {
        str(entry.get("id") or ""),
        str(entry.get("instance_id") or ""),
        module,
        _provider_token(module),
    }


def apply_model_routing_hint(
    settings: dict[str, Any],
    *,
    provider: str | None = None,
    home: Path | None = None,
    force: bool = False,
) -> str | None:
    """Select a provider matrix in an in-memory settings mapping.

    With *provider*, the matching configured instance is used.  Without one,
    the priority provider is considered only when it already has a configured
    ``default_model``.  Existing explicit matrix choices win unless *force* is
    true (an explicit per-launch model choice).  No file is ever written.
    """
    routing = settings.get("routing")
    if not force and isinstance(routing, dict) and routing.get("matrix"):
        return str(routing["matrix"])
    entries = _provider_entries(settings)
    if provider:
        entry = next(
            (candidate for candidate in entries if _entry_matches(candidate, provider)), None
        )
        # An explicit provider family (for example ``--provider anthropic``)
        # is already enough to choose its same-named matrix even when the
        # provider itself comes from the bundle rather than settings.
        if entry is None:
            provider_name = str(provider)
            module_id = None
        else:
            provider_name = str(entry.get("id") or entry.get("instance_id") or provider)
            module_id = str(entry.get("module") or "")
    else:
        if not entries:
            return None
        entry = min(entries, key=_provider_priority)
        config = entry.get("config")
        if not isinstance(config, dict) or not config.get("default_model"):
            return None
        provider_name = str(entry.get("id") or entry.get("instance_id") or "")
        module_id = str(entry.get("module") or "")
    selected_provider = str(provider_name or module_id or "")
    identities = tuple(identity for identity in (module_id,) if identity)
    matrix = _matching_matrix_from_identities(selected_provider, identities, home=home)
    if matrix is None:
        return None
    if not isinstance(routing, dict):
        routing = {}
        settings["routing"] = routing
    routing["matrix"] = matrix
    if force:
        # Per-launch provider/model choices are explicit instructions to keep
        # delegated roles in the same provider family. A persisted
        # ``routing.enabled: false`` must not suppress the companion overlay
        # for this one in-memory boot (nothing here is written to disk).
        routing["enabled"] = True
    return matrix


def persist_model_routing_hint(
    paths: Any,
    scope: Literal["global", "project", "local"],
    *,
    provider_name: str,
    module_id: str | None = None,
    home: Path | None = None,
) -> tuple[str, Path] | None:
    """Persist the matching provider matrix beside a selected model.

    Setup owns the provider's exact ``default_model`` while the routing
    manager owns ``routing.matrix``.  Keeping the write here gives every
    setup surface one shared provider-instance-first matching rule without
    teaching provider configuration about matrix-file discovery.

    ``None`` is intentional for providers that have no same-named matrix:
    configuring a custom endpoint must not replace an explicit routing choice
    with a matrix name the hook cannot load.
    """
    root = home
    if root is None:
        settings_path = getattr(paths, "global_settings", None)
        if isinstance(settings_path, Path):
            root = settings_path.parent
    matrix = matching_matrix(provider_name, module_id, home=root)
    if matrix is None:
        return None

    # Local import avoids making the pure matching helpers depend on the
    # settings writer at import time.
    from .routing_admin import set_active_matrix

    path = set_active_matrix(paths, matrix, scope)
    return matrix, path


def find_matrix_file(name: str, home: Path | None = None) -> Path | None:
    """Find *name* using the same user-before-bundle precedence as the hook."""
    target = name.casefold()
    return next(
        (path for path in _matrix_paths(amplifier_home(home)) if path.stem.casefold() == target),
        None,
    )


def _runtime_provider_identities(coordinator: Any, provider_name: str) -> tuple[str, ...]:
    """Return configured and mounted identities for a live provider.

    Explicit ``/model <provider> <model>`` is allowed even when the session's
    coordinator config is not available (some embedders only expose mounted
    mechanisms).  Mounted provider objects still normally identify their
    family through an attribute or their implementation class, so include
    those values as a read-only inference surface.
    """
    identities: list[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str) and value.strip() and value not in identities:
            identities.append(value.strip())

    config = getattr(coordinator, "config", None)
    entries = config.get("providers") if isinstance(config, dict) else None
    for entry in entries or ():
        if isinstance(entry, dict) and _entry_matches(entry, provider_name):
            for key in ("module", "id", "instance_id"):
                add(entry.get(key))

    try:
        providers = (
            coordinator.get("providers") if callable(getattr(coordinator, "get", None)) else {}
        )
    except Exception:  # noqa: BLE001 - mounted-object inference is best effort
        providers = {}
    provider = providers.get(provider_name) if isinstance(providers, dict) else None
    if provider is not None:
        for attribute in ("module", "module_id", "provider_type", "provider_name", "name"):
            add(getattr(provider, attribute, None))
        provider_config = getattr(provider, "config", None)
        if isinstance(provider_config, dict):
            for key in ("module", "module_id", "provider", "provider_type"):
                add(provider_config.get(key))
        add(type(provider).__module__)
        add(type(provider).__name__)
    return tuple(identities)


def _runtime_matching_matrix(
    coordinator: Any,
    provider_name: str,
    *,
    home: Path | None = None,
) -> str | None:
    """Infer one unambiguous provider-family matrix from live state."""
    identities = _runtime_provider_identities(coordinator, provider_name)
    # Instance names such as ``anthropic-east`` and implementation names such
    # as ``amplifier_module_provider_anthropic`` still carry an unambiguous
    # family even when ``coordinator.config`` is absent. Whole-token matching
    # prevents a substring like "ai" selecting an unrelated matrix.
    return _matching_matrix_from_identities(provider_name, identities, home=home)


def _hook_entry(coordinator: Any) -> dict[str, Any] | None:
    config = getattr(coordinator, "config", None)
    hooks = config.get("hooks") if isinstance(config, dict) else None
    for entry in hooks or ():
        if isinstance(entry, dict) and entry.get("module") == "hooks-routing":
            return entry
    return None


def _preference_dict(preference: Any) -> dict[str, Any]:
    converter = getattr(preference, "to_dict", None)
    if callable(converter):
        value = converter()
        if isinstance(value, dict):
            return value
    provider = getattr(preference, "provider", None)
    model = getattr(preference, "model", None)
    result: dict[str, Any] = {"provider": str(provider), "model": str(model)}
    config = getattr(preference, "config", None)
    if isinstance(config, dict) and config:
        result["config"] = config
    return result


@dataclass(frozen=True)
class LiveMatrixSelection:
    matrix: str | None = None
    live: bool = False
    reason: str = ""


async def activate_live_matrix(
    coordinator: Any,
    provider_name: str,
    *,
    home: Path | None = None,
) -> LiveMatrixSelection:
    """Retarget a mounted matrix strategy for the current session.

    The routing bundle currently exposes a resolver capability rather than a
    public switch method.  We update that documented capability object's
    matrix fields in place so consumers that retained the object and consumers
    that fetch it per call both see the new strategy.  Unknown/custom routing
    strategies are left untouched.
    """
    matrix = _runtime_matching_matrix(coordinator, provider_name, home=home)
    if matrix is None:
        return LiveMatrixSelection(
            reason=f"no unique routing matrix matches provider {provider_name!r}"
        )
    state = getattr(coordinator, "session_state", None)

    path = find_matrix_file(matrix, home)
    if path is None:
        return LiveMatrixSelection(matrix=matrix, reason="matrix source is not cached")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as error:
        return LiveMatrixSelection(matrix=matrix, reason=f"matrix could not be read: {error}")
    roles = data.get("roles") if isinstance(data, dict) else None
    if not isinstance(roles, dict) or not roles:
        return LiveMatrixSelection(matrix=matrix, reason="matrix has no roles")

    getter = getattr(coordinator, "get_capability", None)
    try:
        resolver: Any = getter("model_role_resolver") if callable(getter) else None
    except Exception as error:  # noqa: BLE001 - capability lookup is an external boundary
        return LiveMatrixSelection(
            matrix=matrix,
            reason=f"matrix resolver lookup failed: {error}",
        )
    if resolver is None or not all(
        hasattr(resolver, field) for field in ("_matrix_roles", "_providers", "known_roles")
    ):
        return LiveMatrixSelection(matrix=matrix, reason="matrix resolver is not mounted")

    effective_roles = roles
    hook_entry = _hook_entry(coordinator)
    hook_config = hook_entry.get("config") if isinstance(hook_entry, dict) else None
    overrides = hook_config.get("overrides") if isinstance(hook_config, dict) else None
    if isinstance(overrides, dict) and overrides:
        try:
            compose_matrix = getattr(
                import_module("amplifier_module_hooks_routing.matrix_loader"),
                "compose_matrix",
            )
            effective_roles = compose_matrix(roles, overrides)
        except Exception as error:  # noqa: BLE001 - configuration must not be applied partially
            return LiveMatrixSelection(
                matrix=matrix,
                reason=f"matrix overrides could not be composed: {error}",
            )

    try:
        providers = (
            coordinator.get("providers") if callable(getattr(coordinator, "get", None)) else {}
        )
    except Exception as error:  # noqa: BLE001 - fail before changing the resolver
        return LiveMatrixSelection(
            matrix=matrix,
            reason=f"mounted providers could not be read: {error}",
        )
    provider_map = providers if isinstance(providers, dict) else {}
    matrix_name = str(data.get("name") or matrix)

    # Stage every resolver-dependent agent preference against a shallow clone.
    # This gives us a complete plan -- or a clean failure -- before any shared
    # resolver/config object visible to a concurrent turn is mutated.
    try:
        staged_resolver = copy.copy(resolver)
        staged_resolver._matrix_roles = effective_roles
        staged_resolver._providers = provider_map
        staged_resolver._coordinator = coordinator
        staged_resolver.name = matrix_name
        staged_resolver.known_roles = tuple(effective_roles)
    except Exception as error:  # noqa: BLE001 - third-party resolver may be non-copyable/frozen
        return LiveMatrixSelection(
            matrix=matrix,
            reason=f"matrix resolver cannot be staged safely: {error}",
        )

    config = getattr(coordinator, "config", None)
    agents = config.get("agents") if isinstance(config, dict) else None
    planned_agents: list[tuple[dict[str, Any], list[dict[str, Any]] | None]] = []
    if isinstance(agents, dict):
        for agent_name, agent_config in agents.items():
            if not isinstance(agent_config, dict) or not agent_config.get("model_role"):
                continue
            try:
                preferences = await staged_resolver.resolve(agent_config["model_role"])
            except Exception as error:  # noqa: BLE001 - fail before producing mixed agent defaults
                return LiveMatrixSelection(
                    matrix=matrix,
                    reason=f"matrix could not resolve agent {agent_name!r}: {error}",
                )
            planned_agents.append(
                (
                    agent_config,
                    [_preference_dict(preference) for preference in preferences]
                    if preferences
                    else None,
                )
            )

    # The routing bundle's provider-request hook closes over its boot matrix.
    # Replace that one named handler so the model-visible role catalog agrees
    # with the newly active resolver immediately.
    missing = object()
    resolver_fields = {
        field: getattr(resolver, field, missing)
        for field in ("_matrix_roles", "_providers", "_coordinator", "name", "known_roles")
    }
    agent_snapshots = [
        (agent_config, agent_config.get("provider_preferences", missing))
        for agent_config, _preferences in planned_agents
    ]
    old_hook_config = hook_entry.get("config", missing) if isinstance(hook_entry, dict) else missing
    try:
        old_session_routing = getter("session.routing") if callable(getter) else missing
    except Exception as error:  # noqa: BLE001 - complete staging before hook/resolver mutation
        return LiveMatrixSelection(
            matrix=matrix,
            reason=f"routing session state could not be read: {error}",
        )
    old_ui_state = state.get("ui.routing_matrix", missing) if isinstance(state, dict) else missing

    hooks = getattr(coordinator, "hooks", None)
    install_dynamic_hook = hooks is not None and not getattr(
        coordinator, "_tui_dynamic_routing_context", False
    )
    hook_cleanup: Any = None
    hook_unregister: Any = None
    if install_dynamic_hook:
        register = getattr(hooks, "register", None)
        hook_unregister = getattr(hooks, "unregister", None)
        if not callable(register) or not callable(hook_unregister):
            return LiveMatrixSelection(
                matrix=matrix,
                reason="routing context hook cannot be replaced safely",
            )

        async def on_provider_request(event: str, payload: dict[str, Any]) -> Any:
            del event, payload
            from amplifier_core.models import HookResult

            lines = [f"Active routing matrix: {resolver.name}"]
            lines.append("Available model roles (use model_role parameter when delegating):")
            current_roles = getattr(resolver, "_matrix_roles", {})
            for role_name, role_data in current_roles.items():
                description = (
                    role_data.get("description", "") if isinstance(role_data, dict) else ""
                )
                lines.append(f"  {role_name:16s} — {description}")
            return HookResult(
                action="inject_context", context_injection="\n".join(lines), ephemeral=True
            )

        try:
            hook_cleanup = register(
                "provider:request",
                on_provider_request,
                priority=14,
                name="ui-live-routing-context",
            )
        except Exception as error:  # noqa: BLE001 - fail before resolver mutation
            return LiveMatrixSelection(
                matrix=matrix,
                reason=f"routing context hook could not be staged: {error}",
            )

    register_capability = getattr(coordinator, "register_capability", None)
    try:
        resolver._matrix_roles = effective_roles
        resolver._providers = provider_map
        resolver._coordinator = coordinator
        resolver.name = matrix_name
        resolver.known_roles = tuple(effective_roles)

        for agent_config, preferences in planned_agents:
            if preferences is None:
                agent_config.pop("provider_preferences", None)
            else:
                agent_config["provider_preferences"] = preferences

        if isinstance(hook_entry, dict):
            if isinstance(hook_config, dict):
                hook_entry["config"] = {**hook_config, "default_matrix": matrix}
            else:
                hook_entry["config"] = {"default_matrix": matrix}

        if callable(register_capability):
            current = old_session_routing if isinstance(old_session_routing, dict) else {}
            register_capability("session.routing", {**current, "matrix": matrix})

        if isinstance(state, dict):
            state["ui.routing_matrix"] = {"name": matrix, "live": True}
    except Exception as error:  # noqa: BLE001 - rollback every shared Python surface we changed
        for field, value in resolver_fields.items():
            try:
                if value is missing:
                    delattr(resolver, field)
                else:
                    setattr(resolver, field, value)
            except Exception:  # noqa: BLE001 - best effort for a third-party frozen resolver
                pass
        for agent_config, value in agent_snapshots:
            if value is missing:
                agent_config.pop("provider_preferences", None)
            else:
                agent_config["provider_preferences"] = value
        if isinstance(hook_entry, dict):
            if old_hook_config is missing:
                hook_entry.pop("config", None)
            else:
                hook_entry["config"] = old_hook_config
        if callable(register_capability) and old_session_routing is not missing:
            try:
                register_capability("session.routing", old_session_routing)
            except Exception:  # noqa: BLE001 - no generic capability unregister/transaction API
                pass
        elif callable(register_capability):
            # Python test doubles expose their capability mapping; the Rust
            # coordinator has no generic unregister API, so absence can only be
            # restored where that mapping is explicitly available.
            capabilities = getattr(coordinator, "capabilities", None)
            if isinstance(capabilities, dict):
                capabilities.pop("session.routing", None)
        if isinstance(state, dict):
            if old_ui_state is missing:
                state.pop("ui.routing_matrix", None)
            else:
                state["ui.routing_matrix"] = old_ui_state
        if callable(hook_cleanup):
            try:
                hook_cleanup()
            except Exception:  # noqa: BLE001 - resolver/config rollback is authoritative
                pass
        elif install_dynamic_hook and callable(hook_unregister):
            try:
                hook_unregister("ui-live-routing-context")
            except Exception:  # noqa: BLE001 - resolver/config rollback is authoritative
                pass
        return LiveMatrixSelection(
            matrix=matrix,
            reason=f"live matrix update rolled back: {error}",
        )

    if install_dynamic_hook and callable(hook_unregister):
        # All fallible routing mutations are complete. The replacement handler
        # reads the resolver dynamically, so after removing the boot handler it
        # remains correct across every later switch without another hook swap.
        try:
            hook_unregister("routing-context")
        except Exception:  # noqa: BLE001 - live routing is correct; duplicate context is cosmetic
            pass
        try:
            setattr(coordinator, "_tui_dynamic_routing_context", True)
        except Exception:  # noqa: BLE001 - coordinator lifetime still owns the registered handler
            pass
    return LiveMatrixSelection(matrix=matrix, live=True)


async def maybe_await(value: Any) -> Any:
    """Small public helper for duck-typed cleanup/mount call sites."""
    return await value if inspect.isawaitable(value) else value


__all__ = [
    "LiveMatrixSelection",
    "SHIPPED_PROVIDER_MATRICES",
    "activate_live_matrix",
    "amplifier_home",
    "apply_model_routing_hint",
    "available_matrix_names",
    "find_matrix_file",
    "matching_matrix",
    "maybe_await",
    "persist_model_routing_hint",
    "provider_matrix_candidates",
]
