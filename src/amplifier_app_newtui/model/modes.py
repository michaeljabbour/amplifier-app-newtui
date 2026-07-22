"""The five interaction modes and their cycle (DESIGN-SPEC §4, ADR-0005).

Mode → color/trust table (verbatim from the spec):

======== ======= ==========================================
mode     color   trust string
======== ======= ==========================================
chat     dim     ``ask all · auto read``
plan     blue    ``read-only``
brainstorm teal  ``no tools``
build    green   ``auto read,test · ask write,net,spend``
auto     orange  ``auto read,write · asks if risky``
======== ======= ==========================================

Mode tint appears in exactly three places: composer ``[mode]`` badge,
composer left edge, footer. The composer *edge* for chat uses the ``rule``
token (spec §4) — that is the ``accent`` field; ``color_token`` is the
badge/footer color.

shift+tab cycles modes; this is a fully independent control from the
ctrl-p permission cycle (ADR-0005 amendment — the two 5-state cycles
share four members but diverge at brainstorm vs bypass and must never be
one control).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from .blocks import StyleToken

ModeId = Literal["chat", "plan", "brainstorm", "build", "auto"]


class ModeProfile(BaseModel):
    """One interaction mode's presentation + trust identity.

    - ``id``: mode name, also the trust-preset key in
      :mod:`amplifier_app_newtui.model.trust`.
    - ``color_token``: theme token for the ``[mode]`` badge and footer
      ``mode <id>`` text.
    - ``trust_str``: the exact trust summary shown in mode-change notices
      (``mode <id> · <trust_str>``) and the footer.
    - ``accent``: theme token tinting the composer's 2px left edge
      (``rule`` for chat, else the mode color).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: ModeId
    color_token: StyleToken
    trust_str: str
    accent: StyleToken

    def notice(self) -> str:
        """The transient notice text on mode change: ``mode <id> · <trust>``."""
        return f"mode {self.id} · {self.trust_str}"


MODE_PROFILES: dict[ModeId, ModeProfile] = {
    "chat": ModeProfile(
        id="chat", color_token="dim", trust_str="ask all · auto read", accent="rule"
    ),
    "plan": ModeProfile(
        id="plan", color_token="blue", trust_str="read-only", accent="blue"
    ),
    "brainstorm": ModeProfile(
        id="brainstorm", color_token="teal", trust_str="no tools", accent="teal"
    ),
    "build": ModeProfile(
        id="build",
        color_token="green",
        trust_str="auto read,test · ask write,net,spend",
        accent="green",
    ),
    "auto": ModeProfile(
        id="auto",
        color_token="orange",
        trust_str="auto read,write · asks if risky",
        accent="orange",
    ),
}
"""All five mode profiles keyed by id (DESIGN-SPEC §4 table, verbatim)."""

MODE_CYCLE: tuple[ModeId, ...] = ("chat", "plan", "brainstorm", "build", "auto")
"""shift+tab cycle order (mockup Component.MODES array order, DESIGN-SPEC §4)."""

DEFAULT_MODE: ModeId = "auto"
"""Boot posture. The mockup demo *starts* its scripted history in chat, but the
app defaults to auto — amplifier's natural wide scope (user directive
2026-07-16): auto read/write/test, the rest asks if risky (classifier-gated)."""


def get_mode(mode_id: str | None) -> ModeProfile:
    """Look up a mode profile, falling back to DEFAULT_MODE for unknown/None ids."""
    if mode_id in MODE_PROFILES:
        return MODE_PROFILES[mode_id]  # type: ignore[index]
    return MODE_PROFILES[DEFAULT_MODE]


def cycle_mode(current: str | None, offset: int = 1) -> ModeProfile:
    """Return the next mode in the shift+tab cycle.

    Unknown/None ``current`` lands on the first cycle entry (or last for a
    negative offset) rather than raising — cycling must always succeed.
    """
    try:
        index = MODE_CYCLE.index(current)  # type: ignore[arg-type]
    except ValueError:
        return MODE_PROFILES[MODE_CYCLE[0 if offset >= 0 else -1]]
    return MODE_PROFILES[MODE_CYCLE[(index + offset) % len(MODE_CYCLE)]]


__all__ = [
    "DEFAULT_MODE",
    "MODE_CYCLE",
    "MODE_PROFILES",
    "ModeId",
    "ModeProfile",
    "cycle_mode",
    "get_mode",
]
