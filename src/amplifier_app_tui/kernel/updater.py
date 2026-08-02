"""Update the bundles/modules tui mounts — over amplifier-foundation.

This is NOT the umbrella uv self-update: tui isn't the ``amplifier``
uv-tool umbrella, it *consumes* amplifier-core/foundation as declared deps.
So ``update`` here refreshes the amplifier **runtime cache**
(``~/.amplifier/cache/<repo>-<hash>/``, the source layer foundation fetches
bundles/modules into) for the bundles tui actually composes — the active
bundle + its ``bundle.app`` overlays — via foundation's
``check_bundle_status`` (SHA compare, pinned refs skipped) and
``update_bundle`` (re-download updateable sources + reinstall deps).

``--force`` additionally runs ``uv cache clean`` so a ``@main``-pinned git
source that's stale in uv's *package* cache is genuinely re-fetched.

Updating the app itself, or the whole Amplifier platform, is out of scope
(see :func:`self_update_hint`) — that's ``git pull``/``uv sync`` or
``uv tool upgrade``, not this command.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from .config import (
    DEFAULT_BUNDLE,
    SettingsPaths,
    active_bundle_name,
    composed_overlay_uris,
    load_merged_settings,
)

# Foundation's stock "no remote to compare" summary for local/cache/non-git
# sources. It's technically accurate but unhelpful in a report, so the renderer
# collapses it to a plainer section label (see UNCHECKABLE_LABEL).
_GENERIC_UNCHECKABLE = "Update checking not supported for this source type"

# The clearer header for the deduplicated "couldn't be checked" section.
UNCHECKABLE_LABEL = "local/non-git sources skipped (no remote to compare)"

# The three Amplifier packages the "Amplifier" table reports on, and where
# their upstream lives. The app itself is git-hosted; core is the PyPI one.
APP_PACKAGE = "amplifier-app-tui"
APP_REPO_URL = "https://github.com/michaeljabbour/amplifier-app-tui"
FOUNDATION_REPO_URL = "https://github.com/microsoft/amplifier-foundation"


@dataclass(frozen=True)
class SourceRow:
    """One module/source inside a composed bundle, with its SHA comparison.

    ``has_update`` follows foundation's tri-state: ``True`` (remote is ahead),
    ``False`` (up to date), ``None`` (no remote to compare — local/cache/non-git;
    ``reason`` then carries the honest explanation)."""

    name: str
    cached: str | None = None  # local/cached commit (full or short)
    remote: str | None = None  # remote tip commit
    has_update: bool | None = None
    reason: str | None = None  # populated only when has_update is None


@dataclass(frozen=True)
class BundleUpdate:
    name: str
    target: str  # the raw bundle name/URI to act on
    summary: str
    has_updates: bool
    error: str | None = None
    sources: tuple[SourceRow, ...] = ()  # per-source SHA rows for the table
    unknown: tuple[str, ...] = ()  # per-source "couldn't be checked" reasons


def _short_sha(sha: str | None) -> str | None:
    """Truncate a commit to 7 chars for display; pass through short/None values."""
    return sha[:7] if sha else None


def unique_sources(statuses: Iterable[BundleUpdate]) -> list[SourceRow]:
    """All checkable sources across the whole composition, deduplicated.

    A shared transitive source (``amplifier-foundation``, ``skills``, ``modes``…)
    is referenced by nearly every composed bundle, so a per-bundle listing repeats
    it 15×. This collapses to one row per distinct ``(name, cached, remote)`` — the
    flat, app-cli-style view — so genuinely different pinned versions still show
    separately but identical repeats appear once. Only rows with a real remote
    comparison (``has_update is not None``) are included; local/non-git sources are
    summarized once by :func:`uncheckable_sources`. Pure/offline."""
    seen: dict[tuple[str, str | None, str | None], SourceRow] = {}
    for status in statuses:
        for row in status.sources:
            if row.has_update is not None:
                seen.setdefault((row.name, row.cached, row.remote), row)
    return [seen[key] for key in sorted(seen, key=lambda k: (k[0], k[1] or "", k[2] or ""))]


def uncheckable_sources(statuses: Iterable[BundleUpdate]) -> list[tuple[str, str]]:
    """Deduplicated ``(name, reason)`` for sources with no remote to compare.

    Collapses repeats so a shared module (e.g. ``tool-apply-patch``) used by many
    bundles appears exactly once. The returned ``reason`` is blank for the stock
    "not supported for this source type" case (the section label already says
    so); genuine failures (ls-remote errors, unresolvable refs) keep their text.
    Reads the structured ``sources`` rows when present; falls back to the legacy
    ``unknown`` reason strings (``"name: reason"``) so older/stubbed callers still
    render. Pure/offline."""

    def _clean(reason: str) -> str:
        reason = reason.strip()
        return "" if reason in ("", _GENERIC_UNCHECKABLE) else reason

    seen: dict[str, str] = {}
    for status in statuses:
        if status.sources:
            for row in status.sources:
                if row.has_update is None:
                    seen.setdefault(row.name, _clean(row.reason or ""))
        else:
            for entry in status.unknown:
                name, _, detail = entry.partition(":")
                seen.setdefault(name.strip(), _clean(detail))
    return sorted(seen.items())


def count_stale_sources(statuses: Iterable[BundleUpdate]) -> int:
    """How many unique sources actually have updates — what the prompt advertises.

    Counts :func:`unique_sources` rows with ``has_update`` True, NOT per-bundle
    stale flags: a shared stale source referenced by 11 composed bundles is *one*
    update, not 11 — the number must match the ``●`` rows the table shows.
    Pure/offline."""
    return sum(1 for row in unique_sources(statuses) if row.has_update)


# Foundation cache entries are ``<repo>-<content hash>`` (16 hex chars today;
# tolerate longer/shorter future shapes but never a short version-ish suffix).
_CACHE_HASH_RE = re.compile(r"-[0-9a-f]{12,}$")


def shorten_cache_path(path: str, amplifier_home: Path | None = None) -> str:
    """Compact a cache-absolute source path for the ``--verbose`` skipped listing.

    ``~/.amplifier/cache/<repo>-<hash>/modules/<m>`` → ``<repo>/modules/<m>``
    (prefix stripped, content-hash suffix dropped). Paths outside the cache pass
    through unchanged. Pure/offline."""
    try:
        rel = Path(path).relative_to(_amplifier_home(amplifier_home) / "cache")
    except ValueError:
        return path
    parts = list(rel.parts)
    if parts:
        parts[0] = _CACHE_HASH_RE.sub("", parts[0])
    return "/".join(parts)


# --- Amplifier packages (app + core + foundation) ---------------------------


@dataclass(frozen=True)
class PackageStatus:
    """One row of the "Amplifier" packages table (app-cli parity).

    ``local``/``remote`` are display strings — a version or a short SHA.
    ``has_update`` keeps the SourceRow tri-state; ``None`` means no comparison
    was possible and ``note`` then carries the honest, dim degrade text
    ("could not check" offline — this table never crashes or blocks)."""

    name: str
    local: str | None = None
    remote: str | None = None
    has_update: bool | None = None
    note: str | None = None


def shape_package_status(
    name: str, local: str | None, remote: str | None, *, commits: bool = False
) -> PackageStatus:
    """Fold raw local/remote values into a :class:`PackageStatus` row.

    ``commits=True`` truncates both sides to 7-char short SHAs before comparing.
    Either side missing → tri-state ``None`` with note ``"could not check"``;
    equal → up to date; differing → update available. Pure/offline."""
    if commits:
        local = _short_sha(local)
        remote = _short_sha(remote)
    if not local or not remote:
        return PackageStatus(name, local, remote, None, "could not check")
    return PackageStatus(name, local, remote, local != remote)


def installed_commit(dist_name: str) -> str | None:
    """The VCS commit a distribution was installed from, or ``None``.

    Reads the dist's ``direct_url.json`` (present for git-sourced installs —
    ``uv sync`` git deps, ``uv tool install`` from a repo). PyPI and editable
    installs have no ``vcs_info`` → ``None``. Offline; never raises."""
    import json
    from importlib import metadata

    try:
        raw = metadata.distribution(dist_name).read_text("direct_url.json")
        if not raw:
            return None
        commit = json.loads(raw).get("vcs_info", {}).get("commit_id")
        return str(commit) if commit else None
    except Exception:  # noqa: BLE001 — metadata shape varies by installer; degrade to None
        return None


def _dist_version(dist_name: str) -> str | None:
    """Installed distribution version, or ``None`` when absent (offline)."""
    from importlib import metadata

    try:
        return metadata.version(dist_name)
    except metadata.PackageNotFoundError:
        return None


def _ls_remote_sha(url: str, ref: str = "main", timeout: float = 5.0) -> str | None:
    """Tip SHA of *ref* at *url* via ``git ls-remote``; ``None`` offline (never raises)."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", "ls-remote", url, ref],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None
        return result.stdout.split()[0]
    except Exception:  # noqa: BLE001 — offline-safe: table degrades to "could not check"
        return None


def _pypi_latest(dist_name: str, timeout: float = 5.0) -> str | None:
    """Latest release on PyPI (JSON API, short timeout); ``None`` offline (never raises)."""
    try:
        import httpx

        resp = httpx.get(f"https://pypi.org/pypi/{dist_name}/json", timeout=timeout)
        resp.raise_for_status()
        return str(resp.json()["info"]["version"])
    except Exception:  # noqa: BLE001 — offline-safe: table degrades to "could not check"
        return None


def _version_behind(local: str, remote: str) -> bool:
    """Is *local* older than *remote*? PEP 440 compare when available, else ``!=``."""
    try:
        from packaging.version import InvalidVersion, Version
    except ImportError:
        return local != remote
    try:
        return Version(local) < Version(remote)
    except InvalidVersion:
        return local != remote


def _git_package_status(dist_name: str, repo_url: str, ref: str = "main") -> PackageStatus:
    """Status of a git-hosted package: installed commit vs remote *ref* tip.

    Local comes from the dist's ``direct_url.json``; installs without VCS
    metadata (editable dev clones) fall back to showing the version, which is
    not comparable to a SHA → tri-state ``None``."""
    remote = _ls_remote_sha(repo_url, ref)
    local_commit = installed_commit(dist_name)
    if local_commit:
        return shape_package_status(dist_name, local_commit, remote, commits=True)
    version = _dist_version(dist_name)
    note = "could not check" if remote is None else "no VCS metadata to compare"
    return PackageStatus(dist_name, version, _short_sha(remote), None, note)


def _pypi_package_status(dist_name: str) -> PackageStatus:
    """Status of a PyPI package: installed version vs latest release."""
    local = _dist_version(dist_name)
    remote = _pypi_latest(dist_name)
    if not local or not remote:
        return PackageStatus(dist_name, local, remote, None, "could not check")
    return PackageStatus(dist_name, local, remote, _version_behind(local, remote))


async def check_packages() -> list[PackageStatus]:
    """The "Amplifier" table rows: app (git) + core (PyPI) + foundation (git).

    The three checks run concurrently in threads (each is subprocess/httpx with
    a short timeout). Network failures degrade per-row to "could not check" —
    this never crashes and never blocks past the timeouts. Advisory only:
    applying app/platform updates stays out of scope (see
    :func:`self_update_hint`)."""
    import asyncio

    return list(
        await asyncio.gather(
            asyncio.to_thread(_git_package_status, APP_PACKAGE, APP_REPO_URL),
            asyncio.to_thread(_pypi_package_status, "amplifier-core"),
            asyncio.to_thread(_git_package_status, "amplifier-foundation", FOUNDATION_REPO_URL),
        )
    )


# --- anchors include ref (tracked, not statically pinned) -------------------

# Every live copy of the anchors include declares this ref. The wrapper tracks
# foundation @main (a *floating* ref): bundle.md pins nothing statically — a
# bare 40-hex SHA was tried and abandoned because GitHub stops serving a
# non-tip SHA once foundation advances, which broke clean installs (see #96).
# So "staleness" here means "the local anchors cache is behind upstream", which
# `amplifier-tui update` refreshes; it is NOT a static-pin bump.
_ANCHORS_REF_RE = re.compile(
    r"git\+https://github\.com/microsoft/amplifier-foundation@([^\s#]+)#subdirectory=bundles/anchors"
)


def read_anchors_ref(text: str) -> str | None:
    """Extract the foundation ref the anchors include tracks from bundle text.

    Matches the exact URI shape the anti-drift test relies on
    (``test_kernel_session_config.py``); tolerant of a branch, tag, or SHA.
    Returns ``None`` when no anchors include is present."""
    match = _ANCHORS_REF_RE.search(text)
    return match.group(1) if match else None


def anchors_ref() -> str | None:
    """The foundation ref anchors is tracked at, read from the packaged bundle.

    Offline/pure: reads the shipped ``tui.md`` — never touches the network."""
    from .config import packaged_bundles_dir

    try:
        text = (packaged_bundles_dir() / "tui.md").read_text(encoding="utf-8")
    except OSError:
        return None
    return read_anchors_ref(text)


def pin_files(repo_root: Path) -> tuple[Path, ...]:
    """The three live files that must carry the anchors ref in lockstep.

    Single source of truth for the bump script and the anti-drift tests, so a
    bump can never miss a copy: repo-root ``bundle.md``, the packaged
    byte-identical ``tui.md``, and the packaged ``anchors.md`` pointer."""
    packaged = repo_root / "src" / "amplifier_app_tui" / "data" / "bundles"
    return (repo_root / "bundle.md", packaged / "tui.md", packaged / "anchors.md")


def _is_sha(ref: str | None) -> bool:
    return bool(ref) and len(ref) == 40 and all(c in "0123456789abcdef" for c in ref.lower())


@dataclass(frozen=True)
class AnchorsStatus:
    """Freshness of the tracked anchors include against its upstream ref.

    Degrades honestly: any network/foundation failure sets ``error`` and leaves
    ``has_update`` at ``None`` (never a false "stale" finding), mirroring the
    ``check_bundles()`` offline contract."""

    ref: str | None
    has_update: bool | None = None
    cached_commit: str | None = None
    remote_commit: str | None = None
    detail: str = ""
    error: str | None = None

    @property
    def is_pinned(self) -> bool:
        """The ref is a bare 40-hex SHA (statically pinned — no auto-updates)."""
        return _is_sha(self.ref)

    @property
    def is_stale(self) -> bool:
        """True only when upstream is confirmed ahead of the local cache."""
        return self.has_update is True

    def describe(self) -> str:
        """One honest human line for `update` / `/doctor`."""
        if self.ref is None:
            return "anchors include not found in bundle"
        if self.error is not None:
            return f"anchors ref check unavailable · tracking @{self.ref} ({self.error})"
        if self.is_pinned:
            return f"anchors pinned to {self.ref[:8]} · no auto-updates (bump via update tooling)"
        if self.has_update is True:
            cached = (self.cached_commit or "unknown")[:8]
            remote = (self.remote_commit or "unknown")[:8]
            return (
                f"anchors (@{self.ref}) is behind upstream · {cached} → {remote} · "
                "run `amplifier-tui update`"
            )
        if self.has_update is False:
            cached = (self.cached_commit or self.remote_commit or "")[:8]
            suffix = f" ({cached})" if cached else ""
            return f"anchors up to date · tracking @{self.ref}{suffix}"
        return f"anchors ref check unavailable · tracking @{self.ref}"


def _anchors_uri(ref: str) -> str:
    """The anchors include's full source URI at *ref* (status + refresh share it)."""
    return (
        "git+https://github.com/microsoft/amplifier-foundation@"
        f"{ref}#subdirectory=bundles/anchors/bundle.md"
    )


async def anchors_status(amplifier_home: Path | None = None) -> AnchorsStatus:
    """Check the tracked anchors include against upstream (side-effect-light).

    The anchors bundle is composed via an *include*, and foundation's
    ``check_bundle_status`` deliberately skips included-bundle URIs, so anchors
    freshness is otherwise invisible to ``amplifier-tui update``. This checks
    it directly via foundation's git source handler (an ``ls-remote`` compare
    against the local cache). Offline → ``error`` set, ``has_update`` ``None``."""
    ref = anchors_ref()
    if ref is None:
        return AnchorsStatus(ref=None, error="anchors include not found")
    try:
        from amplifier_foundation.paths.resolution import parse_uri
        from amplifier_foundation.sources.git import GitSourceHandler
    except Exception as error:  # noqa: BLE001 — foundation unavailable
        return AnchorsStatus(ref=ref, error=f"foundation unavailable: {error}")
    cache_dir = _amplifier_home(amplifier_home) / "cache"
    try:
        source = await GitSourceHandler().get_status(parse_uri(_anchors_uri(ref)), cache_dir)
    except Exception as error:  # noqa: BLE001 — never crash the check (offline-safe)
        return AnchorsStatus(ref=ref, error=str(error))
    return AnchorsStatus(
        ref=ref,
        has_update=getattr(source, "has_update", None),
        cached_commit=getattr(source, "cached_commit", None),
        remote_commit=getattr(source, "remote_commit", None),
        detail=str(getattr(source, "summary", "") or ""),
        error=getattr(source, "error", None),
    )


async def refresh_anchors(amplifier_home: Path | None = None) -> bool:
    """Re-fetch the anchors include's cached foundation clone.

    The symmetric *write* to :func:`anchors_status`'s read: foundation's
    ``update_bundle`` skips included-bundle sources, so a stale anchors cache
    is otherwise un-healable by ``update`` (the circular "run update" hint).
    ``GitSourceHandler.update`` removes and re-clones the cache entry at the
    tracked ref. Returns ``False`` on any failure (offline-safe, never raises).
    """
    ref = anchors_ref()
    if ref is None:
        return False
    try:
        from amplifier_foundation.paths.resolution import parse_uri
        from amplifier_foundation.sources.git import GitSourceHandler

        cache_dir = _amplifier_home(amplifier_home) / "cache"
        await GitSourceHandler().update(parse_uri(_anchors_uri(ref)), cache_dir)
        return True
    except Exception:  # noqa: BLE001 — best-effort; caller reports failure
        return False


def _amplifier_home(amplifier_home: Path | None) -> Path:
    return amplifier_home or (Path.home() / ".amplifier")


def display_name(target: str) -> str:
    """A short label for a bundle name or git URI."""
    if "#subdirectory=" in target:
        return target.split("#subdirectory=")[-1]
    if target.startswith(("git+", "http")):
        return target.rsplit("/", 1)[-1].replace(".git", "").split("@")[0]
    return target


def target_bundles(settings: dict) -> list[str]:
    """The bundles tui composes: active bundle + composed overlays.

    Uses :func:`~.config.composed_overlay_uris` — the same set the boot
    composer loads — so the routing-matrix bundle (appended when routing is
    opted in) is checked/updated too, not just literal ``bundle.app`` entries.
    """
    active = active_bundle_name(settings) or DEFAULT_BUNDLE
    out: list[str] = []
    for target in (active, *composed_overlay_uris(settings)):
        if target and target not in out:
            out.append(target)
    return out


async def _load_single(target: str):  # noqa: ANN202 — foundation Bundle
    from amplifier_foundation import load_bundle

    from .bundle_admin import settings_paths
    from .config import bundle_search_paths, discover_bundle

    # Bare names ("tui") only resolve through foundation's persisted registry,
    # which is empty on a fresh machine — resolve against the app's bundle
    # search paths first (project → home → packaged), the same seam
    # ``bundle_admin.load_bundle_info`` uses. URIs pass through untouched.
    paths = settings_paths(None, None)
    search = bundle_search_paths(paths.project_settings.parent.parent, paths.global_settings.parent)
    resolved = discover_bundle(target, search) or target

    bundle = await load_bundle(resolved)
    if isinstance(bundle, dict):
        return next(iter(bundle.values())) if bundle else None
    return bundle


async def check_bundles(
    project_dir: Path | None = None, amplifier_home: Path | None = None
) -> list[BundleUpdate]:
    """Check each composed bundle's sources against remote (side-effect-light).

    Uses foundation ``check_bundle_status`` — SHA compare across the bundle's
    module sources; pinned refs report no update. Per-bundle failures become a
    ``BundleUpdate`` with ``error`` rather than aborting the whole check."""
    paths = SettingsPaths.default(
        (project_dir or Path.cwd()).resolve(), _amplifier_home(amplifier_home)
    )
    settings = load_merged_settings(paths)
    results: list[BundleUpdate] = []
    try:
        from amplifier_foundation import check_bundle_status
    except Exception:  # noqa: BLE001 — foundation unavailable
        return results
    for target in target_bundles(settings):
        name = display_name(target)
        try:
            bundle = await _load_single(target)
            if bundle is None:
                results.append(BundleUpdate(name, target, "not found", False, error="not found"))
                continue
            status = await check_bundle_status(bundle)
            summary = str(getattr(status, "summary", "") or "")
            rows: list[SourceRow] = []
            for source in getattr(status, "sources", None) or []:
                has_update = getattr(source, "has_update", None)
                reason = None
                if has_update is None:
                    reason = str(
                        getattr(source, "error", None)
                        or getattr(source, "summary", "")
                        or "reason unavailable"
                    )
                rows.append(
                    SourceRow(
                        name=display_name(str(getattr(source, "source_uri", "") or "")),
                        cached=_short_sha(getattr(source, "cached_commit", None)),
                        remote=_short_sha(getattr(source, "remote_commit", None)),
                        has_update=has_update,
                        reason=reason,
                    )
                )
            # Legacy reason strings, derived from the structured rows so the
            # two views can never disagree.
            unknown = tuple(
                f"{row.name}: {row.reason or 'reason unavailable'}"
                for row in rows
                if row.has_update is None
            )
            results.append(
                BundleUpdate(
                    name,
                    target,
                    summary,
                    bool(getattr(status, "has_updates", False)),
                    sources=tuple(rows),
                    unknown=unknown,
                )
            )
        except Exception as error:  # noqa: BLE001 — never abort the whole check
            results.append(
                BundleUpdate(name, target, f"check failed: {error}", False, error=str(error))
            )
    return results


async def update_bundles(targets: list[str]) -> tuple[list[str], list[tuple[str, str]]]:
    """Apply ``update_bundle`` to each target.

    Returns ``(updated names, failed (name, reason) pairs)`` — the reason feeds
    the per-item ``✗ <name> — <error>`` apply line (app-cli parity)."""
    updated: list[str] = []
    failed: list[tuple[str, str]] = []
    try:
        from amplifier_foundation import update_bundle
    except Exception as error:  # noqa: BLE001
        return updated, [(display_name(t), f"foundation unavailable: {error}") for t in targets]
    for target in targets:
        name = display_name(target)
        try:
            bundle = await _load_single(target)
            if bundle is None:
                failed.append((name, "not found"))
                continue
            await update_bundle(bundle)
            updated.append(name)
        except Exception as error:  # noqa: BLE001 — report per-bundle, keep going
            failed.append((name, str(error)))
    return updated, failed


def uv_cache_clean() -> bool:
    """``uv cache clean`` — force a fresh fetch of ``@main``-pinned sources."""
    import subprocess

    try:
        subprocess.run(["uv", "cache", "clean"], check=False, capture_output=True, timeout=120)
        return True
    except Exception:  # noqa: BLE001 — best-effort
        return False


def self_update_hint() -> str:
    """How to update the app + platform (out of scope for this command)."""
    return (
        "to update the app itself: `git pull && uv sync` (clone) or "
        "`uv tool install --reinstall .` (tool)\n"
        "to update the Amplifier platform: `uv tool upgrade amplifier`"
    )


__all__ = [
    "APP_PACKAGE",
    "APP_REPO_URL",
    "FOUNDATION_REPO_URL",
    "UNCHECKABLE_LABEL",
    "AnchorsStatus",
    "BundleUpdate",
    "PackageStatus",
    "SourceRow",
    "anchors_ref",
    "anchors_status",
    "check_bundles",
    "check_packages",
    "count_stale_sources",
    "display_name",
    "installed_commit",
    "pin_files",
    "read_anchors_ref",
    "refresh_anchors",
    "self_update_hint",
    "shape_package_status",
    "shorten_cache_path",
    "target_bundles",
    "uncheckable_sources",
    "unique_sources",
    "update_bundles",
    "uv_cache_clean",
]
