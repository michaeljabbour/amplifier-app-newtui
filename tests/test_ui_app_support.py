"""Pure interaction-state helpers kept outside the composition root."""

from __future__ import annotations

from amplifier_app_newtui.ui.app_support import (
    ATTENTION_MIN_TURN_SECONDS,
    EscSequence,
    attention_bell_needed,
)
from amplifier_app_newtui.ui.keymap import ESC_BACKTRACK_WINDOW_SECONDS


def test_esc_sequence_accepts_the_boundary_once() -> None:
    sequence = EscSequence()
    sequence.arm_interrupt(10.0)
    assert sequence.consume_backtrack(10.0 + ESC_BACKTRACK_WINDOW_SECONDS)
    assert not sequence.consume_backtrack(10.1)


def test_esc_sequence_expires_and_clears() -> None:
    sequence = EscSequence()
    sequence.arm_interrupt(10.0)
    assert not sequence.consume_backtrack(10.0 + ESC_BACKTRACK_WINDOW_SECONDS + 0.001)
    assert sequence.interrupted_at is None


# -- attention bell (hook-output adapter for the suppressed hooks-notify) -----


def test_attention_bell_rings_when_a_decision_is_deferred() -> None:
    """A deferred decision always needs the human — elapsed is irrelevant."""
    assert attention_bell_needed("decision_deferred", 0.0, environ={})


def test_attention_bell_rings_only_after_long_turns() -> None:
    """Turn end rings only when the turn ran long enough that the user has
    plausibly looked away; quick exchanges stay silent."""
    assert not attention_bell_needed("turn_finished", 0.0, environ={})
    assert not attention_bell_needed("turn_finished", ATTENTION_MIN_TURN_SECONDS - 0.1, environ={})
    assert attention_bell_needed("turn_finished", ATTENTION_MIN_TURN_SECONDS, environ={})


def test_attention_bell_honors_amplifier_notify_env() -> None:
    """AMPLIFIER_NOTIFY=false/0/no/off disables the bell — same kill switch
    the suppressed hooks-notify module honored."""
    for value in ("false", "0", "no", "off", "FALSE", "Off"):
        assert not attention_bell_needed(
            "decision_deferred", 0.0, environ={"AMPLIFIER_NOTIFY": value}
        )
        assert not attention_bell_needed(
            "turn_finished", 999.0, environ={"AMPLIFIER_NOTIFY": value}
        )
    assert attention_bell_needed("decision_deferred", 0.0, environ={"AMPLIFIER_NOTIFY": "true"})
