"""The attention-notification ladder (ui/notifications.py, issue #47/B7).

Pure ladder policy + OSC 777 escape builder + terminal-support allowlist,
the normalized AttentionRecord/AttentionCenter dedupe + acknowledgement
lifecycle (B7 AC1/AC3/AC5), destination-level failure containment, plus the
app wiring that fires the desktop rung only when the window is unfocused.
Donor parity (amplifier-app-cli, read-only): OSC 777 shape and 80/240
bounds from ``ui/repl.terminal_notification_sequence``; allowlist +
``AMPLIFIER_TERMINAL_NOTIFICATIONS`` override from
``ui/terminal_probe.osc9_notifications_supported``; the unfocused trigger
from ``ui/layered_repl_terminal.notify_turn_complete``.
"""

from __future__ import annotations

import pytest
from textual import events

from amplifier_app_tui.ui.app import TuiApp
from amplifier_app_tui.ui.demo_wiring import DemoRuntimeAdapter
from amplifier_app_tui.ui.notifications import (
    ATTENTION_MIN_TURN_SECONDS,
    AttentionCenter,
    AttentionRecord,
    attention_event_id,
    attention_needed,
    clear_desktop_notification,
    desktop_notifications_supported,
    fire_attention_ladder,
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


class _RaisingDriver(RecordingDriver):
    """A driver whose write always raises -- exercises failure containment."""

    def write(self, data: str) -> None:
        raise RuntimeError("boom")


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
    assert attention_needed("awaiting_approval", 0.0, environ={})
    assert attention_needed("awaiting_clarification", 0.0, environ={})
    assert attention_needed("error", 0.0, environ={})
    assert not attention_needed("completion", 0.0, environ={})
    assert not attention_needed("completion", ATTENTION_MIN_TURN_SECONDS - 0.1, environ={})
    assert attention_needed("completion", ATTENTION_MIN_TURN_SECONDS, environ={})


def test_attention_needed_honours_disable_switch() -> None:
    for value in ("false", "0", "no", "off"):
        env = {"AMPLIFIER_NOTIFY": value}
        assert not attention_needed("awaiting_approval", 0.0, environ=env)
        assert not attention_needed("awaiting_clarification", 0.0, environ=env)
        assert not attention_needed("error", 0.0, environ=env)
        assert not attention_needed("completion", 999.0, environ=env)


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
    assert notification_rungs("completion", 1.0, focused=False, environ=_KITTY) == ()
    assert (
        notification_rungs(
            "awaiting_approval",
            0.0,
            focused=False,
            environ={**_KITTY, "AMPLIFIER_NOTIFY": "off"},
        )
        == ()
    )


def test_ladder_bell_only_when_focused() -> None:
    # Focused: the user is watching, a soft bell is enough (no desktop toast).
    assert notification_rungs("awaiting_approval", 0.0, focused=True, environ=_KITTY) == ("bell",)


def test_ladder_climbs_to_desktop_when_unfocused_on_capable_terminal() -> None:
    assert notification_rungs("awaiting_approval", 0.0, focused=False, environ=_KITTY) == (
        "bell",
        "desktop",
    )
    assert notification_rungs(
        "completion", ATTENTION_MIN_TURN_SECONDS, focused=False, environ=_KITTY
    ) == ("bell", "desktop")


def test_ladder_bell_cap_never_climbs_to_desktop() -> None:
    assert notification_rungs(
        "awaiting_approval",
        0.0,
        focused=False,
        environ={**_KITTY, "AMPLIFIER_NOTIFY": "bell"},
    ) == ("bell",)


def test_ladder_stays_on_bell_when_terminal_cannot_render() -> None:
    assert notification_rungs(
        "awaiting_approval", 0.0, focused=False, environ={"TERM": "xterm-256color"}
    ) == ("bell",)


# -- OSC 777 escape builder ---------------------------------------------------


def test_osc777_sequence_exact_shape() -> None:
    seq = osc777_notification_sequence("Amplifier", "Turn complete")
    assert seq == "\x1b]777;notify;Amplifier;Turn complete\x07"


def test_osc777_sequence_strips_injection_and_bounds_fields() -> None:
    # A smuggled BEL/ESC + a second OSC must not survive into the payload:
    # the whole sequence carries exactly one ESC (its own opener) and one
    # BEL (its own terminator), so nothing can break out mid-notification.
    seq = osc777_notification_sequence("Amp\x07\x1b work", "b" * 400 + "\nline\x1b\\rest")
    assert seq.startswith("\x1b]777;notify;")
    assert seq.endswith("\x07")
    assert seq.count("\x1b") == 1
    assert seq.count("\x07") == 1
    title_field, _, body_field = (
        seq.removeprefix("\x1b]777;notify;").removesuffix("\x07").partition(";")
    )
    assert "\n" not in body_field
    assert len(title_field) <= 80
    assert len(body_field) <= 240


def test_sanitize_collapses_whitespace_and_drops_invisibles() -> None:
    # \u200b (zero-width space, Cf) is dropped; runs collapse to one space.
    assert sanitize_notification_text("a\t b\n\nc\u200bd") == "a b cd"
    assert sanitize_notification_text("  spaced  out  ") == "spaced out"


# -- driver write path + destination failure containment (WHAT TO BUILD #5) --


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


def test_write_desktop_notification_contains_driver_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A destination failure (the driver write raising) must never raise
    out of the notifier -- it is logged and swallowed (WHAT TO BUILD #5)."""
    caplog.set_level("WARNING")
    driver = _RaisingDriver()
    assert write_desktop_notification(driver, "Amplifier", "done") is False  # type: ignore[arg-type]
    assert "desktop notification write failed" in caplog.text


def test_clear_desktop_notification_writes_empty_osc_sequence() -> None:
    driver = RecordingDriver()
    assert clear_desktop_notification(driver)  # type: ignore[arg-type]
    assert driver.writes == ["\x1b]777;notify;;\x07"]


def test_clear_desktop_notification_is_a_safe_noop_without_a_real_terminal() -> None:
    class Headless(RecordingDriver):
        is_headless = True

    assert clear_desktop_notification(Headless()) is False  # type: ignore[arg-type]
    assert clear_desktop_notification(None) is False


def test_fire_attention_ladder_contains_bell_failure_and_still_fires_desktop(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A destination failure must never block the session (WHAT TO BUILD
    #5): a raising bell must not prevent the desktop rung from firing."""
    caplog.set_level("WARNING")
    driver = RecordingDriver()

    def raising_bell() -> None:
        raise RuntimeError("bell jammed")

    fired = fire_attention_ladder(
        ("bell", "desktop"),
        bell=raising_bell,
        driver=driver,  # type: ignore[arg-type]
        title="Amplifier",
        body="done",
    )
    assert fired == ("desktop",)
    assert driver.writes == ["\x1b]777;notify;Amplifier;done\x07"]
    assert "attention destination 'bell' failed" in caplog.text


def test_fire_attention_ladder_returns_empty_for_no_rungs() -> None:
    assert fire_attention_ladder((), bell=lambda: None, driver=None, title="t", body="b") == ()


# -- the normalized attention record + dedupe/ack lifecycle (B7 AC1/AC3/AC5) --


def test_attention_event_id_is_deterministic_and_scoped_to_its_inputs() -> None:
    """Same (session, reason, occasion) always mints the same id; changing
    any one of the three inputs mints a different one."""
    first = attention_event_id("sess-1", "completion", "turn-1")
    assert attention_event_id("sess-1", "completion", "turn-1") == first
    assert attention_event_id("sess-1", "awaiting_approval", "turn-1") != first
    assert attention_event_id("sess-1", "completion", "turn-2") != first
    assert attention_event_id("sess-2", "completion", "turn-1") != first


def test_attention_center_note_mints_one_record_per_transition() -> None:
    """AC1: a transition into an attention state emits ONE normalized record
    carrying session identity and reason."""
    center = AttentionCenter()
    record, is_new = center.note("sess-1", "completion", "turn-1")
    assert is_new
    assert isinstance(record, AttentionRecord)
    assert record.session_id == "sess-1"
    assert record.reason == "completion"
    assert not record.acknowledged


def test_attention_center_dedupes_repeated_rendering_and_reconnects() -> None:
    """AC3: re-noting the SAME occasion (a re-render, a reconnect, or a
    repeated kernel-side ping for an already-parked decision) does not mint
    a new record -- the caller's ``is_new`` flag is the one-shot fire gate."""
    center = AttentionCenter()
    first, is_new_first = center.note("sess-1", "awaiting_approval", "decision-7")
    again, is_new_again = center.note("sess-1", "awaiting_approval", "decision-7")
    third, is_new_third = center.note("sess-1", "awaiting_approval", "decision-7", detail="ignored")
    assert is_new_first
    assert not is_new_again
    assert not is_new_third
    assert again is first  # the exact same record, not a fresh equal one
    assert third is first


def test_attention_center_new_occasion_supersedes_the_old_one() -> None:
    """A genuinely new transition for the same session mints a new record
    and becomes 'current'; the older one is superseded, not lost (it stays
    retrievable by its own id -- only ``current()`` moves on)."""
    center = AttentionCenter()
    first, _ = center.note("sess-1", "completion", "turn-1")
    second, is_new = center.note("sess-1", "awaiting_approval", "decision-1")
    assert is_new
    assert second is not first
    assert center.current("sess-1") is second


def test_attention_center_acknowledge_clears_current_and_is_idempotent() -> None:
    """AC5: acknowledging clears the open record; acking again once already
    acked, or with nothing open, is a documented no-op -- not an error."""
    center = AttentionCenter()
    center.note("sess-1", "awaiting_clarification", "decision-9")
    acked = center.acknowledge("sess-1")
    assert acked is not None
    assert acked.acknowledged
    current = center.current("sess-1")
    assert current is not None
    assert current.acknowledged
    assert center.acknowledge("sess-1") is None  # already acked -- no-op
    assert center.acknowledge("no-such-session") is None  # nothing open -- no-op


def test_attention_center_acknowledge_does_not_suppress_a_later_transition() -> None:
    """Acknowledging one occasion must not accidentally dedupe a LATER,
    genuinely new transition for the same session."""
    center = AttentionCenter()
    center.note("sess-1", "awaiting_approval", "decision-1")
    center.acknowledge("sess-1")
    _record, is_new = center.note("sess-1", "awaiting_approval", "decision-2")
    assert is_new


def test_attention_center_isolates_sessions() -> None:
    """Two sessions never dedupe or acknowledge across each other (B8:
    multiple ambient/delegated sessions must stay independently addressable
    by session identity)."""
    center = AttentionCenter()
    center.note("sess-a", "completion", "turn-1")
    _, is_new_b = center.note("sess-b", "completion", "turn-1")
    assert is_new_b  # same occasion, different session -- still a new record
    center.acknowledge("sess-a")
    current_b = center.current("sess-b")
    assert current_b is not None
    assert not current_b.acknowledged


# -- app wiring: focus tracking + the attention call sites --------------------


def test_app_focus_events_flip_focus_flag() -> None:
    app = TuiApp(DemoRuntimeAdapter(instant=True))
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
    app = TuiApp(DemoRuntimeAdapter(instant=True))
    driver = RecordingDriver()
    app._driver = driver  # type: ignore[assignment]
    bells: list[int] = []
    monkeypatch.setattr(app, "bell", lambda: bells.append(1))

    # Focused: bell only, no desktop escape written.
    app._terminal_focused = True
    app._notify_attention("awaiting_approval", occasion="decision-1", detail="push blocked")
    assert bells == [1]
    assert driver.writes == []

    # Blurred, a DIFFERENT occasion (a distinct decision -- not a re-render
    # of the first one): bell + an OSC 777 carrying the message as the body.
    app._terminal_focused = False
    app._notify_attention("awaiting_approval", occasion="decision-2", detail="push blocked")
    assert bells == [1, 1]
    assert driver.writes == ["\x1b]777;notify;Amplifier;push blocked\x07"]

    # AMPLIFIER_NOTIFY=off silences every rung even while blurred.
    monkeypatch.setenv("AMPLIFIER_NOTIFY", "off")
    app._notify_attention("completion", 999.0, occasion="turn-9")
    assert bells == [1, 1]
    assert len(driver.writes) == 1
    muted_record = app._attention.current(app.adapter.session_id)
    assert muted_record is not None
    assert muted_record.event_id.endswith(":completion:turn-9")


def test_notify_attention_dedupes_the_same_occasion(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC3 at the app-wiring level: repeated calls for the SAME occasion
    (what a re-render or a reconnect would produce) fire the ladder once."""
    monkeypatch.setenv("AMPLIFIER_TERMINAL_NOTIFICATIONS", "force")
    monkeypatch.delenv("AMPLIFIER_NOTIFY", raising=False)
    app = TuiApp(DemoRuntimeAdapter(instant=True))
    driver = RecordingDriver()
    app._driver = driver  # type: ignore[assignment]
    app._terminal_focused = False
    bells: list[int] = []
    monkeypatch.setattr(app, "bell", lambda: bells.append(1))

    for _ in range(3):
        app._notify_attention("awaiting_clarification", occasion="decision-9", detail="pick one")

    assert bells == [1]
    assert len(driver.writes) == 1


def test_on_app_focus_acknowledges_open_attention(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC5: refocusing the terminal window ('resuming') clears whatever
    attention record is currently open, and best-effort clears the desktop
    indicator when the destination supports it."""
    monkeypatch.delenv("AMPLIFIER_NOTIFY", raising=False)
    app = TuiApp(DemoRuntimeAdapter(instant=True))
    driver = RecordingDriver()
    app._driver = driver  # type: ignore[assignment]
    monkeypatch.setattr(app, "bell", lambda: None)
    acknowledged: list[dict[str, object]] = []
    monkeypatch.setattr(
        app.adapter,
        "publish_attention_acknowledged",
        lambda payload: acknowledged.append(dict(payload)),
    )
    app._notify_attention("error", occasion="err-1", detail="boom")
    session_id = app.adapter.session_id
    before = app._attention.current(session_id)
    assert before is not None
    assert not before.acknowledged
    driver.writes.clear()  # isolate the ack-triggered clear write

    app.on_app_focus(events.AppFocus())

    after = app._attention.current(session_id)
    assert after is not None
    assert after.acknowledged
    assert driver.writes == ["\x1b]777;notify;;\x07"]
    assert len(acknowledged) == 1
    assert (
        acknowledged[0].items()
        >= {
            "event_id": before.event_id,
            "session_id": before.session_id,
            "reason": "error",
            "acknowledged": True,
        }.items()
    )
    assert isinstance(acknowledged[0]["acknowledged_at"], float)


def test_on_app_focus_with_nothing_open_is_a_safe_no_op() -> None:
    app = TuiApp(DemoRuntimeAdapter(instant=True))
    app.on_app_focus(events.AppFocus())  # must not raise
    assert app._attention.current(app.adapter.session_id) is None


# -- B7 gap 1: durability (AttentionCenter.bind) ------------------------------


def test_attention_center_bind_none_stays_pure_in_memory(tmp_path) -> None:
    center = AttentionCenter()
    center.bind(None)  # demo / no session dir yet -- must not raise, no file created
    center.note("s1", "completion", "turn-1")
    assert list(tmp_path.iterdir()) == []


def test_attention_center_bind_hydrates_and_persists_across_restart(tmp_path) -> None:
    first = AttentionCenter()
    first.bind(tmp_path)
    _, is_new = first.note("s1", "error", "occ-1", detail="boom")
    assert is_new is True
    assert (tmp_path / "attention.json").exists()

    # A "restart": a brand new AttentionCenter bound to the SAME directory
    # observes the prior state (durability) and its dedupe survives it (AC3).
    second = AttentionCenter()
    second.bind(tmp_path)
    restored = second.current("s1")
    assert restored is not None
    assert restored.detail == "boom"
    assert restored.acknowledged is False
    _, is_new_again = second.note("s1", "error", "occ-1", detail="boom")
    assert is_new_again is False

    acked = second.acknowledge("s1")
    assert acked is not None and acked.acknowledged is True

    third = AttentionCenter()
    third.bind(tmp_path)
    assert third.current("s1").acknowledged is True  # type: ignore[union-attr]


def test_two_prebound_centers_atomically_dedupe_the_same_transition(tmp_path) -> None:
    first = AttentionCenter()
    second = AttentionCenter()
    # Both processes hydrate the same empty snapshot before either writes.
    first.bind(tmp_path)
    second.bind(tmp_path)

    first_record, first_is_new = first.note("s1", "error", "same", detail="boom")
    second_record, second_is_new = second.note("s1", "error", "same", detail="boom")

    assert first_is_new is True
    assert second_is_new is False
    assert second_record == first_record


def test_stale_center_cannot_revert_a_durable_acknowledgement(tmp_path) -> None:
    first = AttentionCenter()
    stale = AttentionCenter()
    first.bind(tmp_path)
    stale.bind(tmp_path)

    original, _ = first.note("s1", "error", "first")
    # Refresh the second center once, then let the first process acknowledge.
    stale.note("s1", "error", "first")
    assert first.acknowledge("s1") is not None

    # A later unrelated transition from the formerly stale process must merge
    # against disk, not replace the acknowledged row with its old snapshot.
    stale.note("s1", "completion", "second")
    assert stale._by_id[original.event_id].acknowledged is True

    restarted = AttentionCenter()
    restarted.bind(tmp_path)
    assert restarted._by_id[original.event_id].acknowledged is True


def test_attention_center_persist_failure_never_raises(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A persistence failure must never block or crash the session."""
    center = AttentionCenter()
    center.bind(tmp_path)

    def _boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk is full")

    monkeypatch.setattr(center._store, "record", _boom)  # type: ignore[union-attr]
    record, is_new = center.note("s1", "completion", "turn-1")  # must not raise
    assert is_new is True
    assert record.session_id == "s1"


def test_attention_center_hydrate_drops_corrupted_reason(tmp_path) -> None:
    """A row with a reason outside the closed set (corrupted file, or a
    foreign app version) is dropped rather than misrepresented."""
    import json

    (tmp_path / "attention.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "by_id": {
                    "s1:bogus:occ": {
                        "session_id": "s1",
                        "reason": "bogus-reason",
                        "event_id": "s1:bogus:occ",
                        "detail": "",
                        "created_at": 1.0,
                        "acknowledged": False,
                    }
                },
                "current": {"s1": "s1:bogus:occ"},
            }
        ),
        encoding="utf-8",
    )
    center = AttentionCenter()
    center.bind(tmp_path)
    assert center.current("s1") is None


# -- B7 gap 2: attention_push_payload (record-derived, event-id-carrying) ----


def test_attention_push_payload_carries_event_id_and_sanitizes_bounds() -> None:
    from amplifier_app_tui.ui.notifications import attention_push_payload

    record, _ = AttentionCenter().note("sess-42", "error", "turn-9", detail="boom\x07\x1b")
    payload = attention_push_payload(record, title="Amplifier", body="x" * 500)

    assert payload["event_id"] == record.event_id
    assert payload["session_id"] == "sess-42"
    assert payload["reason"] == "error"
    assert payload["created_at"] == record.created_at
    assert payload["title"] == "Amplifier"
    assert len(payload["body"]) <= 240  # same bound as the OSC 777 rung
    assert "\x07" not in payload["body"] and "\x1b" not in payload["body"]


def test_notify_attention_publishes_to_adapter_only_when_new(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AMPLIFIER_TERMINAL_NOTIFICATIONS", "force")
    monkeypatch.delenv("AMPLIFIER_NOTIFY", raising=False)
    app = TuiApp(DemoRuntimeAdapter(instant=True))
    monkeypatch.setattr(app, "bell", lambda: None)
    published: list[dict[str, object]] = []
    monkeypatch.setattr(app.adapter, "publish_attention", lambda payload: published.append(payload))

    app._notify_attention("error", occasion="err-1", detail="boom")
    assert len(published) == 1
    assert published[0]["event_id"].endswith(":error:err-1")  # type: ignore[union-attr]

    app._notify_attention("error", occasion="err-1", detail="boom")  # AC3: dedupe
    assert len(published) == 1


def test_new_clarification_binds_event_to_exact_pending_decision(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from amplifier_app_tui.kernel.ambient import reply as ambient_reply

    ambient_root = tmp_path / "ambient"
    monkeypatch.setattr(ambient_reply, "default_ambient_root", lambda: ambient_root)
    monkeypatch.setenv("AMPLIFIER_NOTIFY", "off")
    adapter = DemoRuntimeAdapter(instant=True)
    adapter.session_id = "s-1"
    adapter.session_dir = tmp_path / "projects" / "project-a" / "sessions" / "s-1"
    adapter.session_dir.mkdir(parents=True)
    app = TuiApp(adapter)

    app._notify_attention(
        "awaiting_clarification",
        occasion="decision-7",
        detail="Which test label should I use?",
    )

    event_id = "s-1:awaiting_clarification:decision-7"
    row = ambient_reply.CorrelationTable(ambient_root).resolve(event_id)
    assert row is not None
    assert (
        row.items()
        >= {
            "event_id": event_id,
            "session_id": "s-1",
            "decision_id": "decision-7",
            "session_dir": str(adapter.session_dir),
            "project": "project-a",
        }.items()
    )


# -- B7 gap 3: production error transition #1 -- a failed turn ---------------


@pytest.mark.asyncio
async def test_submit_prompt_failure_mints_an_error_attention_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AMPLIFIER_NOTIFY", raising=False)
    app = TuiApp(DemoRuntimeAdapter(instant=True))

    async def _boom(text: str, attachments: tuple[object, ...] = ()) -> None:
        raise RuntimeError("provider auth expired")

    monkeypatch.setattr(app.adapter, "submit", _boom)
    await app._submit_prompt("hello", ())

    record = app._attention.current(app.adapter.session_id)
    assert record is not None
    assert record.reason == "error"
    assert "provider auth expired" in record.detail
