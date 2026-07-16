"""Startup terminal-capability probe: kitty keyboard protocol support
(DESIGN-SPEC §12; docs/tui-v3-cohesive.md §"Bottom status bar"/§9).

Textual requests progressive keyboard enhancement unconditionally, so
the functional bindings never change — both ``shift+enter`` and the
works-everywhere ``alt+enter`` stay bound (see ``composer.py``). This
probe only decides which chord the UI *advertises*: ``shift+enter``
when the terminal is known to speak the kitty keyboard protocol (or
xterm modifyOtherKeys), ``alt+enter queue`` otherwise.

Pure environment sniff, deliberately conservative: an unknown terminal
gets the fallback label, because advertising ``shift+enter`` on a
legacy terminal points at a chord that is never delivered, while
``alt+enter`` works everywhere.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

_KITTY_TERM_PREFIXES = ("xterm-kitty", "foot", "wezterm", "ghostty", "rio")
"""``TERM`` prefixes owned by terminals that speak the kitty protocol."""

_KITTY_TERM_PROGRAMS = frozenset({"kitty", "wezterm", "ghostty", "rio"})
"""``TERM_PROGRAM`` values (lowercased) with kitty-protocol support."""

_KITTY_ENV_MARKERS = ("KITTY_WINDOW_ID", "WEZTERM_PANE", "GHOSTTY_RESOURCES_DIR", "WT_SESSION")
"""Env vars whose presence identifies a capable terminal (Windows
Terminal delivers shift+enter via win32-input-mode)."""

_ITERM_MIN_VERSION = (3, 5)
"""iTerm2 gained the kitty keyboard protocol in 3.5."""


def probe_kitty_protocol(environ: Mapping[str, str] | None = None) -> bool:
    """True when the hosting terminal is known to deliver shift+enter.

    Reads ``os.environ`` unless an explicit mapping is passed (tests).
    """
    env = os.environ if environ is None else environ
    if env.get("TEXTUAL_DISABLE_KITTY_KEY"):
        return False  # Textual won't request the protocol at all
    term = env.get("TERM", "")
    if "TMUX" in env or term.startswith(("screen", "tmux")):
        return False  # multiplexer passthrough is not dependable
    if any(marker in env for marker in _KITTY_ENV_MARKERS):
        return True
    if term.startswith(_KITTY_TERM_PREFIXES):
        return True
    if "XTERM_VERSION" in env:
        return True  # genuine xterm: modifyOtherKeys delivers shift+enter
    program = env.get("TERM_PROGRAM", "").lower()
    if program in _KITTY_TERM_PROGRAMS:
        return True
    if program == "iterm.app":
        return _parse_version(env.get("TERM_PROGRAM_VERSION", "")) >= _ITERM_MIN_VERSION
    return False


def _parse_version(raw: str) -> tuple[int, int]:
    """Leading ``major.minor`` of a version string; (0, 0) when unparsable."""
    match = re.match(r"(\d+)\.(\d+)", raw)
    return (int(match.group(1)), int(match.group(2))) if match else (0, 0)
