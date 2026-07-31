"""Prompt-stash store, JSONL serde and list renderer (model/prompt_stash.py).

Pure unit + golden coverage for the HGT capability re-expressed from the
opencode donor contract (see ``.ai/oc_donor.md``). No Textual, no core.
"""

from __future__ import annotations

from pathlib import Path

from amplifier_app_tui.model.prompt_stash import (
    MAX_STASH_ENTRIES,
    PromptStash,
    StashEntry,
    format_relative_age,
    parse_stash_jsonl,
    render_stash_list,
    serialize_stash,
    stash_preview,
)

GOLDEN = Path(__file__).resolve().parent / "goldens" / "stash_list.txt"


# -- store --------------------------------------------------------------------


def test_push_pop_is_lifo() -> None:
    stash = PromptStash()
    stash.push("first", now=1.0)
    stash.push("second", now=2.0)
    assert stash.count == 2
    popped = stash.pop()
    assert popped is not None and popped.text == "second"  # most-recent first
    assert stash.pop().text == "first"  # type: ignore[union-attr]
    assert stash.is_empty
    assert stash.pop() is None  # empty pop is a no-op


def test_blank_draft_is_never_stashed() -> None:
    stash = PromptStash()
    assert stash.push("   \n\t ") is None
    assert stash.is_empty


def test_recall_by_newest_first_display_index() -> None:
    stash = PromptStash()
    stash.push("oldest", now=1.0)
    stash.push("middle", now=2.0)
    stash.push("newest", now=3.0)
    # 1 == most recent, 3 == oldest (the order /stashes lists them).
    assert stash.recall(2).text == "middle"  # type: ignore[union-attr]
    assert stash.count == 2
    assert stash.recall(99) is None  # out of range
    assert stash.recall(0) is None
    remaining = [e.text for e in stash.entries]
    assert remaining == ["oldest", "newest"]


def test_cap_keeps_most_recent() -> None:
    stash = PromptStash()
    for i in range(MAX_STASH_ENTRIES + 10):
        stash.push(f"draft {i}", now=float(i))
    assert stash.count == MAX_STASH_ENTRIES
    texts = [e.text for e in stash.entries]
    assert texts[0] == "draft 10"  # oldest 10 dropped
    assert texts[-1] == f"draft {MAX_STASH_ENTRIES + 9}"


# -- serde --------------------------------------------------------------------


def test_serialize_then_parse_round_trips() -> None:
    entries = [
        StashEntry(text="alpha", stamped_at=1.5),
        StashEntry(text="beta\nline", stamped_at=2.0),
    ]
    text = serialize_stash(entries)
    assert text.endswith("\n")
    parsed = parse_stash_jsonl(text)
    assert [(e.text, e.stamped_at) for e in parsed] == [("alpha", 1.5), ("beta\nline", 2.0)]


def test_serialize_empty_is_empty_string() -> None:
    assert serialize_stash([]) == ""


def test_parse_drops_malformed_lines() -> None:
    raw = "\n".join(
        [
            '{"text": "keep me", "stamped_at": 3.0}',
            "not json at all",
            '{"no_text_field": 1}',
            "[1, 2, 3]",
            "   ",
            '{"text": "also kept"}',
        ]
    )
    parsed = parse_stash_jsonl(raw)
    assert [e.text for e in parsed] == ["keep me", "also kept"]
    assert parsed[1].stamped_at == 0.0  # missing stamp defaults


def test_parse_caps_to_max() -> None:
    raw = "\n".join(f'{{"text": "d{i}"}}' for i in range(MAX_STASH_ENTRIES + 5))
    parsed = parse_stash_jsonl(raw)
    assert len(parsed) == MAX_STASH_ENTRIES
    assert parsed[-1].text == f"d{MAX_STASH_ENTRIES + 4}"


# -- render helpers -----------------------------------------------------------


def test_preview_first_line_trimmed_and_truncated() -> None:
    assert stash_preview("  hello there  \nsecond line") == "hello there"
    long = "x" * 80
    preview = stash_preview(long)
    assert len(preview) == 50 and preview.endswith("…")


def test_relative_age_ladder() -> None:
    assert format_relative_age(5) == "just now"
    assert format_relative_age(120) == "2m ago"
    assert format_relative_age(3 * 3600) == "3h ago"
    assert format_relative_age(2 * 86400) == "2d ago"
    assert format_relative_age(-10) == "just now"  # clock skew clamps to 0


def test_render_stash_list_matches_golden() -> None:
    entries = (
        StashEntry(text="first stashed draft", stamped_at=10000.0 - 3 * 3600),
        StashEntry(text="a multiline draft\nsecond line\nthird line", stamped_at=10000.0 - 120),
        StashEntry(
            text="a very long single line draft that should be truncated to fifty chars",
            stamped_at=10000.0 - 5,
        ),
    )
    rendered = render_stash_list(entries, now=10000.0)
    expected = GOLDEN.read_text(encoding="utf-8")
    assert rendered == expected, (
        "stash_list renderer changed — regenerate the golden:\n"
        "  uv run python tests/goldens/regen_stash.py\nthen review the diff."
    )


def test_render_empty_is_a_dimmer_notice_line() -> None:
    assert "no stashed drafts" in render_stash_list((), now=0.0)
