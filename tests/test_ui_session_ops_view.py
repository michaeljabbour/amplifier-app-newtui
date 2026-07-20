"""Span renderers for the in-session ops commands (``ui/session_ops_view``)."""

from __future__ import annotations

from decimal import Decimal

from amplifier_app_newtui.kernel.compaction import CompactionConfig
from amplifier_app_newtui.kernel.session_ops import ModelListing, SkillInfo, StatusInfo
from amplifier_app_newtui.ui.session_ops_view import (
    diff_spans,
    mcp_spans,
    model_listing_spans,
    names_spans,
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
        session_id="abcdef123", provider="anthropic", model="m1", effort="high",
        messages=4, tools=7, agents=("explorer", "critic"),
    )
    text = _text(
        status_spans(
            info,
            mode="build",
            bundle="newtui",
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
    assert "newtui" in text
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
    text = _text(skills_spans((
        SkillInfo("design-patterns", "SOLID principles"),
        SkillInfo("simplify", "cut cruft"),
    )))
    assert "2 available" in text
    assert "design-patterns" in text and "SOLID" in text
    assert "no skills" in _text(skills_spans(()))


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
