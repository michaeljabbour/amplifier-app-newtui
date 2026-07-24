"""Unit tests for the data-safe ``reset`` kernel (``kernel/reset.py``).

Every test operates on a scratch app home under ``tmp_path`` — the real
``~/.amplifier`` is never touched. The safety guards (home confirmation,
within-home containment, secrets-only-when-named) are exercised directly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from amplifier_app_newtui.kernel import reset


def _populate(home: Path) -> Path:
    """Build a fully-stocked scratch app home; return it."""
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.yaml").write_text("bundle: {}\n", encoding="utf-8")
    (home / "settings.local.yaml").write_text("x: 1\n", encoding="utf-8")
    (home / "keys.env").write_text("ANTHROPIC_API_KEY=secret\n", encoding="utf-8")
    (home / "mcp.json").write_text("{}\n", encoding="utf-8")
    (home / "registry.json").write_text("{}\n", encoding="utf-8")
    cache = home / "cache" / "bundle-abc"
    cache.mkdir(parents=True)
    (cache / "blob.txt").write_text("cached\n", encoding="utf-8")
    routing = home / "routing"
    routing.mkdir()
    (routing / "matrix.yaml").write_text("m: 1\n", encoding="utf-8")
    bundles = home / "bundles"
    bundles.mkdir()
    (bundles / "b.md").write_text("bundle\n", encoding="utf-8")
    sessions = home / "projects" / "slug" / "sessions" / "sess-1"
    sessions.mkdir(parents=True)
    (sessions / "transcript.jsonl").write_text("{}\n", encoding="utf-8")
    return home


# -- taxonomy ---------------------------------------------------------------


def test_taxonomy_is_internally_consistent() -> None:
    # Every ordered name is a real category and vice-versa.
    assert set(reset.CATEGORY_ORDER) == set(reset.CATEGORIES)
    # The default is a subset that is safe: auto-regenerating, never secret.
    for name in reset.DEFAULT_CATEGORIES:
        assert reset.CATEGORIES[name].auto_regenerates
        assert not reset.CATEGORIES[name].secret
    # Exactly one secret category, and it is not a default.
    secrets = {n for n, c in reset.CATEGORIES.items() if c.secret}
    assert secrets == {"keys"}
    assert not (secrets & reset.DEFAULT_CATEGORIES)


# -- resolve_app_home -------------------------------------------------------


def test_resolve_app_home_explicit_wins(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AMPLIFIER_HOME", str(tmp_path / "env"))
    assert reset.resolve_app_home(tmp_path / "explicit") == tmp_path / "explicit"


def test_resolve_app_home_honors_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AMPLIFIER_HOME", str(tmp_path / "env"))
    assert reset.resolve_app_home() == tmp_path / "env"


def test_resolve_app_home_defaults_to_dot_amplifier(monkeypatch) -> None:
    monkeypatch.delenv("AMPLIFIER_HOME", raising=False)
    assert reset.resolve_app_home() == Path.home() / ".amplifier"


# -- parse_categories -------------------------------------------------------


def test_parse_categories_empty_is_safe_default() -> None:
    assert reset.parse_categories(None) == set(reset.DEFAULT_CATEGORIES)
    assert reset.parse_categories(()) == set(reset.DEFAULT_CATEGORIES)


def test_parse_categories_splits_commas_and_trims_case() -> None:
    assert reset.parse_categories(("Cache, registry",)) == {"cache", "registry"}
    assert reset.parse_categories(("sessions", "keys")) == {"sessions", "keys"}


def test_parse_categories_all_whitespace_falls_back_to_default() -> None:
    assert reset.parse_categories((" , ",)) == set(reset.DEFAULT_CATEGORIES)


def test_parse_categories_unknown_raises() -> None:
    with pytest.raises(reset.ResetError) as excinfo:
        reset.parse_categories(("cache", "bogus"))
    assert "bogus" in str(excinfo.value)


# -- looks_like_app_home / assert_app_home ----------------------------------


def test_looks_like_app_home_accepts_dot_amplifier(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("AMPLIFIER_HOME", raising=False)
    home = tmp_path / "nested" / ".amplifier"
    home.mkdir(parents=True)
    ok, reason = reset.looks_like_app_home(home)
    assert ok and reason is None


def test_looks_like_app_home_accepts_marker_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("AMPLIFIER_HOME", raising=False)
    home = tmp_path / "scratch-home"
    home.mkdir()
    (home / "settings.yaml").write_text("x: 1\n", encoding="utf-8")
    ok, _ = reset.looks_like_app_home(home)
    assert ok


def test_looks_like_app_home_accepts_env_match(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "scratch-env-home"
    home.mkdir()
    monkeypatch.setenv("AMPLIFIER_HOME", str(home))
    ok, _ = reset.looks_like_app_home(home)
    assert ok


def test_looks_like_app_home_rejects_bare_dir(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("AMPLIFIER_HOME", raising=False)
    home = tmp_path / "just-a-dir"
    home.mkdir()
    ok, reason = reset.looks_like_app_home(home)
    assert not ok
    assert "does not look like" in (reason or "")


def test_looks_like_app_home_rejects_shallow_root() -> None:
    ok, reason = reset.looks_like_app_home(Path("/"))
    assert not ok
    assert "filesystem root" in (reason or "")


def test_looks_like_app_home_rejects_home_dir(monkeypatch) -> None:
    monkeypatch.delenv("AMPLIFIER_HOME", raising=False)
    ok, reason = reset.looks_like_app_home(Path.home())
    assert not ok
    assert "home directory" in (reason or "")


def test_assert_app_home_raises_on_unsafe(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("AMPLIFIER_HOME", raising=False)
    bare = tmp_path / "bare"
    bare.mkdir()
    with pytest.raises(reset.ResetError):
        reset.assert_app_home(bare)


# -- category_targets -------------------------------------------------------


def test_category_targets_lists_present_entries(tmp_path: Path) -> None:
    home = _populate(tmp_path / ".amplifier")
    assert reset.category_targets(home, "cache") == [home / "cache"]
    config = reset.category_targets(home, "config")
    assert home / "settings.yaml" in config
    assert home / "routing" in config


def test_category_targets_absent_is_empty(tmp_path: Path) -> None:
    home = tmp_path / ".amplifier"
    home.mkdir()
    assert reset.category_targets(home, "sessions") == []


# -- run_reset (the heart) --------------------------------------------------


def test_dry_run_removes_nothing(tmp_path: Path) -> None:
    home = _populate(tmp_path / ".amplifier")
    report = reset.run_reset(home, {"cache", "registry", "sessions"}, dry_run=True)
    assert report.dry_run is True
    # It reports what WOULD go, but every file is still on disk.
    assert report.removed
    assert (home / "cache").exists()
    assert (home / "registry.json").exists()
    assert (home / "projects").exists()


def test_default_clears_only_autoregen_and_preserves_the_rest(tmp_path: Path) -> None:
    home = _populate(tmp_path / ".amplifier")
    report = reset.run_reset(home, set(reset.DEFAULT_CATEGORIES), dry_run=False)
    # Removed: cache + registry only.
    assert not (home / "cache").exists()
    assert not (home / "registry.json").exists()
    # Preserved: sessions, config, bundles, keys.
    assert (home / "projects" / "slug" / "sessions" / "sess-1" / "transcript.jsonl").exists()
    assert (home / "settings.yaml").exists()
    assert (home / "keys.env").exists()
    assert (home / "bundles" / "b.md").exists()
    assert "keys" in report.keep and "sessions" in report.keep


def test_each_category_clears_only_its_own_files(tmp_path: Path) -> None:
    home = _populate(tmp_path / ".amplifier")
    reset.run_reset(home, {"sessions"}, dry_run=False)
    # sessions gone...
    assert not (home / "projects").exists()
    # ...everything else intact.
    assert (home / "cache").exists()
    assert (home / "registry.json").exists()
    assert (home / "settings.yaml").exists()
    assert (home / "keys.env").exists()
    assert (home / "bundles").exists()


def test_keys_never_cleared_unless_named(tmp_path: Path) -> None:
    home = _populate(tmp_path / ".amplifier")
    # A broad clear of everything EXCEPT keys leaves the secret file intact.
    everything_but_keys = set(reset.CATEGORIES) - {"keys"}
    report = reset.run_reset(home, everything_but_keys, dry_run=False)
    assert (home / "keys.env").exists()
    assert not report.secret_cleared
    # Only when explicitly named does it go.
    report2 = reset.run_reset(home, {"keys"}, dry_run=False)
    assert not (home / "keys.env").exists()
    assert report2.secret_cleared == ("keys",)


def test_config_clears_settings_mcp_and_routing(tmp_path: Path) -> None:
    home = _populate(tmp_path / ".amplifier")
    reset.run_reset(home, {"config"}, dry_run=False)
    assert not (home / "settings.yaml").exists()
    assert not (home / "settings.local.yaml").exists()
    assert not (home / "mcp.json").exists()
    assert not (home / "routing").exists()
    # keys.env is NOT config — it stays.
    assert (home / "keys.env").exists()


def test_preserved_vs_removed_summary_is_correct(tmp_path: Path) -> None:
    home = _populate(tmp_path / ".amplifier")
    report = reset.run_reset(home, {"cache"}, dry_run=True)
    assert report.removed == [home / "cache"]
    preserved_names = {p.name for p in report.preserved}
    assert "registry.json" in preserved_names
    assert "keys.env" in preserved_names
    assert "projects" in preserved_names


def test_destructive_flag_distinguishes_autoregen(tmp_path: Path) -> None:
    home = _populate(tmp_path / ".amplifier")
    safe = reset.run_reset(home, {"cache", "registry"}, dry_run=True)
    assert safe.destructive_cleared == ()
    risky = reset.run_reset(home, {"sessions", "cache"}, dry_run=True)
    assert risky.destructive_cleared == ("sessions",)


def test_run_reset_refuses_unconfirmable_home(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("AMPLIFIER_HOME", raising=False)
    bare = tmp_path / "not-a-home"
    bare.mkdir()  # deep path, but no marker file and not named .amplifier
    with pytest.raises(reset.ResetError):
        reset.run_reset(bare, {"cache"}, dry_run=True)


def test_run_reset_removes_symlinked_entry_without_following(tmp_path: Path) -> None:
    # A symlink category entry is unlinked, not traversed into the target.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep me\n", encoding="utf-8")
    home = tmp_path / ".amplifier"
    home.mkdir()
    (home / "settings.yaml").write_text("x: 1\n", encoding="utf-8")  # marker
    (home / "registry.json").symlink_to(outside / "keep.txt")
    reset.run_reset(home, {"registry"}, dry_run=False)
    assert not (home / "registry.json").exists()
    # The symlink target outside the home is untouched.
    assert (outside / "keep.txt").exists()
