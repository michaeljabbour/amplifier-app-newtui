"""ADR-0007 perf spike: 5k-block transcript + streaming deltas vs frame budget.

Budget (ADR-0007 resolution / open-q 6): **<16ms per frame during
streaming** with a large durable history mounted. This spike decides the
virtualization question: pure widget-per-block vs hybrid Line-API history.

Measured on Apple Silicon (2026-07-16, textual 8.2, Python 3.12; CI will
be slower — assertions carry generous margins):

===========================  =========================================
pure ``render_block_markup``  2.8 µs/block · full 5k pass = 14 ms
``LiveTail.feed`` call        ~13 µs (paints throttled to ≤30 Hz)
layout frame @ 100 blocks     ~2 ms
layout frame @ 1000 blocks    median ~3 ms  · mean ~7 ms
layout frame @ 2000 blocks    median ~7 ms  · mean ~13 ms
layout frame @ 5000 blocks    median ~33 ms · mean ~63 ms  ← BUDGET MISS
===========================  =========================================

Verdict: the pure renderer and the live-tail throttle are far inside
budget; the miss at 5k is Textual's compositor arranging every mounted
``BlockWidget`` each frame (O(n) ``_arrange_root``/``add_widget``). That
is exactly the ADR's escalation trigger — hybrid Line-API history
(widgets for the last ~1000 blocks, consolidated static content beyond)
is required to hold budget at 5k. See ``NOTES-goldens-perf.md``. The 5k
frame test below is ``xfail`` (non-strict): it starts XPASSing the day
the hybrid lands.
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Iterator

import pytest
from textual.app import App, ComposeResult

from amplifier_app_newtui.model.blocks import (
    Answer,
    Narration,
    Segment,
    ToolLine,
    TranscriptBlock,
    TurnRule,
    UserLine,
)
from amplifier_app_newtui.ui.live_tail import LiveTail
from amplifier_app_newtui.ui.themes import DEFAULT_THEME, register_themes, theme_id
from amplifier_app_newtui.ui.transcript import TranscriptView, render_block_markup

FRAME_BUDGET_SECONDS = 0.016
"""ADR-0007: <16ms/frame during streaming."""

SPIKE_BLOCKS = 5_000
RENDER_WIDTH = 80


def synthetic_blocks(count: int, prefix: str = "p") -> Iterator[TranscriptBlock]:
    """A deterministic 5-kind mix of history blocks (no working_status —
    it carries a spinner timer and never persists to history)."""
    for i in range(count):
        block_id = f"{prefix}{i}"
        match i % 5:
            case 0:
                yield UserLine(id=block_id, text=f"prompt {i}: do the thing", mode="build")
            case 1:
                yield Narration(
                    id=block_id, text=f"Working on step {i} of the synthetic transcript."
                )
            case 2:
                yield ToolLine(
                    id=block_id,
                    summary=f"Ran {i % 7 + 1} shell commands",
                    body=(f"$ synthetic command {i}",),
                    status="completed",
                )
            case 3:
                yield Answer(
                    id=block_id,
                    spans=(
                        Segment(text=f"Answer {i}: the fix is "),
                        Segment(text=f"module_{i}.py", style_token="teal"),
                    ),
                )
            case _:
                yield TurnRule(
                    id=block_id,
                    checkpoint_id=f"t{i}",
                    label=f"{i % 9 + 1}s · 4.2k tok · $0.05 · answer",
                )


class Harness(App[None]):
    """Minimal host: one TranscriptView + one LiveTail, themed."""

    def __init__(self) -> None:
        super().__init__()
        register_themes(self)

    def on_mount(self) -> None:
        self.theme = theme_id(DEFAULT_THEME)

    def compose(self) -> ComposeResult:
        yield TranscriptView(id="transcript")
        yield LiveTail(id="tail")


def _report(label: str, seconds_per_step: list[float]) -> tuple[float, float]:
    mean = statistics.mean(seconds_per_step)
    median = statistics.median(seconds_per_step)
    print(
        f"[perf] {label}: mean={mean * 1000:.2f}ms median={median * 1000:.2f}ms "
        f"max={max(seconds_per_step) * 1000:.2f}ms over {len(seconds_per_step)} steps"
    )
    return mean, median


# --------------------------------------------------------------------------
# Pure renderer: render_block is not the bottleneck at any width/size
# --------------------------------------------------------------------------


def test_pure_render_5k_blocks_under_budget() -> None:
    """Full 5k markup render (the reflow flush's render work) is cheap.

    Measured: 2.8µs/block, 14ms for the full pass — a whole-history
    re-render costs less than one frame budget. Asserted with ~15x CI
    margin.
    """
    blocks = list(synthetic_blocks(SPIKE_BLOCKS))
    start = time.perf_counter()
    for block in blocks:
        render_block_markup(block, RENDER_WIDTH)
    total = time.perf_counter() - start
    per_block = total / len(blocks)
    print(
        f"[perf] pure render 5k @ w{RENDER_WIDTH}: total={total * 1000:.1f}ms "
        f"per-block={per_block * 1e6:.1f}µs"
    )
    assert total < 0.25, f"5k-block render pass took {total * 1000:.1f}ms (budget 250ms)"
    assert per_block < 0.001, f"per-block render {per_block * 1e6:.1f}µs (budget 1ms)"


# --------------------------------------------------------------------------
# Streaming deltas: feed cost + 30Hz throttle
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_delta_feed_under_budget_and_throttled() -> None:
    """600 deltas: each ``feed`` is O(delta) (measured ~13µs) and paints
    coalesce to the 30Hz throttle instead of one paint per delta."""
    app = Harness()
    async with app.run_test(size=(100, 30)) as pilot:
        tail = app.query_one("#tail", LiveTail)
        tail.open_stream()
        feed_times: list[float] = []
        for i in range(600):
            start = time.perf_counter()
            tail.feed(f"delta {i} lorem ipsum dolor sit amet ")
            feed_times.append(time.perf_counter() - start)
            if i % 20 == 19:
                await pilot.pause()
        mean, _median = _report("LiveTail.feed (600 deltas)", feed_times)
        assert mean < 0.002, f"mean feed {mean * 1000:.2f}ms (budget 2ms)"
        assert max(feed_times) < FRAME_BUDGET_SECONDS
        # Throttle: 600 deltas must coalesce, not paint 1:1.
        print(f"[perf] LiveTail paints for 600 deltas: {tail.paint_count}")
        assert tail.paint_count <= 600 / 5, "throttle failed: ~one paint per delta"


# --------------------------------------------------------------------------
# Frame cost with a mounted history (the actual spike)
# --------------------------------------------------------------------------


async def _measure_frames(history: int, samples: int = 10) -> list[float]:
    """Mount ``history`` blocks, then time a full layout frame after each
    of ``samples`` appends — the compositor work a streaming paint pays."""
    app = Harness()
    frames: list[float] = []
    async with app.run_test(size=(100, 30)) as pilot:
        view = app.query_one("#transcript", TranscriptView)
        for block in synthetic_blocks(history):
            view.append(block)
        await pilot.pause()
        for block in synthetic_blocks(samples, prefix="x"):
            view.append(block)
            await pilot.pause()
            start = time.perf_counter()
            app.screen._refresh_layout()  # noqa: SLF001 — timing the real frame path
            frames.append(time.perf_counter() - start)
    return frames


@pytest.mark.asyncio
async def test_append_frame_budget_with_1k_history() -> None:
    """At 1k mounted blocks the budget holds comfortably (median ~3ms
    measured; asserted against the 16ms budget with median to dodge GC
    spikes). Guards against renderer/widget regressions at realistic
    session sizes."""
    frames = await _measure_frames(1_000)
    _mean, median = _report("layout frame @ 1k history", frames)
    assert median < FRAME_BUDGET_SECONDS, (
        f"median frame {median * 1000:.2f}ms at 1k blocks exceeds "
        f"{FRAME_BUDGET_SECONDS * 1000:.0f}ms budget"
    )


@pytest.mark.asyncio
@pytest.mark.xfail(
    reason=(
        "ADR-0007 escalation trigger: widget-per-block misses the <16ms frame "
        "budget at 5k mounted blocks (median ~33ms measured — Textual compositor "
        "arranges every child per frame). Hybrid Line-API history required; "
        "see NOTES-goldens-perf.md. Non-strict: XPASSes when the hybrid lands."
    ),
    strict=False,
)
async def test_append_frame_budget_with_5k_history() -> None:
    """THE spike: 5k synthetic blocks + streaming appends vs frame budget."""
    frames = await _measure_frames(SPIKE_BLOCKS, samples=8)
    _mean, median = _report("layout frame @ 5k history", frames)
    assert median < FRAME_BUDGET_SECONDS, (
        f"median frame {median * 1000:.2f}ms at 5k blocks exceeds "
        f"{FRAME_BUDGET_SECONDS * 1000:.0f}ms budget"
    )
