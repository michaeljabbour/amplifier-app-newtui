"""``/config`` segment renderers (pure spans, no Textual)."""

from __future__ import annotations

from amplifier_app_newtui.model.config import (
    ConfigChange,
    ConfigItem,
    ConfigSnapshotView,
    default_config_state,
)
from amplifier_app_newtui.ui.config_view import (
    config_diff_spans,
    config_help_spans,
    config_item_spans,
    config_show_spans,
)


def _text(spans) -> str:
    return "".join(s.text for s in spans)


def test_show_lists_every_category_with_counts() -> None:
    view = ConfigSnapshotView.of(default_config_state("anchors"))
    text = _text(config_show_spans(view))
    assert "Config" in text
    for category in ("context", "tools", "hooks", "providers", "agents"):
        assert category in text
    assert "read_file" in text
    # Read-only hooks are labelled so the user knows why they can't toggle.
    assert "(read-only)" in text


def test_show_reflects_a_disable_and_change_count() -> None:
    state = default_config_state("anchors")
    state.toggle("tools", "bash", enable=False)
    view = ConfigSnapshotView.of(state)
    text = _text(config_show_spans(view))
    assert "1 change(s)" in text
    # The disabled item uses the hollow glyph, the enabled ones the filled one.
    assert "\u25cb " in text and "\u25cf " in text


def test_show_single_category_filters() -> None:
    view = ConfigSnapshotView.of(default_config_state("anchors"))
    text = _text(config_show_spans(view, category="providers"))
    assert "providers" in text and "anthropic" in text
    assert "read_file" not in text  # tools section not rendered


def test_show_overrides_section() -> None:
    state = default_config_state("anchors")
    state.set_value("session.reasoning_effort", "high")
    view = ConfigSnapshotView.of(state)
    text = _text(config_show_spans(view))
    assert "set values" in text
    assert "session.reasoning_effort" in text


def test_item_spans_found_and_missing() -> None:
    item = ConfigItem("tools", "bash", True, "tool-shell")
    found = _text(config_item_spans(item, category="tools", name="bash"))
    assert "bash" in found and "/config tools disable bash" in found
    missing = _text(config_item_spans(None, category="tools", name="ghost"))
    assert "no tools item named 'ghost'" in missing


def test_diff_spans_empty_and_populated() -> None:
    assert "no changes from session start" in _text(config_diff_spans(()))
    changes = (
        ConfigChange("tools", "bash", "disabled"),
        ConfigChange("set", "x", "= 1"),
    )
    text = _text(config_diff_spans(changes))
    assert "2 change(s)" in text and "tools bash" in text and "disabled" in text


def test_help_spans_lists_subcommands() -> None:
    text = _text(config_help_spans())
    for token in ("show", "diff", "save", "set", "disable"):
        assert token in text
