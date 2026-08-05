"""Anchors ref lifecycle — pure parsing, offline-safe status, bump mechanism.

Everything here is offline (the house rule): ``read_anchors_ref`` runs against
the real packaged bundle, ``anchors_status`` monkeypatches foundation's git
handler, and the bump script rewrites a ``tmp_path`` copy of the three files.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from amplifier_app_tui.kernel import updater

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_bump() -> ModuleType:
    """Load the repo-maintenance bump script by path (it's not a package)."""
    path = REPO_ROOT / "scripts" / "bump_anchors_ref.py"
    spec = importlib.util.spec_from_file_location("bump_anchors_ref", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# -- read_anchors_ref (pure) ------------------------------------------------


def test_read_anchors_ref_extracts_floating_ref() -> None:
    text = (
        "includes:\n  - bundle: git+https://github.com/microsoft/"
        "amplifier-foundation@main#subdirectory=bundles/anchors/bundle.md\n"
    )
    assert updater.read_anchors_ref(text) == "main"


def test_read_anchors_ref_extracts_sha_and_tag() -> None:
    sha = "93615d9847ce40313cc0d60583cb886de4337f9e"
    tag_text = "git+https://github.com/microsoft/amplifier-foundation@v2.2.0#subdirectory=bundles/anchors/x"
    sha_text = f"git+https://github.com/microsoft/amplifier-foundation@{sha}#subdirectory=bundles/anchors/x"
    assert updater.read_anchors_ref(tag_text) == "v2.2.0"
    assert updater.read_anchors_ref(sha_text) == sha


def test_read_anchors_ref_none_when_absent() -> None:
    assert updater.read_anchors_ref("no anchors include here") is None


def test_anchors_ref_reads_real_packaged_bundle() -> None:
    # The shipped bundle is an immutable full commit.
    assert updater._is_sha(updater.anchors_ref())


# -- pin_files: single source of truth --------------------------------------


def test_pin_files_lists_all_three_live_copies() -> None:
    files = updater.pin_files(REPO_ROOT)
    names = {p.name for p in files}
    assert names == {"bundle.md", "tui.md", "anchors.md"}
    for path in files:
        assert path.exists(), f"pin file missing: {path}"


def test_all_pin_copies_share_one_ref() -> None:
    """Generalizes the pairwise lockstep to all three live copies."""
    refs = {
        updater.read_anchors_ref(path.read_text(encoding="utf-8"))
        for path in updater.pin_files(REPO_ROOT)
    }
    assert len(refs) == 1 and None not in refs, f"pin copies drifted: {refs}"


# -- AnchorsStatus.describe honesty -----------------------------------------


def test_describe_behind_names_the_action() -> None:
    status = updater.AnchorsStatus(
        ref="main", has_update=True, cached_commit="aaaa1111", remote_commit="bbbb2222"
    )
    text = status.describe()
    assert status.is_stale
    assert "behind upstream" in text and "amplifier-tui update" in text


def test_describe_current() -> None:
    status = updater.AnchorsStatus(ref="main", has_update=False, cached_commit="cccc3333")
    assert not status.is_stale
    assert "up to date" in status.describe()


def test_describe_offline_is_neutral_not_stale() -> None:
    status = updater.AnchorsStatus(ref="main", error="network down")
    assert not status.is_stale
    assert "unavailable" in status.describe()


def test_describe_flags_bare_sha_pin() -> None:
    status = updater.AnchorsStatus(ref="93615d9847ce40313cc0d60583cb886de4337f9e", has_update=None)
    assert status.is_pinned
    assert "pinned" in status.describe()


# -- anchors_status: offline-safe, monkeypatched foundation -----------------


@pytest.mark.asyncio
async def test_anchors_status_behind(monkeypatch) -> None:
    class _Src:
        has_update = True
        cached_commit = "old12345"
        remote_commit = "new67890"
        summary = "Update available"
        error = None

    class _Handler:
        async def get_status(self, parsed, cache_dir):  # noqa: ANN001
            return _Src()

    import amplifier_foundation.sources.git as git_mod

    monkeypatch.setattr(updater, "anchors_ref", lambda: "main")
    monkeypatch.setattr(git_mod, "GitSourceHandler", _Handler)
    status = await updater.anchors_status()
    assert status.is_stale
    assert status.cached_commit == "old12345"
    assert status.remote_commit == "new67890"


@pytest.mark.asyncio
async def test_anchors_status_degrades_offline(monkeypatch) -> None:
    class _Handler:
        async def get_status(self, parsed, cache_dir):  # noqa: ANN001
            raise OSError("no network")

    import amplifier_foundation.sources.git as git_mod

    monkeypatch.setattr(updater, "anchors_ref", lambda: "main")
    monkeypatch.setattr(git_mod, "GitSourceHandler", _Handler)
    status = await updater.anchors_status()
    assert status.error is not None
    assert status.has_update is None
    assert not status.is_stale  # never a false finding offline


@pytest.mark.asyncio
async def test_anchors_status_for_shipped_sha_is_network_free(monkeypatch) -> None:
    class _Handler:
        def __init__(self):
            raise AssertionError("a static pin must not query the network")

    import amplifier_foundation.sources.git as git_mod

    monkeypatch.setattr(git_mod, "GitSourceHandler", _Handler)
    status = await updater.anchors_status()
    assert status.is_pinned
    assert status.has_update is False
    assert status.cached_commit == status.ref


@pytest.mark.asyncio
async def test_anchors_status_none_when_no_ref(monkeypatch) -> None:
    monkeypatch.setattr(updater, "anchors_ref", lambda: None)
    status = await updater.anchors_status()
    assert status.ref is None and status.error is not None


# -- bump mechanism ---------------------------------------------------------


def _write_pin_copies(root: Path, ref: str) -> None:
    include = (
        "includes:\n  - bundle: git+https://github.com/microsoft/"
        f"amplifier-foundation@{ref}#subdirectory=bundles/anchors/bundle.md\n"
    )
    body = "---\n" + include + "---\nbody\n"
    (root / "bundle.md").write_text(body, encoding="utf-8")
    packaged = root / "src" / "amplifier_app_tui" / "data" / "bundles"
    packaged.mkdir(parents=True, exist_ok=True)
    (packaged / "tui.md").write_text(body, encoding="utf-8")
    (packaged / "anchors.md").write_text("---\n" + include + "---\npointer\n", encoding="utf-8")


def test_bump_rewrites_all_copies(tmp_path: Path, monkeypatch) -> None:
    bump = _load_bump()
    _write_pin_copies(tmp_path, "main")
    monkeypatch.setattr(bump, "REPO_ROOT", tmp_path)
    sha = "93615d9847ce40313cc0d60583cb886de4337f9e"
    monkeypatch.setattr(bump, "ANCHORS_COMMIT", sha)
    assert bump.main([sha]) == 0
    refs = {
        updater.read_anchors_ref(p.read_text(encoding="utf-8")) for p in updater.pin_files(tmp_path)
    }
    assert refs == {sha}
    # byte-identity (bundle.md ↔ tui.md) preserved by the bump.
    root, tui, _ = updater.pin_files(tmp_path)
    assert root.read_bytes() == tui.read_bytes()


def test_bump_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    bump = _load_bump()
    sha = "93615d9847ce40313cc0d60583cb886de4337f9e"
    _write_pin_copies(tmp_path, sha)
    monkeypatch.setattr(bump, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(bump, "ANCHORS_COMMIT", sha)
    assert bump.main([sha]) == 0  # no-op path


def test_bump_refuses_float_or_sha_not_in_recursive_lock(tmp_path: Path, monkeypatch) -> None:
    bump = _load_bump()
    _write_pin_copies(tmp_path, "main")
    monkeypatch.setattr(bump, "REPO_ROOT", tmp_path)
    locked = "93615d9847ce40313cc0d60583cb886de4337f9e"
    monkeypatch.setattr(bump, "ANCHORS_COMMIT", locked)
    assert bump.main(["main"]) == 1
    assert bump.main(["a" * 40]) == 1
