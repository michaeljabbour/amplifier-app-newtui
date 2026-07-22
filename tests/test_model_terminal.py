"""TerminalSurface: the shared, clamped, thread-safe width holder (#35)."""

from __future__ import annotations

import threading

from amplifier_app_newtui.model.terminal import (
    DEFAULT_TERMINAL_COLS,
    MAX_TERMINAL_COLS,
    MIN_TERMINAL_COLS,
    TerminalSurface,
)


def test_defaults_to_vt100_width() -> None:
    assert TerminalSurface().cols == DEFAULT_TERMINAL_COLS == 80


def test_set_cols_updates_width() -> None:
    surface = TerminalSurface()
    surface.set_cols(132)
    assert surface.cols == 132


def test_zero_and_negative_widths_clamp_to_floor() -> None:
    # A transient 0-width report during boot must never leak into the hint.
    surface = TerminalSurface()
    surface.set_cols(0)
    assert surface.cols == MIN_TERMINAL_COLS
    surface.set_cols(-40)
    assert surface.cols == MIN_TERMINAL_COLS


def test_absurd_width_clamps_to_ceiling() -> None:
    surface = TerminalSurface()
    surface.set_cols(10_000)
    assert surface.cols == MAX_TERMINAL_COLS


def test_junk_width_falls_back_to_default() -> None:
    surface = TerminalSurface(200)
    surface.set_cols("not-an-int")  # type: ignore[arg-type]
    assert surface.cols == DEFAULT_TERMINAL_COLS


def test_constructor_clamps_too() -> None:
    assert TerminalSurface(0).cols == MIN_TERMINAL_COLS
    assert TerminalSurface(5_000).cols == MAX_TERMINAL_COLS


def test_concurrent_writes_leave_a_valid_value() -> None:
    # The UI (app loop) writes while the kernel (runtime thread) reads; the
    # lock guarantees a torn value never surfaces.
    surface = TerminalSurface()
    widths = [40, 80, 120, 200, 60]

    def writer(value: int) -> None:
        for _ in range(500):
            surface.set_cols(value)

    threads = [threading.Thread(target=writer, args=(w,)) for w in widths]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert surface.cols in widths
