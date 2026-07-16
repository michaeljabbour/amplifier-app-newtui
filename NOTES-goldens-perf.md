# NOTES — goldens & perf spike (for the integrator / transcript owner)

## Perf spike verdict: 5k blocks MISSES the <16ms frame budget — ADR-0007 escalation triggered

`tests/test_perf_spike.py` ran the ADR-0007 spike (5k synthetic blocks +
streaming deltas). Measured on Apple Silicon, textual 8.2, Python 3.12,
100x30 headless (`App.run_test`):

| measurement | result |
| --- | --- |
| pure `render_block_markup`, 5k blocks @ w80 | **2.8–3.0 µs/block · 14–15 ms full pass** |
| `LiveTail.feed` per delta (600 deltas) | **~13–20 µs**, max 1.4 ms |
| LiveTail paints for 600 deltas | **36** (30 Hz throttle works) |
| full layout frame @ 100 mounted blocks | ~2 ms |
| full layout frame @ 1000 mounted blocks | median ~3 ms · mean ~4–7 ms |
| full layout frame @ 2000 mounted blocks | median ~7 ms · mean ~13 ms |
| full layout frame @ 5000 mounted blocks | **median ~33–36 ms · mean ~59–63 ms — MISS** |

## Where the time goes

NOT the renderer: `render_block` is a pure function at ~3 µs/block; a
whole-history re-render (the reflow flush) costs under one frame budget.
The miss is Textual's compositor: every streaming paint triggers
`Screen._refresh_layout` → `_arrange_root`/`add_widget` over **every**
mounted `BlockWidget` (profiled: ~110k `add_widget` calls across 23
arranges at 5k children; `layouts/vertical.arrange` dominates). Cost is
O(mounted widgets) per frame, so nothing in `ui/transcript.py`'s render
path can fix it — this is exactly the escalation ADR-0007 open-q 6 /
RESEARCH-BRIEF §risk 1 anticipated.

## Recommended change (owner: ui/transcript.py)

Hybrid history per the ADR fallback:

- Keep `BlockWidget`-per-block for the **most recent ~1000 blocks**
  (budget holds with ~5x headroom at 1k; even 2k is borderline-OK).
- Beyond that, consolidate older blocks into a single static Line-API
  region (one widget painting pre-rendered `render_block_markup` output,
  or a `ScrollView`+Line API strip). `render_block(block, width)` is
  already pure and fast enough to re-render the whole consolidated
  region on reflow (14 ms / 5k blocks).
- Interactivity loss beyond the threshold (tool expand, click routing)
  is acceptable per ADR; rewind/lane focus rebuild the view anyway.
- No change needed to `LiveTail` (throttle verified) or to `render_block`.

When the hybrid lands, `tests/test_perf_spike.py::
test_append_frame_budget_with_5k_history` (currently `xfail`,
non-strict) will start XPASSing — flip it to a hard assertion then.

## Goldens

- `tests/goldens/transcript_w{40,80,97,120}.txt` pin the markup rendering
  (text + `$token` styles) of one block of every `TranscriptBlock` kind,
  built from `kernel/demo.py` seed strings.
- Regenerate after an intentional renderer change:
  `uv run python tests/goldens/regen.py` — then review the diff.
- `tests/test_golden_widths.py::test_canonical_set_covers_every_block_kind`
  fails if a new block kind is added to `_RENDERERS` without extending the
  canonical set — extend `tests/goldens/regen.py::canonical_blocks` and
  regenerate.

## Contract observations (no action strictly required)

- `TurnTelemetry.suffix()` renders whole seconds as `3.0s`/`8.0s` inside
  plan headers and the working line; the mockup shows `8s`. If that is a
  spec deviation it is now pinned by the goldens — fix in
  `model/turn.py::_format_elapsed` and regenerate goldens.
- `BlockWidget.repaint_block` falls back to width 80 pre-layout; goldens
  cover 40/97/120 so narrow-terminal first paint is corrected only after
  the first real Resize — already handled, just noting it is load-bearing.
