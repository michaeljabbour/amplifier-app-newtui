"""Tests for the active native-mode set + tool-policy precedence rules."""

from __future__ import annotations

from amplifier_app_newtui.model.native_modes import (
    ActiveNativeModes,
    native_badge_text,
    posture_conflict_notice,
    posture_restricts_tools,
)


# -- ActiveNativeModes: ordered set semantics ---------------------------------


def test_empty_set_is_falsy_with_no_primary() -> None:
    modes = ActiveNativeModes()
    assert not modes
    assert len(modes) == 0
    assert modes.primary is None
    assert "team-pulse" not in modes


def test_add_appends_and_last_is_primary() -> None:
    modes = ActiveNativeModes().add("team-pulse").add("audit")
    assert modes.names == ("team-pulse", "audit")
    assert modes.primary == "audit"  # most-recently added is the enforced slot
    assert len(modes) == 2
    assert "team-pulse" in modes and "audit" in modes


def test_re_adding_promotes_to_primary_without_duplicating() -> None:
    modes = ActiveNativeModes().add("team-pulse").add("audit").add("team-pulse")
    assert modes.names == ("audit", "team-pulse")  # promoted, not duplicated
    assert modes.primary == "team-pulse"


def test_add_blank_is_noop() -> None:
    modes = ActiveNativeModes().add("audit")
    assert modes.add("   ") is modes
    assert modes.add("audit").names == ("audit",)  # idempotent re-add of only mode


def test_remove_promotes_next_and_missing_is_noop() -> None:
    modes = ActiveNativeModes().add("team-pulse").add("audit")
    after = modes.remove("audit")
    assert after.names == ("team-pulse",)
    assert after.primary == "team-pulse"  # next-newest promoted into the slot
    assert modes.remove("nope").names == modes.names  # unknown → unchanged


def test_clear_empties_the_stack() -> None:
    assert ActiveNativeModes().add("a").add("b").clear().names == ()


def test_is_frozen_value_type() -> None:
    modes = ActiveNativeModes().add("a")
    other = modes.add("b")
    assert modes.names == ("a",)  # original untouched (immutable)
    assert other.names == ("a", "b")


def test_iterates_in_activation_order() -> None:
    modes = ActiveNativeModes().add("a").add("b")
    assert list(modes) == ["a", "b"]


# -- footer badge rendering ---------------------------------------------------


def test_badge_empty_when_no_modes() -> None:
    assert native_badge_text(ActiveNativeModes()) == ""
    assert native_badge_text(()) == ""


def test_badge_single_mode_matches_legacy() -> None:
    assert native_badge_text(("team-pulse",)) == "◆ team-pulse"
    assert native_badge_text(ActiveNativeModes().add("team-pulse")) == "◆ team-pulse"


def test_badge_stacked_marks_primary_first() -> None:
    modes = ActiveNativeModes().add("team-pulse").add("audit")
    # audit is primary (◆), team-pulse stacked behind it (+)
    assert native_badge_text(modes) == "◆ audit +team-pulse"


def test_badge_accepts_plain_tuple() -> None:
    assert native_badge_text(("team-pulse", "audit", "careful")) == ("◆ careful +audit +team-pulse")


# -- tool-policy precedence: restrictive postures + conflict notice -----------


def test_only_deny_postures_restrict_tools() -> None:
    assert posture_restricts_tools("brainstorm")  # no tools
    assert posture_restricts_tools("plan")  # read-only (denies write)
    assert not posture_restricts_tools("chat")  # asks, never denies
    assert not posture_restricts_tools("build")  # asks
    assert not posture_restricts_tools("auto")  # allows


def test_no_conflict_when_no_native_modes() -> None:
    assert posture_conflict_notice("brainstorm", ActiveNativeModes()) == ""


def test_no_conflict_under_permissive_posture() -> None:
    modes = ActiveNativeModes().add("team-pulse")
    assert posture_conflict_notice("build", modes) == ""
    assert posture_conflict_notice("auto", modes) == ""


def test_brainstorm_conflict_names_modes_and_the_fix() -> None:
    modes = ActiveNativeModes().add("team-pulse")
    notice = posture_conflict_notice("brainstorm", modes)
    assert "team-pulse" in notice
    assert "brainstorm" in notice
    assert "blocks all tools" in notice  # brainstorm denies even reads
    assert "/mode build" in notice or "/mode auto" in notice


def test_plan_conflict_is_read_only_not_all_blocked() -> None:
    modes = ActiveNativeModes().add("audit").add("team-pulse")
    notice = posture_conflict_notice("plan", modes)
    assert "team-pulse" in notice and "audit" in notice
    assert "read-only" in notice  # plan allows reads
    assert "blocks all tools" not in notice
