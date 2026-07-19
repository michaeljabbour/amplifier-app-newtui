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


def _choice(module_id: str, name: str, stored: set[str]) -> ProviderChoice:
    prefix = provider_env_prefix(module_id)
    key_var = f"{prefix}_API_KEY"
    return ProviderChoice(
        module_id=module_id,
        name=name,
        key_var=key_var,
        base_url_var=f"{prefix}_BASE_URL",
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


__all__ = [
    "ProviderChoice",
    "SetupStatus",
    "discover_providers",
    "keys_file",
    "provider_env_prefix",
    "read_keys",
    "setup_status",
    "stored_key_names",
    "write_key",
]
