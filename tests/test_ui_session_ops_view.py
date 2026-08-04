"""Span renderers for the in-session ops commands (``ui/session_ops_view``)."""

from __future__ import annotations

from decimal import Decimal

from amplifier_app_tui.kernel.compaction import CompactionConfig
from amplifier_app_tui.kernel.session_manager import SessionSummary
from amplifier_app_tui.kernel.session_ops import ModelListing, SkillInfo, StatusInfo
from amplifier_app_tui.ui.session_ops_view import (
    diff_spans,
    mcp_spans,
    model_listing_spans,
    names_spans,
    session_detail_spans,
    sessions_spans,
    skill_loaded_spans,
    skills_spans,
    status_spans,
)


def _text(spans) -> str:
    return "".join(s.text for s in spans)


def test_model_listing_marks_the_current_model() -> None:
    spans = model_listing_spans(
        ModelListing(provider="anthropic", current="m2", available=("m1", "m2"))
    )
    text = _text(spans)
    assert "Model" in text and "anthropic" in text
    current = [s for s in spans if s.text.strip() == "m2"]
    assert current and current[0].bold  # active model is bold
    assert "▸" in text  # current-row glyph


def test_model_listing_no_provider() -> None:
    assert "no provider" in _text(model_listing_spans(ModelListing("", "")))


def test_status_spans_include_mode_and_cost() -> None:
    info = StatusInfo(
        session_id="abcdef123",
        provider="anthropic",
        model="m1",
        effort="high",
        messages=4,
        tools=7,
        agents=("explorer", "critic"),
    )
    text = _text(
        status_spans(
            info,
            mode="build",
            bundle="tui",
            session_short="abcdef",
            cost=Decimal("1.23"),
            compaction=CompactionConfig(
                max_tokens=200_000,
                auto_compact=True,
                compact_threshold=0.8,
            ),
        )
    )
    assert "build" in text
    assert "tui" in text
    assert "$1.23" in text
    assert "high" in text
    assert "2" in text  # agent count
    assert "auto compact" in text
    assert "on · 80% · 200,000 token window · estimated accounting" in text


def test_names_spans_roster_and_empty() -> None:
    assert "3 mounted" in _text(names_spans("Tools", ("a", "b", "c"), "none"))
    assert "none" in _text(names_spans("Tools", (), "none"))


def test_diff_spans_states() -> None:
    assert "not a git repo" in _text(diff_spans(None, staged=False))
    assert "clean" in _text(diff_spans("", staged=False))
    body = _text(diff_spans("diff --git a/x b/x\n+added line\n", staged=False))
    assert "added line" in body


def test_diff_spans_uses_theme_tokens_for_patch_semantics() -> None:
    spans = diff_spans(
        "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-old\n+new\n same",
        staged=False,
    )
    by_text = {span.text.strip(): span for span in spans}
    assert by_text["@@ -1 +1 @@"].style_token == "blue"
    assert by_text["@@ -1 +1 @@"].bold is True
    assert by_text["-old"].style_token == "red"
    assert by_text["-old"].bg_token == "bg-tab"
    assert by_text["+new"].style_token == "green"
    assert by_text["+new"].bg_token == "bg-tab"
    assert by_text["same"].style_token == "dim"


def test_diff_spans_truncates_long_patches() -> None:
    patch = "\n".join(f"+line {i}" for i in range(1000))
    text = _text(diff_spans(patch, staged=False))
    assert "more lines" in text


def test_diff_spans_staged_scope_wording() -> None:
    assert "staged" in _text(diff_spans("", staged=True))


def test_skills_spans_roster_and_empty() -> None:
    text = _text(
        skills_spans(
            (
                SkillInfo("design-patterns", "SOLID principles"),
                SkillInfo("simplify", "cut cruft"),
            )
        )
    )
    assert "2 available" in text
    assert "design-patterns" in text and "SOLID" in text
    assert "no skills" in _text(skills_spans(()))


def test_skills_spans_show_shortcut_aliases() -> None:
    text = _text(
        skills_spans(
            (
                SkillInfo("cranky-old-sam", "crusty review", shortcut="cosam"),
                SkillInfo("simplify", "cut cruft"),
            )
        )
    )
    assert "/cosam" in text  # the alias reads as its slash trigger
    assert "/simplify" not in text  # no fake alias for shortcut-less skills


def test_skill_loaded_spans_has_header_and_body() -> None:
    text = _text(skill_loaded_spans("simplify", "# simplify\n\ncut the cruft"))
    assert "Skill loaded" in text
    assert "simplify" in text
    assert "cut the cruft" in text


def test_mcp_spans_servers_and_empty() -> None:
    text = _text(mcp_spans({"postgres": "stdio · npx"}, ("mcp_postgres_query",)))
    assert "1 server" in text
    assert "postgres" in text
    assert "mcp_postgres_query" in text
    assert "no servers" in _text(mcp_spans({}, ()))


def test_sessions_spans_empty() -> None:
    assert "no stored sessions" in _text(sessions_spans(()))


def test_sessions_spans_lists_rows_and_marks_current() -> None:
    rows = (
        SessionSummary(session_id="abc12345ff", name="auth", bundle="tui", messages=6, mtime=0.0),
        SessionSummary(session_id="def67890aa", name="", bundle="dev", messages=2, mtime=0.0),
    )
    spans = sessions_spans(rows, current="abc12345")
    text = _text(spans)
    assert "Sessions" in text
    assert "abc12345" in text and "def67890" in text
    assert "auth" in text
    assert "6 msgs" in text
    assert "▸" in text  # current-session marker
    # The current session's short id renders bold.
    current = [sp for sp in spans if sp.text.strip() == "abc12345"]
    assert current and current[0].bold


def test_sessions_spans_renders_tag_chips() -> None:
    """Tags trail their row as dim ``#tag`` chips (client-UX delta)."""
    rows = (
        SessionSummary(
            session_id="abc12345ff",
            name="auth",
            bundle="tui",
            messages=6,
            mtime=0.0,
            tags=("frontend", "urgent"),
        ),
        SessionSummary(session_id="def67890aa", name="", bundle="dev", messages=2, mtime=0.0),
    )
    spans = sessions_spans(rows, current="abc12345")
    text = _text(spans)
    # Chips are sorted-as-stored and prefixed with the tag sigil.
    assert "#frontend" in text and "#urgent" in text
    # Rendered as dedicated muted-chip segments (not folded into the id/name).
    chip_spans = [sp for sp in spans if sp.text.strip().startswith("#")]
    assert chip_spans and chip_spans[0].style_token == "dimmer"
    assert "#frontend #urgent" in chip_spans[0].text
    # A tag-less row shows no chip sigil on its detail line.
    assert text.count("#") == 2  # exactly the two chips above


def test_sessions_spans_untagged_row_has_no_chip() -> None:
    """No tags → the row is byte-for-byte the pre-tags render (no ``#``)."""
    rows = (SessionSummary(session_id="abc12345ff", name="auth", bundle="tui", messages=6),)
    text = _text(sessions_spans(rows))
    assert "#" not in text


def test_sessions_spans_labels_recovered_state() -> None:
    """A recovered/corrupt session must show an explicit state chip -- never
    rendered as if it were a normal, healthy row (S2 compliance gap 3)."""
    rows = (SessionSummary(session_id="abc12345ff", name="", bundle="unknown", state="recovered"),)
    spans = sessions_spans(rows)
    text = _text(spans)
    assert "recovered" in text
    chip = next(sp for sp in spans if "recovered" in sp.text)
    assert chip.style_token == "orange"
    assert chip.bold is True


def test_sessions_spans_labels_corrupt_state() -> None:
    rows = (SessionSummary(session_id="deadbeef01", state="corrupt"),)
    spans = sessions_spans(rows)
    text = _text(spans)
    assert "corrupt" in text
    chip = next(sp for sp in spans if "corrupt" in sp.text)
    assert chip.style_token == "red"
    assert chip.bold is True


def test_sessions_spans_ok_state_shows_no_chip() -> None:
    """A healthy row must render exactly as before -- no stray state noise."""
    rows = (SessionSummary(session_id="abc12345ff", name="auth", bundle="tui", messages=6),)
    text = _text(sessions_spans(rows))
    assert "\u26a0" not in text  # no warning glyph on a healthy row


def test_sessions_spans_state_chip_and_tags_coexist() -> None:
    rows = (
        SessionSummary(
            session_id="abc12345ff",
            state="recovered",
            tags=("urgent",),
        ),
    )
    text = _text(sessions_spans(rows))
    assert "#urgent" in text
    assert "recovered" in text


def test_session_detail_spans_shows_full_id_unambiguously() -> None:
    """The detail surface must show the FULL id -- the table only ever
    shows the truncated short_id (S2 compliance gap 1)."""
    full_id = "abc123def456" + "0" * 20  # deliberately longer than short_id
    summary = SessionSummary(session_id=full_id, name="auth", bundle="tui", messages=3)
    spans = session_detail_spans(summary)
    text = _text(spans)
    assert full_id in text
    assert summary.short_id in text  # header still shows the short form too
    full_id_spans = [sp for sp in spans if sp.text.strip() == full_id]
    assert full_id_spans, "full id must appear as its own unambiguous span"
    assert full_id_spans[0].style_token == "bright"
    assert full_id_spans[0].bold is True


def test_session_detail_spans_explains_recovered_state() -> None:
    summary = SessionSummary(session_id="deadbeef" * 4, state="recovered")
    text = _text(session_detail_spans(summary))
    assert "recovered" in text
    assert "metadata.json could not be parsed" in text


def test_session_detail_spans_explains_corrupt_state() -> None:
    summary = SessionSummary(session_id="deadbeef" * 4, state="corrupt")
    text = _text(session_detail_spans(summary))
    assert "corrupt" in text
    assert "could not be summarized" in text


def test_session_detail_spans_ok_state_has_no_warning_glyph() -> None:
    summary = SessionSummary(session_id="deadbeef" * 4, name="auth", bundle="tui")
    text = _text(session_detail_spans(summary))
    assert "\u26a0" not in text


def test_session_detail_spans_includes_tags_and_metadata() -> None:
    summary = SessionSummary(
        session_id="deadbeef" * 4,
        name="auth refactor",
        bundle="tui",
        messages=7,
        turns=3,
        tags=("frontend", "urgent"),
    )
    text = _text(session_detail_spans(summary))
    assert "auth refactor" in text
    assert "tui" in text
    assert "7" in text
    assert "3" in text
    assert "#frontend #urgent" in text
