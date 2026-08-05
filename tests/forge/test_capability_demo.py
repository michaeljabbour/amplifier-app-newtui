"""Demo lane: capabilities A(demo), B, C, D through a real PTY.

Launches the shipped ``amplifier-tui --demo`` binary in a forge PTY at
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

from amplifier_app_tui.kernel.demo import (
    AGENTS_PROMPT,
    AUTO_ANSWER,
    AUTO_PROMPT,
    BUILD_PROMPT,
    DEMO_BUNDLE,
    DEMO_DEFERRED_DECISION,
    FORCE_PUSH_COMMAND,
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
    assert demo_session.wait(r"0\.70", total_timeout_ms=_TURN_TIMEOUT_MS), (
        "footer cost did not update"
    )

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


def test_auto_tool_denial_continues_and_leaves_decision_waiting(
    demo_session: ForgeSession,
) -> None:
    """Auto mode survives a denied tool, completes, and parks the decision."""
    demo_session.submit(AUTO_PROMPT)
    assert demo_session.wait(r"0\.70", total_timeout_ms=_TURN_TIMEOUT_MS), (
        "auto turn did not complete after the denied tool"
    )

    screen = demo_session.screen()
    assert FORCE_PUSH_COMMAND in screen, "denied tool command missing"
    assert "needs your ok" in screen, "denied tool did not expose the decision affordance"
    assert AUTO_ANSWER in " ".join(screen.split()), "auto turn stopped before its final answer"
    assert "1 decision waiting" in screen, "deferred decision badge missing"

    demo_session.press_ctrl("y")
    assert demo_session.wait("Needs", total_timeout_ms=15_000), "ctrl+y did not open Needs you"
    needs_screen = demo_session.screen()
    assert "Needs you  1 deferred decision" in needs_screen
    assert DEMO_DEFERRED_DECISION.text in needs_screen
    assert DEMO_DEFERRED_DECISION.chip_label in needs_screen


def test_custom_decision_accepts_exact_free_text(
    custom_decision_session: ForgeSession,
) -> None:
    """The bottom decision frame accepts and repeats a custom answer exactly."""
    custom_decision_session.press_ctrl("y")
    assert custom_decision_session.wait("Needs", total_timeout_ms=15_000)
    needs_screen = custom_decision_session.screen()
    assert "Which test label should I use?" in needs_screen
    assert "[Alpha]" in needs_screen and "[Beta]" in needs_screen
    assert "[+ type your own]" in needs_screen

    # Number-key navigation maps 1/2 to Alpha/Beta and 3 to custom.
    custom_decision_session.type("3", newline=False)
    assert custom_decision_session.wait("Decision", total_timeout_ms=15_000), (
        "custom answer did not open the bottom decision frame"
    )
    capture_screen = custom_decision_session.screen()
    assert "Decision · Which test label should I use?" in capture_screen
    assert "Enter submits answer" in capture_screen

    exact_answer = "violet-otter"
    custom_decision_session.submit(exact_answer)
    assert custom_decision_session.wait("Applying", total_timeout_ms=15_000)
    answered_screen = custom_decision_session.screen()
    assert f"Applying decision: {exact_answer}" in answered_screen
    assert "decision waiting" not in answered_screen


def test_next_turn_queue_can_be_recalled_and_steered(demo_session: ForgeSession) -> None:
    """A queued next turn is recallable mid-run and can interject immediately."""
    demo_session.submit(BUILD_PROMPT)
    first_word = STORE_NARRATIONS[0].split()[0]
    assert demo_session.wait(first_word, total_timeout_ms=_TURN_TIMEOUT_MS)

    interjection = "interject with this exact text"
    demo_session.type(interjection, newline=False)
    demo_session.press_alt("enter")  # legacy-terminal queue fallback
    assert demo_session.wait("queued", total_timeout_ms=15_000)
    queued_screen = demo_session.screen()
    assert f'queued next: "{interjection}"' in queued_screen
    assert "q1" in queued_screen

    demo_session.press_alt("up")
    assert demo_session.wait("recalled", total_timeout_ms=15_000)
    recalled_screen = demo_session.screen()
    assert f"❯ {interjection}" in recalled_screen
    assert "queued next:" not in recalled_screen
    assert "q1" not in recalled_screen

    demo_session.key("enter")
    assert demo_session.wait("applies", total_timeout_ms=15_000), (
        "recalled message did not become an immediate steer"
    )


def test_narrow_demo_keeps_plan_and_composer_visible(
    narrow_demo_session: ForgeSession,
) -> None:
    """At 80x18 the Plan surface stacks instead of disappearing."""
    narrow_demo_session.submit(BUILD_PROMPT)
    assert narrow_demo_session.wait("Plan", total_timeout_ms=_TURN_TIMEOUT_MS)
    screen = narrow_demo_session.screen()
    assert "Plan 0/3" in screen
    assert "Audit persistence paths" in screen
    assert "Migrate history to durable store" in screen
    assert "Message Amplifier" in screen, "narrow plan occluded the composer"
