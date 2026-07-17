"""``/copy`` — extract the last assistant answer for the clipboard.

Pure extraction only: the newest ``clickable=True`` :class:`Answer`
(``clickable=False`` marks answer-*shaped* recap/agent-tree lines that
are not real answers). Clipboard I/O (OSC 52) lives in the app-side
``copy_answer`` action so this stays model + stdlib only.
"""

from __future__ import annotations

from collections.abc import Sequence

from ..model.blocks import Answer, TranscriptBlock


def last_answer_text(blocks: Sequence[TranscriptBlock]) -> str | None:
    """Span-joined text of the last real answer; ``None`` if there is none."""
    for block in reversed(blocks):
        if isinstance(block, Answer) and block.clickable:
            return "".join(segment.text for segment in block.spans)
    return None


__all__ = ["last_answer_text"]
