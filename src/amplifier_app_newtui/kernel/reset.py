"""Data-safe ``reset`` — selective category clear with explicit preservation.

Re-expresses amplifier-app-cli's ``reset`` recovery command as a guarded,
category-scoped cleaner scoped to newtui's own app home. app-cli's reset is
a data-safe *uninstall/reinstall* that PRESERVES categories of user data (it
maps ``~/.amplifier`` entries to categories — ``projects``/``settings``/
``keys``/``cache``/``registry`` — removes only what you don't preserve, then
``uv tool install``s amplifier again). newtui is not the ``amplifier`` uv
tool, so the reachable, portable core of that contract is the **data half**:
preview + selective clear with explicit preservation, never touching secrets
or user data unless you name them, never reaching outside the app home.

Framing (issue #110): ``--category`` names what to CLEAR (not preserve), the
inverse of app-cli's default ``--preserve`` view but identical to its
``--remove``. The default clears only the two auto-regenerating categories
(``cache``, ``registry``) — the same net effect as app-cli's safe default,
which preserves everything else.

Deliberately NOT ported (see ``.ai/worker_report.md``): ``uv cache clean`` +
``uv tool uninstall/install`` (newtui ships differently), the interactive
checklist TUI, app-cli's ``--full`` nuke option, and its dynamic ``other``
sweep of uncategorized files — this cleaner is allowlist-only for safety and
never deletes a path it wasn't explicitly told to.

Pure path/data logic (ADR-0007: ``kernel/`` layer, no Textual, no
amplifier-core). Every removal is guarded to stay inside the resolved app
home, and :func:`assert_app_home` refuses to run against a path it cannot
confirm is an amplifier home — so tests and the probe operate on a scratch
``AMPLIFIER_HOME`` and the developer's real ``~/.amplifier`` is never at risk.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Category:
    """One resettable class of data and the app-home entries it owns."""

    name: str
    description: str
    entries: tuple[str, ...]
    secret: bool = False
    auto_regenerates: bool = False


# Category taxonomy — mirrors app-cli's (projects/settings/keys/cache/registry)
# adapted to newtui's real footprint under the app home. ``entries`` are plain
# names directly under the app home; never globs, never ``..``.
CATEGORIES: dict[str, Category] = {
    "cache": Category(
        "cache",
        "Downloaded bundle/source cache (auto-regenerates)",
        ("cache",),
        auto_regenerates=True,
    ),
    "registry": Category(
        "registry",
        "Bundle discovery registry (auto-regenerates)",
        ("registry.json",),
        auto_regenerates=True,
    ),
    "sessions": Category(
        "sessions",
        "Session transcripts, UI-event logs & cost history (projects/)",
        ("projects",),
    ),
    "config": Category(
        "config",
        "Settings, MCP servers & routing overrides",
        ("settings.yaml", "settings.local.yaml", "mcp.json", "routing"),
    ),
    "bundles": Category(
        "bundles",
        "Locally added bundles",
        ("bundles",),
    ),
    "keys": Category(
        "keys",
        "Provider API keys / secrets (keys.env)",
        ("keys.env",),
        secret=True,
    ),
}

# Stable display/iteration order (safe → destructive; secrets last).
CATEGORY_ORDER: tuple[str, ...] = (
    "cache",
    "registry",
    "sessions",
    "config",
    "bundles",
    "keys",
)

# Safe default: only the auto-regenerating categories. NOT a nuke-everything
# default, and never a secret category.
DEFAULT_CATEGORIES: frozenset[str] = frozenset({"cache", "registry"})

# Marker names that identify a directory as an amplifier home (any one is
# enough). Used by :func:`looks_like_app_home` as a structural safety check.
_HOME_MARKERS: tuple[str, ...] = (
    "settings.yaml",
    "settings.local.yaml",
    "keys.env",
    "mcp.json",
    "registry.json",
    "projects",
    "bundles",
    "cache",
    "routing",
)


class ResetError(Exception):
    """A guard tripped: unknown category, or an unsafe / unconfirmable home."""


def resolve_app_home(amplifier_home: Path | None = None) -> Path:
    """Resolve the app home the same way the rest of the kernel does.

    An explicit argument always wins (tests/CLI override). Otherwise honor
    the foundation-native ``AMPLIFIER_HOME`` env var, then fall back to
    ``~/.amplifier`` — matching ``kernel.bundle_admin._amplifier_home`` so
    every surface agrees on where the data lives.
    """
    if amplifier_home is not None:
        return amplifier_home.expanduser()
    env_home = os.environ.get("AMPLIFIER_HOME")
    if env_home:
        return Path(env_home).expanduser()
    return Path.home() / ".amplifier"


def parse_categories(values: tuple[str, ...] | list[str] | None) -> set[str]:
    """Normalize CLI ``--category`` values into a validated category set.

    Accepts repeated flags and comma-separated values (``--category cache``
    or ``--category cache,registry``). Empty/None yields the safe default.
    Raises :class:`ResetError` naming any unknown categories.
    """
    if not values:
        return set(DEFAULT_CATEGORIES)
    requested: set[str] = set()
    for value in values:
        for token in value.split(","):
            cleaned = token.strip().lower()
            if cleaned:
                requested.add(cleaned)
    if not requested:
        return set(DEFAULT_CATEGORIES)
    unknown = sorted(requested - set(CATEGORIES))
    if unknown:
        raise ResetError(
            f"unknown category: {', '.join(unknown)} · valid: {', '.join(CATEGORY_ORDER)}"
        )
    return requested


def looks_like_app_home(home: Path) -> tuple[bool, str | None]:
    """Confirm *home* is plausibly an amplifier home before any deletion.

    Refuses obviously-dangerous roots (the filesystem root, a path a couple
    levels below it, or the user's literal ``$HOME``) and anything that
    neither is named ``.amplifier``, matches ``AMPLIFIER_HOME``, nor carries
    a recognizable marker file. Returns ``(ok, reason)`` — ``reason`` is the
    refusal message when ``ok`` is ``False``.
    """
    resolved = home.expanduser().resolve()

    # Guard against catastrophic targets regardless of naming.
    if len(resolved.parts) <= 2:
        return (False, f"refusing to reset a path this close to the filesystem root: {resolved}")
    if resolved == Path.home().resolve():
        return (False, f"refusing to reset the home directory itself: {resolved}")

    # Confirm it actually looks like an amplifier home.
    if resolved.name == ".amplifier":
        return (True, None)
    env_home = os.environ.get("AMPLIFIER_HOME")
    if env_home and Path(env_home).expanduser().resolve() == resolved:
        return (True, None)
    if resolved.is_dir() and any((resolved / marker).exists() for marker in _HOME_MARKERS):
        return (True, None)
    return (
        False,
        f"{resolved} does not look like an amplifier home "
        "(not named .amplifier, not AMPLIFIER_HOME, no marker files)",
    )


def assert_app_home(home: Path) -> Path:
    """Return the resolved home if safe, else raise :class:`ResetError`."""
    ok, reason = looks_like_app_home(home)
    if not ok:
        raise ResetError(reason or "unsafe reset target")
    return home.expanduser().resolve()


def _within_home(home: Path, path: Path) -> bool:
    """True iff *path* is a real entry contained directly within *home*.

    Containment is decided on the entry's PARENT (which follows symlinks in
    ancestor dirs) rather than the entry itself: a symlink sitting under the
    home is a legitimate target — :func:`_remove` unlinks the link and never
    follows it — so we must not reject it just because its target points
    outside. A ``..`` or empty final component is refused outright.
    """
    home_resolved = home.resolve()
    try:
        parent_resolved = path.parent.resolve()
    except OSError:
        return False
    if path.name in ("", "..", "."):
        return False
    return parent_resolved == home_resolved or home_resolved in parent_resolved.parents


def category_targets(home: Path, category: str) -> list[Path]:
    """Existing app-home paths owned by *category* (empty if none present)."""
    found: list[Path] = []
    for entry in CATEGORIES[category].entries:
        candidate = home / entry
        if (candidate.exists() or candidate.is_symlink()) and _within_home(home, candidate):
            found.append(candidate)
    return found


@dataclass
class ResetReport:
    """Outcome of a plan (dry-run) or execution.

    ``removed`` lists concrete paths removed (or, in a dry run, that *would*
    be removed); ``preserved`` lists existing paths kept because their
    category was not selected. ``clear``/``keep`` are the category names.
    """

    home: Path
    dry_run: bool
    clear: tuple[str, ...]
    keep: tuple[str, ...]
    removed: list[Path] = field(default_factory=list)
    preserved: list[Path] = field(default_factory=list)

    @property
    def secret_cleared(self) -> tuple[str, ...]:
        return tuple(name for name in self.clear if CATEGORIES[name].secret)

    @property
    def destructive_cleared(self) -> tuple[str, ...]:
        """Selected categories that do NOT auto-regenerate (need confirming)."""
        return tuple(name for name in self.clear if not CATEGORIES[name].auto_regenerates)


def _ordered(categories: set[str]) -> tuple[str, ...]:
    return tuple(name for name in CATEGORY_ORDER if name in categories)


def run_reset(home: Path, categories: set[str], *, dry_run: bool) -> ResetReport:
    """Preview (``dry_run=True``) or perform a selective category clear.

    Every target is re-checked to be a strict descendant of *home* before it
    is touched; a dry run touches nothing. Preserved paths are the existing
    entries of the categories NOT selected — the report is the preserved-vs-
    removed summary callers print.
    """
    home = assert_app_home(home)
    clear = _ordered(categories)
    keep = _ordered(set(CATEGORIES) - categories)

    report = ResetReport(home=home, dry_run=dry_run, clear=clear, keep=keep)

    for name in clear:
        for target in category_targets(home, name):
            # Defence in depth: never delete outside the confirmed home.
            if not _within_home(home, target):
                continue
            report.removed.append(target)
            if not dry_run:
                _remove(target)

    for name in keep:
        report.preserved.extend(category_targets(home, name))

    return report


def _remove(path: Path) -> None:
    """Remove a file, symlink, or directory tree in place."""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()


__all__ = [
    "CATEGORIES",
    "CATEGORY_ORDER",
    "DEFAULT_CATEGORIES",
    "Category",
    "ResetError",
    "ResetReport",
    "assert_app_home",
    "category_targets",
    "looks_like_app_home",
    "parse_categories",
    "resolve_app_home",
    "run_reset",
]
