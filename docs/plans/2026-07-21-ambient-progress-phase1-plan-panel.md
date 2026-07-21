# Implementation Plan: Ambient Progress — Phase 1: Plan Panel

> **For execution:** Use /execute-plan mode or the subagent-driven-development recipe.

**Date:** 2026-07-21
**Design:** `docs/plans/2026-07-21-ambient-progress-design.md` (D1, D2, D3, D6 + edge cases — READ IT FIRST)
**Branch:** `agent/anchors-migration`
**Phase:** 1 of 3 (Phase 2 = DelegateSummaryBlock, Phase 3 = lane live tail — NOT in this plan)

## Goal

During a turn, the agent's `todo` tool currently renders as a `TodoBlock` appended to the
transcript — it scrolls away, and it duplicates what the bottom chrome should own. Phase 1
moves the live plan out of the transcript and into an **ambient bottom-right panel**:

1. New widget `ui/plan_panel.py` — `PlanPanel` (`#plan-panel`): `Plan N/M` header + `✔`/`▶`/`○`
   rows. Zero-height when there are no todos; `⋮ +N more` overflow beyond 5 rows; collapses to
   the header line alone when everything is complete.
2. Bottom strip: `LanesPanel` (flexible, left) and `PlanPanel` (fixed ~37 cols, right) share one
   horizontal container above the composer.
3. `ReducerHost` gains `plan_changed(items)` (mirrors `lanes_changed()`); the reducer's existing
   `todo` intercept calls it **instead of** appending a `TodoBlock`.
4. `TodoBlock` is fully retired (renderer, union kind, goldens). `TodoItem`/`TodoStatus` stay.
5. `DemoRuntime` emits `todo` beats so the demo lights the panel, keeping the whole flow
   scriptable offline.
6. Responsive ladder: below 90 cols the panel hides and a `Plan N/M` count appears in the
   `FooterBar` instead.

**Non-goals (see design "Out of scope"):** sub-agent todo merging, todo editing, plan history,
`DelegateSummaryBlock` (that's Phase 2 — the *final* plan folding into a durable transcript
block happens there, not here).

## Architecture

```
todo ToolPre (root session only — children diverted at reducer.handle(), reducer.py:460-462)
      │
      ▼
reducer._update_todo (reducer.py:1133)  ──►  host.plan_changed(items)   [NO transcript block]
                                                   │
                                                   ▼
                                    NewTuiApp.plan_changed (ui/app.py)
                                                   │
                                                   ▼
                              app_support.sync_plan_surfaces(app)
                               │                              │
              width ≥ 90 cols  │                              │  width < 90 cols
                               ▼                              ▼
                  PlanPanel.show + update             PlanPanel hidden;
                  (bottom strip, right)               FooterState.plan_done/plan_total
                                                      → "Plan N/M" in footer left segment
```

Layer rules (ADR-0007, enforced by import-linter — DO NOT violate):

- `PlanPanel` lives in `ui/` (Textual allowed). It renders from `model.blocks.TodoItem` only.
- The pure formatter `format_plan_lines(items)` is a function of items → `Segment` lines,
  exactly like `ui/transcript.py` renderers — unit-testable as plain strings via
  `ui/segments.py:line_plain`.
- No new event kinds; no kernel changes except demo beats. Events stay normalized only in
  `kernel/events.py`.
- The reducer mutates **only** through `ReducerHost` — `plan_changed()` is a new host callback,
  the reducer never touches widgets.
- `ui/app.py` is a composition root with a hard budget (ADR-0007 line 19). It is already over;
  your net addition to `app.py` must be ≤ ~20 lines — all logic goes to `app_support.py` and
  `plan_panel.py`.

## Tech Stack

- Python 3.12, `uv` for everything (`uv run pytest -q`, `uv run ruff check .`, `uv run pyright src/`)
- Textual ~8.2 (`Static`, `Horizontal`, theme tokens via `app.theme_variables`)
- pydantic frozen models (`model/blocks.py`)
- pytest + pytest-asyncio (`@pytest.mark.asyncio` + `async with app.run_test(size=SIZE) as pilot` —
  copy the pattern from `tests/test_flow_lanes.py:50-53`)
- Goldens: `uv run python tests/goldens/regen.py` (widths 40/80/97/120) — regenerated **in the
  same commit** as any renderer change
- Snapshots: `textual._doc.take_svg_screenshot` + committed `.raw` files
  (`tests/test_ui_snapshots.py`)

## Taste guardrails (read this twice)

You have exactly four glyphs: `✔` `▶` `○` `⋮`. You have exactly the theme tokens already used in
this repo: `bright`, `fg`, `dim`, `dimmer`, `green`, `orange`, `rule`. **No emoji. No new
colors. No extra borders. No box-drawing art. No gradients of enthusiasm.** The panel is one
`border-top: solid $rule` strip that looks like `LanesPanel`'s sibling, because it is. When in
doubt, copy what `ui/lanes_panel.py` does and delete half of it.

Every task below is: write the failing test → run it, watch it fail → implement → run it, watch
it pass → run the full gate → commit. Do not batch tasks into one commit.

---

## Task 1 — Pure formatter: `format_plan_lines`

**Files:** create `src/amplifier_app_newtui/ui/plan_panel.py`, create `tests/test_ui_plan_panel.py`

### 1a. Write the failing test

Create `tests/test_ui_plan_panel.py`:

```python
"""Tests for the ambient plan panel (ui/plan_panel.py) — Phase 1 of
docs/plans/2026-07-21-ambient-progress-design.md (D1/D2)."""

from __future__ import annotations

from amplifier_app_newtui.model.blocks import TodoItem
from amplifier_app_newtui.ui.plan_panel import PLAN_MAX_ROWS, format_plan_lines
from amplifier_app_newtui.ui.segments import line_plain


def _items(*statuses: str) -> tuple[TodoItem, ...]:
    return tuple(
        TodoItem(content=f"step {i}", status=status)  # type: ignore[arg-type]
        for i, status in enumerate(statuses)
    )


def plains(items: tuple[TodoItem, ...]) -> tuple[str, ...]:
    return tuple(line_plain(line) for line in format_plan_lines(items))


def test_no_items_renders_nothing() -> None:
    assert format_plan_lines(()) == ()


def test_header_counts_and_glyph_rows() -> None:
    items = _items("completed", "in_progress", "pending", "pending")
    assert plains(items) == (
        "Plan 1/4",
        "  ✔ step 0",
        "  ▶ step 1",
        "  ○ step 2",
        "  ○ step 3",
    )


def test_all_complete_collapses_to_header_only() -> None:
    items = _items("completed", "completed", "completed")
    assert plains(items) == ("Plan 3/3",)


def test_overflow_windows_around_active_item_with_more_marker() -> None:
    # 8 items, active at index 4 → window starts one above the active row.
    items = _items(
        "completed", "completed", "pending", "pending",
        "in_progress", "pending", "pending", "pending",
    )
    assert PLAN_MAX_ROWS == 5
    assert plains(items) == (
        "Plan 2/8",
        "  ○ step 3",
        "  ▶ step 4",
        "  ○ step 5",
        "  ○ step 6",
        "  ○ step 7",
        "  ⋮ +3 more",
    )


def test_overflow_with_no_active_item_shows_first_rows() -> None:
    items = _items("pending", "pending", "pending", "pending", "pending", "pending")
    lines = plains(items)
    assert lines[0] == "Plan 0/6"
    assert lines[1] == "  ○ step 0"
    assert lines[-1] == "  ⋮ +1 more"
    assert len(lines) == 1 + PLAN_MAX_ROWS + 1  # header + rows + marker
```

Run it:

```
uv run pytest tests/test_ui_plan_panel.py -q
```

**Expect: FAIL** — `ModuleNotFoundError: No module named 'amplifier_app_newtui.ui.plan_panel'`.

### 1b. Implement

Create `src/amplifier_app_newtui/ui/plan_panel.py`:

```python
"""Ambient plan strip (design 2026-07-21 D1/D2): the ``todo`` tool's live
checklist, rendered in the bottom strip's right column instead of the
transcript.

- Header: ``Plan N/M`` (``Plan`` bright bold, counts dim).
- Rows: ``✔`` green done (dim text), ``▶`` orange bold in-progress
  (bright bold text), ``○`` dimmer pending (dim text).
- Overflow: at most :data:`PLAN_MAX_ROWS` item rows, windowed around the
  in-progress item, then one ``⋮ +N more`` dimmer line.
- All complete: collapses to the header line alone (completion stays
  visible — same "done stays visible" rule as the lanes panel).

Formatting is a pure function of the items (like ``ui/transcript.py``
renderers) so tests pin plain strings via ``ui/segments.py:line_plain``.
"""

from __future__ import annotations

from collections.abc import Sequence

from rich.style import Style
from rich.text import Text
from textual.widgets import Static

from ..model.blocks import Segment, TodoItem
from .segments import Line, line_plain

PLAN_MAX_ROWS = 5
"""Max item rows before collapsing the rest into ``⋮ +N more``."""

PLAN_PANEL_WIDTH = 37
"""Fixed column width of the panel in the bottom strip (design §1 mockup)."""

_GLYPHS: dict[str, tuple[str, str, bool]] = {
    # status -> (prefix, content token, content bold)
    "completed": ("  ✔ ", "dim", False),
    "in_progress": ("  ▶ ", "bright", True),
    "pending": ("  ○ ", "dim", False),
}
_PREFIX_TOKENS = {"completed": "green", "in_progress": "orange", "pending": "dimmer"}


def plan_counts(items: Sequence[TodoItem]) -> tuple[int, int]:
    """``(done, total)`` for the header and the footer fallback."""
    return (sum(1 for item in items if item.status == "completed"), len(items))


def format_plan_lines(
    items: Sequence[TodoItem], *, max_rows: int = PLAN_MAX_ROWS
) -> tuple[Line, ...]:
    """Render the plan as Segment lines — a pure function of the items."""
    if not items:
        return ()
    done, total = plan_counts(items)
    header: Line = (
        Segment(text="Plan", style_token="bright", bold=True),
        Segment(text=f" {done}/{total}", style_token="dim"),
    )
    if done == total:
        return (header,)  # collapse: completion stays visible as one line
    active = next(
        (i for i, item in enumerate(items) if item.status == "in_progress"), 0
    )
    start = max(0, min(active - 1, total - max_rows))
    visible = items[start : start + max_rows]
    lines: list[Line] = [header]
    for item in visible:
        prefix, token, bold = _GLYPHS[item.status]
        lines.append(
            (
                Segment(text=prefix, style_token=_PREFIX_TOKENS[item.status]),
                Segment(text=item.content, style_token=token, bold=bold),
            )
        )
    hidden = total - len(visible)
    if hidden > 0:
        lines.append((Segment(text=f"  ⋮ +{hidden} more", style_token="dimmer"),))
    return tuple(lines)


class PlanPanel(Static):
    """The plan strip widget (``#plan-panel``) — bottom strip, right column.

    Feed it with :meth:`update_plan`; the app decides visibility via
    :meth:`show_panel` / :meth:`hide_panel` (responsive ladder lives in
    ``app_support.sync_plan_surfaces``, not here). Rendering is
    :func:`format_plan_lines` painted with theme tokens — no interaction,
    no focus, no timers.
    """

    DEFAULT_CSS = """
    PlanPanel {
        display: none;
        width: 100%;
        height: auto;
        border-top: solid $rule;
        padding: 0 2;
    }
    """

    def __init__(self, *, id: str | None = None) -> None:  # noqa: A002
        super().__init__(id=id)
        self._items: tuple[TodoItem, ...] = ()

    @property
    def items(self) -> tuple[TodoItem, ...]:
        return self._items

    @property
    def plan_lines(self) -> tuple[str, ...]:
        """The exact plain-text lines currently displayed (test surface)."""
        return tuple(line_plain(line) for line in format_plan_lines(self._items))

    def update_plan(self, items: Sequence[TodoItem]) -> None:
        """Replace the listing (the ``todo`` tool replaces the whole list)."""
        self._items = tuple(items)
        if self.is_mounted:
            self.refresh(layout=True)

    def show_panel(self) -> None:
        self.display = True

    def hide_panel(self) -> None:
        self.display = False

    def render(self) -> Text:
        tokens = self.app.theme_variables
        text = Text()
        for index, line in enumerate(format_plan_lines(self._items)):
            if index:
                text.append("\n")
            for seg in line:
                text.append(
                    seg.text,
                    style=Style(color=tokens.get(seg.style_token), bold=seg.bold),
                )
        return text


__all__ = [
    "PLAN_MAX_ROWS",
    "PLAN_PANEL_WIDTH",
    "PlanPanel",
    "format_plan_lines",
    "plan_counts",
]
```

(The widget class rides along in this task because it is <60 lines and has no logic beyond
painting; its behavior is exercised in Tasks 5/8. The formatter is the tested unit here.)

### 1c. Verify and commit

```
uv run pytest tests/test_ui_plan_panel.py -q        # expect: 5 passed
uv run ruff check .                                  # expect: All checks passed!
uv run pyright src/                                  # expect: 0 errors
git add src/amplifier_app_newtui/ui/plan_panel.py tests/test_ui_plan_panel.py
git commit -m "ui: PlanPanel widget + pure plan-line formatter (ambient progress D1/D2)"
```

---

## Task 2 — Reducer: `plan_changed()` reroutes the `todo` tool away from the transcript

**Files:** `src/amplifier_app_newtui/ui/reducer.py`, `src/amplifier_app_newtui/ui/app.py`
(minimal stub), `tests/test_ui_reducer_outcomes.py` (FakeHost), `tests/test_ui_transcript_render.py`

### 2a. Write the failing test

In `tests/test_ui_transcript_render.py`, **replace** the existing test
`test_todo_tool_becomes_a_todo_block_updated_in_place_not_digested` (lines 767-817) with:

```python
def test_todo_tool_reroutes_to_plan_changed_never_the_transcript() -> None:
    """Design 2026-07-21 D1/D3: the todo tool feeds the plan panel via
    host.plan_changed(); no TodoBlock, no tool_line, no digest entry."""
    import sys

    from amplifier_app_newtui.kernel import events as ev
    from amplifier_app_newtui.model.blocks import BlockIdAllocator
    from amplifier_app_newtui.model.lanes import LaneRegistry
    from amplifier_app_newtui.model.turn import OutcomeLedger
    from amplifier_app_newtui.ui.reducer import TranscriptReducer

    sys.path.insert(0, "tests")
    from test_ui_reducer_outcomes import FakeHost

    host = FakeHost("auto")
    reducer = TranscriptReducer(
        host, allocator=BlockIdAllocator(), ledger=OutcomeLedger(), lanes=LaneRegistry()
    )
    reducer.handle(ev.PromptSubmit(session_id="s", prompt="do it", ts=0.0))

    def todo_call(cid: str, statuses: list[str]) -> None:
        todos = [
            {"content": f"step {i}", "status": st, "activeForm": f"doing {i}"}
            for i, st in enumerate(statuses)
        ]
        reducer.handle(
            ev.ToolPre(
                session_id="s",
                tool_call_id=cid,
                tool_name="todo",
                tool_input={"operation": "update", "todos": todos},
                ts=1.0,
            )
        )
        reducer.handle(
            ev.ToolPost(
                session_id="s",
                tool_call_id=cid,
                tool_name="todo",
                tool_input={"operation": "update", "todos": todos},
                result={"status": "ok"},
                ts=1.0,
            )
        )

    todo_call("t1", ["in_progress", "pending"])
    todo_call("t2", ["completed", "in_progress"])
    # a 'list' op carries no todos — must not fire plan_changed
    reducer.handle(
        ev.ToolPre(
            session_id="s", tool_call_id="t3", tool_name="todo",
            tool_input={"operation": "list"}, ts=2.0,
        )
    )

    assert len(host.plan_changes) == 2  # one push per create/update call
    assert [i.status for i in host.plan_changes[-1]] == ["completed", "in_progress"]
    assert [i.content for i in host.plan_changes[-1]] == ["step 0", "step 1"]
    # never in the transcript, never in the activity digest
    assert not [b for b in host.blocks if b.kind == "todo"]
    assert not [b for b in host.blocks if b.kind == "tool_line"]
```

In `tests/test_ui_reducer_outcomes.py`, extend `FakeHost` (class at line 35): add `TodoItem` to
the existing `amplifier_app_newtui.model.blocks` import at the top of the file, add to
`__init__` (after `self.stream_events...`, line 42):

```python
        self.plan_changes: list[tuple[TodoItem, ...]] = []
```

and add the method right after `lanes_changed` (line 68-69):

```python
    def plan_changed(self, items: tuple[TodoItem, ...]) -> None:
        self.plan_changes.append(items)
```

Run:

```
uv run pytest tests/test_ui_transcript_render.py -q
```

**Expect: FAIL** — `AttributeError: 'FakeHost' object has no attribute 'plan_changes'` is fixed
by the FakeHost edit, then the real failure: `assert len(host.plan_changes) == 2` → `0 == 2`
(the reducer still appends a `TodoBlock`).

### 2b. Implement

All edits in `src/amplifier_app_newtui/ui/reducer.py`:

1. **Protocol** — in `ReducerHost` (lines 297-314), directly after
   `def lanes_changed(self) -> None: ...` (line 309), add:

```python
    def plan_changed(self, items: tuple[TodoItem, ...]) -> None: ...
```

   (`TodoItem` is already imported at line 41.)

2. **Reroute** — replace the body of `_update_todo` (lines 1133-1160) with:

```python
    def _update_todo(self, event: ev.ToolPre) -> None:
        """Route the ``todo`` tool to the ambient plan panel — never the
        transcript (design 2026-07-21 D1/D3).

        The printing ``hooks-todo-display`` is stripped under the TUI, so
        newtui renders the list itself from the tool call's ``todos``
        payload (``create``/``update`` ops carry the full list; ``list``
        carries none). Root-session only: child ToolPre events are
        diverted before dispatch (see ``_is_foreign_turn_event``).
        """
        raw = event.tool_input or {}
        raw_todos = raw.get("todos")
        if not isinstance(raw_todos, list) or not raw_todos:
            return  # a 'list' op or empty payload — nothing to redraw
        items = tuple(
            TodoItem(
                content=str(todo.get("content", "")),
                status=_todo_status(todo.get("status")),
            )
            for todo in raw_todos
            if isinstance(todo, dict)
        )
        self._host.plan_changed(items)
```

3. **Dead state** — delete the now-unused `todo_id: str | None = None` field from `_Turn`
   (line 328) and remove `TodoBlock` from the `..model.blocks` import block (line 40).
   `TodoItem` and `_todo_status` (line 73) stay.

4. **Comment truth** — in `_tool_post` (lines 1006-1009) the comment says "Plans and todos are
   their own blocks". Update it:

```python
        if event.tool_name in ("update_plan", "todo") or turn is None:
            # Plans are their own blocks (rendered from tool:pre); todos
            # feed the ambient plan panel — neither joins the digest.
            return
```

5. **App stub** (keeps `NewTuiApp` satisfying the protocol so pyright stays green this commit) —
   in `src/amplifier_app_newtui/ui/app.py`: add `TodoItem` to the `..model.blocks` import
   (lines 14-21), add to `__init__` near `self._lanes_fanout_open` (line 177):

```python
        self.plan_items: tuple[TodoItem, ...] = ()  # latest root todo list
```

   and add directly after `lanes_changed` (line 631):

```python
    def plan_changed(self, items: tuple[TodoItem, ...]) -> None:
        self.plan_items = items  # panel wiring lands with the bottom strip
```

### 2c. Verify and commit

```
uv run pytest tests/test_ui_transcript_render.py tests/test_ui_reducer_outcomes.py -q
                                                     # expect: all passed
uv run pytest -q                                     # expect: all passed (nothing else
                                                     #   asserted live todo blocks)
uv run ruff check . && uv run pyright src/           # expect: clean / 0 errors
git add -A && git commit -m "reducer: todo tool reroutes to ReducerHost.plan_changed (no transcript block)"
```

Note: `tests/goldens/*` still contain a `=== todo ===` section — that is fine for now; the
renderer and union are untouched until Task 6.

---

## Task 3 — Demo runtime: `todo` beats light the panel offline

**Files:** `src/amplifier_app_newtui/kernel/demo.py`, `tests/test_kernel_demo_turns.py`

The demo currently emits **zero** `todo` tool calls (verified by grep). The store turns
(`run_build_turn` / `run_auto_turn` → `_run_store_turn`, demo.py:1017) already walk a plan via
`update_plan` beats — mirror each one with a `todo` beat so the panel progresses in demo mode
(design D6: "DemoRuntime emits identical typed events").

### 3a. Write the failing test

In `tests/test_kernel_demo_turns.py`:

1. Next to `PLAN = ["tool_pre", "tool_post"]` (line 40) add:

```python
TODO = ["tool_pre", "tool_post"]
```

2. In `_BUILD_KINDS` (lines 104-117), `_AUTO_KINDS` (lines 228-237), and the other store-turn
   kinds tables (the denied-flow table at ~202-205 and the interrupt test's
   `PLAN + PLAN + PLAN + PLAN` at ~287) replace **every** `PLAN` with `PLAN + TODO`.
   ⚠️ Only store-turn tables — if any plan-mode-turn table uses `PLAN`, leave it alone
   (plan mode gets no todo beats).

3. Add a progression test after `test_build_turn_plan_progression` (line 139):

```python
def test_build_turn_todo_progression_mirrors_the_plan() -> None:
    _, events = play("run_build_turn")
    todos = [e for e in events if e.kind == "tool_pre" and e.tool_name == "todo"]
    statuses = [tuple(t["status"] for t in e.tool_input["todos"]) for e in todos]
    assert statuses == [
        ("pending", "pending", "pending"),
        ("in_progress", "pending", "pending"),
        ("completed", "pending", "pending"),
        ("completed", "in_progress", "pending"),
        ("completed", "completed", "pending"),
        ("completed", "completed", "in_progress"),
        ("completed", "completed", "completed"),
    ]
    assert all(e.tool_input["operation"] == "update" for e in todos)
    assert [t["content"] for t in todos[0].tool_input["todos"]] == list(STORE_STEPS)
```

Run:

```
uv run pytest tests/test_kernel_demo_turns.py -q
```

**Expect: FAIL** — kinds mismatches plus `assert statuses == [...]` → `[] == [...]`.

### 3b. Implement

In `src/amplifier_app_newtui/kernel/demo.py`:

1. After `_plan` (lines 862-881) add:

```python
    _TODO_STATUS_BY_PLAN = {
        "pending": "pending",
        "active": "in_progress",
        "done": "completed",
    }

    async def _todo(self, steps: Sequence[str], statuses: Sequence[str]) -> None:
        """Mirror the plan as a ``todo`` tool call (ambient plan panel beat)."""
        await self._tool(
            "todo",
            {
                "operation": "update",
                "todos": [
                    {
                        "content": step,
                        "status": self._TODO_STATUS_BY_PLAN[status],
                        "activeForm": step,
                    }
                    for step, status in zip(steps, statuses, strict=True)
                ],
            },
            {"ok": True},
        )
```

2. In `_run_store_turn`, after **each** of the four
   `await self._plan(STORE_PLAN_TITLE, STORE_STEPS, statuses)` calls (lines 1020, 1029, 1090,
   1108) add:

```python
        await self._todo(STORE_STEPS, statuses)
```

   (Match each site's indentation — the ones at 1090 and 1108 sit inside the loop.)

### 3c. Verify and commit

```
uv run pytest tests/test_kernel_demo_turns.py -q     # expect: all passed
uv run pytest -q                                     # full suite — if any OTHER test pins the
                                                     # store-turn event sequence, fix it the
                                                     # same way (PLAN → PLAN + TODO)
uv run ruff check . && uv run pyright src/
git add -A && git commit -m "demo: store turns emit todo beats mirroring the plan (offline panel flow)"
```

---

## Task 4 — App wiring: bottom strip + responsive sync

**Files:** `src/amplifier_app_newtui/ui/app.py`, `src/amplifier_app_newtui/ui/app_support.py`,
create `tests/test_flow_plan_panel.py`

### 4a. Write the failing test

Create `tests/test_flow_plan_panel.py`:

```python
"""Flow tests — ambient plan panel over the demo runtime
(docs/plans/2026-07-21-ambient-progress-design.md, Phase 1)."""

from __future__ import annotations

import pytest

from amplifier_app_newtui.kernel.demo import BUILD_PROMPT
from amplifier_app_newtui.ui.app import NewTuiApp

from .test_flow_helpers import SIZE, GatedDemoAdapter, blocks_of, seed_done, wait_for


@pytest.mark.asyncio
async def test_plan_panel_lights_up_mid_turn_and_collapses_when_done() -> None:
    adapter = GatedDemoAdapter()
    app = NewTuiApp(adapter)
    async with app.run_test(size=SIZE) as pilot:
        await seed_done(pilot, app)
        app.submit_prompt(BUILD_PROMPT)
        # parks at the first virtual wait: plan seeded + step 0 in progress
        assert await wait_for(
            pilot,
            lambda: app.plan_panel.display
            and any(line.startswith("  ▶ ") for line in app.plan_panel.plan_lines),
        )
        assert app.plan_panel.plan_lines[0] == "Plan 0/3"
        adapter.release()
        assert await wait_for(pilot, lambda: not app.turn_active)
        # all steps complete → collapsed to the header, still visible
        assert app.plan_panel.display
        assert app.plan_panel.plan_lines == ("Plan 3/3",)
        # D3: the transcript never gets a live todo block
        assert blocks_of(app, "todo") == []


@pytest.mark.asyncio
async def test_plan_panel_hides_below_90_cols() -> None:
    adapter = GatedDemoAdapter()
    app = NewTuiApp(adapter)
    async with app.run_test(size=(80, 40)) as pilot:
        await seed_done(pilot, app)
        app.submit_prompt(BUILD_PROMPT)
        assert await wait_for(pilot, lambda: bool(app.plan_items))
        assert not app.plan_panel.display  # ladder: count-only below 90 cols
        adapter.release()
        assert await wait_for(pilot, lambda: not app.turn_active)
        assert not app.plan_panel.display
```

Run:

```
uv run pytest tests/test_flow_plan_panel.py -q
```

**Expect: FAIL** — `AttributeError: 'NewTuiApp' object has no attribute 'plan_panel'`.

### 4b. Implement

In `src/amplifier_app_newtui/ui/app_support.py`, add near `footer_state` (line 571):

```python
PLAN_PANEL_MIN_WIDTH = 90
"""Below this terminal width the plan panel yields; a ``Plan N/M`` count
falls back to the footer (design D2 responsive ladder)."""


def apply_plan_change(app: NewTuiApp, items: tuple[TodoItem, ...]) -> None:
    """Reducer pushed a new root todo list — repaint the ambient surfaces."""
    app.plan_items = tuple(items)
    sync_plan_surfaces(app)


def sync_plan_surfaces(app: NewTuiApp) -> None:
    """One decision point for the plan's responsive ladder (D2).

    Wide (≥ 90 cols) with todos → the bottom-strip panel; otherwise the
    panel hides and the footer carries the count (Task 5). Called on
    every plan change and on terminal resize.
    """
    app.plan_panel.update_plan(app.plan_items)
    if app.plan_items and app.size.width >= PLAN_PANEL_MIN_WIDTH:
        app.plan_panel.show_panel()
    else:
        app.plan_panel.hide_panel()
    app.refresh_status()  # footer carries the fallback count (Task 5)
```

(`app.refresh_status()` is the public repaint app_support already uses everywhere — app.py:1209
refreshes title + footer.) Add `TodoItem` to app_support's `..model.blocks` import if not
already there, and add `"apply_plan_change"`, `"sync_plan_surfaces"`, `"PLAN_PANEL_MIN_WIDTH"`
to its `__all__`.

In `src/amplifier_app_newtui/ui/app.py` (total net growth here must stay ≤ ~20 lines):

1. Imports: extend `from textual.containers import Container` → `import Container, Horizontal`;
   add `from .plan_panel import PlanPanel` to the relative-import block (near
   `from .lanes_panel import LanesPanel`, line ~37); add `PLAN_PANEL_WIDTH` if you prefer the
   constant in CSS via substitution — simpler: hardcode `37` in CSS with a comment.

2. `__init__` (after `self.lanes_panel = LanesPanel(id="lanes-panel")`, line 183):

```python
        self.plan_panel = PlanPanel(id="plan-panel")
```

3. `compose()` — replace `yield self.lanes_panel` (line 197) with:

```python
        with Horizontal(id="bottom-strip"):
            yield self.lanes_panel
            yield self.plan_panel
```

4. `CSS` (inside the existing `CSS = """ ... """`, before the closing quotes at line 134):

```
    /* Bottom strip (design 2026-07-21 §1): lanes flexible left, plan
       fixed right. Both children default display:none, height:auto —
       an empty strip occupies zero rows. */
    #bottom-strip { width: 100%; height: auto; }
    #bottom-strip > #lanes-panel { width: 1fr; }
    #bottom-strip > #plan-panel { width: 37; }
```

5. Replace the Task 2 stub body of `plan_changed` and add `on_resize`:

```python
    def plan_changed(self, items: tuple[TodoItem, ...]) -> None:
        app_support.apply_plan_change(self, items)

    def on_resize(self, event: events.Resize) -> None:
        app_support.sync_plan_surfaces(self)  # responsive ladder (D2)
```

### 4c. Verify and commit

```
uv run pytest tests/test_flow_plan_panel.py -q       # expect: 2 passed
uv run pytest tests/test_flow_lanes.py tests/test_ui_snapshots.py -q
    # lanes tests: agents turn has no todos → lanes panel keeps full width — expect: passed
    # snapshot: brainstorm turn has no todos → zero-height strip, pixels unchanged — expect: passed
    # If the snapshot FAILS, your strip is taking up rows while empty. Fix the CSS
    # (height: auto + display:none children); do NOT regenerate the old snapshot to paper over it.
uv run pytest -q && uv run ruff check . && uv run pyright src/
wc -l src/amplifier_app_newtui/ui/app.py             # expect: ≤ 1234 (was 1214 — ADR-0007
                                                     # budget is already blown; do not add to the debt)
git add -A && git commit -m "ui: bottom strip — lanes left, plan panel right, resize-aware (D2)"
```

---

## Task 5 — Footer fallback: `Plan N/M` below 90 cols

**Files:** `src/amplifier_app_newtui/ui/footer.py`, `src/amplifier_app_newtui/ui/app_support.py`,
`tests/test_ui_footer.py`, `tests/test_flow_plan_panel.py`

### 5a. Write the failing tests

In `tests/test_ui_footer.py` (uses the module-level `FULL_STATE` fixture, line 22), add:

```python
def test_plan_count_segment_appears_only_when_total_positive() -> None:
    """Design D2 ladder step 3: 'Plan N/M' rides the footer left segment."""
    state = FULL_STATE.model_copy(update={"plan_done": 2, "plan_total": 4})
    assert footer_left_text(state).endswith(" · Plan 2/4")
    assert "Plan" not in footer_left_text(FULL_STATE)  # default total=0 → absent
```

In `tests/test_flow_plan_panel.py`, extend `test_plan_panel_hides_below_90_cols` — add after the
first `assert not app.plan_panel.display`:

```python
        from amplifier_app_newtui.ui.footer import footer_left_text

        assert "Plan 0/3" in footer_left_text(app.footer_bar.state)
```

and after the final `assert not app.plan_panel.display`:

```python
        assert "Plan 3/3" in footer_left_text(app.footer_bar.state)
```

Run:

```
uv run pytest tests/test_ui_footer.py tests/test_flow_plan_panel.py -q
```

**Expect: FAIL** — `pydantic ValidationError: ... extra_forbidden` for `plan_done` (FooterState
is `extra="forbid"`).

### 5b. Implement

In `src/amplifier_app_newtui/ui/footer.py`:

1. `FooterState` (after `waiting`, line 65):

```python
    plan_done: int = Field(default=0, ge=0)
    plan_total: int = Field(default=0, ge=0)
    """Plan fallback count — non-zero only while the plan panel is hidden
    (narrow terminal); the footer then carries ``Plan N/M`` (design D2)."""
```

2. `footer_left_text` (after the `queued` append, line 88):

```python
    if state.plan_total:
        parts.append(f"Plan {state.plan_done}/{state.plan_total}")
```

3. `_repaint` (after the `queued` markup, line 248):

```python
        if state.plan_total:
            markup += f"[$dimmer]{SEPARATOR}[/][$dim]$plan_part[/]"
            substitutions["plan_part"] = f"Plan {state.plan_done}/{state.plan_total}"
```

   (Substitution, not inline f-string into markup — same injection-safety rule the rest of
   `_repaint` follows. `_update_wrap` already measures via `footer_left_text`, so wrapping keeps
   working for free.)

In `src/amplifier_app_newtui/ui/app_support.py`:

4. Add next to `sync_plan_surfaces`:

```python
def plan_footer_counts(app: NewTuiApp) -> tuple[int, int]:
    """``(done, total)`` for the footer — (0, 0) unless the panel is hidden
    while todos exist (the count never shows twice; design D2)."""
    if not app.plan_items or app.plan_panel.display:
        return (0, 0)
    done = sum(1 for item in app.plan_items if item.status == "completed")
    return (done, len(app.plan_items))
```

5. In `footer_state` (lines 571-585), add to the `FooterState(...)` kwargs:

```python
        plan_done=plan_footer_counts(app)[0],
        plan_total=plan_footer_counts(app)[1],
```

   (Or bind `done, total = plan_footer_counts(app)` above the return — your call; one call is
   cheaper.)

### 5c. Verify and commit

```
uv run pytest tests/test_ui_footer.py tests/test_flow_plan_panel.py -q   # expect: all passed
uv run pytest -q && uv run ruff check . && uv run pyright src/
git add -A && git commit -m "footer: Plan N/M fallback while the plan panel is hidden (<90 cols)"
```

---

## Task 6 — Retire `TodoBlock`: renderer, union kind, goldens (ONE commit)

**Files:** `src/amplifier_app_newtui/ui/transcript.py`, `src/amplifier_app_newtui/model/blocks.py`,
`tests/goldens/regen.py`, `tests/goldens/transcript_w{40,80,97,120}.txt`,
`tests/test_ui_transcript_render.py`, `docs/ARCHITECTURE.md`

`TodoBlock`'s complete consumer list (verified by grep — reducer already cleaned in Task 2):
`transcript.py:67,325,814` · `blocks.py:219,477,531` · `goldens/regen.py:63,120` ·
`test_ui_transcript_render.py:39,748` · `docs/ARCHITECTURE.md:356`. Nothing in `kernel/`,
`demo.py`, or persistence references it — full deletion is safe. `TodoItem`/`TodoStatus`
(blocks.py:208-216) **stay** — the panel and reducer use them.

### 6a. Make the coverage test fail first

`tests/test_golden_widths.py::test_canonical_set_covers_every_block_kind` pins
`set(kinds) == set(_RENDERERS)`. Start by deleting the renderer entry, run, watch it fail, then
finish the sweep:

1. `src/amplifier_app_newtui/ui/transcript.py`: delete `"todo": _render_todo,` (line 814).

```
uv run pytest tests/test_golden_widths.py -q
```

**Expect: FAIL** — "canonical set out of sync with the renderer table".

### 6b. Finish the deletion sweep

2. `transcript.py`: delete `_render_todo` and `TODO_BAR_WIDTH` (lines 321-370); remove
   `TodoBlock,` from the import at line 67.
3. `src/amplifier_app_newtui/model/blocks.py`: delete the `TodoBlock` class (lines 219-227);
   delete `| TodoBlock` from the union (line 477); delete `"TodoBlock",` from `__all__`
   (line 531). Keep `TodoStatus` + `TodoItem`, and reword `TodoItem`'s docstring to drop the
   dead cross-reference:

```python
class TodoItem(_FrozenModel):
    """One row of the ``todo`` tool's list, rendered by the ambient plan
    panel (``ui/plan_panel.py``): ``○`` pending / ``▶`` in-progress /
    ``✔`` completed."""
```

4. `tests/goldens/regen.py`: remove `TodoBlock,` (line 63) from the import (keep `TodoItem` only
   if still referenced — after removing the block it isn't, so remove both) and delete the
   `TodoBlock(id="g6b", ...)` entry (lines 120-127) from `canonical_blocks()`.
5. `tests/test_ui_transcript_render.py`: delete
   `test_todo_block_renders_header_glyphs_and_progress_bar` (lines 745-764) and the now-unused
   `TodoBlock`/`TodoItem` imports (lines 39-40). Keep the Task 2 reroute test.
6. `docs/ARCHITECTURE.md` line 356: remove the `TodoBlock` mention —
   `` `DoctorBlock`, `ImproveBlock`, `BrainstormIdea`, `LiveCommand`, `TodoBlock`. `` becomes
   `` `DoctorBlock`, `ImproveBlock`, `BrainstormIdea`, `LiveCommand`. `` (If the surrounding
   section counts block kinds, decrement it.)

### 6c. Regenerate goldens, verify, commit — all together

```
uv run python tests/goldens/regen.py
    # expect: "wrote .../transcript_w40.txt" (×4)
git diff tests/goldens/
    # expect: ONLY the "=== todo ===" section disappears from each width file.
    # Any other diff line means you broke an unrelated renderer — stop and fix.
uv run pytest -q && uv run ruff check . && uv run pyright src/
git add -A && git commit -m "model/ui: retire TodoBlock — plan lives in the ambient panel (goldens regen'd)"
```

(Golden regen in the same commit as the renderer change — DEVELOPMENT.md rule, design D6.)

---

## Task 7 — Snapshot: regression-lock the bottom strip

**Files:** `tests/test_ui_snapshots.py`,
`tests/__snapshots__/test_ui_snapshots/test_plan_panel_bottom_strip_snapshot.raw` (new)

### 7a. Write the failing test

Append to `tests/test_ui_snapshots.py` (add `BUILD_PROMPT` to the existing
`amplifier_app_newtui.kernel.demo` import):

```python
_PLAN_SNAPSHOT = (
    Path(__file__).parent
    / "__snapshots__"
    / "test_ui_snapshots"
    / "test_plan_panel_bottom_strip_snapshot.raw"
)


def test_plan_panel_bottom_strip_snapshot(monkeypatch) -> None:
    """Post-build-turn bottom strip: plan collapsed to 'Plan 3/3', still visible."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLORTERM", "truecolor")
    adapter = GatedDemoAdapter()
    app = NewTuiApp(adapter)

    async def run_build(pilot) -> None:
        await seed_done(pilot, app)
        app.submit_prompt(BUILD_PROMPT)
        assert await wait_for(pilot, lambda: app.plan_panel.display)
        adapter.release()
        assert await wait_for(pilot, lambda: not app.turn_active)
        assert app.plan_panel.plan_lines == ("Plan 3/3",)

    actual = take_svg_screenshot(app=app, terminal_size=SIZE, run_before=run_build)
    expected = _PLAN_SNAPSHOT.read_text(encoding="utf-8")
    assert expected == _clean_svg(expected), "snapshot must remain whitespace-clean"
    assert _clean_svg(actual) == expected
```

Run:

```
uv run pytest tests/test_ui_snapshots.py -q
```

**Expect: FAIL** — `FileNotFoundError` for the `.raw` file.

### 7b. Bootstrap the snapshot, eyeball it, commit

```bash
uv run python - <<'EOF'
"""One-time bootstrap for the plan-panel snapshot. Review before committing."""
import os
os.environ.pop("NO_COLOR", None)
os.environ["TERM"] = "xterm-256color"
os.environ["COLORTERM"] = "truecolor"

from textual._doc import take_svg_screenshot

from amplifier_app_newtui.kernel.demo import BUILD_PROMPT
from amplifier_app_newtui.ui.app import NewTuiApp
from tests.test_flow_helpers import SIZE, GatedDemoAdapter, seed_done, wait_for
from tests.test_ui_snapshots import _PLAN_SNAPSHOT, _clean_svg

adapter = GatedDemoAdapter()
app = NewTuiApp(adapter)

async def run_build(pilot) -> None:
    await seed_done(pilot, app)
    app.submit_prompt(BUILD_PROMPT)
    assert await wait_for(pilot, lambda: app.plan_panel.display)
    adapter.release()
    assert await wait_for(pilot, lambda: not app.turn_active)

svg = take_svg_screenshot(app=app, terminal_size=SIZE, run_before=run_build)
_PLAN_SNAPSHOT.write_text(_clean_svg(svg), encoding="utf-8")
print(f"wrote {_PLAN_SNAPSHOT}")
EOF
```

**Mandatory eyeball step** (this is where questionable taste gets caught):

```
cp tests/__snapshots__/test_ui_snapshots/test_plan_panel_bottom_strip_snapshot.raw /tmp/plan_panel.svg
open /tmp/plan_panel.svg
```

Check against the design mockup (§1): one thin `─` rule above the strip, the collapsed
`Plan 3/3` line bottom-right, transcript untouched above, composer + footer below. If you see
anything decorative you added yourself, remove it.

```
uv run pytest tests/test_ui_snapshots.py -q          # expect: 2 passed
uv run pytest -q && uv run ruff check . && uv run pyright src/
git add -A && git commit -m "tests: snapshot-lock the plan-panel bottom strip"
```

---

## Task 8 — Full gate + phase close-out

No new code. Prove the whole phase, then stop.

```
uv run pytest -q
    # expect: full suite green, 0 failures, no skips you introduced
uv run ruff check .
    # expect: All checks passed!
uv run pyright src/
    # expect: 0 errors, 0 warnings
uv run amplifier-newtui --demo
    # manual check: submit the build-turn demo prompt; watch the plan panel appear
    # bottom-right, progress ▶ through 3 steps, and collapse to "Plan 3/3".
    # Resize the terminal below 90 cols: panel hides, footer shows "Plan N/M".
git log --oneline -8
    # expect: the 7 commits from Tasks 1-7, in order
```

Also confirm the phase left the right seams for Phase 2 (do not build them):

- `app.plan_items` holds the latest list → Phase 2's `DelegateSummaryBlock.plan_final` will read
  it at turn end.
- `events.jsonl` already logs every `todo` ToolPre (`kernel/persistence.py`) → replay
  reconstruction needs no new persistence.

If anything above fails, fix it inside the offending task's scope and amend that task's commit —
do not pile fixups onto the end.

## Edge cases the tests must keep covering (from the design)

| Case | Covered by |
|---|---|
| No todos this turn → zero-height panel, strip lanes-only | Task 4 CSS + existing snapshot staying byte-identical |
| More todos than panel rows → `⋮ +N more`, window around `▶` | Task 1 overflow tests |
| All complete → header-only collapse, stays visible | Task 1 + Task 4 flow test |
| `list` op / empty payload → no `plan_changed` | Task 2 reroute test |
| Child-session todos → never reach the panel (v2) | free — `handle()` diverts child ToolPre before `_tool_pre` (reducer.py:460-462, 519-550); do not add code for this |
| < 90 cols → panel hides, footer `Plan N/M` | Tasks 4/5 flow + footer tests |
| Approval bar active | approval swaps inside `#composer-slot` (app_support.mount_approval) — the strip is a sibling, no collision; if a flow test says otherwise, the strip yields (hide_panel) |
