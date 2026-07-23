"""Demo lane: capabilities A(demo), B, C, D through a real PTY.

Launches the shipped ``amplifier-newtui --demo`` binary in a forge PTY at
a fixed 120x40 and asserts user-visible behavior.  The demo runtime is
deterministic (virtual clock, seeded RNG, fixed costs), so every assertion
is exact.  Observation is screen-only by design -- the demo path does not
persist a ledger (see ``_ledger`` docstring); ledger assertions live in the
real lane.

Synchronization is always a bounded ``forge wait`` on a single-token anchor
(ANSI can split multi-word phrases in the buffer); richer multi-word
assertions run against the already-rendered, ANSI-free ``screen()`` text.
"""

from __future__ import annotations

import pytest

from amplifier_app_newtui.kernel.demo import (
    AGENTS_PROMPT,
    BUILD_PROMPT,
    DEMO_BUNDLE,
    PLAN_PROMPT,
    PLAN_TITLE,
    STORE_NARRATIONS,
    STORE_PLAN_TITLE,
)

from ._forge import ForgeSession

pytestmark = pytest.mark.forge

# Turns pace through real (virtual-clock) waits; give completion room.
_TURN_TIMEOUT_MS = 90_000


def test_boot_to_composer(demo_session: ForgeSession) -> None:
    """A(demo): the binary boots to a real composer + footer chrome."""
    screen = demo_session.screen()
    assert "Message" in screen, "composer prompt missing"
    assert DEMO_BUNDLE in screen, "footer/title bundle name missing"
    assert "mode auto" in screen, "footer mode strip missing"
    assert "$0.57" in screen, "footer session cost missing"


def test_palette_and_slash_commands(demo_session: ForgeSession) -> None:
    """B: /status + /model run, and `/` opens the command palette."""
    # /status -- distinctive Status panel (submit clears the composer).
    demo_session.submit("/status")
    assert demo_session.wait("Status", total_timeout_ms=15_000), "/status produced no panel"
    status_screen = demo_session.screen()
    assert "bundle" in status_screen and DEMO_BUNDLE in status_screen
    assert "cost" in status_screen and "$0.57" in status_screen

    # /model -- no provider is mounted under the demo runtime.
    demo_session.submit("/model")
    assert demo_session.wait("provider", total_timeout_ms=15_000), "/model produced no output"
    assert "no provider mounted" in demo_session.screen()

    # `/` opens the palette (keymap open_palette).
    demo_session.type("/", newline=False)
    assert demo_session.wait("select", total_timeout_ms=15_000), "palette did not open"
    palette_screen = demo_session.screen()
    assert "esc close" in palette_screen, "palette footer missing"
    assert "/status" in palette_screen and "/model" in palette_screen


def test_demo_turn_streams_plan_and_cost(demo_session: ForgeSession) -> None:
    """C: a full demo turn -- streaming, plan panel, footer cost."""
    demo_session.submit(BUILD_PROMPT)
    # Plan panel header lands first.
    assert demo_session.wait("Refactor", total_timeout_ms=_TURN_TIMEOUT_MS), "no plan panel"
    # First narration streams into the transcript.
    first_word = STORE_NARRATIONS[0].split()[0]  # "Mapping"
    assert demo_session.wait(first_word, total_timeout_ms=_TURN_TIMEOUT_MS), "no streamed text"
    # Turn completes -> footer session cost advances 0.57 -> 0.70.
    assert demo_session.wait(r"0\.70", total_timeout_ms=_TURN_TIMEOUT_MS), "footer cost did not update"

    screen = demo_session.screen()
    assert STORE_PLAN_TITLE in screen, "plan panel title missing"
    assert "Plan" in screen, "ambient plan panel missing"
    assert "$0.70" in screen, "footer cost figure missing"


def test_plan_turn_renders_proposed_panel(demo_session: ForgeSession) -> None:
    """C(plan): the read-only plan turn renders the Proposed-plan panel."""
    demo_session.submit(PLAN_PROMPT)
    assert demo_session.wait("Proposed", total_timeout_ms=_TURN_TIMEOUT_MS), "no proposed plan"
    assert PLAN_TITLE in demo_session.screen(), "proposed-plan title missing"


def test_agents_fanout_lanes_and_tail(demo_session: ForgeSession) -> None:
    """D: fan-out -- lanes appear, delegate summary, ctrl+o tail focus."""
    demo_session.submit(AGENTS_PROMPT)
    assert demo_session.wait("researcher", total_timeout_ms=_TURN_TIMEOUT_MS), "no lanes"
    assert demo_session.wait("delegates", total_timeout_ms=_TURN_TIMEOUT_MS), "no delegate summary"

    lanes_screen = demo_session.screen()
    for lane in ("researcher", "coder", "tester"):
        assert lane in lanes_screen, f"lane {lane!r} missing from panel"

    # ctrl+o cycles tail focus (keymap cycle_tail) -- outside forge's fixed
    # key list, so pressed as a raw control byte.  The app must survive it
    # and keep rendering the lanes.
    demo_session.press_ctrl("o")
    assert demo_session.wait("tester", total_timeout_ms=15_000), "lanes vanished after ctrl+o"
