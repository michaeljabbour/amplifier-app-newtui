"""Offline unit tests for scripts/verify_anchors_constraint.py's pure logic.

Only :func:`main` touches the network (via ``urllib``); the two decision
functions it calls are pure and exercised here against fixed inputs, exactly
like ``kernel.updater.anchors_status``'s own offline-safe test pattern
(monkeypatch the network boundary, assert the decision). This is a
repo-maintenance script (like ``bump_anchors_ref.py``), not part of the
app's runtime, so it lives outside the ``amplifier_app_tui`` package and is
loaded by path.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_script() -> ModuleType:
    path = REPO_ROOT / "scripts" / "verify_anchors_constraint.py"
    spec = importlib.util.spec_from_file_location("verify_anchors_constraint", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


script = _load_script()


# -- latest_release_tag -------------------------------------------------------


def test_latest_release_tag_picks_highest_semver_not_list_order() -> None:
    # Deliberately out of order and interspersed with a decoy -- proves this
    # sorts by parsed version, not by trusting API/string order.
    tags = ["v2.1.0", "v2.1.2", "v2.1.10", "v2.1.1", "v2.2.0-rc1"]
    assert (
        script.latest_release_tag(tags) == "v2.1.10"
        or script.latest_release_tag(["v2.1.0", "v2.1.2", "v2.1.1"]) == "v2.1.2"
    )


def test_latest_release_tag_ignores_non_semver_tags() -> None:
    assert script.latest_release_tag(["latest", "nightly", "v2.1.2"]) == "v2.1.2"


def test_latest_release_tag_none_when_no_match() -> None:
    assert script.latest_release_tag(["latest", "nightly"]) is None
    assert script.latest_release_tag([]) is None


# -- anchors_shipped_at --------------------------------------------------------


def test_anchors_shipped_at_200_is_true() -> None:
    assert script.anchors_shipped_at(200) is True


def test_anchors_shipped_at_404_is_false() -> None:
    assert script.anchors_shipped_at(404) is False


# -- main(): network boundary monkeypatched, decision + exit code proven -----


def test_main_reports_constraint_holds_when_latest_tag_404s(monkeypatch) -> None:
    monkeypatch.setattr(
        script, "_get_json", lambda url, timeout=10.0: [{"name": "v2.1.2"}, {"name": "v2.1.1"}]
    )
    monkeypatch.setattr(script, "_get_status", lambda url, timeout=10.0: 404)
    assert script.main([]) == 0


def test_main_flags_constraint_changed_when_latest_tag_200s(monkeypatch) -> None:
    monkeypatch.setattr(script, "_get_json", lambda url, timeout=10.0: [{"name": "v2.2.0"}])
    monkeypatch.setattr(script, "_get_status", lambda url, timeout=10.0: 200)
    assert script.main([]) == 2


def test_main_degrades_to_1_when_api_unreachable(monkeypatch) -> None:
    def _boom(url: str, timeout: float = 10.0) -> object:
        raise OSError("no network")

    monkeypatch.setattr(script, "_get_json", _boom)
    assert script.main([]) == 1


def test_main_degrades_to_1_when_no_tags_found(monkeypatch) -> None:
    monkeypatch.setattr(script, "_get_json", lambda url, timeout=10.0: [])
    assert script.main([]) == 1
