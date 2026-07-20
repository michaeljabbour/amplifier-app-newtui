"""Amplifier-native allowed/denied directory policy and persistence.

The mounted ``tool-filesystem`` remains the hard enforcement mechanism. This
module owns the app-facing administration seam and a small mutable policy view
used by the governance hook to apply the same boundary to obvious paths in
shell calls. Settings use the same ``modules.tools/tool-filesystem`` shape as
amplifier-app-cli.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .bundle_admin import Scope, read_scope, scope_file, write_scope
from .config import SettingsPaths

DirectoryKind = Literal["allowed", "denied"]
_CONFIG_KEY: dict[DirectoryKind, str] = {
    "allowed": "allowed_write_paths",
    "denied": "denied_write_paths",
}


@dataclass(frozen=True)
class DirectoryEntry:
    path: str
    scope: str


def _filesystem_config(settings: dict[str, Any], *, create: bool) -> dict[str, Any] | None:
    modules = settings.get("modules")
    if not isinstance(modules, dict):
        if not create:
            return None
        modules = {}
        settings["modules"] = modules
    tools = modules.get("tools")
    if not isinstance(tools, list):
        if not create:
            return None
        tools = []
        modules["tools"] = tools
    for tool in tools:
        if isinstance(tool, dict) and tool.get("module") == "tool-filesystem":
            config = tool.get("config")
            if not isinstance(config, dict):
                if not create:
                    return None
                config = {}
                tool["config"] = config
            return config
    if not create:
        return None
    entry: dict[str, Any] = {"module": "tool-filesystem", "config": {}}
    tools.append(entry)
    return entry["config"]


def configured_entries(
    paths: SettingsPaths,
    kind: DirectoryKind,
    *,
    scope_filter: Scope | None = None,
) -> tuple[DirectoryEntry, ...]:
    """Configured paths with most-specific-scope provenance."""
    key = _CONFIG_KEY[kind]
    ordered: tuple[Scope, ...] = ("local", "project", "global")
    seen: set[str] = set()
    result: list[DirectoryEntry] = []
    for scope in ordered:
        if scope_filter is not None and scope != scope_filter:
            continue
        config = _filesystem_config(read_scope(scope_file(paths, scope)), create=False)
        values = config.get(key, []) if config is not None else []
        if not isinstance(values, list):
            continue
        for raw in values:
            value = str(raw)
            if value not in seen:
                seen.add(value)
                result.append(DirectoryEntry(value, scope))
    return tuple(result)


def update_configured_path(
    paths: SettingsPaths,
    kind: DirectoryKind,
    operation: Literal["add", "remove"],
    raw_path: str,
    scope: Scope,
) -> tuple[bool, str, Path]:
    """Add/remove one resolved path at a persistent settings scope."""
    path = scope_file(paths, scope)
    changed, resolved = update_settings_path(path, kind, operation, raw_path)
    return changed, resolved, path


def update_settings_path(
    path: Path,
    kind: DirectoryKind,
    operation: Literal["add", "remove"],
    raw_path: str,
) -> tuple[bool, str]:
    """Add/remove one path in an arbitrary settings file (including session)."""
    resolved = str(Path(raw_path).expanduser().resolve())
    settings = read_scope(path)
    config = _filesystem_config(settings, create=operation == "add")
    key = _CONFIG_KEY[kind]
    values = config.get(key, []) if config is not None else []
    if not isinstance(values, list):
        values = []
    changed = False
    if operation == "add":
        if resolved not in values:
            values.append(resolved)
            changed = True
        assert config is not None
        config[key] = values
    else:
        for candidate in (resolved, raw_path):
            if candidate in values:
                values.remove(candidate)
                changed = True
                break
        if config is not None:
            config[key] = values
    if changed:
        write_scope(path, settings)
    return changed, resolved


def settings_path_values(settings: dict[str, Any], kind: DirectoryKind) -> tuple[str, ...]:
    config = _filesystem_config(settings, create=False)
    values = config.get(_CONFIG_KEY[kind], []) if config is not None else []
    return tuple(str(value) for value in values) if isinstance(values, list) else ()


class DirectoryPolicy:
    """Mutable effective write boundary shared by filesystem and governance."""

    def __init__(
        self,
        project_dir: Path,
        *,
        allowed: tuple[str, ...] = (),
        denied: tuple[str, ...] = (),
    ) -> None:
        self.project_dir = project_dir.resolve()
        self._base_allowed = self._stable((str(self.project_dir), *allowed))
        self._base_denied = self._stable(denied)
        self._session_allowed: list[str] = []
        self._session_denied: list[str] = []

    @staticmethod
    def _stable(values: tuple[str, ...] | list[str]) -> list[str]:
        result: list[str] = []
        for raw in values:
            value = str(Path(raw).expanduser().resolve())
            if value not in result:
                result.append(value)
        return result

    @property
    def allowed(self) -> tuple[str, ...]:
        return tuple(self._stable([*self._base_allowed, *self._session_allowed]))

    @property
    def denied(self) -> tuple[str, ...]:
        return tuple(self._stable([*self._base_denied, *self._session_denied]))

    @property
    def session_allowed(self) -> tuple[str, ...]:
        return tuple(self._session_allowed)

    @property
    def session_denied(self) -> tuple[str, ...]:
        return tuple(self._session_denied)

    def set_session(self, kind: DirectoryKind, values: tuple[str, ...]) -> None:
        target = self._session_allowed if kind == "allowed" else self._session_denied
        target[:] = self._stable(list(values))

    def add_session(self, kind: DirectoryKind, path: str) -> str:
        resolved = str(Path(path).expanduser().resolve())
        target = self._session_allowed if kind == "allowed" else self._session_denied
        if resolved not in target:
            target.append(resolved)
        return resolved

    def remove_session(self, kind: DirectoryKind, path: str) -> bool:
        resolved = str(Path(path).expanduser().resolve())
        target = self._session_allowed if kind == "allowed" else self._session_denied
        for candidate in (resolved, path):
            if candidate in target:
                target.remove(candidate)
                return True
        return False

    def check_write(self, path: str | Path, *, cwd: Path | None = None) -> tuple[bool, str]:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = (cwd or self.project_dir) / candidate
        resolved = candidate.resolve(strict=False)
        if self._within_any(resolved, self.denied):
            return (False, f"path is within denied directories · {resolved}")
        if self._within_any(resolved, self.allowed):
            return (True, "within allowed write directories")
        return (False, f"path is outside allowed write directories · {resolved}")

    def within_allowed(self, path: str | Path, *, cwd: Path | None = None) -> bool:
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = (cwd or self.project_dir) / candidate
        return self._within_any(candidate.resolve(strict=False), self.allowed)

    @staticmethod
    def _within_any(candidate: Path, roots: tuple[str, ...]) -> bool:
        for raw in roots:
            root = Path(raw).expanduser().resolve(strict=False)
            if candidate == root or root in candidate.parents:
                return True
        return False

    def shell_outside_target(self, command: str) -> tuple[str, str] | None:
        """Return the first obvious shell path outside/inside a deny rule.

        This is a governance signal, not a shell sandbox. It covers absolute,
        home-relative, ``./``/``../`` and redirection targets while leaving
        the mounted bash tool's own safety validator in charge of command form.
        """
        try:
            lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
            lexer.whitespace_split = True
            tokens = list(lexer)
        except ValueError:
            tokens = command.split()
        for raw in tokens:
            token = raw.strip("'\";,(){}[]")
            if token.startswith(("http://", "https://")):
                continue
            if not token.startswith(("/", "~/", "./", "../")):
                continue
            allowed, reason = self.check_write(token)
            if not allowed:
                return (token, reason)
        return None

    def merged_tool_config(self, config: dict[str, Any]) -> dict[str, Any]:
        merged = dict(config)
        merged["allowed_write_paths"] = list(self.allowed)
        merged["denied_write_paths"] = list(self.denied)
        return merged


def policy_from_mount_plan(mount_plan: dict[str, Any], project_dir: Path) -> DirectoryPolicy:
    config: dict[str, Any] = {}
    for tool in mount_plan.get("tools") or []:
        if isinstance(tool, dict) and tool.get("module") == "tool-filesystem":
            raw = tool.get("config")
            config = raw if isinstance(raw, dict) else {}
            break
    allowed = config.get("allowed_write_paths", [])
    denied = config.get("denied_write_paths", [])
    return DirectoryPolicy(
        project_dir,
        allowed=tuple(str(value) for value in allowed) if isinstance(allowed, list) else (),
        denied=tuple(str(value) for value in denied) if isinstance(denied, list) else (),
    )


def apply_policy_to_mount_plan(
    mount_plan: dict[str, Any], policy: DirectoryPolicy
) -> None:
    """Write the effective policy back to the prepared plan in place."""
    for tool in mount_plan.get("tools") or []:
        if not isinstance(tool, dict) or tool.get("module") != "tool-filesystem":
            continue
        raw = tool.get("config")
        config = raw if isinstance(raw, dict) else {}
        tool["config"] = policy.merged_tool_config(config)


__all__ = [
    "DirectoryEntry",
    "DirectoryKind",
    "DirectoryPolicy",
    "apply_policy_to_mount_plan",
    "configured_entries",
    "policy_from_mount_plan",
    "settings_path_values",
    "update_configured_path",
    "update_settings_path",
]
