"""``/permissions`` — the trust-slot listing/editing model surface.

Mockup description: *edit trust slots: boundary, blocks, exceptions*.
This module is the editable model behind that editor (DESIGN-SPEC §4/§6):

- **slots** — one row per :class:`CapabilityClass` showing the effective
  allow/ask/deny decision (mode default, or a user override);
- **boundary** — the project scope trust applies within
  (``within project`` by default; widening it is the mockup's ``add fork
  remote to boundary`` move);
- **exceptions** — always-allow patterns (the allowlist that ``Allow
  always`` and ``/improve`` proposals feed);
- **blocks** — always-deny patterns that beat everything else.

Resolution precedence for one tool call: blocks → exceptions → user slot
override → mode default (:func:`amplifier_app_newtui.model.trust.resolve`).
The UI edits this surface; the kernel governance hook consults it on
every ``tool:pre``. Nothing here imports Textual or amplifier-core.
"""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict

from ..model.modes import DEFAULT_MODE
from ..model.trust import (
    CapabilityClass,
    Decision,
    TrustDecision,
    classify_tool,
    resolve,
    resolve_capability,
)

SLOT_ORDER: tuple[CapabilityClass, ...] = (
    CapabilityClass.READ,
    CapabilityClass.TEST,
    CapabilityClass.WRITE,
    CapabilityClass.NET,
    CapabilityClass.SPEND,
    CapabilityClass.EXEC,
    CapabilityClass.OUTSIDE_PROJECT,
)
"""Display order of capability slots in the editor (safest first)."""

DEFAULT_BOUNDARY = "within project"


def mode_default(mode: str, capability: CapabilityClass) -> TrustDecision:
    """The mode's static decision for one capability slot."""
    return resolve_capability(mode, capability)


class TrustSlot(BaseModel):
    """One editable capability slot as the editor lists it.

    ``overridden`` is True when the user changed this slot away from the
    mode default (the editor renders those distinctly and offers reset).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    capability: CapabilityClass
    decision: Decision
    default_decision: Decision
    overridden: bool
    classifier_gated: bool = False

    def label(self) -> str:
        """Row text, e.g. ``write · ask`` or ``net · deny (default ask)``."""
        text = f"{self.capability.value} · {self.decision}"
        if self.overridden:
            text += f" (default {self.default_decision})"
        return text


class PermissionsSnapshot(BaseModel):
    """Frozen view of the whole surface for rendering / persistence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: str
    boundary: str
    slots: tuple[TrustSlot, ...]
    exceptions: tuple[str, ...]
    blocks: tuple[str, ...]


def _clean_pattern(pattern: object) -> str:
    clean = " ".join(str(pattern).split())
    if not clean:
        raise ValueError("pattern cannot be empty")
    return clean


def _matches(pattern: str, tool_name: str, command: str) -> bool:
    """One pattern matches a call by exact tool name or command prefix.

    Command prefix matching is whole-token (``git push`` matches
    ``git push origin`` but not ``git pushx``) — the 2-token-prefix
    scoping ADR-0007 uses for "Allow always" on bash.
    """
    if pattern == tool_name:
        return True
    if command:
        return command == pattern or command.startswith(f"{pattern} ")
    return False


class PermissionSurface:
    """Mutable trust-slot editor state — one instance per session.

    The UI mutates it through the methods below; the governance hook
    calls :meth:`resolve_call` on every ``tool:pre``. Edits here are
    explicit user actions (``/improve`` proposes; only this surface,
    driven by the human, applies).
    """

    def __init__(self, mode: str = DEFAULT_MODE) -> None:
        self._mode = mode
        self._boundary = DEFAULT_BOUNDARY
        self._overrides: dict[CapabilityClass, Decision] = {}
        self._exceptions: list[str] = []
        self._blocks: list[str] = []

    # --- mode ----------------------------------------------------------
    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        """Track the active mode; user slot overrides survive mode changes
        (they are the user's word against any mode's default)."""
        self._mode = mode

    # --- boundary ------------------------------------------------------
    @property
    def boundary(self) -> str:
        return self._boundary

    def set_boundary(self, boundary: object) -> None:
        self._boundary = _clean_pattern(boundary)

    # --- slots ----------------------------------------------------------
    @property
    def overrides(self) -> Mapping[CapabilityClass, Decision]:
        return dict(self._overrides)

    def set_slot(self, capability: CapabilityClass, decision: Decision) -> None:
        """Override one capability slot; setting the mode default clears it."""
        if mode_default(self._mode, capability).decision == decision:
            self._overrides.pop(capability, None)
        else:
            self._overrides[capability] = decision

    def clear_slot(self, capability: CapabilityClass) -> None:
        self._overrides.pop(capability, None)

    def resolve_capability(self, capability: CapabilityClass) -> TrustDecision:
        """Effective decision for a capability classified by the kernel."""
        override = self._overrides.get(capability)
        if override is not None:
            return TrustDecision(
                decision=override,
                capability=capability,
                reason=f"user trust slot · {capability.value} {override}",
            )
        return mode_default(self._mode, capability)

    def slots(self) -> tuple[TrustSlot, ...]:
        """All capability slots with effective decisions, in display order."""
        rows = []
        for capability in SLOT_ORDER:
            default = mode_default(self._mode, capability)
            override = self._overrides.get(capability)
            rows.append(
                TrustSlot(
                    capability=capability,
                    decision=override if override is not None else default.decision,
                    default_decision=default.decision,
                    overridden=override is not None,
                    classifier_gated=(default.classifier_gated if override is None else False),
                )
            )
        return tuple(rows)

    # --- exceptions / blocks --------------------------------------------
    @property
    def exceptions(self) -> tuple[str, ...]:
        return tuple(self._exceptions)

    @property
    def blocks(self) -> tuple[str, ...]:
        return tuple(self._blocks)

    def add_exception(self, pattern: object) -> None:
        clean = _clean_pattern(pattern)
        if clean not in self._exceptions:
            self._exceptions.append(clean)

    def remove_exception(self, pattern: object) -> None:
        self._exceptions.remove(_clean_pattern(pattern))

    def add_block(self, pattern: object) -> None:
        clean = _clean_pattern(pattern)
        if clean not in self._blocks:
            self._blocks.append(clean)

    def remove_block(self, pattern: object) -> None:
        self._blocks.remove(_clean_pattern(pattern))

    # --- resolution -----------------------------------------------------
    def resolve_call(
        self, tool_name: str, tool_input: Mapping[str, object] | None = None
    ) -> TrustDecision:
        """Effective decision for one tool call.

        Precedence: blocks → exceptions → slot override → mode default.
        Blocks beat exceptions: an always-deny the user wrote wins over
        an old allowlist entry (fail-closed on conflict).
        """
        command = ""
        if tool_input:
            command = str(tool_input.get("command", "") or tool_input.get("cmd", "")).strip()
        capability = classify_tool(tool_name, tool_input)
        for pattern in self._blocks:
            if _matches(pattern, tool_name, command):
                return TrustDecision(
                    decision="deny",
                    capability=capability,
                    reason=f"blocked by permissions blocklist · {pattern}",
                )
        for pattern in self._exceptions:
            if _matches(pattern, tool_name, command):
                return TrustDecision(
                    decision="allow",
                    capability=capability,
                    reason=f"allowlisted · {pattern}",
                )
        override = self._overrides.get(capability)
        if override is not None:
            return TrustDecision(
                decision=override,
                capability=capability,
                reason=f"user trust slot · {capability.value} {override}",
            )
        return resolve(self._mode, tool_name, tool_input)

    # --- snapshot ---------------------------------------------------------
    def snapshot(self) -> PermissionsSnapshot:
        return PermissionsSnapshot(
            mode=self._mode,
            boundary=self._boundary,
            slots=self.slots(),
            exceptions=self.exceptions,
            blocks=self.blocks,
        )


__all__ = [
    "DEFAULT_BOUNDARY",
    "PermissionSurface",
    "PermissionsSnapshot",
    "SLOT_ORDER",
    "TrustSlot",
    "mode_default",
]
