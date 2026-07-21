# Ambient Progress — Phase 3: Lane Live Tail + Spec Documentation

> **For execution:** Use /execute-plan mode or the subagent-driven-development recipe.

**Source design:** `docs/plans/2026-07-21-ambient-progress-design.md` (D4 + Phase 3 section — read it first).
**Assumes merged:** Phase 1 (`PlanPanel`, `plan_changed()` on `ReducerHost`) and Phase 2 (`DelegateSummaryBlock`).
**Branch:** `agent/anchors-migration`.

## Goal

During a multi-agent fan-out, when the root model is silent, the transcript goes dark.
Phase 3 makes the `LiveTail` widget show the **focused lane's** stream instead: up to 3 dim
`┆`-guttered lines of the sub-agent's live prose, repainted at most every ~0.05s. The root
stream **always** preempts instantly. Tail content is **ephemeral** — it never becomes a
transcript block (durable child prose lives in the lane's own transcript; durable root prose
arrives via Channel B). A `▸` marker in the LanesPanel shows which lane is being tailed, and
**ctrl+o** cycles the tail among running lanes. Finally, `docs/DESIGN-SPEC.md` and
`docs/USER-GUIDE.md` are updated to document the whole Ambient Progress feature (Phases 1–3).

```
│  ● 3 delegates running…                                                          │
│                                                                                  │
│  ┆ …the queue bridge normalizes delegate lifecycle events at a single            │
│  ┆ boundary, so the lanes are fed from the same UIEvent union as the             │
│  ┆ transcript — checking trackers/task_status.py next                            │
├────────────────────────────────────────────┬─────────────────────────────────────┤
│ Lanes                                      │ Plan                            2/4 │
│ ◐ zen-architect ▸   ADR-0007.md      00:09 │ ▶ assess architecture               │
```

## Architecture

Strict 4-layer rule (`ui → model → kernel`, ADR-0007). **No kernel changes except
`kernel/demo.py`** (demo script content, not contract). Do **NOT** touch `kernel/events.py`
(no new event kinds needed) or `kernel/trackers/stream_status.py` (root-only by design —
"child streams stay dark", stream_status.py:10-11; lanes light them up in the UI layer).

Data flow (all pieces verified against the current tree):

```
child StreamBlockDelta (session_id = sub-session)
  └─ reducer.handle → _is_foreign_turn_event (reducer.py:519-550)  [existing]
       └─ _track_child_activity (reducer.py:552-615)               [existing — keeps activity labels]
            └─ NEW _lane_tail_delta:
                 buffer per lane (cap 2 000 chars)
                 lanes.note_stream_activity(sid)      ── model: most-recent tracking
                 if root idle AND lane == lanes.tail_lane:
                    throttle 0.05s (LANE_TAIL_NOTIFY_SECONDS,
                                    mirrors _DELTA_NOTIFY_SECONDS, stream_status.py:35)
                    host.lane_tail_updated(buffer)    ── ReducerHost (NEW method)
                         └─ app.live_tail.show_lane_tail(text)   ── LiveTail lane mode (NEW)

root StreamBlockStart → reducer sets _root_streaming, host.lane_tail_cleared()
                        + app.stream_opened → live_tail.open_stream() clears lane mode
AgentCompleted / PromptComplete → tail buffers dropped, display cleared  (ephemeral)
ctrl+o → app.action_cycle_tail → lanes.cycle_tail_focus() → ▸ moves in LanesPanel
```

Key decisions (from design doc D4 + D6):

- **Accumulate-then-notify, not delta-forwarding.** The reducer buffers each lane's text and
  sends the *whole buffer* on each (throttled) update — throttling drops paints, never text.
  Same shape as `StreamStatusTracker._on_delta` (stream_status.py:168-184).
- **Focused lane lives in `LaneRegistry`** (`model/lanes.py`) — the registry is already the
  shared mutable lane store (one per app, used by both reducer and app), so tail focus joins
  it. Default = most-recently-streaming running lane; explicit ctrl+o choice wins while that
  lane still runs.
- **Reducer mutates only via `ReducerHost`** (reducer.py:297-314): two new protocol methods,
  `lane_tail_updated(text)` and `lane_tail_cleared()`. The reducer never touches widgets.
- **DemoRuntime emits identical typed events** — `run_agents_turn` (demo.py:1197-1228) gains
  child-session `StreamBlock*` bursts so the tail is fully scriptable offline.
- **Goldens:** Phase 3 changes no `ui/transcript.py` block renderer, so no golden regen is
  expected. If you end up touching any renderer, run `uv run python tests/goldens/regen.py`
  and commit the regenerated goldens **in the same commit**.

Verified reference points (pre-Phase-1/2 tree — if Phases 1–2 shifted line numbers, anchor on
the quoted symbols/strings, not the numbers):

| What | Where |
|---|---|
| `_DELTA_NOTIFY_SECONDS = 0.05` | `src/amplifier_app_newtui/kernel/trackers/stream_status.py:35` |
| Child `Stream*` diversion | `ui/reducer.py:519-550` (`_is_foreign_turn_event`) |
| Child activity labels (KEEP) | `ui/reducer.py:552-615` (`_track_child_activity`, `StreamBlockDelta` arm at 597-598) |
| `ReducerHost` protocol | `ui/reducer.py:297-314` |
| App host impl (`stream_opened/delta/closed`, Channel-B comment) | `ui/app.py:646-660` |
| `lanes_changed` → panel | `ui/app.py:619-631` |
| `LiveTail` widget (`open_stream`/`feed`/`consolidate`) | `ui/live_tail.py:321-495`; composed as `#live-tail` at `app.py:180,194` |
| `LaneRegistry` (`active`, `_resolve_id`, `complete`) | `model/lanes.py:116-331` |
| Keymap table + taken chords | `ui/keymap.py:105-157` — taken: ctrl+t/l/y/r/p/j/d/c/v (+ctrl+q quit). **ctrl+o is free.** |
| `_GLOBAL_ACTIONS` → app bindings | `ui/app_support.py:49-58`, `global_bindings()` 87-107 |
| `LANES_HEADER_HINT` exact string | `ui/lanes_panel.py:38`; asserted verbatim in `tests/test_flow_lanes.py:67` |
| Demo agents turn / env / lanes | `kernel/demo.py:1197-1228`, `_env` 767-774, `_text` 801-816, `DemoLane` 297-318 |
| Test patterns | `FakeHost` (`tests/test_ui_reducer_steer_turns.py:22-76`), `TailHarness` (`tests/test_ui_transcript_live_tail.py:27-44`), `GatedDemoAdapter` (`tests/test_flow_helpers.py`), SVG snapshot (`tests/test_ui_snapshots.py`) |

## Tech Stack

- Python 3.12, `uv` for everything (`uv run pytest -q`, `uv run ruff check .`, `uv run pyright src/`)
- Textual ~8.2 (widgets, Pilot tests, `take_svg_screenshot`), pydantic v2 models
- pytest + pytest-asyncio; all tests offline (DemoRuntime / fake hosts / fake clocks)
- Commit after every green task: `git add -A && git commit -m "<msg>"`

## Out of scope (do NOT build)

Per-lane simultaneous mini-tails, sub-agent todo merging, plan-panel changes, lane drill-down
changes, anything in `kernel/events.py` or `kernel/trackers/`.

---

## Task 1 — `LaneRegistry` tail focus (model layer)

**Files:** `src/amplifier_app_newtui/model/lanes.py`, `tests/test_model_turn_queues_lanes.py`

**1a. Write the failing test.** Append to `tests/test_model_turn_queues_lanes.py` (match its
existing imports — it already imports `LaneRegistry`; add the import if missing):

```python
# -- lane tail focus (DESIGN-SPEC §8: live tail) -------------------------------


def test_tail_lane_defaults_to_first_running_then_most_recent_stream() -> None:
    lanes = LaneRegistry()
    assert lanes.tail_lane is None
    lanes.register("s1", parent_id="root", name="researcher")
    lanes.register("s2", parent_id="root", name="coder")
    tailed = lanes.tail_lane
    assert tailed is not None and tailed.session_id == "s1"  # fallback: first running
    lanes.note_stream_activity("s2")
    tailed = lanes.tail_lane
    assert tailed is not None and tailed.session_id == "s2"  # most recent stream wins


def test_cycle_tail_focus_pins_and_falls_back_when_lane_completes() -> None:
    lanes = LaneRegistry()
    lanes.register("s1", parent_id="root", name="researcher")
    lanes.register("s2", parent_id="root", name="coder")
    lanes.note_stream_activity("s2")
    pinned = lanes.cycle_tail_focus()  # from s2 → next running lane: s1
    assert pinned is not None and pinned.session_id == "s1"
    lanes.note_stream_activity("s2")  # recent changes, but the pin holds
    tailed = lanes.tail_lane
    assert tailed is not None and tailed.session_id == "s1"
    lanes.complete("s1")  # pinned lane done → falls back to most recent
    tailed = lanes.tail_lane
    assert tailed is not None and tailed.session_id == "s2"
    lanes.complete("s2")
    assert lanes.tail_lane is None
    assert lanes.cycle_tail_focus() is None


def test_note_stream_activity_ignores_done_and_unknown_lanes() -> None:
    lanes = LaneRegistry()
    lanes.register("s1", parent_id="root", name="researcher")
    lanes.note_stream_activity("never-registered")  # dropped, not fatal
    lanes.complete("s1")
    lanes.note_stream_activity("s1")  # done lanes never become the tail
    assert lanes.tail_lane is None
```

**1b. Run — expect FAIL:**

```
uv run pytest tests/test_model_turn_queues_lanes.py -q
```

Expected: the 3 new tests fail with `AttributeError: 'LaneRegistry' object has no attribute 'tail_lane'`.

**1c. Implement.** In `model/lanes.py`:

Add to `LaneRegistry.__init__` (after `self._pending_sessions: dict[str, str | None] = {}`,
line 130):

```python
        self._tail_focus: str | None = None
        self._tail_recent: str | None = None
```

Add these methods after `complete()` (after line 284), matching the file's docstring style:

```python
    # -- lane tail focus (DESIGN-SPEC §8: live tail) ------------------------

    @property
    def tail_lane(self) -> LaneRecord | None:
        """The lane whose stream feeds the live tail.

        An explicit ctrl-o choice wins while that lane still runs; then the
        most-recently-streaming running lane; then the first running lane.
        None when nothing is running (the tail goes dark).
        """
        for candidate in (self._tail_focus, self._tail_recent):
            if candidate is None:
                continue
            key = self._resolve_id(candidate)
            record = self._records.get(key) if key is not None else None
            if record is not None and record.lane.state != "done":
                return record
        active = self.active
        return active[0] if active else None

    def note_stream_activity(self, session_id: str) -> None:
        """Record *session_id* as the most-recently-streaming lane.

        Unknown or finished lanes are dropped, not fatal (same tolerance
        as :meth:`update`).
        """
        key = self._resolve_id(session_id)
        record = self._records.get(key) if key is not None else None
        if record is not None and record.lane.state != "done":
            self._tail_recent = key

    def cycle_tail_focus(self) -> LaneRecord | None:
        """Pin the tail to the next running lane (ctrl-o), in lane order."""
        active = self.active
        if not active:
            self._tail_focus = None
            return None
        ids = [record.session_id for record in active]
        current = self.tail_lane
        if current is not None and current.session_id in ids:
            index = (ids.index(current.session_id) + 1) % len(ids)
        else:
            index = 0
        self._tail_focus = ids[index]
        return self._records[ids[index]]
```

**1d. Run — expect PASS:** `uv run pytest tests/test_model_turn_queues_lanes.py -q` → all pass.

**1e. Commit:**

```
git add -A && git commit -m "model: LaneRegistry tail focus — most-recent default, ctrl-o pin, done-lane fallback"
```

---

## Task 2 — `LiveTail` lane mode (dim `┆` gutter, 3-line cap, root wins)

**Files:** `src/amplifier_app_newtui/ui/live_tail.py`, `tests/test_ui_transcript_live_tail.py`

**2a. Write the failing tests.** Append to `tests/test_ui_transcript_live_tail.py` (it already
has `TailHarness`, `_tail`, and `pytest`; add `lane_tail_markup` to the existing
`from amplifier_app_newtui.ui.live_tail import (...)` block):

```python
# -- lane mode (design doc D4: focused-lane live tail) --------------------------


def test_lane_tail_markup_gutters_dims_and_caps_at_three_lines() -> None:
    markup = lane_tail_markup("one\ntwo\nthree\nfour\n")
    assert markup == "[$dim]┆ two\n┆ three\n┆ four[/]"


def test_lane_tail_markup_escapes_and_handles_empty() -> None:
    assert lane_tail_markup("") == ""
    assert lane_tail_markup("   \n") == ""
    markup = lane_tail_markup("[red]not markup")
    assert markup.startswith("[$dim]")
    assert "┆ \\[red]not markup" in markup  # escaped — content is never interpreted


@pytest.mark.asyncio
async def test_lane_mode_yields_to_root_stream_and_clears() -> None:
    app = TailHarness()
    async with app.run_test():
        tail = _tail(app)
        tail.show_lane_tail("agent prose")
        assert tail.lane_mode
        tail.open_stream("text")  # root preempts instantly
        assert not tail.lane_mode
        tail.show_lane_tail("ignored while root streams")
        assert not tail.lane_mode  # refused: root owns the tail
        tail.feed("root text")
        tail.consolidate("blk-1")  # root stream closed
        tail.show_lane_tail("agent prose again")
        assert tail.lane_mode  # lanes may resume after the root goes idle
        tail.clear_lane_tail()
        assert not tail.lane_mode
```

**2b. Run — expect FAIL** (`ImportError: cannot import name 'lane_tail_markup'`):

```
uv run pytest tests/test_ui_transcript_live_tail.py -q
```

**2c. Implement.** In `ui/live_tail.py`:

Add a module constant next to `THROTTLE_SECONDS` (line 36):

```python
LANE_TAIL_LINES = 3
"""Max painted lines of a focused lane's live tail (design doc D4)."""
```

Add a pure helper just above `class LiveTail` (line 321):

```python
def lane_tail_markup(text: str) -> str:
    """Markup for a focused lane's tail: the last :data:`LANE_TAIL_LINES`
    non-blank lines, ``┆``-guttered, dim (DESIGN-SPEC §8). Pure function —
    unit-testable without a widget; content is escaped, never interpreted.
    """
    from textual.markup import escape

    lines = [line for line in text.split("\n") if line.strip()][-LANE_TAIL_LINES:]
    if not lines:
        return ""
    body = "\n".join(f"┆ {escape(line)}" for line in lines)
    return f"[$dim]{body}[/]"
```

In `LiveTail.__init__` (after `self._async_render_active = False`, line 357):

```python
        self._lane_mode = False
        self._root_open = False
```

In `open_stream` (line 378), add as the FIRST two lines of the body:

```python
        self._lane_mode = False  # root always preempts the lane tail (D4)
        self._root_open = True
```

In `consolidate` (line 401), add `self._root_open = False` right after the
`self._cancel_timer()` line.

Add new public API after `consolidate` / before `attach_evidence` (line 419):

```python
    @property
    def lane_mode(self) -> bool:
        """True while the tail shows a focused lane's stream, not the root's."""
        return self._lane_mode

    def show_lane_tail(self, text: str) -> None:
        """Paint the focused lane's accumulated tail (dim, ``┆``-guttered).

        The root always preempts: refused while a root stream is open. The
        reducer owns accumulation and the ~0.05s throttle
        (``LANE_TAIL_NOTIFY_SECONDS``); this widget just paints the last
        :data:`LANE_TAIL_LINES` lines. Lane content is ephemeral — it is
        never consolidated into a transcript block.
        """
        if self._root_open:
            return
        self._lane_mode = True
        self.update(lane_tail_markup(text))

    def clear_lane_tail(self) -> None:
        """Drop the lane tail (root preemption / lane done / turn end)."""
        if not self._lane_mode:
            return
        self._lane_mode = False
        self.update("")
```

Add `"LANE_TAIL_LINES"` and `"lane_tail_markup"` to `__all__` (keep it sorted).

**2d. Run — expect PASS:** `uv run pytest tests/test_ui_transcript_live_tail.py -q` → all pass.

**2e. Commit:**

```
git add -A && git commit -m "ui: LiveTail lane mode — dim ┆-guttered 3-line tail, root stream always preempts"
```

---

## Task 3 — Reducer: tail routing, focus, throttle (new `ReducerHost` methods)

**Files:** `src/amplifier_app_newtui/ui/reducer.py`, new `tests/test_ui_reducer_lane_tail.py`

**3a. Write the failing tests.** Create `tests/test_ui_reducer_lane_tail.py`:

> NOTE: event constructor kwargs below follow `kernel/events.py` (envelope fields
> `session_id`/`ts` have defaults; `StreamBlockDelta` carries
> `request_id, block_index, block_type, sequence, text` — see events.py:65-97). If pydantic
> rejects a missing required field, read the model in `kernel/events.py` and supply it —
> do NOT change `kernel/events.py`.

```python
"""Lane live tail: focused-lane child deltas → ReducerHost (design doc D4).

Offline unit tests with a fake host + fake clock: buffering, focus
selection, ctrl-o pinning, the 0.05s repaint throttle, root-stream
preemption, and ephemerality (cleared on lane completion / turn end;
never a transcript block).
"""

from __future__ import annotations

from amplifier_app_newtui.kernel import events as ev
from amplifier_app_newtui.model.blocks import BlockIdAllocator, TranscriptBlock
from amplifier_app_newtui.model.lanes import LaneRegistry
from amplifier_app_newtui.model.turn import OutcomeLedger
from amplifier_app_newtui.ui.reducer import LANE_TAIL_NOTIFY_SECONDS, TranscriptReducer

ROOT = "root-session"
CHILD_A = "child-aaaaaaaaaaaaaaaa"
CHILD_B = "child-bbbbbbbbbbbbbbbb"


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class FakeHost:
    """Minimal ReducerHost recording lane-tail traffic + appended blocks."""

    mode_id = "auto"

    def __init__(self) -> None:
        self.blocks: list[TranscriptBlock] = []
        self.tail_updates: list[str] = []
        self.tail_cleared = 0

    def append_block(self, block: TranscriptBlock) -> None:
        self.blocks.append(block)

    def replace_block(self, block: TranscriptBlock) -> None: ...
    def remove_block(self, block_id: str) -> None: ...
    def show_notice(self, text: str) -> None: ...
    def set_mode_by_id(self, mode_id: str, *, notify: bool = True) -> None: ...
    def turn_started(self) -> None: ...
    def turn_finished(self) -> None: ...
    def lanes_changed(self) -> None: ...
    def plan_changed(self) -> None: ...  # Phase 1
    def approval_opened(self, prompt: str, options: tuple[str, ...]) -> None: ...
    def decision_deferred(self, message: str) -> None: ...
    def stream_opened(self, block_type: str) -> None: ...
    def stream_delta(self, text: str) -> None: ...
    def stream_closed(self) -> None: ...

    def lane_tail_updated(self, text: str) -> None:
        self.tail_updates.append(text)

    def lane_tail_cleared(self) -> None:
        self.tail_cleared += 1


def make() -> tuple[TranscriptReducer, FakeHost, FakeClock]:
    host = FakeHost()
    clock = FakeClock()
    reducer = TranscriptReducer(
        host,
        allocator=BlockIdAllocator(),
        ledger=OutcomeLedger(),
        lanes=LaneRegistry(),
        tail_clock=clock,
    )
    reducer.handle(ev.PromptSubmit(prompt="fan out", ts=1.0, session_id=ROOT))
    return reducer, host, clock


def spawn(reducer: TranscriptReducer, sub: str, name: str) -> None:
    reducer.handle(
        ev.AgentSpawned(
            session_id=ROOT,
            ts=1.0,
            agent=name,
            sub_session_id=sub,
            parent_session_id=ROOT,
        )
    )


def delta(
    reducer: TranscriptReducer, sub: str, text: str, *, block_type: str = "text"
) -> None:
    reducer.handle(
        ev.StreamBlockDelta(
            session_id=sub,
            request_id=f"req-{sub}",
            block_index=0,
            block_type=block_type,
            sequence=0,
            text=text,
        )
    )


def test_child_text_delta_paints_the_accumulated_buffer() -> None:
    reducer, host, clock = make()
    spawn(reducer, CHILD_A, "researcher")
    delta(reducer, CHILD_A, "reading the ")
    clock.now += LANE_TAIL_NOTIFY_SECONDS
    delta(reducer, CHILD_A, "queue bridge")
    assert host.tail_updates == ["reading the ", "reading the queue bridge"]


def test_thinking_deltas_never_reach_the_tail() -> None:
    reducer, host, _ = make()
    spawn(reducer, CHILD_A, "researcher")
    delta(reducer, CHILD_A, "hmm", block_type="thinking")
    assert host.tail_updates == []


def test_deltas_within_the_notify_window_coalesce_without_losing_text() -> None:
    reducer, host, clock = make()
    spawn(reducer, CHILD_A, "researcher")
    delta(reducer, CHILD_A, "one ")
    delta(reducer, CHILD_A, "two ")  # same clock instant — paint throttled
    assert host.tail_updates == ["one "]
    clock.now += LANE_TAIL_NOTIFY_SECONDS
    delta(reducer, CHILD_A, "three")
    assert host.tail_updates == ["one ", "one two three"]  # nothing lost


def test_focus_follows_the_most_recently_streaming_lane() -> None:
    reducer, host, clock = make()
    spawn(reducer, CHILD_A, "researcher")
    spawn(reducer, CHILD_B, "coder")
    delta(reducer, CHILD_A, "aaa")
    clock.now += LANE_TAIL_NOTIFY_SECONDS
    delta(reducer, CHILD_B, "bbb")
    assert host.tail_updates == ["aaa", "bbb"]
    tailed = reducer.lanes.tail_lane
    assert tailed is not None and tailed.session_id == CHILD_B


def test_explicit_cycle_pin_wins_over_recent_activity() -> None:
    reducer, host, clock = make()
    spawn(reducer, CHILD_A, "researcher")
    spawn(reducer, CHILD_B, "coder")
    delta(reducer, CHILD_A, "aaa")
    pinned = reducer.lanes.cycle_tail_focus()  # A (current) → B
    assert pinned is not None and pinned.session_id == CHILD_B
    clock.now += LANE_TAIL_NOTIFY_SECONDS
    delta(reducer, CHILD_A, "more-a")  # not focused: buffered, not painted
    clock.now += LANE_TAIL_NOTIFY_SECONDS
    delta(reducer, CHILD_B, "bbb")
    assert host.tail_updates == ["aaa", "bbb"]
```

**3b. Run — expect FAIL:**

```
uv run pytest tests/test_ui_reducer_lane_tail.py -q
```

Expected: `ImportError: cannot import name 'LANE_TAIL_NOTIFY_SECONDS'` (then, as you
implement, `TypeError: ... unexpected keyword argument 'tail_clock'`).

**3c. Implement.** In `ui/reducer.py`:

1. Module constants, after `_CHARS_PER_TOKEN = 4` (line 60):

```python
LANE_TAIL_NOTIFY_SECONDS = 0.05
"""Lane-tail repaint floor — mirrors ``_DELTA_NOTIFY_SECONDS`` in
``kernel/trackers/stream_status.py``. The per-lane buffer accumulates
between paints, so throttling drops paints — never text."""

_LANE_TAIL_MAX_CHARS = 2_000
"""Per-lane tail buffer cap; the widget paints only the last 3 lines."""
```

2. Import `LaneRecord`: change `from ..model.lanes import LaneRegistry, LaneStateName`
(line 50) to `from ..model.lanes import LaneRecord, LaneRegistry, LaneStateName`.

3. Extend the `ReducerHost` protocol (reducer.py:297-314) — add after `stream_closed`
(and after Phase 1's `plan_changed` if present):

```python
    def lane_tail_updated(self, text: str) -> None: ...
    def lane_tail_cleared(self) -> None: ...
```

4. Constructor: add keyword `tail_clock: Any = None` after
`session_cost_start: Decimal = Decimal("0")` (line 383), and add state after
`self._tree_order: list[str] = []` (line 414):

```python
        self._tail_clock = tail_clock or time.monotonic
        self._lane_tails: dict[str, str] = {}
        self._lane_tail_last = 0.0
        self._lane_tail_shown: str | None = None
        self._root_streaming = False
```

5. Route child text deltas. In `_track_child_activity` (reducer.py:597-598), change:

```python
            case ev.StreamBlockDelta():
                activity = "thinking" if event.block_type == "thinking" else "writing response"
```

to:

```python
            case ev.StreamBlockDelta():
                activity = "thinking" if event.block_type == "thinking" else "writing response"
                self._lane_tail_delta(record, event)
```

(The activity-label behavior is deliberately KEPT — the tail is additive.)

6. New private methods, placed right after `_track_child_activity` (before
`_record_change`, line 617):

```python
    # -- lane live tail (DESIGN-SPEC §8, design doc D4) ---------------------

    def _lane_tail_delta(self, record: LaneRecord, event: ev.StreamBlockDelta) -> None:
        """Buffer a child text delta; repaint the focused lane's tail.

        Accumulate-then-notify (the ``StreamStatusTracker._on_delta``
        shape): the host is repainted with the whole buffer at most every
        ``LANE_TAIL_NOTIFY_SECONDS``, so throttling drops paints, never
        text. The root stream always preempts; thinking blocks stay dark.
        """
        if event.block_type not in ("", "text"):
            return
        if event.text:
            buffered = self._lane_tails.get(record.session_id, "") + event.text
            self._lane_tails[record.session_id] = buffered[-_LANE_TAIL_MAX_CHARS:]
        self.lanes.note_stream_activity(record.session_id)
        if self._root_streaming:
            return  # root always preempts (D4)
        focused = self.lanes.tail_lane
        if focused is None or focused.session_id != record.session_id:
            return
        now = self._tail_clock()
        if self._lane_tail_shown == record.session_id and (
            now - self._lane_tail_last < LANE_TAIL_NOTIFY_SECONDS
        ):
            return
        self._lane_tail_last = now
        self._lane_tail_shown = record.session_id
        self._host.lane_tail_updated(self._lane_tails.get(record.session_id, ""))

    def _clear_lane_tail(self, session_id: str | None = None) -> None:
        """Drop lane-tail state: one lane's buffer, or everything.

        Ephemeral by design — tail text never becomes a transcript block
        (durable content arrives via Channel B; see app.py stream_closed).
        """
        if session_id is None:
            self._lane_tails.clear()
        else:
            self._lane_tails.pop(session_id, None)
        if self._lane_tail_shown is not None and (
            session_id is None or self._lane_tail_shown == session_id
        ):
            self._lane_tail_shown = None
            self._host.lane_tail_cleared()
```

**3d. Run — expect PASS:**

```
uv run pytest tests/test_ui_reducer_lane_tail.py -q
```

Expected: `6 passed` — EXCEPT any test exercising root preemption (that's Task 4). All 6
above should pass now. Also run the pre-existing reducer tests (old FakeHosts lack the new
methods but the reducer only calls them on child-delta paths those tests never exercise):

```
uv run pytest tests/test_ui_reducer_steer_turns.py tests/test_ui_reducer_outcomes.py -q
```

**3e. Commit:**

```
git add -A && git commit -m "ui: reducer lane-tail routing — buffered, throttled (0.05s), focus-aware host updates"
```

---

## Task 4 — Reducer: root preemption + ephemerality (clear rules)

**Files:** `src/amplifier_app_newtui/ui/reducer.py`, `tests/test_ui_reducer_lane_tail.py`

**4a. Write the failing tests.** Append to `tests/test_ui_reducer_lane_tail.py`:

```python
def test_root_stream_preempts_clears_and_suppresses_the_tail() -> None:
    reducer, host, clock = make()
    spawn(reducer, CHILD_A, "researcher")
    delta(reducer, CHILD_A, "child text")
    assert host.tail_updates == ["child text"]
    reducer.handle(
        ev.StreamBlockStart(
            session_id=ROOT, request_id="req-root", block_index=0, block_type="text"
        )
    )
    assert host.tail_cleared == 1  # cleared the instant the root speaks
    clock.now += LANE_TAIL_NOTIFY_SECONDS
    delta(reducer, CHILD_A, " while root streams")  # buffered, never painted
    assert host.tail_updates == ["child text"]
    reducer.handle(
        ev.StreamBlockEnd(
            session_id=ROOT, request_id="req-root", block_index=0, block_type="text"
        )
    )
    clock.now += LANE_TAIL_NOTIFY_SECONDS
    delta(reducer, CHILD_A, ", resumes")
    # Preemption DISCARDED the old buffer (ephemeral, D4) — the tail
    # restarts from whatever streamed after the root went idle again.
    assert host.tail_updates[-1] == " while root streams, resumes"


def test_lane_completion_clears_a_shown_tail() -> None:
    reducer, host, _ = make()
    spawn(reducer, CHILD_A, "researcher")
    delta(reducer, CHILD_A, "child text")
    reducer.handle(
        ev.AgentCompleted(
            session_id=ROOT,
            agent="researcher",
            sub_session_id=CHILD_A,
            parent_session_id=ROOT,
            success=True,
            result="3 findings",
        )
    )
    assert host.tail_cleared == 1


def test_turn_end_discards_all_tail_state_and_leaves_no_block_behind() -> None:
    reducer, host, _ = make()
    spawn(reducer, CHILD_A, "researcher")
    delta(reducer, CHILD_A, "ephemeral child prose")
    reducer.handle(ev.PromptComplete(ts=2.0, session_id=ROOT))
    assert host.tail_cleared == 1
    # Ephemeral: the tail text never became transcript content.
    assert not any(
        "ephemeral child prose" in getattr(span, "text", "")
        for block in host.blocks
        for span in getattr(block, "spans", ())
    )
```

**4b. Run — expect FAIL** (`host.tail_cleared == 0`):

```
uv run pytest tests/test_ui_reducer_lane_tail.py -q
```

**4c. Implement.** In `ui/reducer.py`:

1. `handle()` dispatch (reducer.py:471-481) — change the three root-stream arms:

```python
            case ev.StreamBlockStart():
                self._root_streaming = True
                self._clear_lane_tail()
                self._host.stream_opened(event.block_type)
                if event.block_type == "thinking":
                    self.set_activity("thinking")
            case ev.StreamBlockDelta():
                self._host.stream_delta(event.text)
            case ev.StreamBlockEnd():
                self._root_streaming = False
                self._host.stream_closed()
            case ev.StreamAborted():
                self._root_streaming = False
                self._host.stream_closed()
                self._host.show_notice(f"stream aborted · {event.error_message}".rstrip(" ·"))
```

(Only the `_root_streaming` / `_clear_lane_tail` lines are new. These arms are root-only:
child `Stream*` events were already diverted by `_is_foreign_turn_event`.)

2. `_agent_completed` (reducer.py:1349; Phase 2 may have reshaped this method — anchor on
"the method handling `ev.AgentCompleted`" and insert BEFORE the `self.lanes.complete(...)`
call):

```python
        record = self.lanes.get(event.sub_session_id)
        self._clear_lane_tail(
            record.session_id if record is not None else event.sub_session_id
        )
```

3. `_finish_turn` (reducer.py:719) — right after the `if turn is None: return` guard:

```python
        self._clear_lane_tail()
        self._root_streaming = False
```

**4d. Run — expect PASS:** `uv run pytest tests/test_ui_reducer_lane_tail.py -q` → `9 passed`.

**4e. Commit:**

```
git add -A && git commit -m "ui: reducer lane-tail lifecycle — root preempts instantly, tail ephemeral at lane/turn end"
```

---

## Task 5 — App wiring: the two new host methods

**Files:** `src/amplifier_app_newtui/ui/app.py`

No new test in this task (the flow test in Task 9 exercises this wiring end-to-end); the
gate here is pyright + the existing suite.

**5a. Implement.** In `ui/app.py`, add the new `ReducerHost` methods right after
`on_live_tail_consolidated` (app.py:659-660):

```python
    def lane_tail_updated(self, text: str) -> None:
        # Throttle + focus policy live in the reducer (design doc D4);
        # this just paints. LiveTail itself refuses while a root stream
        # is open, so preemption is belt-and-braces.
        self.live_tail.show_lane_tail(text)

    def lane_tail_cleared(self) -> None:
        self.live_tail.clear_lane_tail()
```

**5b. Verify:**

```
uv run pyright src/
```

Expected: `0 errors` (pyright checks the `ReducerHost` protocol is now satisfied by the app).

```
uv run pytest tests/test_flow_lanes.py tests/test_flow_steer_queue.py -q
```

Expected: all pass (behavior unchanged until the demo emits child streams in Task 8).

**5c. Commit:**

```
git add -A && git commit -m "ui: app implements the lane_tail ReducerHost methods"
```

---

## Task 6 — LanesPanel `▸` marker on the tailed lane

**Files:** `src/amplifier_app_newtui/ui/lanes_panel.py`, `tests/test_ui_lanes.py`

**6a. Write the failing test.** Append to `tests/test_ui_lanes.py` (it already imports from
`lanes_panel`; extend the import to include `format_lane_lines` and `LaneState` from
`model.lanes` if not present):

```python
def test_format_lane_lines_marks_the_tailed_lane_and_keeps_alignment() -> None:
    lanes = (
        LaneState.for_state(name="researcher", state="running", activity="scanning docs"),
        LaneState.for_state(name="coder", state="working", activity="migrating store"),
    )
    lines = format_lane_lines(lanes, tailed_index=1)
    assert "coder ▸" in lines[1]
    assert "▸" not in lines[0]
    # The name column still pads to the widest entry (marker included):
    assert lines[0].index(" · ") == lines[1].index(" · ")
    # No marker → identical to today's output shape.
    assert "▸" not in "".join(format_lane_lines(lanes))
```

**6b. Run — expect FAIL** (`TypeError: format_lane_lines() got an unexpected keyword
argument 'tailed_index'`):

```
uv run pytest tests/test_ui_lanes.py -q
```

**6c. Implement.** In `ui/lanes_panel.py`:

1. `format_lane_lines` (lines 59-78) — new signature and name column:

```python
def format_lane_lines(
    lanes: Sequence[LaneState], tailed_index: int | None = None
) -> tuple[str, ...]:
    """Aligned lane lines per Claude Code's live agent panel:
    ``  <glyph> <name> · <activity> · <elapsed> · ↓ Nk tokens · $<cost>``.

    Name, activity, elapsed and token columns are padded to the widest
    entry so every ``·`` separator column lines up (mockup alignment).
    ``tailed_index`` appends the DESIGN-SPEC §8 ``▸`` tail marker to that
    lane's name (inside the padded name column, so alignment holds).
    """
    if not lanes:
        return ()
    names = [
        f"{lane.name} ▸" if index == tailed_index else lane.name
        for index, lane in enumerate(lanes)
    ]
    elapsed = [lane_elapsed(lane.elapsed) for lane in lanes]
    tokens = [f"↓ {_format_tokens(lane.tokens)} tokens" for lane in lanes]
    name_w = max(len(name) for name in names)
    act_w = max(len(lane.activity) for lane in lanes)
    el_w = max(len(text) for text in elapsed)
    tok_w = max(len(text) for text in tokens)
    return tuple(
        f"  {lane.glyph} {names[i]:<{name_w}} · {lane.activity:<{act_w}}"
        f" · {elapsed[i]:<{el_w}} · {tokens[i]:<{tok_w}} · ${lane.cost:.2f}"
        for i, lane in enumerate(lanes)
    )
```

2. `LanesPanel.__init__` (line 215) — add `self._tailed: str | None = None` after
`self._selected = 0`.

3. `update_lanes` (lines 243-248) — new keyword + store:

```python
    def update_lanes(
        self,
        records: Sequence[LaneRecord],
        *,
        tailed_session_id: str | None = None,
    ) -> None:
        """Replace the lane listing (registration order, per LaneRegistry)."""
        self._records = tuple(records)
        self._tailed = tailed_session_id
        self._selected = min(self._selected, max(0, len(self._records) - 1))
        self._sync_motion()
        self._refresh_or_rebuild_rows()
```

4. `lane_lines` property (lines 232-235) — compute the index:

```python
    @property
    def lane_lines(self) -> tuple[str, ...]:
        """The exact aligned lane line strings currently displayed."""
        tailed_index = next(
            (
                index
                for index, record in enumerate(self._records)
                if record.session_id == self._tailed
            ),
            None,
        )
        return format_lane_lines(
            tuple(record.lane for record in self._records), tailed_index
        )
```

(`_LaneRow.render`'s shimmer uses `self.line.find(lane.name)` — the raw name is still a
prefix of `"name ▸"`, so no change needed there.)

5. Now hand the tailed lane to the panel from the app. In `ui/app.py` `lanes_changed`
(app.py:619-620), change:

```python
        self.lanes_panel.update_lanes(self.lanes.lanes)
```

to:

```python
        tailed = self.lanes.tail_lane
        self.lanes_panel.update_lanes(
            self.lanes.lanes,
            tailed_session_id=None if tailed is None else tailed.session_id,
        )
```

**6d. Run — expect PASS:**

```
uv run pytest tests/test_ui_lanes.py tests/test_flow_lanes.py -q
uv run pyright src/
```

**6e. Commit:**

```
git add -A && git commit -m "ui: lanes panel ▸ marker on the tailed lane (alignment-preserving)"
```

---

## Task 7 — ctrl+o binding: cycle the tailed lane

**Files:** `src/amplifier_app_newtui/ui/keymap.py`, `src/amplifier_app_newtui/ui/app_support.py`,
`src/amplifier_app_newtui/ui/app.py`, `src/amplifier_app_newtui/ui/lanes_panel.py`,
`tests/test_ui_keymap.py`, `tests/test_flow_lanes.py`

ctrl+o is verified free: taken chords are ctrl+t/l/y/r/p/j/d/c/v (+Textual's ctrl+q quit).
`keymap.validate()` runs at app boot (app.py:142) and rejects collisions, so a mistake here
fails loudly.

**7a. Write the failing test.** Append to `tests/test_ui_keymap.py`:

```python
def test_cycle_tail_is_bound_to_ctrl_o_everywhere_but_approval() -> None:
    binding = next(b for b in keymap.KEYMAP if b.action == "cycle_tail")
    assert binding.keys == ("ctrl+o",)
    assert binding.contexts == keymap.NO_APPROVAL
```

(Match the file's existing import style — it imports the `keymap` module or names from it;
adapt the reference accordingly.)

**7b. Run — expect FAIL** (`StopIteration`):

```
uv run pytest tests/test_ui_keymap.py -q
```

**7c. Implement.**

1. `ui/keymap.py` — in the `# Panels / pickers.` section (line 117), after `toggle_lanes`:

```python
    _b("cycle_tail", ("ctrl+o",), "ctrl-o", NO_APPROVAL),
```

2. `ui/keymap.py` — the advertised lanes-header hint lives in `lanes_panel.py`, but the
context hints table stays untouched (the footer hint strings are EXACT per DESIGN-SPEC §2 —
the doc task updates §8 instead, where the lanes header is specified).

3. `ui/lanes_panel.py:38` — extend the header hint (this is the discoverability surface):

```python
LANES_HEADER_HINT = "· ↑↓ select · enter focus · ctrl-o tail · esc close"
```

4. `tests/test_flow_lanes.py:67` — update the verbatim assert to match:

```python
        assert LANES_HEADER == "Agent lanes · ↑↓ select · enter focus · ctrl-o tail · esc close"
```

5. `ui/app_support.py:49-58` — add `"cycle_tail"` to `_GLOBAL_ACTIONS` (keeps the
single-source keymap→bindings pipe, `global_bindings()` app_support.py:87-107).

6. `ui/app.py` — add the action next to `action_toggle_lanes` (app.py:1082):

```python
    def action_cycle_tail(self) -> None:
        """ctrl+o: pin the live tail to the next running lane (spec §8)."""
        record = self.lanes.cycle_tail_focus()
        if record is None:
            self.show_notice("no running lanes to tail")
            return
        self.lanes_changed()  # repaints the ▸ marker with the new pin
        self.show_notice(f"tail · {record.lane.name}")
```

**7d. Run — expect PASS:**

```
uv run pytest tests/test_ui_keymap.py tests/test_flow_lanes.py -q
```

(If another test pins the old `LANES_HEADER` string, grep and update it:
`grep -rn "enter focus · esc close" tests/ src/ docs/`.)

**7e. Commit:**

```
git add -A && git commit -m "ui: ctrl+o cycles the tailed lane; lanes header advertises it"
```

---

## Task 8 — Demo: child-session stream bursts in the agents turn

**Files:** `src/amplifier_app_newtui/kernel/demo.py`

The demo must emit the SAME typed events a real runtime produces (ADR-0007) so the tail is
visible offline in `--demo`. Child events differ from root events only in their envelope:
`session_id = lane.sub_session_id`, `parent_id = DEMO_SESSION_ID`.

**8a. Implement.** In `kernel/demo.py`:

1. Add a child-envelope helper right after `_env` (demo.py:767-774):

```python
    def _child_env(self, sub_session_id: str) -> dict[str, Any]:
        """Envelope for a CHILD-session event (lane live tail, spec §8)."""
        self._seq += 1
        return {
            "event_id": f"demo-{self._seq}",
            "session_id": sub_session_id,
            "parent_id": DEMO_SESSION_ID,
            "ts": self.clock,
        }
```

2. Add a lane-stream helper after `_text` (demo.py:801-816):

```python
    async def _lane_stream(self, lane: DemoLane) -> None:
        """One child-session Channel-A text burst — feeds the lane live tail.

        Channel A only: the child's durable record stays in its own
        transcript (lane focus), never the parent's (design doc D4).
        """
        common = {
            "request_id": f"demo-req-{lane.name}",
            "block_index": 0,
            "block_type": "text",
        }
        await self._emit(
            StreamBlockStart(**self._child_env(lane.sub_session_id), **common, name="lane")
        )
        rows = [row for row in lane.log if row.kind in ("narration", "answer")]
        for sequence, row in enumerate(rows):
            await self._emit(
                StreamBlockDelta(
                    **self._child_env(lane.sub_session_id),
                    **common,
                    sequence=sequence,
                    text=row.text + "\n",
                )
            )
        await self._emit(
            StreamBlockEnd(**self._child_env(lane.sub_session_id), **common)
        )
```

(Every `DemoLane.log` — demo.py:338-398 — has at least one narration/answer row, so each
lane produces at least one delta; `tester`'s comes from its `answer` row.)

3. Wire it into `run_agents_turn` (demo.py:1199-1211) — after the `AgentSpawned` loop and
BEFORE the completion loop's first `_wait` (this placement is what makes the
`GatedDemoAdapter` park with the tail already painted — its gate trips on the first
`_wait`, test_flow_helpers.py):

```python
        for lane in DEMO_LANES:
            await self._lane_stream(lane)
```

**8b. Verify.** The demo-shape and flow suites must stay green — child stream events set
lane activity to `writing response`/`reviewing response` mid-turn (pre-existing reducer
behavior at reducer.py:597-600 that the demo simply never triggered before). If
`tests/test_kernel_demo_turns.py`, `tests/test_kernel_demo_data.py`, or
`tests/test_ui_lanes_telemetry.py` assert mid-turn lane activities or exact event
sequences, update those expectations deliberately — that is the intended behavior change.

```
uv run pytest tests/test_kernel_demo_turns.py tests/test_kernel_demo_data.py tests/test_ui_lanes_telemetry.py tests/test_flow_lanes.py -q
```

Expected: all pass (after any deliberate expectation updates).

**8c. Commit:**

```
git add -A && git commit -m "demo: agents turn emits child-session stream bursts for the lane live tail"
```

---

## Task 9 — Flow test: tail visible mid-fan-out, ctrl+o moves ▸, cleared at turn end

**Files:** `tests/test_flow_lanes.py`

**9a. Write the failing test.** Append to `tests/test_flow_lanes.py` (imports of
`GatedDemoAdapter`, `line_texts`, `rules`, `seed_done`, `wait_for`, `SIZE` come from
`.test_flow_helpers` — extend the existing import block):

```python
@pytest.mark.asyncio
async def test_lane_tail_streams_mid_fanout_then_clears() -> None:
    """Design doc D4: focused-lane deltas fill LiveTail while the root is
    idle; ctrl+o moves the ▸ pin; the tail is ephemeral at turn end."""
    adapter = GatedDemoAdapter()
    app = NewTuiApp(adapter)
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        app.submit_prompt(AGENTS_PROMPT)
        # Child bursts land before the script's first _wait parks the turn.
        assert await wait_for(pilot, lambda: app.live_tail.lane_mode)
        marked = [i for i, line in enumerate(app.lanes_panel.lane_lines) if "▸" in line]
        assert len(marked) == 1  # exactly one tailed lane

        await pilot.press("ctrl+o")
        await pilot.pause()
        moved = [i for i, line in enumerate(app.lanes_panel.lane_lines) if "▸" in line]
        assert len(moved) == 1 and moved != marked  # the pin cycled

        adapter.release()
        assert await wait_for(pilot, lambda: rules(app) >= 2 and not app.turn_active)
        assert not app.live_tail.lane_mode  # root answer preempted, then turn ended
        # Ephemeral: child prose never became a transcript block.
        assert not any(
            "undocumented streaming flags" in text for text in line_texts(app)
        )
```

("undocumented streaming flags" is the researcher lane's narration row, demo.py:345-348 —
it flows through the tail but must never land in the parent transcript.)

**9b. Run — expect PASS immediately** (Tasks 1-8 built everything; this test locks the
integration):

```
uv run pytest tests/test_flow_lanes.py -q
```

If it fails, debug in this order: (1) demo burst placement vs the gate (Task 8.3), (2) app
host wiring (Task 5), (3) reducer focus (Task 3). Fix, re-run, then:

**9c. Commit:**

```
git add -A && git commit -m "test: flow — lane tail mid-fan-out, ctrl+o pin cycling, ephemeral at turn end"
```

---

## Task 10 — SVG snapshot: tail visible

**Files:** `tests/test_ui_snapshots.py`,
`tests/__snapshots__/test_ui_snapshots/test_lane_tail_snapshot.raw` (new)

A whole-app mid-turn screenshot is nondeterministic (ticking working line, lane shimmer),
so this snapshot locks the **LiveTail widget in lane mode** in a minimal timer-free harness
— same `take_svg_screenshot` + `.raw` + `_clean_svg` machinery as the existing
`test_double_esc_rewind_snapshot` (test_ui_snapshots.py).

**10a. Write the test.** Append to `tests/test_ui_snapshots.py` (add
`from textual.app import App, ComposeResult`, `from amplifier_app_newtui.ui.live_tail
import LiveTail`, and `from amplifier_app_newtui.ui.themes import DEFAULT_THEME,
register_themes, theme_id` to the imports):

```python
_TAIL_SNAPSHOT = (
    Path(__file__).parent
    / "__snapshots__"
    / "test_ui_snapshots"
    / "test_lane_tail_snapshot.raw"
)


class _LaneTailShot(App[None]):
    """Minimal deterministic harness: LiveTail in lane mode, no timers."""

    def __init__(self) -> None:
        super().__init__()
        register_themes(self)

    def on_mount(self) -> None:
        self.theme = theme_id(DEFAULT_THEME)

    def compose(self) -> ComposeResult:
        yield LiveTail(id="live-tail")


def test_lane_tail_snapshot(monkeypatch) -> None:
    """The dim ┆-guttered lane tail rendering is regression-locked."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLORTERM", "truecolor")
    app = _LaneTailShot()

    async def paint_tail(pilot) -> None:
        tail = app.query_one("#live-tail", LiveTail)
        tail.show_lane_tail(
            "…the queue bridge normalizes delegate lifecycle events at a single\n"
            "boundary, so the lanes are fed from the same UIEvent union as the\n"
            "transcript — checking trackers/task_status.py next"
        )
        await pilot.pause()

    actual = take_svg_screenshot(app=app, terminal_size=(90, 8), run_before=paint_tail)
    expected = _TAIL_SNAPSHOT.read_text(encoding="utf-8")
    assert expected == _clean_svg(expected), "snapshot must remain whitespace-clean"
    assert _clean_svg(actual) == expected
```

**10b. Bootstrap the snapshot.** Temporarily insert this line directly above the
`expected = _TAIL_SNAPSHOT.read_text(...)` line:

```python
    _TAIL_SNAPSHOT.write_text(_clean_svg(actual), encoding="utf-8")  # BOOTSTRAP — REMOVE
```

Run once, REMOVE the line, run again:

```
uv run pytest tests/test_ui_snapshots.py -q          # first run: writes the .raw
# remove the BOOTSTRAP line, then:
uv run pytest tests/test_ui_snapshots.py -q          # expect: 2 passed
```

Eyeball the generated `.raw` (it's an SVG): it must show three dim lines each starting with
`┆ `. If it doesn't, stop and fix the widget, not the snapshot.

**10c. Commit (snapshot file included):**

```
git add -A && git commit -m "test: snapshot — LiveTail lane mode (┆-guttered dim tail) locked"
```

---

## Task 11 — `docs/DESIGN-SPEC.md`: §2, §3, §8, §11

**Files:** `docs/DESIGN-SPEC.md`

Match the file's exact style: checkbox requirement lines, terse, mockup-verbatim strings.
Phases 1–2 shipped code without spec updates (docs are Phase 3 scope, per the design doc) —
this task documents ALL of Ambient Progress. **Reconcile with the merged Phase 1/2 code**
(read `ui/plan_panel.py` and the `DelegateSummaryBlock` model/renderer first; if a detail
below contradicts the shipped code, the code wins — document what shipped).

1. **§2 Screen layout**, item 4 (line 42-47): change the strip list entry
   `- Agent lanes panel` to:

```markdown
   - Bottom strip: agent lanes panel (left) | plan panel (right — the turn's todo
     checklist, `Plan n/m` header; collapses to a count in the strip header, then to
     the FooterBar, as width shrinks)
```

2. **§3 Transcript block grammar**: add one checkbox after the **Turn rule** bullets
   (line 72):

```markdown
- [ ] **Delegate summary** (fan-out turns, at turn end): one durable line
  `● Used N delegates · Plan n/m · <duration> ▸`; click/enter expands (`▾`) to per-agent
  rows (`✔`/`✖`/`⊘` `<agent> <elapsed> · "<result snippet>"`) plus a final plan line.
  Every past summary in scrollback stays expandable; reconstructed from `events.jsonl`
  on resume. The live todo checklist no longer appends to the transcript — while a turn
  runs it lives in the plan panel (§2) and folds into this summary at close.
```

3. **§8 Agent lanes & subagent focus**:
   - First bullet (line 122): update the header string to the Task 7 value:
     `` header `Agent lanes · ↑↓ select · enter focus · ctrl-o tail · esc close` ``.
   - Replace the second bullet (line 123, "Multi-agent turn renders a compact live
     tree…") with what shipped in Phase 2 + this phase:

```markdown
- [ ] Multi-agent turn: per-agent progress lives in the lanes panel and the delegate
  summary (§3), not per-agent transcript tree lines. Successful native file writes still
  aggregate into one expandable, diff-styled `Changed N files` row.
- [ ] **Lane live tail**: while lanes run and the root stream is idle, the LiveTail
  region shows the focused lane's stream — up to 3 dim `┆`-guttered lines, repainted at
  most every 0.05s. Focus defaults to the most-recently-streaming running lane; ctrl-o
  cycles the pin among running lanes; the tailed lane carries a `▸` after its name in
  the panel. The root stream always preempts instantly. Tail content is ephemeral —
  never a transcript block; durable child prose lives in the lane's own transcript.
```

4. **§11 Turn lifecycle & telemetry**: add one checkbox after line 144:

```markdown
- [ ] Fan-out close-out: the running chrome (lane tail, live plan panel state) collapses
  into the durable delegate summary (§3) at turn end; the tail clears; summary
  expansion still works after `resume` (rebuilt from `events.jsonl`).
```

**Verify:** `uv run pytest tests/test_flow_lanes.py -q` (no code touched — just guarding
against accidental edits), then read the diff: `git diff docs/DESIGN-SPEC.md`.

**Commit:**

```
git add -A && git commit -m "docs: DESIGN-SPEC — plan panel strip, delegate summary, lane live tail, fan-out close-out"
```

---

## Task 12 — `docs/USER-GUIDE.md`: keybinding + lanes section

**Files:** `docs/USER-GUIDE.md`

Match the guide's voice (second person, tables, bold keys).

1. **§8 Keys** table (line 237-256): add after the `ctrl+t` row:

```markdown
| ctrl+o | cycle which running agent the live tail follows | agents fanned out |
```

2. **§9 Agent lanes (subagents)** (line 275-288): append a paragraph after the
   "Child tool and stream events update that row…" paragraph:

```markdown
While agents run and the root model is quiet, the area under the transcript shows a live
**tail** of one agent's stream — up to three dim `┆`-prefixed lines, so you can always see
the work happening. It follows whichever running agent spoke most recently; press
**ctrl+o** to pin it to a different one (the `▸` after a lane's name marks who you're
tailing — also shown in the panel header hint). The moment the root model speaks, the tail
switches back to it. Tail text is a live preview only: the agent's full prose lives in its
own transcript (focus the lane to read it), and nothing from the tail lands in yours.
```

3. If the guide's §9 (or Phase 1/2 sections) still lacks plan-panel/delegate-summary
   coverage, leave that to the shipped Phase 1/2 text — this task adds ONLY the Phase 3
   keybinding + tail note. Do not rewrite other sections.

**Verify:** `git diff docs/USER-GUIDE.md` reads in the guide's voice; no other sections touched.

**Commit:**

```
git add -A && git commit -m "docs: USER-GUIDE — ctrl+o tail cycling and the lane live tail"
```

---

## Task 13 — Final gate

**Run the full offline suite and linters:**

```
uv run pytest -q
```

Expected: **all tests pass, 0 failed** (suite is fully offline; the new tests from Tasks
1-10 are included). If demo-sequence or lane-telemetry tests fail, revisit Task 8b — update
expectations only for the deliberate `writing response` activity change.

```
uv run ruff check .
```

Expected: `All checks passed!`

```
uv run pyright src/
```

Expected: `0 errors, 0 warnings`.

**Goldens check:** Phase 3 must not have touched any `ui/transcript.py` renderer. Confirm:

```
git diff --stat main -- src/amplifier_app_newtui/ui/transcript.py tests/goldens/
```

Expected for THIS phase's commits: no changes beyond what Phases 1-2 already merged. If you
did change a renderer, run `uv run python tests/goldens/regen.py`, review the diff, and
amend it into the renderer commit.

**Manual smoke (offline, no credentials):**

```
uv run amplifier-newtui --demo
```

Type the agents demo prompt (`run the DTU reality check across provider docs, store, and
tests`) and watch for: `┆` tail lines while lanes run → `▸` in the lanes panel → ctrl+o
moves it → tail vanishes the moment the final answer streams.

**Commit anything outstanding, then hand off:**

```
git add -A && git commit -m "phase 3: lane live tail — gate green (pytest, ruff, pyright, demo smoke)"
```

---

## Task summary

| # | What | Layer | Test file |
|---|---|---|---|
| 1 | LaneRegistry tail focus | model | test_model_turn_queues_lanes.py |
| 2 | LiveTail lane mode | ui widget | test_ui_transcript_live_tail.py |
| 3 | Reducer routing + throttle + host protocol | ui reducer | test_ui_reducer_lane_tail.py |
| 4 | Root preemption + ephemerality | ui reducer | test_ui_reducer_lane_tail.py |
| 5 | App host wiring | ui app | (pyright + flows) |
| 6 | LanesPanel ▸ marker | ui widget | test_ui_lanes.py |
| 7 | ctrl+o binding + header hint | ui keymap/app | test_ui_keymap.py, test_flow_lanes.py |
| 8 | Demo child stream bursts | kernel/demo | demo + flow suites |
| 9 | End-to-end flow test | tests | test_flow_lanes.py |
| 10 | SVG snapshot (tail visible) | tests | test_ui_snapshots.py |
| 11 | DESIGN-SPEC §2/§3/§8/§11 | docs | — |
| 12 | USER-GUIDE keys + lanes | docs | — |
| 13 | Full gate + demo smoke | — | everything |
