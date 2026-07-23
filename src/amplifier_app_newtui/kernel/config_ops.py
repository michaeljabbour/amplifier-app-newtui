"""Bridge the ``/config`` domain model to the real session + settings files.

The pure state/diff/toggle/set logic lives in
:mod:`amplifier_app_newtui.model.config` (Textual-free, amplifier-free).
This kernel module supplies the two side-effecting halves the model
cannot own:

- **seed** a :class:`~amplifier_app_newtui.model.config.SessionConfigState`
  from the resolved mount plan (what actually mounted this session);
- **persist** the session's changes to a settings scope file on
  ``/config save`` -- reusing newtui's own settings machinery
  (``kernel/bundle_admin`` atomic scope writes + ``kernel/config``
  deep-merge), NEVER amplifier-app-cli's ``AppSettings``.

Donor: amplifier-app-cli's ``_handle_config_save`` delegates to
``SessionConfigurator.save(scope)``, which writes a ``configurator:``
block into ``settings.yaml``. This module writes the SAME shape into
newtui's settings scopes, so the two apps stay file-compatible without a
code dependency between them.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, cast

from ..model.config import SessionConfigState, state_from_mount_plan
from .bundle_admin import Scope, read_scope, scope_file, settings_paths, write_scope
from .config import deep_merge

CONFIGURATOR_KEY = "configurator"
"""Top-level settings key the session config changes persist under
(amplifier-app-cli parity)."""


def amplifier_home() -> Path:
    """The amplifier home dir, honouring ``AMPLIFIER_HOME`` (reference CLI parity).

    The acceptance probe points this at a scratch dir inside the worktree
    so ``/config save`` never touches the developer's real config.
    """
    override = os.environ.get("AMPLIFIER_HOME", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".amplifier"


def state_from_plan(mount_plan: dict[str, Any], *, bundle: str = "") -> SessionConfigState:
    """Seed a live config state from a resolved mount plan (kernel entrypoint)."""
    return state_from_mount_plan(mount_plan, bundle=bundle)


def save_config(
    state: SessionConfigState,
    *,
    scope: str = "global",
    project_dir: Path | None = None,
    home: Path | None = None,
) -> tuple[bool, str]:
    """Persist *state*'s session changes to the *scope* settings file.

    Merges ``state.to_settings()`` under the ``configurator`` key into the
    existing scope file (deep-merge, most-specific wins) and writes it
    back atomically. An empty change set drops any stale configurator block
    so ``save`` is idempotent and the message is honest.

    Returns ``(ok, message)``; never raises into the UI.
    """
    if scope not in ("global", "project", "local"):
        return (False, f"unknown scope '{scope}' \u00b7 use global | project | local")
    changes = state.to_settings()
    paths = settings_paths(project_dir or Path.cwd(), home or amplifier_home())
    path = scope_file(paths, cast(Scope, scope))
    try:
        existing = read_scope(path)
        if changes:
            merged = deep_merge(existing, {CONFIGURATOR_KEY: changes})
        else:
            # Nothing changed this session: drop any stale configurator block
            # rather than leave a misleading one behind.
            merged = dict(existing)
            merged.pop(CONFIGURATOR_KEY, None)
        write_scope(path, merged)
    except OSError as error:
        return (False, f"could not write {scope} settings \u00b7 {error}")
    count = state.change_count
    detail = f"{count} change(s)" if count else "no session changes"
    return (True, f"\u2713 config saved \u00b7 {scope} scope \u00b7 {detail} \u00b7 {path}")


__all__ = [
    "CONFIGURATOR_KEY",
    "amplifier_home",
    "save_config",
    "state_from_plan",
]
