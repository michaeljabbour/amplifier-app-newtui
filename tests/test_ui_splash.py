"""Boot splash (ui/splash.py): pure frame functions + app lifecycle.

Frame functions are pure (art, frame) → Line rows, so most coverage is
plain assertions; the Pilot tests pin the lifecycle contract — mounted by
the first ``boot_progress``, guards mid-boot ops, dissolved by
``announce_ready``, removed instantly by ``announce_boot_failure``.
"""

from __future__ import annotations

import asyncio

import pytest

from amplifier_app_newtui.ui import splash
from amplifier_app_newtui.ui.app import NewTuiApp
from amplifier_app_newtui.ui.runtime_adapter import RuntimeAdapter
from amplifier_app_newtui.ui.segments import line_plain
from amplifier_app_newtui.ui.splash import (
    FALLBACK,
    WORDMARK,
    art_for,
    decay_grid,
    dissolve_frame,
    hold_frame,
    status_line,
    sweep_frame,
)

SPLASH_TOKENS = {"orange", "bright", "fg", "dim", "dimmer"}
"""§1 tokens the splash may reference (fg comes from the shimmer band)."""


# -- pure frame functions ------------------------------------------------------------


def test_wordmark_is_rectangular_and_wide() -> None:
    widths = {len(line) for line in WORDMARK}
    assert len(widths) == 1
    assert len(WORDMARK) == 5
    assert widths == {55}


def test_art_for_picks_wordmark_when_it_fits_else_fallback() -> None:
    assert art_for(110, 30) is WORDMARK
    assert art_for(50, 30) is FALLBACK  # too narrow
    assert art_for(110, 6) is FALLBACK  # too short
    assert art_for(20, 3) is FALLBACK


def test_sweep_reveals_left_to_right_and_finishes() -> None:
    width = len(WORDMARK[0])
    first = sweep_frame(WORDMARK, 0)
    assert first is not None
    # Frame 0: nothing revealed yet — only the bright noise edge.
    assert all(
        all(segment.style_token == "bright" for segment in line) for line in first
    )
    mid = sweep_frame(WORDMARK, 5)
    assert mid is not None
    reveal = 5 * splash.SWEEP_COLS_PER_FRAME
    for row, line in enumerate(mid):
        plain = line_plain(line)
        assert plain[:reveal] == WORDMARK[row][:reveal]  # revealed art is verbatim
        assert len(plain) == reveal + 3  # plus the three-cell edge
    frames_needed = -(-width // splash.SWEEP_COLS_PER_FRAME)
    assert sweep_frame(WORDMARK, frames_needed) is None


def test_hold_frame_never_changes_plain_text() -> None:
    # Motion is style-only (ui/motion.py rule): copy/selection stay stable.
    for frame in range(0, 80, 7):
        held = hold_frame(WORDMARK, frame)
        assert tuple(line_plain(line) for line in held) == WORDMARK


def test_hold_frame_shimmer_moves() -> None:
    def styles(frame: int) -> tuple[tuple[str, bool], ...]:
        return tuple(
            (segment.style_token, segment.bold)
            for line in hold_frame(WORDMARK, frame)
            for segment in line
        )

    assert styles(2) != styles(6)  # the band actually drifts


def test_dissolve_is_deterministic_and_reaches_empty() -> None:
    grid = decay_grid(WORDMARK)
    assert grid == decay_grid(WORDMARK)  # fixed seed
    last = splash.DISSOLVE_SPREAD_FRAMES + splash.DISSOLVE_DOT_FRAMES
    art_chars = set("".join(WORDMARK))
    for frame in range(last + 1):
        rows = dissolve_frame(WORDMARK, grid, frame)
        assert rows is not None
        for line in rows:
            assert set(line_plain(line)) <= art_chars | {"·", " "}
    final = dissolve_frame(WORDMARK, grid, last)
    assert final is not None
    # By the last frame every surviving cell is a dot or blank — no art left.
    assert set("".join(line_plain(line) for line in final)) <= {"·", " "}
    assert dissolve_frame(WORDMARK, grid, last + 1) is None


def test_all_frames_use_theme_tokens_only() -> None:
    grid = decay_grid(WORDMARK)
    frames = [
        sweep_frame(WORDMARK, 3),
        hold_frame(WORDMARK, 12),
        dissolve_frame(WORDMARK, grid, 4),
    ]
    for rows in frames:
        assert rows is not None
        for line in rows:
            assert all(segment.style_token in SPLASH_TOKENS for segment in line)


def test_status_line_centers_and_truncates() -> None:
    width = len(WORDMARK[0])
    line = status_line(width, "installing · amplifier-foundation", "✳")
    plain = line_plain(line)
    assert "✳ installing · amplifier-foundation" in plain
    assert plain.startswith(" ")  # centered under the wordmark
    long = status_line(width, "x" * 200, "✳")
    assert len(line_plain(long)) <= width + 2


# -- app lifecycle -------------------------------------------------------------------


async def _wait_for(pilot, predicate, *, tries: int = 120) -> bool:  # noqa: ANN001
    for _ in range(tries):
        if predicate():
            return True
        await pilot.pause(0.05)
    return predicate()


class _SlowBootAdapter(RuntimeAdapter):
    """Boot parks on an event, like a real module prepare on a cold cache."""

    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    async def start(self, ready) -> None:  # noqa: ANN001
        self.app.boot_progress("installing", "amplifier-foundation")
        await self.release.wait()
        ready()


class _FailBootAdapter(RuntimeAdapter):
    async def start(self, ready) -> None:  # noqa: ANN001
        self.app.boot_progress("preparing", "newtui")
        raise RuntimeError("no provider configured")


def _splash_widgets(app: NewTuiApp) -> list:  # noqa: ANN401
    return list(app.query("#boot-splash"))


@pytest.mark.asyncio
async def test_splash_mounts_during_boot_and_dissolves_on_ready() -> None:
    adapter = _SlowBootAdapter()
    app = NewTuiApp(adapter)
    async with app.run_test(size=(110, 40)) as pilot:
        assert await _wait_for(pilot, lambda: bool(_splash_widgets(app)))
        widget = _splash_widgets(app)[0]
        assert widget._status == "installing · amplifier-foundation"

        # Later phases update the same splash instead of stacking widgets.
        app.boot_progress("creating", "session")
        assert widget._status == "creating · session"
        assert len(_splash_widgets(app)) == 1

        # Mid-boot submits are kept, not eaten (and never reach the runtime).
        app.submit_prompt("hello world")
        assert not any(b.kind == "user_line" for b in app.transcript.blocks)

        # Let the frame timer draw at least one frame before dismissing so
        # the dissolve path (not the not-yet-laid-out shortcut) runs.
        assert await _wait_for(pilot, lambda: widget._art is not None)
        adapter.release.set()
        assert await _wait_for(pilot, lambda: app._splash is None)
        assert await _wait_for(pilot, lambda: not _splash_widgets(app))


@pytest.mark.asyncio
async def test_boot_failure_removes_splash_immediately() -> None:
    adapter = _FailBootAdapter()
    app = NewTuiApp(adapter)
    async with app.run_test(size=(110, 40)) as pilot:
        assert await _wait_for(
            pilot,
            lambda: any(
                b.kind == "answer" and "session failed to start" in line_plain(b.spans)
                for b in app.transcript.blocks
            ),
        )
        # No dissolve on failure — the wordmark must not melt over the error.
        assert not _splash_widgets(app)
        assert app._splash is None
