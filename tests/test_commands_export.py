"""``/export`` — transcript-to-markdown rendering and file writing.

Pure-logic tests over ``commands.export``: block → markdown mapping
(user lines as ``> `` quotes, answers as prose, tool lines as fences),
filename shape, and the injectable-root file write.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from amplifier_app_newtui.commands.export import (
    export_filename,
    render_transcript_markdown,
    write_export,
)
from amplifier_app_newtui.model.blocks import (
    Answer,
    Narration,
    Segment,
    SessionBanner,
    ToolLine,
    UserLine,
)

NOW = datetime(2026, 1, 1, 12, 34, 56)


def test_user_line_renders_as_blockquote() -> None:
    blocks = (UserLine(id="b1", text="fix the flaky test", mode="build"),)
    assert render_transcript_markdown(blocks) == "> fix the flaky test\n"


def test_multiline_user_line_prefixes_every_line() -> None:
    blocks = (UserLine(id="b1", text="line one\nline two"),)
    assert render_transcript_markdown(blocks) == "> line one\n> line two\n"


def test_answer_joins_spans_as_prose() -> None:
    blocks = (
        Answer(
            id="b2",
            spans=(
                Segment(text="The fix is in "),
                Segment(text="app.py", style_token="teal"),
                Segment(text=", shipped."),
            ),
        ),
    )
    assert render_transcript_markdown(blocks) == "The fix is in app.py, shipped.\n"


def test_tool_line_renders_fenced_with_body() -> None:
    blocks = (
        ToolLine(
            id="b3",
            summary="Ran uv run pytest",
            body=("42 passed", "0 failed"),
            status="completed",
        ),
    )
    assert render_transcript_markdown(blocks) == (
        "```\nRan uv run pytest\n42 passed\n0 failed\n```\n"
    )


def test_tool_line_renders_fenced_without_body() -> None:
    blocks = (ToolLine(id="b3", summary="Read blocks.py"),)
    assert render_transcript_markdown(blocks) == "```\nRead blocks.py\n```\n"


def test_non_exported_kinds_are_skipped() -> None:
    blocks = (
        SessionBanner(id="b0", headline="Amplifier 1.0"),
        UserLine(id="b1", text="hello"),
        Narration(id="b2", text="scanning the repo"),
    )
    assert render_transcript_markdown(blocks) == "> hello\n"


def test_blocks_separated_by_blank_lines() -> None:
    blocks = (
        UserLine(id="b1", text="hello"),
        Answer(id="b2", spans=(Segment(text="hi there"),)),
        ToolLine(id="b3", summary="Ran ls", status="completed"),
    )
    assert render_transcript_markdown(blocks) == ("> hello\n\nhi there\n\n```\nRan ls\n```\n")


def test_empty_transcript_renders_empty_string() -> None:
    assert render_transcript_markdown(()) == ""


def test_export_filename_format() -> None:
    assert export_filename("a1b2c3", NOW) == "a1b2c3-20260101-123456.md"


def test_write_export_creates_root_and_returns_path(tmp_path: Path) -> None:
    root = tmp_path / "exports"  # does not exist yet
    blocks = (UserLine(id="b1", text="hello"),)
    path = write_export(blocks, "a1b2c3", NOW, root)
    assert path == root / "a1b2c3-20260101-123456.md"
    assert path.read_text(encoding="utf-8") == "> hello\n"


# --------------------------------------------------------------------------
# secret scrubbing at the export sink (issue #23)
# --------------------------------------------------------------------------

_AWS_KEY = "AKIAIOSFODNN7EXAMPLE"


def test_export_redacts_aws_key_in_answer() -> None:
    blocks = (
        Answer(id="b1", spans=(Segment(text=f"your key is {_AWS_KEY} keep it"),)),
    )
    out = render_transcript_markdown(blocks)
    assert _AWS_KEY not in out
    assert out == "your key is [REDACTED] keep it\n"


def test_export_redacts_bearer_token_in_tool_body() -> None:
    blocks = (
        ToolLine(
            id="b1",
            summary="curl the API",
            body=("Authorization: Bearer sk-live-abcdef123456",),
            status="completed",
        ),
    )
    out = render_transcript_markdown(blocks)
    assert "sk-live-abcdef123456" not in out
    assert "Bearer [REDACTED]" in out


def test_export_redacts_secret_in_user_line() -> None:
    blocks = (UserLine(id="b1", text=f"here: {_AWS_KEY}"),)
    out = render_transcript_markdown(blocks)
    assert _AWS_KEY not in out
    assert out == "> here: [REDACTED]\n"


def test_write_export_persists_redacted_markdown(tmp_path: Path) -> None:
    blocks = (Answer(id="b1", spans=(Segment(text=f"key {_AWS_KEY}"),)),)
    path = write_export(blocks, "a1b2c3", NOW, tmp_path / "exports")
    written = path.read_text(encoding="utf-8")
    assert _AWS_KEY not in written
    assert written == "key [REDACTED]\n"
