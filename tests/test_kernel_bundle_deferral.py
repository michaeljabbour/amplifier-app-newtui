"""Fast-boot deferral split (``kernel/config.py``): boot vs deferred overlays.

Pure config logic — no foundation, no session. Covers the ``bundle.deferred``
opt-out: which overlays compose at boot, which are held back for on-demand
``/bundle load``, name/URI resolution, and the "never a silent drop" notice.
Backward compatibility is the load-bearing invariant: with no deferral
configured, boot composes exactly what it did before.
"""

from __future__ import annotations

from amplifier_app_tui.kernel.config import (
    boot_overlay_uris,
    composed_overlay_uris,
    deferred_bundle_entries,
    deferred_overlays_notice,
    deferred_overlay_uris,
    overlay_uris,
    resolve_deferred_bundle,
)

A = "git+https://x/a@main"
B = "git+https://x/b@main"
C = "git+https://x/c@main"


def _settings(app: list[str], deferred: list[str], added: dict[str, str] | None = None) -> dict:
    bundle: dict = {"app": app, "deferred": deferred}
    if added is not None:
        bundle["added"] = added
    return {"bundle": bundle}


# -- backward compatibility: no deferral => identical behavior ----------------


def test_no_deferral_boots_every_overlay() -> None:
    settings = {"bundle": {"app": [A, B]}}
    assert deferred_bundle_entries(settings) == ()
    assert deferred_overlay_uris(settings) == ()
    assert boot_overlay_uris(settings) == (A, B)
    # composed_overlay_uris (what boot composes) is unchanged from overlay_uris.
    assert composed_overlay_uris(settings) == overlay_uris(settings) == (A, B)
    assert deferred_overlays_notice(()) is None


def test_empty_settings_are_inert() -> None:
    assert boot_overlay_uris({}) == ()
    assert deferred_overlay_uris({}) == ()
    assert deferred_bundle_entries({}) == ()
    assert resolve_deferred_bundle("anything", {}) is None


# -- the split: deferred overlays are held back from boot ---------------------


def test_deferred_by_uri_is_held_back() -> None:
    settings = _settings([A, B, C], deferred=[B])
    assert boot_overlay_uris(settings) == (A, C)
    assert deferred_overlay_uris(settings) == (B,)
    # Order follows bundle.app, not the deferred list.
    settings2 = _settings([A, B, C], deferred=[C, A])
    assert deferred_overlay_uris(settings2) == (A, C)
    assert boot_overlay_uris(settings2) == (B,)


def test_deferred_by_registered_name_is_held_back() -> None:
    # A bundle.deferred entry may be a bundle.added NAME, not just a URI.
    settings = _settings([A, B], deferred=["heavy"], added={"heavy": B})
    assert deferred_overlay_uris(settings) == (B,)
    assert boot_overlay_uris(settings) == (A,)


def test_deferring_a_non_overlay_is_a_harmless_noop() -> None:
    # Deferring something never listed in bundle.app drops nothing.
    settings = _settings([A], deferred=[C])
    assert deferred_overlay_uris(settings) == ()
    assert boot_overlay_uris(settings) == (A,)


def test_composed_overlay_uris_excludes_deferred_and_keeps_routing() -> None:
    settings = _settings([A, B], deferred=[B])
    settings["routing"] = {"matrix": "anthropic"}
    composed = composed_overlay_uris(settings)
    assert B not in composed  # deferred, held back
    assert composed[0] == A  # boot overlay first
    assert any("routing-matrix" in uri for uri in composed)  # routing still appended


def test_junk_deferred_shape_is_ignored() -> None:
    assert deferred_bundle_entries({"bundle": {"deferred": "notalist"}}) == ()
    assert boot_overlay_uris({"bundle": {"app": [A], "deferred": {}}}) == (A,)


# -- resolve_deferred_bundle: the /bundle load argument resolver --------------


def test_resolve_deferred_by_uri_and_name() -> None:
    settings = _settings([A, B], deferred=["heavy"], added={"heavy": B})
    assert resolve_deferred_bundle("heavy", settings) == B  # by name
    assert resolve_deferred_bundle(B, settings) == B  # by URI
    assert resolve_deferred_bundle("  heavy  ", settings) == B  # trimmed


def test_resolve_refuses_non_deferred_bundle() -> None:
    # A is composed at boot, not deferred — nothing to load on demand.
    settings = _settings([A, B], deferred=[B], added={"heavy": B})
    assert resolve_deferred_bundle(A, settings) is None
    assert resolve_deferred_bundle("unknown", settings) is None
    assert resolve_deferred_bundle("", settings) is None


# -- the notice: deferral is opt-in but never silent -------------------------


def test_deferred_notice_names_the_count_and_command() -> None:
    notice = deferred_overlays_notice((A,))
    assert notice is not None
    assert "1 overlay" in notice and "/bundle load" in notice
    plural = deferred_overlays_notice((A, B))
    assert plural is not None and "2 overlays" in plural
