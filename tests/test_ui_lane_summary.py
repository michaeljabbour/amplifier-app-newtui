"""`_lane_result_summary`: a delegate's Markdown result → clean one-line lane label (#91)."""

from __future__ import annotations

from amplifier_app_newtui.ui.reducer import _lane_result_summary


def test_strips_heading_and_inline_markdown() -> None:
    raw = "## What Amplifier attractors do\n\n**Core concept.** An attractor is a workflow."
    out = _lane_result_summary(raw)
    assert out.startswith("What Amplifier attractors do")
    assert "##" not in out and "**" not in out


def test_takes_first_nonempty_line() -> None:
    assert _lane_result_summary("\n\n  # Title line  \nbody") == "Title line"


def test_prefers_first_sentence_when_long() -> None:
    raw = "An attractor is a multi-stage AI workflow defined as a DOT graph. More detail follows here."
    out = _lane_result_summary(raw, width=80)
    assert out == "An attractor is a multi-stage AI workflow defined as a DOT graph"
    assert "More detail" not in out


def test_unwraps_links_and_truncates() -> None:
    assert _lane_result_summary("see [the docs](http://x)") == "see the docs"
    long = "x" * 200
    assert len(_lane_result_summary(long)) <= 52


def test_empty_result_is_empty() -> None:
    assert _lane_result_summary("") == ""
    assert _lane_result_summary("   \n  ") == ""
