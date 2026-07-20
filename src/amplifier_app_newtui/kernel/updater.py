"""Update the bundles/modules newtui mounts — over amplifier-foundation.

This is NOT the umbrella uv self-update: newtui isn't the ``amplifier``
uv-tool umbrella, it *consumes* amplifier-core/foundation as declared deps.
So ``update`` here refreshes the amplifier **runtime cache**
(``~/.amplifier/cache/<repo>-<hash>/``, the source layer foundation fetches
bundles/modules into) for the bundles newtui actually composes — the active
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

from dataclasses import dataclass
from pathlib import Path

from .config import (
    DEFAULT_BUNDLE,
    SettingsPaths,
    active_bundle_name,
    load_merged_settings,
    overlay_uris,
)


@dataclass(frozen=True)
class BundleUpdate:
    name: str
    target: str  # the raw bundle name/URI to act on
    summary: str
    has_updates: bool
    error: str | None = None


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
    """The bundles newtui composes: active bundle + ``bundle.app`` overlays."""
    active = active_bundle_name(settings) or DEFAULT_BUNDLE
    out: list[str] = []
    for target in (active, *overlay_uris(settings)):
        if target and target not in out:
            out.append(target)
    return out


async def _load_single(target: str):  # noqa: ANN202 — foundation Bundle
    from amplifier_foundation import load_bundle

    bundle = await load_bundle(target)
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
            results.append(
                BundleUpdate(name, target, summary, bool(getattr(status, "has_updates", False)))
            )
        except Exception as error:  # noqa: BLE001 — never abort the whole check
            results.append(BundleUpdate(name, target, f"check failed: {error}", False, error=str(error)))
    return results


async def update_bundles(targets: list[str]) -> tuple[list[str], list[str]]:
    """Apply ``update_bundle`` to each target; returns (updated, failed) names."""
    updated: list[str] = []
    failed: list[str] = []
    try:
        from amplifier_foundation import update_bundle
    except Exception:  # noqa: BLE001
        return updated, failed
    for target in targets:
        name = display_name(target)
        try:
            bundle = await _load_single(target)
            if bundle is None:
                failed.append(name)
                continue
            await update_bundle(bundle)
            updated.append(name)
        except Exception:  # noqa: BLE001 — report per-bundle, keep going
            failed.append(name)
    return updated, failed


def uv_cache_clean() -> bool:
    """``uv cache clean`` — force a fresh fetch of ``@main``-pinned sources."""
    import subprocess

    try:
        subprocess.run(
            ["uv", "cache", "clean"], check=False, capture_output=True, timeout=120
        )
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
    "BundleUpdate",
    "check_bundles",
    "display_name",
    "self_update_hint",
    "target_bundles",
    "update_bundles",
    "uv_cache_clean",
]
