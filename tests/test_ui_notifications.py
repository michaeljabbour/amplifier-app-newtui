"""The attention-notification ladder (ui/notifications.py, issue #47).

Pure ladder policy + OSC 777 escape builder + terminal-support allowlist,
plus the app wiring that fires the desktop rung only when the window is
unfocused. Donor parity (amplifier-app-cli, read-only): OSC 777 shape and
80/240 bounds from ``ui/repl.terminal_notification_sequence``; allowlist +
``AMPLIFIER_TERMINAL_NOTIFICATIONS`` override from
``ui/terminal_probe.osc9_notifications_supported``; the unfocused trigger
from ``ui/layered_repl_terminal.notify_turn_complete``.
"""

from __future__ import annotations

import pytest
from textual import events

from amplifier_app_newtui.ui.app import NewTuiApp
from amplifier_app_newtui.ui.demo_wiring import DemoRuntimeAdapter
from amplifier_app_newtui.ui.notifications import (
    ATTENTION_MIN_TURN_SECONDS,
    attention_needed,
    desktop_notifications_supported,
    notification_rungs,
    notify_ceiling,
    osc777_notification_sequence,
    sanitize_notification_text,
    write_desktop_notification,
)

_KITTY = {"TERM": "xterm-kitty"}


class RecordingDriver:
    """A non-headless driver stand-in that captures OSC writes + flushes."""

    is_headless = False
    is_web = False

    def __init__(self) -> None:
        self.writes: list[str] = []
        self.flushes = 0

    def write(self, data: str) -> None:
        self.writes.append(data)

    def flush(self) -> None:
        self.flushes += 1


# -- AMPLIFIER_NOTIFY ceiling parsing -----------------------------------------


def test_notify_ceiling_off_bell_and_desktop() -> None:
    for value in ("false", "0", "no", "off", "OFF", "False"):
        assert notify_ceiling({"AMPLIFIER_NOTIFY": value}) == "off"
    assert notify_ceiling({"AMPLIFIER_NOTIFY": "bell"}) == "bell"
    assert notify_ceiling({"AMPLIFIER_NOTIFY": "BELL"}) == "bell"
    # Unset, truthy, and explicit desktop all open the full ladder; an
    # unknown value defaults up (a typo must not silence you).
    for value in ("", "true", "1", "on", "desktop", "osc777", "wat"):
        assert notify_ceiling({"AMPLIFIER_NOTIFY": value}) == "desktop"
    assert notify_ceiling({}) == "desktop"


# -- attention predicate (bell-rung floor) ------------------------------------


def test_attention_needed_defers_always_and_turns_after_threshold() -> None:
    assert attention_needed("decision_deferred", 0.0, environ={})
    assert not attention_needed("turn_finished", 0.0, environ={})
    assert not attention_needed(
        "turn_finished", ATTENTION_MIN_TURN_SECONDS - 0.1, environ={}
    )
    assert attention_needed("turn_finished", ATTENTION_MIN_TURN_SECONDS, environ={})


def test_attention_needed_honours_disable_switch() -> None:
    for value in ("false", "0", "no", "off"):
        env = {"AMPLIFIER_NOTIFY": value}
        assert not attention_needed("decision_deferred", 0.0, environ=env)
        assert not attention_needed("turn_finished", 999.0, environ=env)


# -- terminal-support allowlist -----------------------------------------------


def test_desktop_supported_allowlists_known_terminals() -> None:
    assert desktop_notifications_supported({"TERM_PROGRAM": "iTerm.app"})
    assert desktop_notifications_supported({"TERM_PROGRAM": "ghostty"})
    assert desktop_notifications_supported({"TERM_PROGRAM": "WezTerm"})
    assert desktop_notifications_supported({"TERM_PROGRAM": "WarpTerminal"})
    assert desktop_notifications_supported({"TERM": "xterm-kitty"})
    assert desktop_notifications_supported({"KITTY_WINDOW_ID": "1"})


def test_desktop_supported_excludes_unknown_and_honours_override() -> None:
    assert not desktop_notifications_supported({"TERM": "xterm-256color"})
    assert not desktop_notifications_supported({"TERM_PROGRAM": "Apple_Terminal"})
    # Override wins both ways over the allowlist.
    assert desktop_notifications_supported(
        {"TERM": "xterm-256color", "AMPLIFIER_TERMINAL_NOTIFICATIONS": "force"}
    )
    assert not desktop_notifications_supported(
        {"TERM": "xterm-kitty", "AMPLIFIER_TERMINAL_NOTIFICATIONS": "off"}
    )


# -- the ladder ---------------------------------------------------------------


def test_ladder_silent_when_no_attention_or_disabled() -> None:
    assert notification_rungs("turn_finished", 1.0, focused=False, environ=_KITTY) == ()
    assert (
        notification_rungs(
            "decision_deferred", 0.0, focused=False,
            environ={**_KITTY, "AMPLIFIER_NOTIFY": "off"},
        )
        == ()
    )


def test_ladder_bell_only_when_focused() -> None:
    # Focused: the user is watching, a soft bell is enough (no desktop toast).
    assert notification_rungs(
        "decision_deferred", 0.0, focused=True, environ=_KITTY
    ) == ("bell",)


def test_ladder_climbs_to_desktop_when_unfocused_on_capable_terminal() -> None:
    assert notification_rungs(
        "decision_deferred", 0.0, focused=False, environ=_KITTY
    ) == ("bell", "desktop")
    assert notification_rungs(
        "turn_finished", ATTENTION_MIN_TURN_SECONDS, focused=False, environ=_KITTY
    ) == ("bell", "desktop")


def test_ladder_bell_cap_never_climbs_to_desktop() -> None:
    assert notification_rungs(
        "decision_deferred", 0.0, focused=False,
        environ={**_KITTY, "AMPLIFIER_NOTIFY": "bell"},
    ) == ("bell",)


def test_ladder_stays_on_bell_when_terminal_cannot_render() -> None:
    assert notification_rungs(
        "decision_deferred", 0.0, focused=False, environ={"TERM": "xterm-256color"}
    ) == ("bell",)


# -- OSC 777 escape builder ---------------------------------------------------


def test_osc777_sequence_exact_shape() -> None:
    seq = osc777_notification_sequence("Amplifier", "Turn complete")
    assert seq == "\x1b]777;notify;Amplifier;Turn complete\x07"


def test_osc777_sequence_strips_injection_and_bounds_fields() -> None:
    # A smuggled BEL/ESC + a second OSC must not survive into the payload:
    # the whole sequence carries exactly one ESC (its own opener) and one
    # BEL (its own terminator), so nothing can break out mid-notification.
    seq = osc777_notification_sequence(
        "Amp\x07\x1b work", "b" * 400 + "\nline\x1b\\rest"
    )
    assert seq.startswith("\x1b]777;notify;")
    assert seq.endswith("\x07")
    assert seq.count("\x1b") == 1
    assert seq.count("\x07") == 1
    title_field, _, body_field = seq.removeprefix("\x1b]777;notify;").removesuffix(
        "\x07"
    ).partition(";")
    assert "\n" not in body_field
    assert len(title_field) <= 80
    assert len(body_field) <= 240


def test_sanitize_collapses_whitespace_and_drops_invisibles() -> None:
    # \u200b (zero-width space, Cf) is dropped; runs collapse to one space.
    assert sanitize_notification_text("a\t b\n\nc\u200bd") == "a b cd"
    assert sanitize_notification_text("  spaced  out  ") == "spaced out"


# -- driver write path --------------------------------------------------------


def test_write_desktop_notification_uses_osc_and_flushes() -> None:
    driver = RecordingDriver()
    assert write_desktop_notification(driver, "Amplifier", "done")  # type: ignore[arg-type]
    assert driver.writes == ["\x1b]777;notify;Amplifier;done\x07"]
    assert driver.flushes == 1


def test_write_desktop_notification_skips_when_no_real_terminal() -> None:
    class Headless(RecordingDriver):
        is_headless = True

    driver = Headless()
    assert not write_desktop_notification(driver, "Amplifier", "done")  # type: ignore[arg-type]
    assert driver.writes == []
    assert write_desktop_notification(None, "Amplifier", "done") is False


# -- app wiring: focus tracking + the two attention sites ---------------------


def test_app_focus_events_flip_focus_flag() -> None:
    app = NewTuiApp(DemoRuntimeAdapter(instant=True))
    assert app._terminal_focused is True  # assumed focused until a blur
    app.on_app_blur(events.AppBlur())
    assert app._terminal_focused is False
    app.on_app_focus(events.AppFocus())
    assert app._terminal_focused is True


def test_app_notify_attention_ladder_via_recording_driver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AMPLIFIER_TERMINAL_NOTIFICATIONS", "force")
    monkeypatch.delenv("AMPLIFIER_NOTIFY", raising=False)
    app = NewTuiApp(DemoRuntimeAdapter(instant=True))
    driver = RecordingDriver()
    app._driver = driver  # type: ignore[assignment]
    bells: list[int] = []
    monkeypatch.setattr(app, "bell", lambda: bells.append(1))

    # Focused: bell only, no desktop escape written.
    app._terminal_focused = True
    app._notify_attention("decision_deferred", detail="push blocked")
    assert bells == [1]
    assert driver.writes == []

    # Blurred: bell + an OSC 777 carrying the deferral message as the body.
    app._terminal_focused = False
    app._notify_attention("decision_deferred", detail="push blocked")
    assert bells == [1, 1]
    assert driver.writes == ["\x1b]777;notify;Amplifier;push blocked\x07"]

    # AMPLIFIER_NOTIFY=off silences every rung even while blurred.
    monkeypatch.setenv("AMPLIFIER_NOTIFY", "off")
    app._notify_attention("turn_finished", 999.0)
    assert bells == [1, 1]
    assert len(driver.writes) == 1
