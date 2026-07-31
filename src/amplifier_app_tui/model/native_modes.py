"""Active native (bundle-composed) modes as an ordered set + precedence rules.

Two independent tool-policy layers coexist in tui:

1. **Posture** — the single shift+tab trust baseline (chat/plan/brainstorm/
   build/auto, :mod:`amplifier_app_tui.model.modes` +
   :mod:`amplifier_app_tui.model.trust`). What capabilities auto-run/ask/
   deny. Always exactly one.
2. **Native modes** — bundle-composed modes (team-pulse, audit, superpowers,
   …) activated through the mounted ``mode`` tool. Each may declare/imply its
   own tool needs (``safe_tools``).

**Upstream boundary (verified against ``amplifier_module_tool_mode`` +
``amplifier_module_hooks_mode``):** the mode tool and hooks-mode are strictly
*single-slot* — ``coordinator.session_state["active_mode"]`` is one string;
``set`` replaces it, ``clear`` nulls it, and hooks-mode enforces exactly that
one mode's tool policy + instructions. There is no upstream multi-activation.

So tui models an ordered **stack** of active native modes client-side. The
**primary** (most-recently activated, the top of the stack) is what tui
points the upstream single slot at — so its policy + instructions are the ones
hooks-mode actually enforces. The rest of the stack is retained here for
display and precedence; removing the primary promotes the next one back into
the enforced slot. A single active native mode therefore behaves exactly as
the old single-slot ``_native_mode`` did (backward compatible).

**Tool-policy precedence rule.** The posture is the trust baseline; an active
native mode's *declared* tools take precedence over a tool-restrictive posture:

- The kernel governance hook lets a tool the active native mode declares
  ``safe`` through (abstains — ``continue``) regardless of posture, so a
  no-tools posture no longer *silently* nullifies a native mode's own tools.
  hooks-mode remains authoritative for those tools. (This is the "make the
  declared tools survive the posture" half — see
  :class:`amplifier_app_tui.kernel.governance_hook.GovernanceHook`.)
- Where a native mode's needs cannot be settled from ``safe_tools`` alone (a
  mode leaning on ``default_action`` rather than an explicit safe list), the
  app surfaces a clear conflict notice — :func:`posture_conflict_notice` —
  instead of a silent denial: "team-pulse active but brainstorm blocks tools —
  /mode build to run them".
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from .modes import get_mode
from .trust import CapabilityClass, resolve_capability

NATIVE_BADGE = "◆"
"""Footer glyph prefixing the active native-mode badge."""


class ActiveNativeModes(BaseModel):
    """Ordered, de-duplicated set of active native modes (last == primary).

    Frozen value type: :meth:`add` / :meth:`remove` / :meth:`clear` return a
    new instance rather than mutating, so the app can hold one and swap it.
    ``names`` is activation order — the LAST element is the :attr:`primary`,
    the one tui points the upstream single slot at.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    names: tuple[str, ...] = ()

    @property
    def primary(self) -> str | None:
        """The enforced native mode (top of the stack), or ``None`` if empty."""
        return self.names[-1] if self.names else None

    def add(self, name: str) -> ActiveNativeModes:
        """Activate *name*, moving it to primary if already present.

        A blank name is a no-op (returns self). Re-adding an active mode
        promotes it to primary rather than duplicating it — the newest
        intent wins the single upstream slot.
        """
        clean = name.strip()
        if not clean:
            return self
        rest = tuple(existing for existing in self.names if existing != clean)
        return ActiveNativeModes(names=(*rest, clean))

    def remove(self, name: str) -> ActiveNativeModes:
        """Deactivate *name*; a no-op when it is not active."""
        clean = name.strip()
        return ActiveNativeModes(names=tuple(n for n in self.names if n != clean))

    def clear(self) -> ActiveNativeModes:
        """Deactivate every native mode."""
        return ActiveNativeModes()

    def __contains__(self, name: object) -> bool:
        return name in self.names

    def __bool__(self) -> bool:
        return bool(self.names)

    def __len__(self) -> int:
        return len(self.names)

    def __iter__(self):  # type: ignore[override]
        return iter(self.names)


def _ordered_for_display(modes: ActiveNativeModes | tuple[str, ...]) -> tuple[str, ...]:
    """Primary first, then the rest of the stack newest-to-oldest."""
    names = modes.names if isinstance(modes, ActiveNativeModes) else tuple(modes)
    return tuple(reversed(names))


def native_badge_text(modes: ActiveNativeModes | tuple[str, ...]) -> str:
    """The footer badge for the active native-mode set (``""`` when empty).

    A single mode renders ``◆ team-pulse`` (unchanged from the single-slot
    era). A stacked set renders the primary first then the others as ``+``
    entries — ``◆ audit +team-pulse`` — so the one actually enforced upstream
    (``◆``) is visually distinct from the ones stacked behind it (``+``).
    """
    ordered = _ordered_for_display(modes)
    if not ordered:
        return ""
    primary, *rest = ordered
    badge = f"{NATIVE_BADGE} {primary}"
    if rest:
        badge += " " + " ".join(f"+{name}" for name in rest)
    return badge


def posture_restricts_tools(posture_id: str) -> bool:
    """True when *posture_id* denies (not merely asks for) tool use.

    Derived from :mod:`amplifier_app_tui.model.trust` rather than a
    hardcoded list: a posture restricts tools when it *denies* the write
    capability (plan = read-only, brainstorm = no tools). chat/build ask and
    auto allows, so none of those nullify a native mode's tools.
    """
    return resolve_capability(posture_id, CapabilityClass.WRITE).decision == "deny"


def posture_conflict_notice(posture_id: str, modes: ActiveNativeModes | tuple[str, ...]) -> str:
    """Notice text when a tool-restrictive posture coexists with native modes.

    Returns ``""`` when there is no conflict (no native modes, or a posture
    that does not deny tools). The message names the active modes and the
    posture that is blocking them, and points at the fix — never a silent
    nullification.
    """
    ordered = _ordered_for_display(modes)
    if not ordered or not posture_restricts_tools(posture_id):
        return ""
    profile = get_mode(posture_id)
    names = ", ".join(ordered)
    reads_denied = resolve_capability(posture_id, CapabilityClass.READ).decision == "deny"
    blocks = "blocks all tools" if reads_denied else f"is {profile.trust_str}"
    return f"{names} active · {profile.id} {blocks} — /mode build or /mode auto to run its tools"


__all__ = [
    "NATIVE_BADGE",
    "ActiveNativeModes",
    "native_badge_text",
    "posture_conflict_notice",
    "posture_restricts_tools",
]
