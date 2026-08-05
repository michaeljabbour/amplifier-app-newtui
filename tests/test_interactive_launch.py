"""Interactive-launch overrides: ``amplifier-tui [-p/-m/--mode]`` (S2, #148).

The bare ``amplifier-tui`` launcher (and ``run`` with no prompt on a TTY)
must boot the full-screen TUI with the same ephemeral per-invocation overrides
the headless ``run`` command documents:

- ``--provider``/``--model`` mutate only the resolved in-memory plan (threaded
  into ``RealRuntimeAdapter`` -> ``RealRuntime``, never persisted); and
- ``--mode`` seeds the opening interaction posture on ``TuiApp``.

Three layers are exercised: the CLI wiring (flags reach ``_launch_tui``), the
shared validation rules (``--model`` requires ``--provider``; unknown ``--mode``
fails loud), and the seams the overrides ride (adapter kwargs + app posture).

Also covers S4/AC4: the pre-takeover mount/provider preflight that gates
``_launch_tui`` (a failure must stop before Textual is ever imported) and the
``--dry-run`` flag that reports the same resolution without launching.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner

import amplifier_app_tui.main as main_mod
from amplifier_app_tui.kernel.preflight import PreflightReport
from amplifier_app_tui.main import main
from amplifier_app_tui.ui.app import TuiApp
from amplifier_app_tui.ui.demo_wiring import DemoRuntimeAdapter
from amplifier_app_tui.ui.runtime_adapter import RealRuntimeAdapter

_OK_REPORT = PreflightReport(
    ok=True,
    bundle_name="tui",
    bundle_uri="file:///tui.md",
    provider="anthropic",
    model="claude-x",
    provider_count=1,
    tool_count=3,
    routing_enabled=False,
)


@pytest.fixture
def capture_launch(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Replace ``_launch_tui`` (+ the provider gate + preflight) so no real TUI boots."""
    launched: dict[str, object] = {}

    async def fake_launch(**kwargs: object) -> int:
        launched.update(kwargs)
        return 0

    async def fake_gate() -> int | None:
        return None

    async def fake_preflight(
        bundle: str | None,
        provider: str | None,
        model: str | None,
        **_kwargs: object,
    ) -> PreflightReport:
        del bundle, provider, model
        return _OK_REPORT

    monkeypatch.setattr(main_mod, "_launch_tui", fake_launch)
    monkeypatch.setattr(main_mod, "_first_run_gate", fake_gate)
    monkeypatch.setattr(main_mod, "_run_preflight", fake_preflight)
    return launched


# ---------------------------------------------------------------------------
# CLI wiring: the bare launcher threads each override into _launch_tui
# ---------------------------------------------------------------------------


def test_bare_launch_threads_no_overrides(capture_launch: dict[str, object]) -> None:
    """No flags => every override is None (untouched default launch)."""
    result = CliRunner().invoke(main, [])
    assert result.exit_code == 0
    assert capture_launch["demo"] is False
    assert capture_launch["provider"] is None
    assert capture_launch["model"] is None
    assert capture_launch["mode"] is None


def test_launch_threads_provider_and_model(capture_launch: dict[str, object]) -> None:
    result = CliRunner().invoke(main, ["-p", "anthropic", "-m", "claude-sonnet-5"])
    assert result.exit_code == 0
    assert capture_launch["provider"] == "anthropic"
    assert capture_launch["model"] == "claude-sonnet-5"


def test_launch_threads_mode_posture(capture_launch: dict[str, object]) -> None:
    result = CliRunner().invoke(main, ["--mode", "chat"])
    assert result.exit_code == 0
    assert capture_launch["mode"] == "chat"


def test_launch_threads_all_overrides_with_bundle(capture_launch: dict[str, object]) -> None:
    """Samuel's exact command shape now boots the TUI in chat mode."""
    result = CliRunner().invoke(
        main, ["--mode", "chat", "-p", "anthropic", "-m", "claude-sonnet-5", "--bundle", "custom"]
    )
    assert result.exit_code == 0
    assert capture_launch == {
        "demo": False,
        "bundle": "custom",
        "resume_id": None,
        "mode": "chat",
        "provider": "anthropic",
        "model": "claude-sonnet-5",
    }


def test_doctor_composes_real_launch_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    """Standalone doctor cannot report green when the next launch is blocked."""
    from amplifier_app_tui.commands import doctor as doctor_module
    from amplifier_app_tui.kernel import updater

    async def fake_anchors() -> object:
        return None

    async def fake_preflight(
        bundle: str | None,
        provider: str | None,
        model: str | None,
        **kwargs: object,
    ) -> PreflightReport:
        assert (bundle, provider, model) == (None, None, None)
        assert kwargs == {"strict": True}
        return PreflightReport(
            ok=False,
            error="provider-anthropic failed to import",
            remediation="run `amplifier-tui update --force`",
        )

    captured: dict[str, object] = {}

    def fake_standalone(**kwargs: object) -> int:
        captured.update(kwargs)
        return 1

    monkeypatch.setattr(updater, "anchors_status", fake_anchors)
    monkeypatch.setattr(main_mod, "_run_preflight", fake_preflight)
    monkeypatch.setattr(doctor_module, "run_standalone", fake_standalone)

    result = CliRunner().invoke(main, ["doctor"])
    assert result.exit_code == 1
    checks = captured["additional_checks"]
    assert isinstance(checks, tuple)
    assert len(checks) == 1
    check = checks[0]
    assert not check.ok
    assert "provider-anthropic failed to import" in check.message
    assert "amplifier-tui update --force" in check.message


# ---------------------------------------------------------------------------
# Shared validation: same rules as the headless `run` command
# ---------------------------------------------------------------------------


def test_launch_model_without_provider_errors(capture_launch: dict[str, object]) -> None:
    result = CliRunner().invoke(main, ["-m", "claude-sonnet-5"])
    assert result.exit_code == 1
    assert "requires --provider" in result.stderr
    assert capture_launch == {}  # never reached a launch


def test_launch_unknown_mode_errors(capture_launch: dict[str, object]) -> None:
    result = CliRunner().invoke(main, ["--mode", "bogus"])
    assert result.exit_code == 1
    assert "unknown mode" in result.stderr
    assert "chat" in result.stderr  # valid ids are listed
    assert capture_launch == {}


def test_gate_nonzero_stops_before_launch(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing first-run gate returns its exit code without booting the TUI."""
    launched: list[object] = []

    async def fake_launch(**kwargs: object) -> int:
        launched.append(kwargs)
        return 0

    async def fake_gate() -> int | None:
        return 3

    monkeypatch.setattr(main_mod, "_launch_tui", fake_launch)
    monkeypatch.setattr(main_mod, "_first_run_gate", fake_gate)
    result = CliRunner().invoke(main, ["--mode", "chat"])
    assert result.exit_code == 3
    assert launched == []


# ---------------------------------------------------------------------------
# Preflight (S4/AC4): resolve mounts/providers BEFORE Textual takes over
# ---------------------------------------------------------------------------


def test_preflight_failure_stops_before_launch_without_importing_textual(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failing preflight prints a plain error and exits nonzero -- ``_launch_tui``
    (the only place that imports Textual) is never reached."""
    launched: list[object] = []

    async def fake_launch(**kwargs: object) -> int:
        launched.append(kwargs)
        return 0

    async def fake_gate() -> int | None:
        return None

    async def fake_preflight(bundle, provider, model, **kwargs) -> PreflightReport:  # noqa: ANN001
        del bundle, provider, model
        assert kwargs == {"strict": False}
        return PreflightReport(
            ok=False,
            error="no provider configured",
            remediation="run `amplifier-tui init` to configure a provider",
        )

    monkeypatch.setattr(main_mod, "_launch_tui", fake_launch)
    monkeypatch.setattr(main_mod, "_first_run_gate", fake_gate)
    monkeypatch.setattr(main_mod, "_run_preflight", fake_preflight)

    result = CliRunner().invoke(main, [])
    assert result.exit_code == 1
    assert launched == []  # _launch_tui (and therefore Textual) never touched
    assert "cannot launch" in result.stderr
    assert "no provider configured" in result.stderr
    assert "amplifier-tui init" in result.stderr


def test_explicit_invalid_model_fails_strictly_before_screen_takeover(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launched: list[object] = []

    async def fake_launch(**kwargs: object) -> int:
        launched.append(kwargs)
        return 0

    async def fake_gate() -> int | None:
        return None

    async def fake_preflight(
        bundle: str | None,
        provider: str | None,
        model: str | None,
        *,
        strict: bool = False,
    ) -> PreflightReport:
        assert (bundle, provider, model) == (None, "anthropic", "definitely-not-a-model")
        assert strict is True
        return PreflightReport(
            ok=False,
            error="model 'definitely-not-a-model' is not available for provider 'anthropic'",
            remediation="pick a listed model",
        )

    monkeypatch.setattr(main_mod, "_launch_tui", fake_launch)
    monkeypatch.setattr(main_mod, "_first_run_gate", fake_gate)
    monkeypatch.setattr(main_mod, "_run_preflight", fake_preflight)

    result = CliRunner().invoke(
        main,
        ["--provider", "anthropic", "--model", "definitely-not-a-model"],
    )
    assert result.exit_code == 1
    assert launched == []
    assert "definitely-not-a-model" in result.stderr


def test_preflight_success_proceeds_to_launch(
    capture_launch: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[object, object, object, dict[str, object]]] = []
    real_fake = main_mod._run_preflight

    async def spying_preflight(bundle, provider, model, **kwargs):  # noqa: ANN001
        calls.append((bundle, provider, model, kwargs))
        return await real_fake(bundle, provider, model, **kwargs)

    monkeypatch.setattr(main_mod, "_run_preflight", spying_preflight)
    result = CliRunner().invoke(main, ["--bundle", "custom", "-p", "anthropic", "-m", "claude-x"])
    assert result.exit_code == 0
    assert calls == [("custom", "anthropic", "claude-x", {"strict": True})]
    assert capture_launch["bundle"] == "custom"


def test_ordinary_launch_without_model_stays_non_strict(
    capture_launch: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, object]] = []
    real_fake = main_mod._run_preflight

    async def spying_preflight(bundle, provider, model, **kwargs):  # noqa: ANN001
        calls.append(kwargs)
        return await real_fake(bundle, provider, model, **kwargs)

    monkeypatch.setattr(main_mod, "_run_preflight", spying_preflight)
    result = CliRunner().invoke(main, [])
    assert result.exit_code == 0
    assert calls == [{"strict": False}]
    assert capture_launch["model"] is None


def test_demo_skips_preflight_entirely(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--demo`` has no real mounts/providers -- preflight must not even run."""

    async def fake_launch(**kwargs: object) -> int:
        return 0

    async def boom(*args: object, **kwargs: object) -> PreflightReport:
        raise AssertionError("preflight must not run in --demo mode")

    monkeypatch.setattr(main_mod, "_launch_tui", fake_launch)
    monkeypatch.setattr(main_mod, "_run_preflight", boom)
    result = CliRunner().invoke(main, ["--demo"])
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# --dry-run (S4/AC4): report what would mount; never launch
# ---------------------------------------------------------------------------


def test_help_lists_dry_run_flag() -> None:
    """``--dry-run`` is discoverable the same way ``reset --dry-run`` is."""
    for args in (["--help"], ["run", "--help"]):
        result = CliRunner().invoke(main, args)
        assert result.exit_code == 0
        assert "--dry-run" in result.output


def test_dry_run_reports_and_exits_zero_without_launching(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, object]] = []

    async def fake_preflight(bundle, provider, model, **kwargs) -> PreflightReport:  # noqa: ANN001
        del bundle, provider, model
        calls.append(kwargs)
        return _OK_REPORT

    async def boom_gate() -> int | None:
        raise AssertionError("--dry-run must not run the (interactive) first-run gate")

    async def boom_launch(**kwargs: object) -> int:
        raise AssertionError("--dry-run must never launch the TUI")

    monkeypatch.setattr(main_mod, "_run_preflight", fake_preflight)
    monkeypatch.setattr(main_mod, "_first_run_gate", boom_gate)
    monkeypatch.setattr(main_mod, "_launch_tui", boom_launch)

    result = CliRunner().invoke(main, ["--dry-run"])
    assert result.exit_code == 0
    assert "tui" in result.stdout
    assert "anthropic" in result.stdout
    assert "claude-x" in result.stdout
    assert "DRY RUN" in result.stdout
    assert "nothing was launched" in result.stdout
    assert calls == [{"strict": True}]


def test_dry_run_failure_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_preflight(bundle, provider, model, **kwargs) -> PreflightReport:  # noqa: ANN001
        del bundle, provider, model, kwargs
        return PreflightReport(
            ok=False, error="bundle not found: nope", remediation="check --bundle name/path"
        )

    monkeypatch.setattr(main_mod, "_run_preflight", fake_preflight)
    result = CliRunner().invoke(main, ["--dry-run", "--bundle", "nope"])
    assert result.exit_code == 1
    assert "bundle not found" in result.stderr
    assert "check --bundle" in result.stderr


def test_dry_run_with_demo_skips_preflight_and_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(*args: object, **kwargs: object) -> PreflightReport:
        raise AssertionError("--demo --dry-run must not touch real mounts/providers")

    monkeypatch.setattr(main_mod, "_run_preflight", boom)
    result = CliRunner().invoke(main, ["--demo", "--dry-run"])
    assert result.exit_code == 0
    assert "no real mounts" in result.stdout


def test_run_command_dry_run_short_circuits_without_tty_or_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run --dry-run`` must not require a TTY or a prompt -- it short-circuits
    before the interactive-vs-headless branch (and before ``--resume`` lookup)."""

    async def fake_preflight(bundle, provider, model, **kwargs) -> PreflightReport:  # noqa: ANN001
        del bundle, provider, model, kwargs
        return _OK_REPORT

    monkeypatch.setattr(main_mod, "_run_preflight", fake_preflight)
    monkeypatch.setattr(main_mod, "_is_interactive_terminal", lambda: False)
    result = CliRunner().invoke(main, ["run", "--dry-run"])
    assert result.exit_code == 0
    assert "Prompt required" not in result.output
    assert "DRY RUN" in result.stdout


def test_run_command_dry_run_failure_exits_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_preflight(bundle, provider, model, **kwargs) -> PreflightReport:  # noqa: ANN001
        del bundle, provider, model, kwargs
        return PreflightReport(ok=False, error="no provider configured", remediation="run init")

    monkeypatch.setattr(main_mod, "_run_preflight", fake_preflight)
    result = CliRunner().invoke(main, ["run", "--dry-run"])
    assert result.exit_code == 1
    assert "no provider configured" in result.stderr


# ---------------------------------------------------------------------------
# `run` with no prompt on a TTY launches interactive (Samuel's exact command)
# ---------------------------------------------------------------------------


def test_run_without_prompt_on_tty_launches_interactive(
    capture_launch: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(main_mod, "_is_interactive_terminal", lambda: True)
    result = CliRunner().invoke(
        main, ["run", "-p", "anthropic", "-m", "claude-x", "--mode", "chat"]
    )
    assert result.exit_code == 0
    assert capture_launch["provider"] == "anthropic"
    assert capture_launch["model"] == "claude-x"
    assert capture_launch["mode"] == "chat"
    assert capture_launch["demo"] is False


def test_run_without_prompt_not_tty_stays_prompt_required(
    capture_launch: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Non-interactive / piped `run` with no prompt still fails loud (headless)."""
    monkeypatch.setattr(main_mod, "_is_interactive_terminal", lambda: False)
    result = CliRunner().invoke(main, ["run", "--mode", "chat"])
    assert result.exit_code != 0
    assert "Prompt required" in result.output
    assert capture_launch == {}


def test_run_without_prompt_json_output_stays_prompt_required(
    capture_launch: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even on a TTY, a JSON output format keeps `run` headless (prompt-required)."""
    monkeypatch.setattr(main_mod, "_is_interactive_terminal", lambda: True)
    result = CliRunner().invoke(main, ["run", "--output-format", "json"])
    assert result.exit_code != 0
    assert capture_launch == {}


# ---------------------------------------------------------------------------
# Seams the overrides ride: adapter kwargs + app posture
# ---------------------------------------------------------------------------


def test_real_adapter_stores_provider_and_model_overrides() -> None:
    adapter = RealRuntimeAdapter(
        bundle="offline", provider_override="anthropic", model_override="claude-x"
    )
    assert adapter._provider_override == "anthropic"
    assert adapter._model_override == "claude-x"


def test_app_seeds_initial_mode() -> None:
    app = TuiApp(DemoRuntimeAdapter(), initial_mode="chat")
    assert app.mode_id == "chat"


def test_app_defaults_to_auto_without_initial_mode() -> None:
    app = TuiApp(DemoRuntimeAdapter())
    assert app.mode_id == "auto"
