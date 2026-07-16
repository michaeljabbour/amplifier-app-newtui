"""Startup capability probe: which queue chord the UI advertises
(DESIGN-SPEC §12; docs/tui-v3-cohesive.md — 'the label is probe-dependent').
"""

from __future__ import annotations

from amplifier_app_newtui.ui.term_probe import probe_kitty_protocol


def test_kitty_protocol_terminals_detected() -> None:
    assert probe_kitty_protocol({"TERM": "xterm-kitty", "KITTY_WINDOW_ID": "1"})
    assert probe_kitty_protocol({"TERM": "xterm-256color", "WEZTERM_PANE": "0"})
    assert probe_kitty_protocol({"TERM": "xterm-ghostty", "GHOSTTY_RESOURCES_DIR": "/x"})
    assert probe_kitty_protocol({"TERM": "foot"})
    assert probe_kitty_protocol({"TERM": "xterm-256color", "TERM_PROGRAM": "WezTerm"})
    assert probe_kitty_protocol({"TERM": "xterm-256color", "WT_SESSION": "guid"})
    assert probe_kitty_protocol({"TERM": "xterm", "XTERM_VERSION": "XTerm(390)"})


def test_iterm_needs_3_5() -> None:
    base = {"TERM": "xterm-256color", "TERM_PROGRAM": "iTerm.app"}
    assert probe_kitty_protocol({**base, "TERM_PROGRAM_VERSION": "3.5.13"})
    assert probe_kitty_protocol({**base, "TERM_PROGRAM_VERSION": "4.0"})
    assert not probe_kitty_protocol({**base, "TERM_PROGRAM_VERSION": "3.4.19"})
    assert not probe_kitty_protocol(base)


def test_legacy_terminals_fall_back() -> None:
    assert not probe_kitty_protocol({})
    assert not probe_kitty_protocol({"TERM": "xterm-256color"})
    assert not probe_kitty_protocol({"TERM": "xterm-256color", "TERM_PROGRAM": "Apple_Terminal"})
    assert not probe_kitty_protocol({"TERM": "xterm-256color", "TERM_PROGRAM": "vscode"})


def test_multiplexers_and_explicit_disable_fall_back() -> None:
    # tmux/screen passthrough is not dependable even under a capable outer terminal
    assert not probe_kitty_protocol({"TERM": "tmux-256color", "TMUX": "/tmp/t,1,0", "KITTY_WINDOW_ID": "1"})
    assert not probe_kitty_protocol({"TERM": "screen-256color"})
    # honoring Textual's own kill switch: it won't request the protocol at all
    assert not probe_kitty_protocol({"TERM": "xterm-kitty", "TEXTUAL_DISABLE_KITTY_KEY": "1"})
