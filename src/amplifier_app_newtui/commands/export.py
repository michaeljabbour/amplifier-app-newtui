"""``/export`` — transcript → markdown export.

Pure rendering: user lines become ``> `` blockquotes, answers become
prose (span texts joined), tool lines become fenced code blocks; every
other block kind is UI chrome and is skipped. File I/O lives in
:func:`write_export` with an injectable root and clock so the app-side
``export_transcript`` action stays a one-liner.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from ..model.blocks import Answer, ToolLine, TranscriptBlock, UserLine


def _render_block(block: TranscriptBlock) -> str | None:
    """Markdown for one block; ``None`` for kinds the export skips."""
    if isinstance(block, UserLine):
        return "\n".join(f"> {line}" for line in block.text.splitlines())
    if isinstance(block, Answer):
        return "".join(segment.text for segment in block.spans)
    if isinstance(block, ToolLine):
        return "\n".join(("```", block.summary, *block.body, "```"))
    return None


def render_transcript_markdown(blocks: Iterable[TranscriptBlock]) -> str:
    """Markdown for the exportable blocks; ``""`` for an empty transcript.

    Sections are blank-line separated; the document ends with a newline.
    """
    sections = [text for text in map(_render_block, blocks) if text is not None]
    if not sections:
        return ""
    return "\n\n".join(sections) + "\n"


def export_filename(session_short: str, now: datetime) -> str:
    """``<session-short>-<YYYYMMDD-HHMMSS>.md``."""
    return f"{session_short}-{now:%Y%m%d-%H%M%S}.md"


def write_export(
    blocks: Iterable[TranscriptBlock],
    session_short: str,
    now: datetime,
    root: Path,
) -> Path:
    """Write the markdown export under *root* (created if missing); return the path."""
    root.mkdir(parents=True, exist_ok=True)
    path = root / export_filename(session_short, now)
    path.write_text(render_transcript_markdown(blocks), encoding="utf-8")
    return path


__all__ = ["export_filename", "render_transcript_markdown", "write_export"]
