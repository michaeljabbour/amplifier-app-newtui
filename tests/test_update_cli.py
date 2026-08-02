"""`amplifier-tui update` — pure helpers + CLI wiring.

The foundation-backed check/apply (check_bundles/update_bundles) hit the
network/cache, so the CLI tests stub them; the pure helpers are tested
directly.
"""

from __future__ import annotations

from click.testing import CliRunner

from amplifier_app_tui.kernel import updater
from amplifier_app_tui.main import main


# -- pure helpers -----------------------------------------------------------


def test_display_name_variants() -> None:
    assert updater.display_name("tui") == "tui"
    assert (
        updater.display_name("git+https://github.com/microsoft/amplifier-bundle-skills@main")
        == "amplifier-bundle-skills"
    )
    assert (
        updater.display_name("git+https://x/repo@main#subdirectory=behaviors/team-pulse.yaml")
        == "behaviors/team-pulse.yaml"
    )


def test_target_bundles_active_plus_overlays_deduped() -> None:
    settings = {"bundle": {"active": "tui", "app": ["git+u/a", "git+u/a", "git+u/b"]}}
    assert updater.target_bundles(settings) == ["tui", "git+u/a", "git+u/b"]


def test_target_bundles_defaults_to_packaged() -> None:
    assert updater.target_bundles({})[0] == "tui"


def test_self_update_hint_mentions_uv() -> None:
    hint = updater.self_update_hint()
    assert "uv sync" in hint and "uv tool upgrade amplifier" in hint


# -- uncheckable_sources: deduplicated, plainly labeled (pure) ---------------


def test_uncheckable_sources_dedupes_shared_module() -> None:
    """A module used by several bundles collapses to one entry."""
    generic = "Update checking not supported for this source type"
    statuses = [
        updater.BundleUpdate(
            "tui",
            "tui",
            "",
            False,
            sources=(
                updater.SourceRow("tool-apply-patch", has_update=None, reason=generic),
                updater.SourceRow("tool-bash", "aaaaaaa", "aaaaaaa", has_update=False),
            ),
        ),
        updater.BundleUpdate(
            "skills",
            "git+u/skills",
            "",
            False,
            sources=(updater.SourceRow("tool-apply-patch", has_update=None, reason=generic),),
        ),
    ]
    result = updater.uncheckable_sources(statuses)
    # tool-apply-patch once, tool-bash (checkable) excluded.
    assert result == [("tool-apply-patch", "")]


def test_uncheckable_sources_keeps_real_errors_but_drops_generic() -> None:
    statuses = [
        updater.BundleUpdate(
            "tui",
            "tui",
            "",
            False,
            sources=(
                updater.SourceRow("tool-a", has_update=None, reason="ls-remote failed: timeout"),
                updater.SourceRow(
                    "tool-b",
                    has_update=None,
                    reason="Update checking not supported for this source type",
                ),
            ),
        ),
    ]
    assert updater.uncheckable_sources(statuses) == [
        ("tool-a", "ls-remote failed: timeout"),
        ("tool-b", ""),
    ]


def test_uncheckable_sources_falls_back_to_legacy_unknown() -> None:
    """Stubs that only set the legacy ``unknown`` tuple still render."""
    statuses = [
        updater.BundleUpdate(
            "tui",
            "tui",
            "",
            False,
            unknown=("tool-local: ls-remote failed", "tool-local: ls-remote failed"),
        ),
    ]
    assert updater.uncheckable_sources(statuses) == [("tool-local", "ls-remote failed")]


# -- CLI wiring (stubbed foundation) ----------------------------------------


def _stub(monkeypatch, statuses, *, cleaned=None, applied=None, anchors=None, refreshed=None):
    async def _check(*a, **k):
        return statuses

    async def _apply(targets):
        if applied is not None:
            applied.extend(targets)
        return ([updater.display_name(t) for t in targets], [])

    async def _anchors(*a, **k):
        # Default: offline/neutral so CLI tests never touch the network.
        return anchors or updater.AnchorsStatus(ref="main", error="offline (test stub)")

    async def _refresh(*a, **k):
        if refreshed is not None:
            refreshed.append(True)
        return True

    monkeypatch.setattr(updater, "check_bundles", _check)
    monkeypatch.setattr(updater, "update_bundles", _apply)
    monkeypatch.setattr(updater, "anchors_status", _anchors)
    monkeypatch.setattr(updater, "refresh_anchors", _refresh)
    monkeypatch.setattr(
        updater, "uv_cache_clean", lambda: cleaned.append(True) if cleaned is not None else True
    )


def test_update_all_up_to_date(monkeypatch) -> None:
    _stub(monkeypatch, [updater.BundleUpdate("tui", "tui", "up to date", False)])
    result = CliRunner().invoke(main, ["update"])
    assert result.exit_code == 0
    assert "up to date" in result.output


def test_update_check_only_does_not_apply(monkeypatch) -> None:
    applied: list = []
    _stub(
        monkeypatch,
        [updater.BundleUpdate("tui", "tui", "1 update available", True)],
        applied=applied,
    )
    result = CliRunner().invoke(main, ["update", "--check-only"])
    assert result.exit_code == 0
    assert applied == []  # nothing applied in check-only


def test_update_applies_stale_with_yes(monkeypatch) -> None:
    applied: list = []
    _stub(
        monkeypatch,
        [
            updater.BundleUpdate("tui", "tui", "1 update available", True),
            updater.BundleUpdate("skills", "git+u/skills", "up to date", False),
        ],
        applied=applied,
    )
    result = CliRunner().invoke(main, ["update", "-y"])
    assert result.exit_code == 0
    assert applied == ["tui"]  # only the stale one
    assert "updated: tui" in result.output


def test_update_force_cleans_cache_and_updates_all(monkeypatch) -> None:
    cleaned: list = []
    applied: list = []
    _stub(
        monkeypatch,
        [updater.BundleUpdate("tui", "tui", "up to date", False)],
        cleaned=cleaned,
        applied=applied,
    )
    result = CliRunner().invoke(main, ["update", "--force", "-y"])
    assert result.exit_code == 0
    assert cleaned == [True]  # uv cache cleaned
    assert applied == ["tui"]  # --force updates all, not just stale


# -- SHA-diff table + deduplicated uncheckable section -----------------------


def test_update_renders_sha_table(monkeypatch) -> None:
    _stub(
        monkeypatch,
        [
            updater.BundleUpdate(
                "tui",
                "tui",
                "1 update available",
                True,
                sources=(
                    updater.SourceRow("tool-bash", "aaaaaaa1", "aaaaaaa1", has_update=False),
                    updater.SourceRow("tool-todo", "bbbbbbb2", "ccccccc3", has_update=True),
                ),
            )
        ],
    )
    result = CliRunner().invoke(main, ["update", "--check-only"])
    assert result.exit_code == 0
    # Table header + legend + per-source SHAs.
    assert "Local" in result.output and "Remote" in result.output
    assert "Legend" in result.output and "update available" in result.output
    assert "tool-bash" in result.output and "tool-todo" in result.output
    # Truncated SHAs appear (7 chars).
    assert "ccccccc" in result.output


def test_update_table_dedupes_shared_sources_across_bundles(monkeypatch) -> None:
    """A source shared by many composed bundles renders ONCE, not once per
    bundle (the flat, app-cli-style view — regression for the repeated-content
    complaint)."""
    shared = updater.SourceRow("amplifier-foundation", "af7b19b", "32d4052", has_update=True)
    _stub(
        monkeypatch,
        [
            updater.BundleUpdate(
                "memory",
                "git+u/memory",
                "",
                True,
                sources=(
                    shared,
                    updater.SourceRow(
                        "amplifier-module-tool-memory", "111", "111", has_update=False
                    ),
                ),
            ),
            updater.BundleUpdate(
                "attractor",
                "git+u/attractor",
                "",
                True,
                sources=(
                    shared,
                    updater.SourceRow("amplifier-module-tool-bash", "222", "222", has_update=False),
                ),
            ),
        ],
    )
    result = CliRunner().invoke(main, ["update", "--check-only"])
    assert result.exit_code == 0
    # foundation is in BOTH bundles but must appear exactly once in the table.
    assert result.output.count("amplifier-foundation") == 1
    # Split: modules under a Modules table, foundation under Bundles.
    assert "Bundles" in result.output and "Modules" in result.output
    assert "amplifier-module-tool-memory" in result.output


def test_unique_sources_collapses_and_splits() -> None:
    """Pure: dedup by (name, cached, remote); distinct versions kept."""
    a = updater.SourceRow("amplifier-foundation", "af7b19b", "32d4052", has_update=True)
    b = updater.SourceRow("amplifier-module-tool-bash", "111", "111", has_update=False)
    local = updater.SourceRow("tool-apply-patch", has_update=None, reason="local")
    s1 = updater.BundleUpdate("x", "x", "", True, sources=(a, b, local))
    s2 = updater.BundleUpdate("y", "y", "", True, sources=(a, b))  # exact repeats
    rows = updater.unique_sources([s1, s2])
    names = [r.name for r in rows]
    assert names.count("amplifier-foundation") == 1
    assert names.count("amplifier-module-tool-bash") == 1
    assert "tool-apply-patch" not in names  # local/non-git excluded


def test_update_dedupes_uncheckable_sources_with_plain_label(monkeypatch) -> None:
    generic = "Update checking not supported for this source type"
    _stub(
        monkeypatch,
        [
            updater.BundleUpdate(
                "tui",
                "tui",
                "up to date",
                False,
                sources=(updater.SourceRow("tool-apply-patch", has_update=None, reason=generic),),
            ),
            updater.BundleUpdate(
                "skills",
                "git+u/skills",
                "up to date",
                False,
                sources=(updater.SourceRow("tool-apply-patch", has_update=None, reason=generic),),
            ),
        ],
    )
    result = CliRunner().invoke(main, ["update", "--check-only"])
    assert result.exit_code == 0
    # Plainer label, not foundation's opaque per-source message.
    assert "local or non-git sources (no remote to compare)" in result.output
    assert "not supported for this source type" not in result.output
    # Deduplicated: the shared module appears exactly once.
    assert result.output.count("tool-apply-patch") == 1


# -- anchors freshness line -------------------------------------------------


def test_update_reports_anchors_behind(monkeypatch) -> None:
    behind = updater.AnchorsStatus(
        ref="main",
        has_update=True,
        cached_commit="aaaaaaaa1111",
        remote_commit="bbbbbbbb2222",
    )
    _stub(
        monkeypatch,
        [updater.BundleUpdate("tui", "tui", "up to date", False)],
        anchors=behind,
    )
    result = CliRunner().invoke(main, ["update", "--check-only"])
    assert result.exit_code == 0
    assert "anchors" in result.output and "behind upstream" in result.output
    # Must not falsely claim everything is up to date when anchors is behind.
    assert "all bundles up to date" not in result.output


def test_update_reports_anchors_current(monkeypatch) -> None:
    current = updater.AnchorsStatus(ref="main", has_update=False, cached_commit="cccccccc3333")
    _stub(
        monkeypatch,
        [updater.BundleUpdate("tui", "tui", "up to date", False)],
        anchors=current,
    )
    result = CliRunner().invoke(main, ["update", "--check-only"])
    assert result.exit_code == 0
    assert "anchors up to date" in result.output


def test_update_applies_anchors_refresh_when_stale(monkeypatch) -> None:
    """A stale anchors cache is applicable work: update refreshes it and
    reports it — the "run `amplifier-tui update`" hint is no longer circular."""
    refreshed: list = []
    applied: list = []
    behind = updater.AnchorsStatus(
        ref="main", has_update=True, cached_commit="aaaaaaaa1111", remote_commit="bbbbbbbb2222"
    )
    _stub(
        monkeypatch,
        [updater.BundleUpdate("tui", "tui", "up to date", False)],
        anchors=behind,
        applied=applied,
        refreshed=refreshed,
    )
    result = CliRunner().invoke(main, ["update", "-y"])
    assert result.exit_code == 0, result.output
    assert refreshed == [True]
    assert applied == []  # no stale bundles — only anchors needed work
    assert "updated: anchors" in result.output


def test_update_check_only_never_refreshes_anchors(monkeypatch) -> None:
    refreshed: list = []
    behind = updater.AnchorsStatus(
        ref="main", has_update=True, cached_commit="aaaaaaaa1111", remote_commit="bbbbbbbb2222"
    )
    _stub(
        monkeypatch,
        [updater.BundleUpdate("tui", "tui", "up to date", False)],
        anchors=behind,
        refreshed=refreshed,
    )
    result = CliRunner().invoke(main, ["update", "--check-only"])
    assert result.exit_code == 0
    assert refreshed == []


def test_update_reports_anchors_refresh_failure(monkeypatch) -> None:
    behind = updater.AnchorsStatus(
        ref="main", has_update=True, cached_commit="aaaaaaaa1111", remote_commit="bbbbbbbb2222"
    )
    _stub(
        monkeypatch,
        [updater.BundleUpdate("tui", "tui", "up to date", False)],
        anchors=behind,
    )

    async def _refresh_fail(*a, **k):
        return False

    monkeypatch.setattr(updater, "refresh_anchors", _refresh_fail)
    result = CliRunner().invoke(main, ["update", "-y"])
    assert result.exit_code == 1
    assert "failed: anchors" in result.output


# -- check errors are rendered, never silently swallowed ---------------------


def test_update_renders_bundle_error(monkeypatch) -> None:
    """A bundle whose check errored (e.g. unresolvable bare name on a fresh
    machine) must be visible — the old behavior printed "all bundles up to
    date" while the check had totally failed."""
    _stub(
        monkeypatch,
        [
            updater.BundleUpdate(
                "tui",
                "tui",
                "check failed: Could not resolve URI: tui",
                False,
                error="Could not resolve URI: tui",
            )
        ],
    )
    result = CliRunner().invoke(main, ["update"])
    assert result.exit_code == 1
    assert "Could not resolve URI: tui" in result.output
    assert "could not be checked" in result.output
    assert "all bundles up to date" not in result.output


# -- fresh-machine name resolution ------------------------------------------


def test_load_single_resolves_bare_name_to_packaged_bundle(monkeypatch, tmp_path) -> None:
    """Bare names ("tui") resolve via the app's bundle search paths — not
    foundation's persisted registry, which is empty on a fresh machine."""
    import asyncio

    import amplifier_foundation

    monkeypatch.setenv("AMPLIFIER_HOME", str(tmp_path))
    captured: list = []

    async def _fake_load(target, *a, **k):
        captured.append(target)
        return None

    monkeypatch.setattr(amplifier_foundation, "load_bundle", _fake_load)
    asyncio.run(updater._load_single("tui"))
    assert len(captured) == 1
    assert str(captured[0]).endswith("data/bundles/tui.md")


def test_target_bundles_includes_routing_when_enabled() -> None:
    from amplifier_app_tui.kernel.config import ROUTING_MATRIX_BUNDLE_URI

    targets = updater.target_bundles({"routing": {"enabled": True}})
    assert targets[0] == "tui"
    assert ROUTING_MATRIX_BUNDLE_URI in targets
