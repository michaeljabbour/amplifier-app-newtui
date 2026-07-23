"""``/context`` usage computation → ContextBlock (DESIGN-SPEC §6/§10).

Pure math: token counts in → :class:`ContextUsage` →
:class:`~amplifier_app_newtui.model.blocks.ContextBlock` with the
``████████░░`` bar segmented conversation / tools / memory / free. The
mockup line:

    · Context  41% of 200k
      ████████░░░░░░░░░░░░  conversation 52k · tools 18k · memory 8k · free 118k
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..model.blocks import ContextBlock

DEFAULT_WINDOW_TOKENS = 200_000
DEFAULT_BAR_WIDTH = 20
"""Bar cell count in the mockup's ``/context`` line (20 × 5% cells)."""


def format_tokens(tokens: int) -> str:
    """``742`` / ``4.1k`` / ``52k`` / ``1.2m`` — mockup token formatting."""
    if tokens < 1_000:
        return str(tokens)
    if tokens < 1_000_000:
        thousands = tokens / 1_000
        if thousands < 10 and round(thousands, 1) != round(thousands):
            return f"{thousands:.1f}k"
        return f"{round(thousands)}k"
    return f"{tokens / 1_000_000:.1f}m"


class ContextUsage(BaseModel):
    """Token accounting for the active context window.

    ``conversation`` / ``tools`` / ``memory`` are the used buckets in the
    order the bar renders them; ``window`` is the model context window
    (200k default per the spec header ``NN% of 200k``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    conversation: int = Field(default=0, ge=0)
    tools: int = Field(default=0, ge=0)
    memory: int = Field(default=0, ge=0)
    window: int = Field(default=DEFAULT_WINDOW_TOKENS, gt=0)

    @model_validator(mode="after")
    def _fits_window(self) -> "ContextUsage":
        if self.used > self.window:
            raise ValueError(f"used tokens ({self.used}) exceed the context window ({self.window})")
        return self

    @property
    def used(self) -> int:
        return self.conversation + self.tools + self.memory

    @property
    def free(self) -> int:
        return self.window - self.used

    @property
    def used_pct(self) -> int:
        """Whole-number percentage for the ``NN% of 200k`` header."""
        return round(self.used / self.window * 100)

    @property
    def window_label(self) -> str:
        """``200k`` — the header's window figure."""
        return format_tokens(self.window)

    def header_text(self) -> str:
        """``Context  41% of 200k`` (the ``· `` glyph is the renderer's)."""
        return f"Context  {self.used_pct}% of {self.window_label}"


def _bar_cells(values: tuple[int, ...], bar_width: int) -> tuple[int, ...]:
    """Largest-remainder apportionment of *bar_width* cells over *values*.

    Guarantees cells sum exactly to ``bar_width`` and any non-zero bucket
    keeps at least one cell (so tiny-but-real usage stays visible).
    """
    total = sum(values)
    if total <= 0:
        return tuple(0 for _ in values)
    exact = [value / total * bar_width for value in values]
    cells = [int(x) for x in exact]
    # Non-zero buckets never render as zero cells.
    for index, value in enumerate(values):
        if value > 0 and cells[index] == 0:
            cells[index] = 1
    # Reconcile to the exact bar width, adjusting the largest remainders
    # (shrink the biggest allocations first when over).
    while sum(cells) > bar_width:
        candidates = [i for i, c in enumerate(cells) if c > (1 if values[i] > 0 else 0)]
        if not candidates:
            break
        largest = max(candidates, key=lambda i: cells[i])
        cells[largest] -= 1
    remainders = sorted(range(len(values)), key=lambda i: exact[i] - int(exact[i]), reverse=True)
    cursor = 0
    while sum(cells) < bar_width and remainders:
        cells[remainders[cursor % len(remainders)]] += 1
        cursor += 1
    return tuple(cells)


def usage_segments(
    usage: ContextUsage, bar_width: int = DEFAULT_BAR_WIDTH
) -> tuple[tuple[str, int], ...]:
    """``(label, cells)`` pairs in conversation/tools/memory/free order.

    Labels carry the mockup legend text (``conversation 52k``); cells sum
    to *bar_width* for the ``████████░░`` bar.
    """
    values = (usage.conversation, usage.tools, usage.memory, usage.free)
    names = ("conversation", "tools", "memory", "free")
    cells = _bar_cells(values, bar_width)
    return tuple(
        (f"{name} {format_tokens(value)}", cell)
        for name, value, cell in zip(names, values, cells, strict=True)
    )


def build_context_block(
    block_id: str, usage: ContextUsage, *, bar_width: int = DEFAULT_BAR_WIDTH
) -> ContextBlock:
    """Assemble the ``/context`` transcript block from a usage snapshot."""
    return ContextBlock(
        id=block_id,
        used_pct=usage.used_pct,
        window_label=usage.window_label,
        segments=usage_segments(usage, bar_width),
        bar_width=bar_width,
    )


__all__ = [
    "ContextUsage",
    "DEFAULT_BAR_WIDTH",
    "DEFAULT_WINDOW_TOKENS",
    "build_context_block",
    "format_tokens",
    "usage_segments",
]
