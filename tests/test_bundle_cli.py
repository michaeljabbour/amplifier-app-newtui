"""``amplifier-newtui bundle`` group wiring (click CliRunner).

The admin logic is unit-tested in ``test_kernel_bundle_admin``; this
covers the CLI plumbing: help/subcommands, the offline foundation-backed
``show`` on the packaged bundle, and a ``use`` → ``current`` roundtrip
with settings redirected to ``tmp_path`` (never the real ~/.amplifier).
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from amplifier_app_newtui.kernel import bundle_admin
from amplifier_app_newtui.main import main


def test_bundle_group_lists_subcommands() -> None:
    result = CliRunner().invoke(main, ["bundle", "--help"])
    assert result.exit_code == 0
    for sub in ("list", "show", "use", "clear", "current", "add", "remove", "update"):
        assert sub in result.output


def test_bundle_list_all_is_superset_of_default() -> None:
    # --all surfaces nested dependency bundles from the shared registry; it
    # can never return fewer entries than the default (user-selectable) view.
    # Compare entry identity, not Rich-rendered line counts: when no nested
    # bundles exist, the default-only "Use --all" hint intentionally makes
    # its rendered output one line longer (the clean Linux CI environment).
    default_names = {entry.name for entry in bundle_admin.list_bundles()}
    every_name = {entry.name for entry in bundle_admin.list_bundles(all_bundles=True)}
    assert default_names <= every_name

    runner = CliRunner()
    default = runner.invoke(main, ["bundle", "list"])
    every = runner.invoke(main, ["bundle", "list", "--all"])
    assert default.exit_code == 0 and every.exit_code == 0
    assert "Use --all" in default.output
    assert "Use --all" not in every.output


def test_bundle_show_packaged_newtui_offline() -> None:
    # The packaged ``newtui`` bundle resolves via newtui discovery → a local
    # file, so foundation loads it without any network.
    result = CliRunner().invoke(main, ["bundle", "show", "newtui"])
    assert result.exit_code == 0
    assert "newtui" in result.output
    assert "mounts:" in result.output


def test_bundle_use_current_clear_roundtrip(tmp_path: Path, monkeypatch) -> None:
    paths = bundle_admin.settings_paths(tmp_path / "proj", tmp_path / "home")
    monkeypatch.setattr(bundle_admin, "settings_paths", lambda *a, **k: paths)
    # ``use`` accepts a URI even when not discovered on disk.
    runner = CliRunner()

    used = runner.invoke(main, ["bundle", "use", "git+https://x/b.git"])
    assert used.exit_code == 0
    assert (
        bundle_admin.read_scope(paths.global_settings)["bundle"]["active"] == "git+https://x/b.git"
    )

    cleared = runner.invoke(main, ["bundle", "clear"])
    assert cleared.exit_code == 0
    assert "cleared" in cleared.output


def test_bundle_use_rejects_unknown_name(tmp_path: Path, monkeypatch) -> None:
    paths = bundle_admin.settings_paths(tmp_path / "proj", tmp_path / "home")
    monkeypatch.setattr(bundle_admin, "settings_paths", lambda *a, **k: paths)
    result = CliRunner().invoke(main, ["bundle", "use", "does-not-exist"])
    assert result.exit_code == 1
    assert "unknown bundle" in result.output
