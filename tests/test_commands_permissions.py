"""/permissions trust-slot surface: slots, overrides, boundary, resolution."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from amplifier_app_newtui.commands.permissions import (
    DEFAULT_BOUNDARY,
    PermissionSurface,
    SLOT_ORDER,
    TrustSlot,
    mode_default,
)
from amplifier_app_newtui.model.trust import CapabilityClass


def test_mode_defaults_match_spec_table() -> None:
    # build: auto read,test · ask write,net,spend (DESIGN-SPEC §4)
    assert mode_default("build", CapabilityClass.READ).decision == "allow"
    assert mode_default("build", CapabilityClass.TEST).decision == "allow"
    assert mode_default("build", CapabilityClass.WRITE).decision == "ask"
    assert mode_default("build", CapabilityClass.NET).decision == "ask"
    assert mode_default("build", CapabilityClass.SPEND).decision == "ask"
    # plan: read-only
    assert mode_default("plan", CapabilityClass.READ).decision == "allow"
    assert mode_default("plan", CapabilityClass.WRITE).decision == "deny"
    # brainstorm: no tools
    assert mode_default("brainstorm", CapabilityClass.READ).decision == "deny"
    # auto: auto read,write · asks if risky elsewhere
    assert mode_default("auto", CapabilityClass.WRITE).decision == "allow"
    assert mode_default("auto", CapabilityClass.NET).classifier_gated
    assert mode_default("auto", CapabilityClass.OUTSIDE_PROJECT).classifier_gated
    assert mode_default("build", CapabilityClass.OUTSIDE_PROJECT).decision == "ask"


def test_slots_listing_order_and_defaults() -> None:
    surface = PermissionSurface(mode="build")
    slots = surface.slots()
    assert tuple(slot.capability for slot in slots) == SLOT_ORDER
    by_cap = {slot.capability: slot for slot in slots}
    assert by_cap[CapabilityClass.READ].decision == "allow"
    assert by_cap[CapabilityClass.WRITE].decision == "ask"
    assert not any(slot.overridden for slot in slots)


def test_override_set_clear_and_labels() -> None:
    surface = PermissionSurface(mode="build")
    surface.set_slot(CapabilityClass.NET, "deny")
    slot = {s.capability: s for s in surface.slots()}[CapabilityClass.NET]
    assert slot.overridden
    assert slot.decision == "deny"
    assert slot.default_decision == "ask"
    assert slot.label() == "net · deny (default ask)"
    surface.clear_slot(CapabilityClass.NET)
    assert not {s.capability: s for s in surface.slots()}[CapabilityClass.NET].overridden


def test_setting_slot_to_mode_default_clears_override() -> None:
    surface = PermissionSurface(mode="build")
    surface.set_slot(CapabilityClass.WRITE, "ask")  # already the default
    assert surface.overrides == {}


def test_resolution_precedence_blocks_beat_exceptions_beat_overrides() -> None:
    surface = PermissionSurface(mode="build")
    surface.set_slot(CapabilityClass.EXEC, "allow")
    surface.add_exception("git push")
    surface.add_block("git push")
    decision = surface.resolve_call("bash", {"command": "git push origin main"})
    assert decision.decision == "deny"
    assert "blocklist" in decision.reason

    surface.remove_block("git push")
    decision = surface.resolve_call("bash", {"command": "git push origin main"})
    assert decision.decision == "allow"
    assert "allowlisted" in decision.reason

    surface.remove_exception("git push")
    decision = surface.resolve_call("bash", {"command": "git push origin main"})
    assert decision.decision == "allow"
    assert "user trust slot" in decision.reason

    surface.clear_slot(CapabilityClass.EXEC)
    decision = surface.resolve_call("bash", {"command": "git push origin main"})
    assert decision.decision == "ask"  # build mode default for exec


def test_command_prefix_matching_is_whole_token() -> None:
    surface = PermissionSurface(mode="build")
    surface.add_exception("git push")
    assert surface.resolve_call("bash", {"command": "git push origin"}).decision == "allow"
    assert surface.resolve_call("bash", {"command": "git pushx origin"}).decision == "ask"


def test_exception_matches_tool_name_exactly() -> None:
    surface = PermissionSurface(mode="chat")
    surface.add_exception("web_fetch")
    assert surface.resolve_call("web_fetch", {"url": "https://x"}).decision == "allow"
    assert surface.resolve_call("web_search", {}).decision == "ask"


def test_mode_change_keeps_user_overrides() -> None:
    surface = PermissionSurface(mode="build")
    surface.set_slot(CapabilityClass.NET, "deny")
    surface.set_mode("auto")
    assert surface.mode == "auto"
    assert surface.resolve_call("web_fetch", {}).decision == "deny"


def test_boundary_editing() -> None:
    surface = PermissionSurface()
    assert surface.boundary == DEFAULT_BOUNDARY
    surface.set_boundary("within project + fork remote")
    assert surface.boundary == "within project + fork remote"
    with pytest.raises(ValueError):
        surface.set_boundary("   ")


def test_snapshot_is_frozen_and_complete() -> None:
    surface = PermissionSurface(mode="plan")
    surface.add_exception("uv run pytest")
    surface.add_block("rm -rf")
    snap = surface.snapshot()
    assert snap.mode == "plan"
    assert snap.boundary == DEFAULT_BOUNDARY
    assert snap.exceptions == ("uv run pytest",)
    assert snap.blocks == ("rm -rf",)
    assert len(snap.slots) == len(SLOT_ORDER)
    assert all(isinstance(slot, TrustSlot) for slot in snap.slots)
    with pytest.raises(ValidationError):
        snap.mode = "chat"  # type: ignore[misc]


def test_duplicate_patterns_not_added_twice() -> None:
    surface = PermissionSurface()
    surface.add_exception("git push")
    surface.add_exception("git  push")
    assert surface.exceptions == ("git push",)
