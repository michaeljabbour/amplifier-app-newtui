"""Golden width-matrix tests for the transcript renderer (ADR-0007).

``render_block(block, width)`` is a pure function — these tests pin its
output at every matrix width (40/80/97/120) over the canonical block set
(one block of every ``TranscriptBlock`` kind, built from DemoRuntime's
seed strings) against the checked-in goldens in ``tests/goldens/``.

A diff here means the renderer's visible output changed. If the change is
intentional, regenerate and review:

    uv run python tests/goldens/regen.py
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from rich.cells import cell_len

from amplifier_app_newtui.model.blocks import TranscriptBlock
from amplifier_app_newtui.ui.segments import lines_plain
from amplifier_app_newtui.ui.transcript import (
    _RENDERERS,  # noqa: PLC2701 — coverage check over the renderer table
    render_block,
)

_REGEN_PATH = Path(__file__).resolve().parent / "goldens" / "regen.py"
_spec = importlib.util.spec_from_file_location("goldens_regen", _REGEN_PATH)
assert _spec is not None and _spec.loader is not None
regen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(regen)

WIDTHS: tuple[int, ...] = regen.WIDTHS


def _blocks() -> tuple[TranscriptBlock, ...]:
    return regen.canonical_blocks()


def test_width_matrix_is_the_adr_matrix() -> None:
    assert WIDTHS == (40, 80, 97, 120)


def test_canonical_set_covers_every_block_kind() -> None:
    """One block of every kind the renderer supports — no kind untested."""
    kinds = [block.kind for block in _blocks()]
    assert len(kinds) == len(set(kinds)), "duplicate kind in canonical set"
    assert set(kinds) == set(_RENDERERS), (
        "canonical set out of sync with the renderer table; "
        "update tests/goldens/regen.py and regenerate"
    )


@pytest.mark.parametrize("width", WIDTHS)
def test_golden_width_matrix(width: int) -> None:
    path = regen.golden_path(width)
    assert path.exists(), f"missing golden {path.name}; run tests/goldens/regen.py"
    expected = path.read_text(encoding="utf-8")
    actual = regen.golden_text(width)
    assert actual == expected, (
        f"transcript rendering changed at width {width}. If intentional, "
        "regenerate goldens (uv run python tests/goldens/regen.py) and review the diff."
    )


@pytest.mark.parametrize("width", WIDTHS)
def test_turn_rule_fills_width_exactly(width: int) -> None:
    """The turn rule is the width-parametric block: rule + label == width."""
    block = next(b for b in _blocks() if b.kind == "turn_rule")
    lines = render_block(block, width)
    label_width = cell_len(block.label)
    if width >= label_width + 4:
        assert len(lines) == 1
        assert cell_len(lines_plain(lines)) == width
    else:
        # Too narrow to share a line: full-width rule, right-aligned label.
        assert len(lines) == 2
        rule_line, label_line = (lines_plain([line]) for line in lines)
        assert set(rule_line) == {"─"}
        assert cell_len(rule_line) == width
        assert label_line.endswith(block.label)
        assert cell_len(label_line) == max(width, label_width)


@pytest.mark.parametrize("width", WIDTHS)
def test_rendering_is_deterministic(width: int) -> None:
    """Same (block, width) → same lines; pure function, no hidden state."""
    for block in _blocks():
        assert render_block(block, width) == render_block(block, width)
