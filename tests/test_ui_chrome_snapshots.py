"""Rendered width-matrix snapshots for the title bar chrome (D4 gap 3).

The reviewer's gap: existing coverage (``test_ui_footer.py``,
``test_ui_chrome.py``) asserts substrings and cell-width invariants, but
none of it is a RENDERED snapshot -- a byte-for-byte pin of what actually
paints. This follows the two mechanisms the repo already has rather than
inventing a third:

- the ADR-0007 width matrix (40/80/97/120 -- ``tests/goldens/regen.py``,
  ``test_ui_footer.py``'s own ``_SUPPORTED_WIDTHS``);
- rendered SVG snapshots via ``textual._doc.take_svg_screenshot`` saved
  under ``tests/__snapshots__/`` (``test_ui_snapshots.py``,
  ``test_ui_composer_status_seam.py``).

Like ``test_ui_composer_status_seam.py``'s focused seam harness (composer +
footer only, not the whole app), this mounts ONLY the title bar -- the
whole-screen states are already covered by ``test_ui_snapshots.py``. One
long, realistic resolved bundle URI (the shape ``kernel/config.
resolve_bundle_source`` actually produces, not a bare name) is used at
EVERY width so the four snapshots together are a rendered proof of D4 gap
2's viewport-aware fitting ladder: shown in full where there is room (120),
progressively truncated with a visible ellipsis where there is not
(97/80/40), never wrapping the ``height: 1`` row down onto the composer.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from textual._doc import take_svg_screenshot
from textual.app import App, ComposeResult

from amplifier_app_tui.ui.chrome import TitleBar
from amplifier_app_tui.ui.themes import DEFAULT_THEME, register_themes, theme_id

_SNAPSHOT_DIR = Path(__file__).parent / "__snapshots__" / "test_ui_chrome_snapshots"
_DYNAMIC_TERMINAL_ID = re.compile(r"terminal-\d+")

GOLDEN_WIDTHS = (40, 80, 97, 120)
"""The ADR-0007 width matrix -- the same one ``tests/goldens/regen.py`` and
``test_ui_footer.py``'s ``_SUPPORTED_WIDTHS`` treat as "every width the app
is expected to run at". Reused verbatim rather than a second definition."""

LONG_REALISTIC_BUNDLE_URI = (
    "git+https://github.com/microsoft/amplifier-foundation@main#bundles/anchors"
)
"""A real shape of RESOLVED bundle URI (``kernel/config.resolve_bundle_source``
-- an actual fetchable include URI, not a bare name like "anchors"). 74
cells: long enough that the viewport-aware fitting (D4 gap 2) is actually
exercised across the whole matrix -- shown in full at 120, truncated with
an ellipsis at 97/80/40 -- rather than trivially fitting everywhere."""


def _clean_svg(value: str) -> str:
    """Remove Textual's per-process namespace and trailing whitespace."""
    stable_ids = _DYNAMIC_TERMINAL_ID.sub("terminal-SNAPSHOT", value)
    return "\n".join(line.rstrip() for line in stable_ids.splitlines()) + "\n"


def snapshot_path(width: int) -> Path:
    return _SNAPSHOT_DIR / f"test_title_bar_w{width}.raw"


def _assert_matches_snapshot(actual: str, width: int) -> None:
    path = snapshot_path(width)
    expected = path.read_text(encoding="utf-8")
    assert expected == _clean_svg(expected), "snapshot must remain whitespace-clean"
    assert _clean_svg(actual) == expected, (
        f"title bar rendering changed at width {width} -- if intentional, "
        "regenerate the .raw snapshot (tests/test_ui_chrome_snapshots.py "
        "docstring) and review the diff"
    )


class TitleBarHarness(App[None]):
    """Just the title bar chrome, isolated -- mirrors
    ``test_ui_composer_status_seam.py``'s focused-fragment harness rather
    than a whole-screen snapshot (``test_ui_snapshots.py`` already covers
    those two full-app states).
    """

    def __init__(self) -> None:
        super().__init__()
        register_themes(self)
        self.theme = theme_id(DEFAULT_THEME)
        self.title_bar = TitleBar(id="title")

    def compose(self) -> ComposeResult:
        yield self.title_bar


def _render_title_bar(width: int, *, state_text: str, bundle_uri: str, session_short: str) -> str:
    app = TitleBarHarness()

    async def set_state(pilot) -> None:
        app.title_bar.state_text = state_text
        app.title_bar.bundle_uri = bundle_uri
        app.title_bar.session_short = session_short
        await pilot.pause()

    return take_svg_screenshot(app=app, terminal_size=(width, 3), run_before=set_state)


@pytest.mark.parametrize("width", GOLDEN_WIDTHS)
def test_title_bar_snapshot_at_golden_width(monkeypatch, width: int) -> None:
    """Rendered (not just asserted-substring) proof of D4 gap 2's
    viewport-aware fitting at every ADR-0007 width, with a long/realistic
    resolved bundle URI so the fitting behavior is actually captured."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("COLORTERM", "truecolor")
    actual = _render_title_bar(
        width,
        state_text="ready",
        bundle_uri=LONG_REALISTIC_BUNDLE_URI,
        session_short="a1b2c3",
    )
    _assert_matches_snapshot(actual, width)
