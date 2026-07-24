"""Notification configuration admin (``amplifier-newtui notify ...``).

amplifier-app-cli exposes a ``notify`` group (status/desktop/ntfy/reset)
through its own ``AppSettings`` + ``KeyManager``; newtui is NOT built on those
classes, so this module re-expresses the same capability over the layers that
ARE shared:

- **newtui's own settings scope files** (``kernel/config.py`` + ``bundle_admin``
  scope helpers) for the non-secret keys. Everything lands in the exact
  ``config.notifications.*`` keys the runtime already consumes via the
  :func:`kernel.config.inject_notifications_config` /
  :func:`kernel.config.apply_notification_ladder_env` bridges.
- **``~/.amplifier/keys.env``** for the ntfy *topic* only. ntfy.sh topics are
  public (anyone who knows the topic can read your notifications), so the push
  module treats the topic as a secret and reads it only from
  ``AMPLIFIER_NTFY_TOPIC``. ``notify set topic`` writes there, never a settings
  scope -- matching the donor's security posture.

Everything here is pure file/dict work over ``tmp_path``-able scope files, so
it unit-tests with no amplifier session and no real ``~/.amplifier``. Nothing
here imports ``ui`` (ADR-0007 layering: ``kernel`` sits below ``ui``); the
one-shot OSC 777 emission for ``notify test`` is composed in ``main.py``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .bundle_admin import Scope, read_scope, scope_file, settings_paths, write_scope
from .config import (
    apply_notification_ladder_env,
    load_merged_settings,
    merged_push_settings,
    notification_settings,
)

# Env vars mirrored from ``ui/notifications`` + the hooks-notify-push module.
# Duplicated as literals (kernel must not import ``ui``) -- a stable public
# contract documented in docs/SETTINGS.md.
NOTIFY_ENV = "AMPLIFIER_NOTIFY"
NOTIFY_TERMINAL_ENV = "AMPLIFIER_TERMINAL_NOTIFICATIONS"
NTFY_TOPIC_ENV = "AMPLIFIER_NTFY_TOPIC"
NTFY_SERVER_ENV = "AMPLIFIER_NTFY_SERVER"
NOTIFY_PUSH_ENABLED_ENV = "AMPLIFIER_NOTIFY_PUSH_ENABLED"

_NOTIFY_DISABLED_VALUES = frozenset({"false", "0", "no", "off"})
_NOTIFY_BELL_ONLY_VALUES = frozenset({"bell"})
_TERMINAL_OFF_VALUES = frozenset({"off", "0", "false", "never", "none"})
_TERMINAL_FORCE_VALUES = frozenset({"force", "on", "1", "true", "always"})
_TRUE_STRINGS = frozenset({"true", "1", "yes", "on"})
_FALSE_STRINGS = frozenset({"false", "0", "no", "off"})


class UnknownNotifyKeyError(KeyError):
    """``notify set`` was given a key newtui does not honor."""


class InvalidNotifyValueError(ValueError):
    """``notify set`` was given a value that does not parse for its key."""


# --------------------------------------------------------------------------
# The keys `notify set` accepts (a tight, honestly-honored surface)
# --------------------------------------------------------------------------

ValueKind = Literal["bool", "str", "list", "secret"]


@dataclass(frozen=True)
class KeySpec:
    """One settable notification key: its value kind + where it persists."""

    dotted: str
    kind: ValueKind
    # Path under ``config.notifications`` (settings keys); ``None`` for the
    # secret topic, which persists to keys.env instead.
    settings_path: tuple[str, ...] | None
    summary: str


KNOWN_KEYS: tuple[KeySpec, ...] = (
    KeySpec(
        "suppress",
        "bool",
        ("suppress",),
        "silence the whole local ladder (bell + desktop)",
    ),
    KeySpec(
        "desktop.enabled",
        "bool",
        ("desktop", "enabled"),
        "desktop OSC 777 rung: false=off, true=force on any terminal",
    ),
    KeySpec("push.enabled", "bool", ("push", "enabled"), "ntfy push on/off"),
    KeySpec("push.server", "str", ("push", "server"), "ntfy server URL"),
    KeySpec(
        "push.priority",
        "str",
        ("push", "priority"),
        "ntfy priority (min|low|default|high|urgent)",
    ),
    KeySpec("push.tags", "list", ("push", "tags"), "ntfy emoji tags (comma-separated)"),
    KeySpec(
        "topic",
        "secret",
        None,
        "ntfy topic -- a secret, stored in keys.env (never a settings file)",
    ),
)

_KEYS_BY_NAME = {spec.dotted: spec for spec in KNOWN_KEYS}


def known_key_names() -> tuple[str, ...]:
    """The dotted keys ``notify set`` accepts (for help + error messages)."""
    return tuple(spec.dotted for spec in KNOWN_KEYS)


def _find_key(dotted: str) -> KeySpec:
    spec = _KEYS_BY_NAME.get(dotted)
    if spec is None:
        raise UnknownNotifyKeyError(dotted)
    return spec


def _coerce_bool(raw: str) -> bool:
    low = raw.strip().lower()
    if low in _TRUE_STRINGS:
        return True
    if low in _FALSE_STRINGS:
        return False
    raise InvalidNotifyValueError(f"expected a boolean (true/false), got {raw!r}")


def parse_value(spec: KeySpec, raw: str) -> Any:
    """Parse a raw CLI string into the typed value *spec* expects."""
    if spec.kind == "bool":
        return _coerce_bool(raw)
    if spec.kind == "list":
        return [item.strip() for item in raw.split(",") if item.strip()]
    return raw


# --------------------------------------------------------------------------
# Writers -- settings scope files (bundle_admin) + keys.env for the topic
# --------------------------------------------------------------------------


def _set_nested(data: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    node = data
    for key in path[:-1]:
        child = node.get(key)
        if not isinstance(child, dict):
            child = {}
            node[key] = child
        node = child
    node[path[-1]] = value


def write_topic_to_keys_env(topic: str, amplifier_home: Path) -> Path:
    """Upsert ``AMPLIFIER_NTFY_TOPIC=<topic>`` in ``<home>/keys.env``.

    ntfy topics are secrets, so the topic never touches a settings scope (it
    would be world-readable in a committed settings.yaml). keys.env is the
    single home ``config.load_keys_env`` already sources at boot, so a topic
    written here is picked up by the push module's ``AMPLIFIER_NTFY_TOPIC``
    read. Other lines/comments are preserved; the file is written atomically.
    """
    keys_file = amplifier_home / "keys.env"
    lines: list[str] = []
    if keys_file.is_file():
        lines = keys_file.read_text(encoding="utf-8").splitlines()
    replaced = False
    new_line = f"{NTFY_TOPIC_ENV}={topic}"
    for index, raw in enumerate(lines):
        stripped = raw.strip()
        if stripped.startswith("#") or "=" not in stripped:
            continue
        if stripped.split("=", 1)[0].strip() == NTFY_TOPIC_ENV:
            lines[index] = new_line
            replaced = True
            break
    if not replaced:
        lines.append(new_line)
    keys_file.parent.mkdir(parents=True, exist_ok=True)
    tmp = keys_file.with_suffix(keys_file.suffix + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(keys_file)
    return keys_file


@dataclass(frozen=True)
class SetResult:
    """What a ``notify set`` write did (for the CLI's confirmation line)."""

    dotted: str
    value: Any
    path: Path
    is_secret: bool


def set_key(
    paths,
    dotted: str,
    raw_value: str,
    scope: Scope,
    *,
    amplifier_home: Path | None = None,
) -> SetResult:
    """Persist one notification key. Unknown key -> :class:`UnknownNotifyKeyError`.

    Non-secret keys land in ``config.notifications.*`` at *scope* via the shared
    scope-file writers (never hand-edited). The secret ``topic`` is routed to
    keys.env instead (``amplifier_home`` required for that key).
    """
    spec = _find_key(dotted)
    if spec.kind == "secret":
        if not raw_value.strip():
            raise InvalidNotifyValueError("topic cannot be empty")
        home = amplifier_home or scope_file(paths, "global").parent
        keys_path = write_topic_to_keys_env(raw_value.strip(), home)
        return SetResult(dotted=dotted, value="configured", path=keys_path, is_secret=True)
    value = parse_value(spec, raw_value)
    assert spec.settings_path is not None
    path = scope_file(paths, scope)
    data = read_scope(path)
    _set_nested(data, ("config", "notifications", *spec.settings_path), value)
    write_scope(path, data)
    return SetResult(dotted=dotted, value=value, path=path, is_secret=False)


def set_enabled(
    paths, target: Literal["desktop", "push"], enabled: bool, scope: Scope
) -> SetResult:
    """Enable/disable a notification channel (``notify enable|disable``)."""
    return set_key(paths, f"{target}.enabled", "true" if enabled else "false", scope)


# --------------------------------------------------------------------------
# Reads -- effective status for `notify show` + `notify test`
# --------------------------------------------------------------------------


def topic_configured(amplifier_home: Path, environ: Mapping[str, str] | None = None) -> bool:
    """Whether an ntfy topic is set (env var or a keys.env entry)."""
    import os

    env = os.environ if environ is None else environ
    if env.get(NTFY_TOPIC_ENV, "").strip():
        return True
    keys_file = amplifier_home / "keys.env"
    if not keys_file.is_file():
        return False
    try:
        for raw in keys_file.read_text(encoding="utf-8").splitlines():
            stripped = raw.strip()
            if stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            if key.strip() == NTFY_TOPIC_ENV and value.strip():
                return True
    except OSError:
        return False
    return False


def resolved_environ(
    settings: dict[str, Any], environ: Mapping[str, str] | None = None
) -> dict[str, str]:
    """A copy of *environ* with the settings->ladder env folded in (env wins).

    The real boot mutates ``os.environ`` via
    :func:`config.apply_notification_ladder_env`; this returns a throwaway copy
    with the same fold applied, so ``notify show`` / ``notify test`` can reason
    about the effective ladder without touching the process environment.
    """
    import os

    merged = dict(os.environ if environ is None else environ)
    apply_notification_ladder_env(settings, merged)
    return merged


@dataclass(frozen=True)
class NotificationStatus:
    """The effective, resolved notification config for ``notify show``."""

    ceiling: str  # off | bell | desktop
    ceiling_source: str  # env | settings | default
    desktop_gate: str  # off | force | allowlist
    desktop_gate_source: str  # env | settings | default
    suppress: bool
    push_enabled: bool | None
    push_server: str | None
    push_priority: str | None
    push_tags: tuple[str, ...]
    topic: bool  # configured?


def _source(name: str, base: Mapping[str, str], merged: Mapping[str, str]) -> str:
    if name in base:
        return "env"
    if name in merged:
        return "settings"
    return "default"


def effective_status(
    settings: dict[str, Any],
    environ: Mapping[str, str] | None = None,
    amplifier_home: Path | None = None,
) -> NotificationStatus:
    """Resolve settings + env into the single effective notification picture."""
    import os

    base = dict(os.environ if environ is None else environ)
    merged = resolved_environ(settings, base)
    notifications = notification_settings(settings)
    push = merged_push_settings(notifications)

    notify_val = merged.get(NOTIFY_ENV, "").strip().lower()
    if notify_val in _NOTIFY_DISABLED_VALUES:
        ceiling = "off"
    elif notify_val in _NOTIFY_BELL_ONLY_VALUES:
        ceiling = "bell"
    else:
        ceiling = "desktop"

    term_val = merged.get(NOTIFY_TERMINAL_ENV, "").strip().lower()
    if term_val in _TERMINAL_OFF_VALUES:
        gate = "off"
    elif term_val in _TERMINAL_FORCE_VALUES:
        gate = "force"
    else:
        gate = "allowlist"

    push_enabled = push.get("enabled")
    if isinstance(push_enabled, str):
        push_enabled = push_enabled.strip().lower() in _TRUE_STRINGS
    elif not isinstance(push_enabled, bool):
        push_enabled = None

    tags = push.get("tags")
    if isinstance(tags, str):
        tags_tuple = tuple(t.strip() for t in tags.split(",") if t.strip())
    elif isinstance(tags, list):
        tags_tuple = tuple(str(t) for t in tags)
    else:
        tags_tuple = ()

    server = push.get("server") or base.get(NTFY_SERVER_ENV)
    priority = push.get("priority")

    home = amplifier_home or scope_file(settings_paths(None, None), "global").parent
    return NotificationStatus(
        ceiling=ceiling,
        ceiling_source=_source(NOTIFY_ENV, base, merged),
        desktop_gate=gate,
        desktop_gate_source=_source(NOTIFY_TERMINAL_ENV, base, merged),
        suppress=bool(notifications.get("suppress")),
        push_enabled=push_enabled,
        push_server=str(server) if server else None,
        push_priority=str(priority) if priority else None,
        push_tags=tags_tuple,
        topic=topic_configured(home, base),
    )


def load_status(
    project_dir: Path | None = None,
    amplifier_home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> NotificationStatus:
    """Load merged settings and resolve the effective notification status."""
    paths = settings_paths(project_dir, amplifier_home)
    settings = load_merged_settings(paths)
    home = paths.global_settings.parent
    return effective_status(settings, environ, amplifier_home=home)


__all__ = [
    "KNOWN_KEYS",
    "KeySpec",
    "NOTIFY_ENV",
    "NOTIFY_PUSH_ENABLED_ENV",
    "NOTIFY_TERMINAL_ENV",
    "NTFY_SERVER_ENV",
    "NTFY_TOPIC_ENV",
    "InvalidNotifyValueError",
    "NotificationStatus",
    "SetResult",
    "UnknownNotifyKeyError",
    "ValueKind",
    "effective_status",
    "known_key_names",
    "load_status",
    "parse_value",
    "resolved_environ",
    "set_enabled",
    "set_key",
    "topic_configured",
    "write_topic_to_keys_env",
]
