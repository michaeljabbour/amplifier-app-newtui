"""First-run setup: the logic behind ``amplifier-newtui init``.

amplifier-app-cli's ``init`` is an interactive provider/routing dashboard
built on its own ``ProviderManager`` / ``KeyManager`` (app-cli-internal,
not shared). newtui reuses the two shared pieces:

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
``tmp_path`` keys file; only :func:`discover_providers` touches amplifier.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal


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


@dataclass(frozen=True)
class ProviderFields:
    """A provider's authoritative config schema (from ``get_info()``)."""

    module_id: str
    key_var: str  # secret field's env_var, e.g. ANTHROPIC_API_KEY
    key_field_id: str  # e.g. "api_key"
    base_url_var: str | None
    base_url_default: str | None
    has_models: bool


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


def _instantiate_provider(cls: Any) -> Any:
    """Try the provider constructor signatures app-cli probes; None on failure."""
    for kwargs in (
        {"api_key": "x", "config": {}},
        {"base_url": "", "api_key": "x", "config": {}},
        {"config": {}},
        {},
    ):
        try:
            return cls(**kwargs)
        except Exception:  # noqa: BLE001
            continue
    return None


def load_provider_info(module_id: str) -> ProviderFields | None:
    """Authoritative env-var + field schema from the provider's ``get_info()``.

    This is how app-cli learns a provider wants ``ANTHROPIC_API_KEY`` vs
    ``OPENAI_API_KEY`` vs a namespaced var — the convention guess is wrong for
    azure/gemini/copilot. Returns ``None`` when the provider can't be loaded
    (caller falls back to the convention)."""
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
    for field in getattr(info, "config_fields", None) or []:
        ftype = getattr(field, "field_type", None)
        env_var = getattr(field, "env_var", None)
        fid = getattr(field, "id", None)
        if ftype == "secret" and key_var is None and env_var:
            key_var = str(env_var)
            key_field = str(fid or "api_key")
        if fid == "base_url" or (env_var and str(env_var).endswith("_BASE_URL")):
            base_url_var = str(env_var) if env_var else None
            default = getattr(field, "default", None)
            base_url_default = str(default) if default else None
    if not key_var:
        return None
    return ProviderFields(
        module_id=module_id,
        key_var=key_var,
        key_field_id=key_field,
        base_url_var=base_url_var,
        base_url_default=base_url_default,
        has_models=hasattr(inst, "list_models"),
    )


def _choice(module_id: str, name: str, stored: set[str]) -> ProviderChoice:
    """A setup choice using the authoritative env var when discoverable."""
    info = load_provider_info(module_id)
    if info is not None:
        key_var = info.key_var
        base_url_var = info.base_url_var or f"{provider_env_prefix(module_id)}_BASE_URL"
    else:
        prefix = provider_env_prefix(module_id)
        key_var = f"{prefix}_API_KEY"
        base_url_var = f"{prefix}_BASE_URL"
    return ProviderChoice(
        module_id=module_id,
        name=name,
        key_var=key_var,
        base_url_var=base_url_var,
        has_key=key_var in stored,
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


def write_key(path: Path, name: str, value: str, *, update_environ: bool = True) -> None:
    """Set ``name=value`` in the keys file (line-preserving), then ``chmod 600``.

    Existing lines for *name* are replaced in place (comments and other
    keys preserved); a new key is appended. Also updates ``os.environ`` so
    the value is live in-process (KeyManager parity)."""
    path.parent.mkdir(parents=True, exist_ok=True)
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
    key_var: str,
    model: str | None = None,
    base_url: str | None = None,
    base_url_var: str | None = None,
    priority: int = 1,
) -> dict[str, Any]:
    """A ``config.providers`` entry with ``${VAR}`` placeholders (never literals)."""
    config: dict[str, Any] = {}
    if model:
        config["default_model"] = model
    config["api_key"] = f"${{{key_var}}}"
    if base_url and base_url_var:
        config["base_url"] = f"${{{base_url_var}}}"
    config["priority"] = priority
    return {"module": module_id, "config": config}


def write_provider_config(
    paths: Any, scope: Literal["global", "project", "local"], entry: dict[str, Any]
) -> Path:
    """Persist a provider entry into ``config.providers`` at *scope*.

    New entry goes first at priority 1 (active); a same-module entry is
    replaced and any other priority-1 entry is demoted to 10 — mirroring
    app-cli's ``AppSettings.set_provider_override``."""
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
    module = entry.get("module")
    kept: list[Any] = []
    for provider in providers:
        if isinstance(provider, dict) and provider.get("module") == module and not provider.get("id"):
            continue  # replace the same-module entry
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


__all__ = [
    "PROVIDER_CREDENTIAL_VARS",
    "ProviderChoice",
    "ProviderFields",
    "SetupStatus",
    "auto_init_from_env",
    "detect_provider_from_env",
    "discover_providers",
    "keys_file",
    "load_provider_info",
    "provider_config_entry",
    "provider_env_prefix",
    "read_keys",
    "setup_status",
    "stored_key_names",
    "write_key",
    "write_provider_config",
]
