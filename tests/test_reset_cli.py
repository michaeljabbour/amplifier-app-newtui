"""``amplifier-newtui reset`` CLI wiring (click CliRunner).

The path/data logic is unit-tested in ``test_kernel_reset``; this covers the
command plumbing: the taxonomy listing, the dry-run/confirm/--yes guard flow,
the secrets-only-when-named rule, and the outside-the-home refusal. Every
invocation targets a scratch home via ``--home`` — never the real ~/.amplifier.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from amplifier_app_newtui.main import main


def _populate(home: Path) -> Path:
    home.mkdir(parents=True, exist_ok=True)
    (home / "settings.yaml").write_text("bundle: {}\n", encoding="utf-8")
    (home / "keys.env").write_text("ANTHROPIC_API_KEY=secret\n", encoding="utf-8")
    (home / "registry.json").write_text("{}\n", encoding="utf-8")
    cache = home / "cache" / "bundle-abc"
    cache.mkdir(parents=True)
    (cache / "blob.txt").write_text("cached\n", encoding="utf-8")
    sessions = home / "projects" / "slug" / "sessions" / "sess-1"
    sessions.mkdir(parents=True)
    (sessions / "transcript.jsonl").write_text("{}\n", encoding="utf-8")
    return home


def test_reset_help_lists_guard_flags() -> None:
    result = CliRunner().invoke(main, ["reset", "--help"])
    assert result.exit_code == 0
    for flag in ("--category", "--dry-run", "--yes", "--list"):
        assert flag in result.output


def test_reset_list_shows_taxonomy_and_tags() -> None:
    result = CliRunner().invoke(main, ["reset", "--list"])
    assert result.exit_code == 0
    for name in ("cache", "registry", "sessions", "config", "bundles", "keys"):
        assert name in result.output
    assert "default" in result.output  # cache/registry marked default
    assert "secret" in result.output  # keys marked secret


def test_reset_unknown_category_errors_nonzero(tmp_path: Path) -> None:
    home = _populate(tmp_path / ".amplifier")
    result = CliRunner().invoke(main, ["reset", "--home", str(home), "-c", "bogus"])
    assert result.exit_code == 2
    assert "unknown category" in result.output
    # Nothing touched.
    assert (home / "cache").exists()


def test_reset_dry_run_removes_nothing(tmp_path: Path) -> None:
    home = _populate(tmp_path / ".amplifier")
    result = CliRunner().invoke(
        main, ["reset", "--home", str(home), "-c", "cache,registry,sessions", "--dry-run"]
    )
    assert result.exit_code == 0
    assert "DRY RUN" in result.output
    assert "would remove:" in result.output
    # Every file still present.
    assert (home / "cache").exists()
    assert (home / "registry.json").exists()
    assert (home / "projects").exists()


def test_reset_requires_confirmation_and_cancels(tmp_path: Path) -> None:
    home = _populate(tmp_path / ".amplifier")
    result = CliRunner().invoke(main, ["reset", "--home", str(home), "-c", "sessions"], input="n\n")
    assert result.exit_code == 0
    assert "cancelled" in result.output
    # Declining leaves sessions intact.
    assert (home / "projects").exists()


def test_reset_confirmation_yes_executes(tmp_path: Path) -> None:
    home = _populate(tmp_path / ".amplifier")
    result = CliRunner().invoke(main, ["reset", "--home", str(home), "-c", "sessions"], input="y\n")
    assert result.exit_code == 0
    assert not (home / "projects").exists()
    # Preserved summary mentions kept files.
    assert "preserved" in result.output


def test_reset_yes_flag_clears_only_named_category(tmp_path: Path) -> None:
    home = _populate(tmp_path / ".amplifier")
    result = CliRunner().invoke(main, ["reset", "--home", str(home), "-c", "cache", "--yes"])
    assert result.exit_code == 0
    # Only cache gone; secrets, sessions, registry preserved.
    assert not (home / "cache").exists()
    assert (home / "keys.env").exists()
    assert (home / "projects").exists()
    assert (home / "registry.json").exists()


def test_reset_default_preserves_secrets_and_sessions(tmp_path: Path) -> None:
    home = _populate(tmp_path / ".amplifier")
    result = CliRunner().invoke(main, ["reset", "--home", str(home), "--yes"])
    assert result.exit_code == 0
    # Default = cache + registry only.
    assert not (home / "cache").exists()
    assert not (home / "registry.json").exists()
    assert (home / "keys.env").exists()
    assert (home / "projects").exists()


def test_reset_keys_only_cleared_when_named_with_warning(tmp_path: Path) -> None:
    home = _populate(tmp_path / ".amplifier")
    result = CliRunner().invoke(main, ["reset", "--home", str(home), "-c", "keys", "--yes"])
    assert result.exit_code == 0
    assert "WARNING" in result.output and "secrets" in result.output
    assert not (home / "keys.env").exists()


def test_reset_refuses_outside_app_home(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("AMPLIFIER_HOME", raising=False)
    bare = tmp_path / "not-a-home"
    bare.mkdir()
    result = CliRunner().invoke(main, ["reset", "--home", str(bare), "-c", "cache", "--yes"])
    assert result.exit_code == 2
    assert "refusing to reset" in result.output


def test_reset_reports_when_nothing_to_remove(tmp_path: Path) -> None:
    home = tmp_path / ".amplifier"
    home.mkdir()
    (home / "settings.yaml").write_text("x: 1\n", encoding="utf-8")  # marker, but not cache
    result = CliRunner().invoke(main, ["reset", "--home", str(home), "-c", "cache", "--yes"])
    assert result.exit_code == 0
    assert "nothing to remove" in result.output
