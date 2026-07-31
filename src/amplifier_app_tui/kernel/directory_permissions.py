"""Amplifier-native allowed/denied directory policy and persistence.

The mounted ``tool-filesystem`` remains the hard enforcement mechanism. This
module owns the app-facing administration seam and a small mutable policy view
used by the governance hook to apply the same boundary to obvious paths in
shell calls. Settings use the same ``modules.tools/tool-filesystem`` shape as
amplifier-app-cli.
"""

from __future__ import annotations

import re
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

WriteBoundary = Literal["open", "guarded"]
"""App-level write-boundary posture.

``open`` (default) matches amplifier-app-cli: no governance pre-flight for
writes outside the project and no write-shaped shell gating — the mounted
filesystem tool remains the sole write-path enforcement (a graceful tool
error, never an approval gate). ``guarded`` restores the app-level gate:
outside writes are blocked pre-flight and write-shaped shell escapes are
classified outside-project. Denied and protected paths are enforced in
both postures.

Audit H2 posture (2026-07-22): ``open`` is a *deliberate* default, not a
parity accident — but it is only sound while a filesystem write-enforcer is
actually mounted to back it. :func:`resolve_write_boundary` asserts that at
startup and degrades ``open`` → ``guarded`` (with a boot notice) when no
``tool-filesystem`` is planned, so enforcement is never silently delegated
to a non-existent tool.
"""


def write_boundary_setting(settings: dict[str, Any]) -> WriteBoundary:
    """Resolve ``permissions.write_boundary`` from merged settings."""
    permissions = settings.get("permissions")
    raw = permissions.get("write_boundary") if isinstance(permissions, dict) else None
    return "guarded" if raw == "guarded" else "open"


def filesystem_write_enforcer_present(mount_plan: dict[str, Any]) -> bool:
    """True when the mount plan includes a ``tool-filesystem`` module.

    The mounted filesystem tool is the hard write enforcement backing the
    ``open`` posture — its allow/deny lists are injected via
    :meth:`DirectoryPolicy.merged_tool_config`, and outside-project writes get
    a graceful tool error from it rather than an app-level gate. When no such
    module is planned, ``open`` would hand enforcement to nothing, so callers
    degrade to the app-level ``guarded`` gate. Verifying the tool's *internal*
    enforcement is a separate, deeper task (audit H1); at this app seam,
    presence in the plan is the assertion we can make deterministically.
    """
    for tool in mount_plan.get("tools") or []:
        if isinstance(tool, dict) and tool.get("module") == "tool-filesystem":
            return True
    return False


WRITE_BOUNDARY_DEGRADE_NOTICE = (
    "write boundary degraded to guarded · no filesystem tool is mounted to enforce "
    "writes outside the project, so the app-level gate is restored · run doctor for details"
)
"""Boot notice emitted when ``open`` is degraded to ``guarded`` (audit H2)."""


def resolve_write_boundary(
    settings: dict[str, Any],
    mount_plan: dict[str, Any],
) -> tuple[WriteBoundary, str | None]:
    """Resolve the effective boundary, asserting ``open`` is backed by a tool.

    Audit H2: the ``open`` default delegates 100% of outside-project write
    enforcement to the mounted filesystem tool. This resolver makes that a
    verified decision rather than a silent assumption:

    - An explicit ``guarded`` setting is honored unchanged (no notice — the
      user chose the app-level gate).
    - ``open`` with a ``tool-filesystem`` in the plan stays ``open`` (app-cli
      parity, the common case — behavior unchanged, no notice).
    - ``open`` with *no* filesystem write-enforcer degrades to ``guarded`` and
      returns a boot notice, so the app-level gate covers the gap loudly
      instead of trusting a non-existent enforcer.
    """
    if write_boundary_setting(settings) == "guarded":
        return ("guarded", None)
    if filesystem_write_enforcer_present(mount_plan):
        return ("open", None)
    return ("guarded", WRITE_BOUNDARY_DEGRADE_NOTICE)


PROTECTED_PROJECT_PATHS: tuple[str, ...] = (
    ".git",
    ".agents",
    ".codex",
    "AGENTS.md",
)
"""Instruction and repository-control paths denied inside writable roots."""

_WRITE_COMMANDS = frozenset(
    {
        "chgrp",
        "chmod",
        "chown",
        "cp",
        "dd",
        "install",
        "ln",
        "mkdir",
        "mv",
        "rm",
        "rmdir",
        "rsync",
        "shred",
        "tee",
        "touch",
        "truncate",
        "unlink",
    }
)
"""Command heads that treat their path arguments as write targets."""

_COMMAND_SEPARATORS = frozenset({"&&", "||", ";", "|"})


def _compile_protected_pattern(relative: str) -> re.Pattern[str]:
    """Compile a fail-closed matcher for one protected path.

    Protected *files* (``AGENTS.md``) match as a whole path segment.
    Protected *directories* (``.git``, ``.agents``, ``.codex``) match only
    when a concrete subpath follows (``.git/config``) and are exempt when a
    glob metacharacter follows the slash (``./.git/*``) -- a glob names the
    directory to *exclude* it (a read filter), not to write into it. The
    leading lookbehind keeps ``.gitignore``/``.github`` and a bare repo like
    ``foo.git`` from matching ``.git``.
    """
    escaped = re.escape(relative)
    if Path(relative).suffix:
        return re.compile(rf"(?<![\w.\-]){escaped}(?![\w])")
    return re.compile(rf"(?<![\w.\-]){escaped}/(?![*?])")


_PROTECTED_REFERENCE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (relative, _compile_protected_pattern(relative)) for relative in PROTECTED_PROJECT_PATHS
)
"""Precompiled fail-closed matchers for protected paths embedded anywhere in
an EXEC command string (audit H1). The command-head/redirect token pass in
:meth:`DirectoryPolicy.shell_outside_target` cannot see a path buried inside a
quoted interpreter script (``python3 -c "...open('.git/config')..."``) or hidden
behind a directory prefix (``vendored/.git/config``); these patterns close that
gap."""


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
        write_boundary: WriteBoundary = "open",
    ) -> None:
        self.write_boundary: WriteBoundary = write_boundary
        self.project_dir = project_dir.resolve()
        self._base_allowed = self._stable((str(self.project_dir), *allowed))
        self._base_denied = self._stable(denied)
        self._protected = self._stable(
            [str(self.project_dir / relative) for relative in PROTECTED_PROJECT_PATHS]
        )
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
        return tuple(self._stable([*self._protected, *self._base_denied, *self._session_denied]))

    @property
    def protected(self) -> tuple[str, ...]:
        return tuple(self._protected)

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
        if self._within_any(resolved, self.protected):
            return (False, f"path is protected by default · {resolved}")
        if self._within_any(resolved, self.denied):
            return (False, f"path is within denied directories · {resolved}")
        if self._within_any(resolved, self.allowed):
            return (True, "within allowed write directories")
        if self.write_boundary == "open":
            # App-cli parity: no app-level gate outside the project. The
            # mounted filesystem tool stays the hard write enforcement
            # (its allowlist is injected via merged_tool_config), so write
            # tools get a graceful tool error there — never a governance
            # block or an approval.
            return (True, f"outside project · filesystem tool enforces writes · {resolved}")
        return (False, f"path is outside allowed write directories · {resolved}")

    def check_read(self, path: str | Path, *, cwd: Path | None = None) -> tuple[bool, str]:
        """Reads roam anywhere except denied directories (within reason).

        Reads are denylist-bounded, not allowlist-bounded — matching
        amplifier-app-cli's permissive read defaults. Only user-configured
        denied directories (and the protected set) gate read access.
        """
        candidate = Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = (cwd or self.project_dir) / candidate
        resolved = candidate.resolve(strict=False)
        if self._within_any(resolved, self.denied):
            return (False, f"path is within denied directories · {resolved}")
        return (True, "read roams outside the project · denylist-bounded")

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
        """Return the first shell path that escapes the write boundary.

        This is a governance signal, not a shell sandbox. Deny-listed and
        protected paths are flagged wherever they appear; merely-outside
        paths are flagged only in write contexts (write-command heads and
        redirection targets). Read-shaped commands may roam outside the
        project — reads are denylist-bounded, not allowlist-bounded — while
        the mounted bash tool's own safety validator stays in charge of
        command form.
        """
        try:
            lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
            lexer.whitespace_split = True
            tokens = list(lexer)
        except ValueError:
            tokens = command.split()
        cleaned = [raw.strip("'\";,(){}[]") for raw in tokens]
        heads = {Path(cleaned[0]).name} if cleaned else set()
        heads.update(
            Path(cleaned[index + 1]).name
            for index, token in enumerate(cleaned[:-1])
            if token in _COMMAND_SEPARATORS
        )
        write_head = bool(heads & _WRITE_COMMANDS)
        redirect_targets = {
            index + 1 for index, token in enumerate(cleaned) if token in (">", ">>")
        }
        for index, token in enumerate(cleaned):
            if token.startswith(("http://", "https://", "/dev/")):
                continue
            if ("*" in token or "?" in token) and not write_head and index not in redirect_targets:
                # A glob in a read-shaped command is a filter pattern, not a
                # concrete target — `find -not -path "./.git/*"` names .git
                # precisely to AVOID it (false-positive block found live).
                # Write-shaped commands keep strict flagging: `rm -rf .git/*`
                # and `> .git/*` must still stop.
                continue
            protected_relative = any(
                token == relative or token.startswith(f"{relative}/")
                for relative in PROTECTED_PROJECT_PATHS
            )
            pathish = token.startswith(("/", "~/", "./", "../"))
            if not protected_relative and not pathish and index not in redirect_targets:
                continue
            allowed, reason = self.check_write(token)
            if allowed:
                continue
            if reason.startswith(("path is protected", "path is within denied")):
                return (token, reason)
            if write_head or index in redirect_targets:
                return (token, reason)
        # Fail-closed fallback (audit H1): the token pass above is command-list
        # based -- writes via `python3 -c`, `sed -i`, `curl -o`, or a
        # directory-prefixed path hide the target from write-head/redirect
        # detection. The mounted bash tool's validator is a dangerous-command
        # blocklist that enforces NO write-path list, so a protected path buried
        # anywhere in the command would otherwise reach the shell unseen. Scan
        # the raw string for a protected reference and escalate to *ask*.
        return self._embedded_protected_reference(command)

    def _embedded_protected_reference(self, command: str) -> tuple[str, str] | None:
        """Return a protected path referenced anywhere in an EXEC command.

        Where the token pass flags a *concrete target token* and hard-blocks
        it, this scan catches a protected path lurking inside a quoted
        interpreter script (``python3 -c "...open('.git/config')..."``), a sed
        expression, or behind a directory prefix (``vendored/.git/config``).
        An embedded reference is lower confidence than a target token -- it may
        be a harmless mention -- so the reason routes to *ask* (the human
        adjudicates) rather than a silent allow. Glob filters that name a
        protected directory to exclude it (``find ... -not -path './.git/*'``)
        stay exempt; ``.gitignore`` and ``.github`` never match ``.git``.
        """
        for relative, pattern in _PROTECTED_REFERENCE_PATTERNS:
            if pattern.search(command):
                return (
                    relative,
                    f"protected path referenced in command \u00b7 {relative} "
                    "\u00b7 review before exec",
                )
        return None

    def merged_tool_config(self, config: dict[str, Any]) -> dict[str, Any]:
        merged = dict(config)
        merged["allowed_write_paths"] = list(self.allowed)
        merged["denied_write_paths"] = list(self.denied)
        return merged


def policy_from_mount_plan(
    mount_plan: dict[str, Any],
    project_dir: Path,
    *,
    write_boundary: WriteBoundary = "open",
) -> DirectoryPolicy:
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
        write_boundary=write_boundary,
    )


def apply_policy_to_mount_plan(mount_plan: dict[str, Any], policy: DirectoryPolicy) -> None:
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
    "PROTECTED_PROJECT_PATHS",
    "apply_policy_to_mount_plan",
    "configured_entries",
    "policy_from_mount_plan",
    "settings_path_values",
    "update_configured_path",
    "update_settings_path",
]
