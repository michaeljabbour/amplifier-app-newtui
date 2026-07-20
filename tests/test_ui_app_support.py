"""Pure interaction-state helpers kept outside the composition root."""

from __future__ import annotations

from amplifier_app_newtui.ui.app_support import EscSequence
from amplifier_app_newtui.ui.keymap import ESC_BACKTRACK_WINDOW_SECONDS


def test_esc_sequence_accepts_the_boundary_once() -> None:
    sequence = EscSequence()
    sequence.arm_interrupt(10.0)
    assert sequence.consume_backtrack(10.0 + ESC_BACKTRACK_WINDOW_SECONDS)
    assert not sequence.consume_backtrack(10.1)


def test_esc_sequence_expires_and_clears() -> None:
    sequence = EscSequence()
    sequence.arm_interrupt(10.0)
    assert not sequence.consume_backtrack(
        10.0 + ESC_BACKTRACK_WINDOW_SECONDS + 0.001
    )
    assert sequence.interrupted_at is None
