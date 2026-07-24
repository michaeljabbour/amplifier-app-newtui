"""`amplifier-newtui update` — pure helpers + CLI wiring.

The foundation-backed check/apply (check_bundles/update_bundles) hit the
network/cache, so the CLI tests stub them; the pure helpers are tested
directly.
"""

from __future__ import annotations

from click.testing import CliRunner

from amplifier_app_newtui.kernel import updater
from amplifier_app_newtui.main import main


# -- pure helpers -----------------------------------------------------------


def test_display_name_variants() -> None:
    assert updater.display_name("newtui") == "newtui"
    assert (
        updater.display_name("git+https://github.com/microsoft/amplifier-bundle-skills@main")
        == "amplifier-bundle-skills"
    )
    assert (
        updater.display_name("git+https://x/repo@main#subdirectory=behaviors/team-pulse.yaml")
        == "behaviors/team-pulse.yaml"
    )


def test_target_bundles_active_plus_overlays_deduped() -> None:
    settings = {"bundle": {"active": "newtui", "app": ["git+u/a", "git+u/a", "git+u/b"]}}
    assert updater.target_bundles(settings) == ["newtui", "git+u/a", "git+u/b"]


def test_target_bundles_defaults_to_packaged() -> None:
    assert updater.target_bundles({})[0] == "newtui"


def test_self_update_hint_mentions_uv() -> None:
    hint = updater.self_update_hint()
    assert "uv sync" in hint and "uv tool upgrade amplifier" in hint


# -- CLI wiring (stubbed foundation) ----------------------------------------


def _stub(monkeypatch, statuses, *, cleaned=None, applied=None, anchors=None):
    async def _check(*a, **k):
        return statuses

    async def _apply(targets):
        if applied is not None:
            applied.extend(targets)
        return ([updater.display_name(t) for t in targets], [])

    async def _anchors(*a, **k):
        # Default: offline/neutral so CLI tests never touch the network.
        return anchors or updater.AnchorsStatus(ref="main", error="offline (test stub)")

    monkeypatch.setattr(updater, "check_bundles", _check)
    monkeypatch.setattr(updater, "update_bundles", _apply)
    monkeypatch.setattr(updater, "anchors_status", _anchors)
    monkeypatch.setattr(
        updater, "uv_cache_clean", lambda: cleaned.append(True) if cleaned is not None else True
    )


def test_update_all_up_to_date(monkeypatch) -> None:
    _stub(monkeypatch, [updater.BundleUpdate("newtui", "newtui", "up to date", False)])
    result = CliRunner().invoke(main, ["update"])
    assert result.exit_code == 0
    assert "up to date" in result.output


def test_update_check_only_does_not_apply(monkeypatch) -> None:
    applied: list = []
    _stub(
        monkeypatch,
        [updater.BundleUpdate("newtui", "newtui", "1 update available", True)],
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
            updater.BundleUpdate("newtui", "newtui", "1 update available", True),
            updater.BundleUpdate("skills", "git+u/skills", "up to date", False),
        ],
        applied=applied,
    )
    result = CliRunner().invoke(main, ["update", "-y"])
    assert result.exit_code == 0
    assert applied == ["newtui"]  # only the stale one
    assert "updated: newtui" in result.output


def test_update_force_cleans_cache_and_updates_all(monkeypatch) -> None:
    cleaned: list = []
    applied: list = []
    _stub(
        monkeypatch,
        [updater.BundleUpdate("newtui", "newtui", "up to date", False)],
        cleaned=cleaned,
        applied=applied,
    )
    result = CliRunner().invoke(main, ["update", "--force", "-y"])
    assert result.exit_code == 0
    assert cleaned == [True]  # uv cache cleaned
    assert applied == ["newtui"]  # --force updates all, not just stale


# -- "unknown" sources are explained, not swallowed -------------------------


def test_update_explains_unknown_sources(monkeypatch) -> None:
    _stub(
        monkeypatch,
        [
            updater.BundleUpdate(
                "newtui",
                "newtui",
                "Up to date (1 source(s) could not be checked)",
                False,
                unknown=("tool-local: Update checking not supported for this source type",),
            )
        ],
    )
    result = CliRunner().invoke(main, ["update", "--check-only"])
    assert result.exit_code == 0
    assert "couldn't be checked" in result.output
    assert "tool-local" in result.output
    assert "not supported for this source type" in result.output


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
        [updater.BundleUpdate("newtui", "newtui", "up to date", False)],
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
        [updater.BundleUpdate("newtui", "newtui", "up to date", False)],
        anchors=current,
    )
    result = CliRunner().invoke(main, ["update", "--check-only"])
    assert result.exit_code == 0
    assert "anchors up to date" in result.output
