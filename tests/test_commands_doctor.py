"""/doctor named checks, report shape, block build, standalone CLI."""

from __future__ import annotations

from pathlib import Path

import pytest

from amplifier_app_tui.commands import doctor as doctor_module
from amplifier_app_tui.commands.doctor import (
    APP_INSTALL_URI,
    CheckResult,
    DoctorReport,
    McpServerStats,
    PermissionFacts,
    PythonUvFacts,
    build_doctor_block,
    check_anchors_pin,
    check_install,
    check_mounts,
    check_path,
    check_permissions,
    check_platform,
    check_python_uv,
    check_repeated_approvals,
    check_settings,
    check_unused_mcp,
    detect_permissions,
    detect_platform,
    detect_python_uv,
    render_text,
    run_checks,
    run_standalone,
)
from amplifier_app_tui.install_contract import (
    SOURCE_INSTALL_COMMAND,
    SOURCE_INSTALL_LAUNCH_COMMAND,
)
from amplifier_app_tui.commands.improve import ApprovalTally
from amplifier_app_tui.kernel import updater
from amplifier_app_tui.kernel.session_factory import MountReport
from amplifier_app_tui.kernel.updater import AnchorsStatus


@pytest.fixture(autouse=True)
def _deterministic_machine_checks(monkeypatch):
    """Pin platform/python-uv/permissions to healthy defaults for run_checks().

    These three checks introspect the REAL machine (OS, Python, uv, home-dir
    writability) by design -- exactly what makes them useful in production.
    Left alone, that would make `finding_count`/`healthy_summary` assertions
    in THIS file depend on whatever machine happens to run the suite -- the
    class of bug this repo has hit before (an ambient fact silently changing
    a test's outcome; see ``VLLM_CONTEXT_WINDOW`` in conftest.py). Tests that
    specifically exercise these checks call ``check_platform``/
    ``check_python_uv``/``check_permissions`` directly with their own
    explicit facts, bypassing this stub entirely (a direct import binds its
    own name in this module, independent of the patched module attribute
    ``run_checks`` looks up at call time).
    """
    monkeypatch.setattr(doctor_module, "detect_platform", lambda: ("Darwin", "arm64"))
    monkeypatch.setattr(
        doctor_module,
        "detect_python_uv",
        lambda package=doctor_module.PACKAGE_NAME: PythonUvFacts(
            python_version="3.13.0", min_python="3.12", uv_version="0.9.0"
        ),
    )
    monkeypatch.setattr(
        doctor_module,
        "detect_permissions",
        lambda: PermissionFacts(
            bin_dir=Path("/stub/bin"),
            bin_dir_writable=True,
            amplifier_home=Path("/stub/home/.amplifier"),
            amplifier_home_writable=True,
        ),
    )


def _ok(name: str, message: str) -> CheckResult:
    return CheckResult(name=name, ok=True, message=message)


def _finding(name: str, message: str) -> CheckResult:
    return CheckResult(name=name, ok=False, message=message)


def test_check_install_healthy_and_broken() -> None:
    assert check_install("amplifier-app-tui").ok
    assert check_install("amplifier-app-tui").message == "install healthy"
    broken = check_install("definitely-not-a-package-xyz")
    assert not broken.ok
    assert "not found" in broken.message


def test_check_path() -> None:
    assert check_path("python3").ok
    assert check_path("python3").message == "PATH clean"
    # Failure path is fully overridden here (never depends on this machine's
    # real PATH/home/search dirs) -- a generic executable name gets the
    # generic message, no amplifier-specific reinstall suggestion.
    missing = check_path(
        "no-such-binary-xyz",
        path_env="",
        shell_env="",
        home=Path("/stub/home"),
        search_dirs=(Path("/stub/nowhere"),),
    )
    assert not missing.ok
    assert missing.message == (
        "no-such-binary-xyz not on PATH and not found in the usual install dir(s) (/stub/nowhere)"
    )


# -- check_path: PATH repair guidance (D1 AC5) -------------------------------


def test_check_path_landed_reports_location_directory_and_zsh_export(tmp_path: Path) -> None:
    bin_dir = tmp_path / "local_bin"
    bin_dir.mkdir()
    exe = bin_dir / "amplifier-tui"
    exe.write_text("#!/bin/sh\n")
    result = check_path(
        "amplifier-tui",
        path_env="",
        shell_env="/bin/zsh",
        home=tmp_path,
        search_dirs=(bin_dir,),
    )
    assert not result.ok
    assert str(exe) in result.message
    assert str(bin_dir) in result.message
    assert "uv tool update-shell" in result.message
    assert f'export PATH="{bin_dir}:$PATH"' in result.message
    assert "~/.zshrc" in result.message


def test_check_path_landed_fish_uses_fish_add_path(tmp_path: Path) -> None:
    bin_dir = tmp_path / "local_bin"
    bin_dir.mkdir()
    (bin_dir / "amplifier-tui").write_text("#!/bin/sh\n")
    result = check_path(
        "amplifier-tui",
        path_env="",
        shell_env="/usr/local/bin/fish",
        home=tmp_path,
        search_dirs=(bin_dir,),
    )
    assert f"fish_add_path {bin_dir}" in result.message
    assert "config.fish" in result.message


def test_check_path_landed_unknown_shell_falls_back_generic(tmp_path: Path) -> None:
    bin_dir = tmp_path / "local_bin"
    bin_dir.mkdir()
    (bin_dir / "amplifier-tui").write_text("#!/bin/sh\n")
    result = check_path(
        "amplifier-tui",
        path_env="",
        shell_env="/bin/csh",  # not in _SHELL_RC_FILES
        home=tmp_path,
        search_dirs=(bin_dir,),
    )
    assert not result.ok
    assert "your shell's startup file" in result.message


def test_check_path_not_found_anywhere_suggests_reinstall_for_real_executable(
    tmp_path: Path,
) -> None:
    result = check_path(
        "amplifier-tui",  # the real EXECUTABLE_NAME default
        path_env="",
        shell_env="",
        home=tmp_path,
        search_dirs=(tmp_path / "nowhere",),
    )
    assert not result.ok
    assert SOURCE_INSTALL_LAUNCH_COMMAND in result.message
    assert "uv tool update-shell" in result.message


def test_check_path_never_raises_on_permission_error(tmp_path: Path, monkeypatch) -> None:
    """A search dir that can't even be stat'd degrades to "not found", never raises."""

    class _Boom(Path().__class__):  # pragma: no cover - trivial shim
        pass

    def _raise(*_a, **_k):
        raise OSError("permission denied")

    monkeypatch.setattr(doctor_module.Path, "is_file", _raise)
    result = check_path(
        "amplifier-tui",
        path_env="",
        shell_env="",
        home=tmp_path,
        search_dirs=(tmp_path,),
    )
    assert not result.ok  # degraded, not raised


# -- check_platform (D1 AC5) --------------------------------------------------


def test_check_platform_flags_native_windows() -> None:
    result = check_platform("Windows", "AMD64")
    assert not result.ok
    assert "WSL2" in result.message
    assert "wsl --install" in result.message


def test_check_platform_flags_unclaimed_posix_system() -> None:
    result = check_platform("FreeBSD", "amd64")
    assert not result.ok
    assert "outside the supported platform matrix" in result.message


def test_check_platform_flags_32_bit() -> None:
    result = check_platform("Linux", "i686")
    assert not result.ok
    assert "64-bit" in result.message


def test_check_platform_ok_on_macos_and_linux() -> None:
    mac = check_platform("Darwin", "arm64")
    assert mac.ok
    assert "Darwin" in mac.message and "arm64" in mac.message
    linux = check_platform("Linux", "x86_64")
    assert linux.ok


def test_check_platform_ok_on_wsl_reports_as_linux() -> None:
    """WSL's kernel genuinely identifies as Linux -- never conflated with
    native Windows, so a WSL user is never falsely flagged."""
    assert check_platform("Linux", "x86_64").ok


def test_detect_platform_never_raises() -> None:
    system, machine = detect_platform()
    assert isinstance(system, str) and system
    assert isinstance(machine, str)


# -- check_python_uv (D1 AC5) --------------------------------------------------


def test_check_python_uv_flags_old_python_with_upgrade_command() -> None:
    facts = PythonUvFacts(python_version="3.9.0", min_python="3.12", uv_version="0.9.0")
    result = check_python_uv(facts)
    assert not result.ok
    assert "3.9.0" in result.message and "3.12" in result.message
    assert "uv python install 3.12" in result.message
    assert SOURCE_INSTALL_COMMAND in result.message


def test_check_python_uv_flags_missing_uv_with_install_command() -> None:
    facts = PythonUvFacts(python_version="3.13.0", min_python="3.12", uv_version=None)
    result = check_python_uv(facts)
    assert not result.ok
    assert "uv not found" in result.message
    assert SOURCE_INSTALL_COMMAND in result.message


def test_check_python_uv_flags_both_independently() -> None:
    facts = PythonUvFacts(python_version="3.9.0", min_python="3.12", uv_version=None)
    result = check_python_uv(facts)
    assert not result.ok
    assert "3.9.0" in result.message
    assert "uv not found" in result.message


def test_check_python_uv_healthy_when_both_satisfied() -> None:
    facts = PythonUvFacts(python_version="3.13.0", min_python="3.12", uv_version="0.10.2")
    result = check_python_uv(facts)
    assert result.ok
    assert "3.13.0" in result.message and "0.10.2" in result.message


def test_check_python_uv_no_declared_floor_never_crashes() -> None:
    """A package with no readable ``Requires-Python`` degrades to "nothing to
    compare" -- never a crash, never a false finding."""
    facts = PythonUvFacts(python_version="3.13.0", min_python=None, uv_version="0.10.2")
    result = check_python_uv(facts)
    assert result.ok
    assert "3.13.0" in result.message


def test_check_python_uv_exact_floor_is_healthy() -> None:
    """Python exactly AT the floor satisfies it (>=, not >)."""
    facts = PythonUvFacts(python_version="3.12.0", min_python="3.12", uv_version="0.10.2")
    assert check_python_uv(facts).ok


def test_min_from_requires_python_variants() -> None:
    assert doctor_module._min_from_requires_python(">=3.12") == "3.12"
    assert doctor_module._min_from_requires_python(">= 3.10") == "3.10"
    assert doctor_module._min_from_requires_python(">=3.9,<4.0") == "3.9"
    assert doctor_module._min_from_requires_python("~=3.12") is None


def test_declared_min_python_reads_real_project_metadata() -> None:
    """This project's OWN declared floor, read via the real installed dist --
    never hardcoded (regression guard for AC5's "do not hardcode")."""
    assert doctor_module._declared_min_python("amplifier-app-tui") == "3.12"


def test_declared_min_python_missing_package_is_none() -> None:
    assert doctor_module._declared_min_python("definitely-not-a-package-xyz") is None


def test_uv_version_missing_binary_is_none(monkeypatch) -> None:
    monkeypatch.setattr(doctor_module.shutil, "which", lambda _name: None)
    assert doctor_module._uv_version() is None


def test_uv_version_parses_stdout(monkeypatch) -> None:
    class _Result:
        returncode = 0
        stdout = "uv 0.10.2 (a788db7e5 2026-02-10)\n"

    monkeypatch.setattr(doctor_module.shutil, "which", lambda _name: "/usr/local/bin/uv")
    monkeypatch.setattr(doctor_module.subprocess, "run", lambda *a, **k: _Result())
    assert doctor_module._uv_version() == "0.10.2"


def test_uv_version_never_raises_on_subprocess_failure(monkeypatch) -> None:
    def _boom(*_a, **_k):
        raise TimeoutError("hung")

    monkeypatch.setattr(doctor_module.shutil, "which", lambda _name: "/usr/local/bin/uv")
    monkeypatch.setattr(doctor_module.subprocess, "run", _boom)
    assert doctor_module._uv_version() is None


def test_detect_python_uv_never_raises() -> None:
    facts = detect_python_uv()
    assert isinstance(facts, PythonUvFacts)
    assert facts.python_version


# -- check_permissions (D1 AC5) ------------------------------------------------


def test_check_permissions_flags_unwritable_bin_dir() -> None:
    facts = PermissionFacts(
        bin_dir=Path("/opt/nope"),
        bin_dir_writable=False,
        amplifier_home=Path("/home/u/.amplifier"),
        amplifier_home_writable=True,
    )
    result = check_permissions(facts)
    assert not result.ok
    assert "/opt/nope" in result.message
    assert "sudo chown" in result.message
    assert "UV_TOOL_BIN_DIR" in result.message


def test_check_permissions_flags_unwritable_amplifier_home() -> None:
    facts = PermissionFacts(
        bin_dir=Path("/home/u/.local/bin"),
        bin_dir_writable=True,
        amplifier_home=Path("/home/u/.amplifier"),
        amplifier_home_writable=False,
    )
    result = check_permissions(facts)
    assert not result.ok
    assert "/home/u/.amplifier" in result.message
    assert "AMPLIFIER_HOME" in result.message


def test_check_permissions_flags_both_independently() -> None:
    facts = PermissionFacts(
        bin_dir=Path("/opt/nope"),
        bin_dir_writable=False,
        amplifier_home=Path("/home/u/.amplifier"),
        amplifier_home_writable=False,
    )
    result = check_permissions(facts)
    assert not result.ok
    assert "/opt/nope" in result.message
    assert "/home/u/.amplifier" in result.message


def test_check_permissions_healthy_when_both_writable() -> None:
    facts = PermissionFacts(
        bin_dir=Path("/home/u/.local/bin"),
        bin_dir_writable=True,
        amplifier_home=Path("/home/u/.amplifier"),
        amplifier_home_writable=True,
    )
    assert check_permissions(facts).ok


def test_writable_true_for_a_real_writable_tmp_dir(tmp_path: Path) -> None:
    assert doctor_module._writable(tmp_path)


def test_writable_true_for_not_yet_created_child_of_writable_dir(tmp_path: Path) -> None:
    """A not-yet-created app home is judged by whether it COULD be created."""
    not_yet = tmp_path / "amplifier_home_not_created_yet"
    assert not not_yet.exists()
    assert doctor_module._writable(not_yet)


def test_writable_false_when_no_ancestor_exists() -> None:
    # A path built from a UUID-shaped root essentially never exists.
    bogus = Path("/nonexistent-root-8f3a2c1d/also-nonexistent/child")
    assert doctor_module._writable(bogus) is False


def test_detect_permissions_never_raises() -> None:
    facts = detect_permissions()
    assert isinstance(facts, PermissionFacts)


# -- cross-check: every install surface shares one contract -------------------


def test_app_install_uri_matches_kernel_updater_repo_url() -> None:
    """The pure top-level contract keeps commands and kernel guidance identical."""
    assert APP_INSTALL_URI == f"git+{updater.APP_REPO_URL}"
    assert SOURCE_INSTALL_COMMAND in updater.self_update_hint(
        updater.AppIdentity("0.1.0", "abc1234", "git")
    )


# -- run_checks wiring: the three new dimensions actually surface findings ----


def test_run_checks_surfaces_platform_python_uv_permissions_findings(monkeypatch) -> None:
    monkeypatch.setattr(doctor_module, "detect_platform", lambda: ("Windows", "AMD64"))
    monkeypatch.setattr(
        doctor_module,
        "detect_python_uv",
        lambda package=doctor_module.PACKAGE_NAME: PythonUvFacts(
            python_version="3.9.0", min_python="3.12", uv_version=None
        ),
    )
    monkeypatch.setattr(
        doctor_module,
        "detect_permissions",
        lambda: PermissionFacts(
            bin_dir=Path("/opt/nope"),
            bin_dir_writable=False,
            amplifier_home=Path("/h/.amplifier"),
            amplifier_home_writable=False,
        ),
    )
    report = run_checks(settings_paths=(), package="amplifier-app-tui", executable="python3")
    assert report.finding_count == 3
    texts = [f.text for f in report.findings]
    assert any("WSL2" in t for t in texts)
    assert any("uv not found" in t for t in texts)
    assert any("not writable" in t for t in texts)


def test_run_checks_healthy_line_includes_new_dimensions_when_ok() -> None:
    # Uses the autouse-stubbed (healthy) detect_* -- default fixture above.
    report = run_checks(settings_paths=(), package="amplifier-app-tui", executable="python3")
    assert report.finding_count == 0
    assert "platform Darwin (arm64) supported" in report.healthy_summary
    assert "Python 3.13.0 (>=3.12)" in report.healthy_summary
    assert "uv 0.9.0" in report.healthy_summary
    assert "install/app-home dirs writable" in report.healthy_summary


def test_check_settings_parses_yaml_and_json(tmp_path: Path) -> None:
    good_yaml = tmp_path / "settings.yaml"
    good_yaml.write_text("theme: slate\n", encoding="utf-8")
    good_json = tmp_path / "settings.json"
    good_json.write_text('{"theme": "slate"}', encoding="utf-8")
    assert check_settings((good_yaml, good_json)).ok
    assert check_settings((good_yaml, good_json)).message == "settings parse"


def test_check_settings_missing_file_is_healthy(tmp_path: Path) -> None:
    assert check_settings((tmp_path / "nope.yaml",)).ok


def test_check_settings_flags_broken_file(tmp_path: Path) -> None:
    bad = tmp_path / "settings.json"
    bad.write_text("{not json", encoding="utf-8")
    result = check_settings((bad,))
    assert not result.ok
    assert "settings parse failed" in result.message


def test_check_unused_mcp_finding_matches_mockup_shape() -> None:
    stats = (
        McpServerStats(name="alpha", last_used_days_ago=45, tokens_per_session=2_100),
        McpServerStats(name="beta", last_used_days_ago=None, tokens_per_session=2_000),
        McpServerStats(name="live", last_used_days_ago=2, tokens_per_session=900),
    )
    result = check_unused_mcp(stats)
    assert not result.ok
    assert result.message == "2 MCP servers unused in 30 days · cost 4.1k tok/session"


def test_check_unused_mcp_all_in_use() -> None:
    stats = (McpServerStats(name="live", last_used_days_ago=1),)
    assert check_unused_mcp(stats).ok


def test_check_repeated_approvals() -> None:
    tallies = (
        ApprovalTally(action="read docs/", approved=14, asked=14, capability="read"),
        ApprovalTally(action="rm -rf /", approved=0, asked=3, capability="exec"),
    )
    result = check_repeated_approvals(tallies)
    assert not result.ok
    assert result.message == "14 identical read-only approvals this week · candidate allowlist"
    # Below threshold, or not read-only, or not always approved → healthy.
    assert check_repeated_approvals(
        (ApprovalTally(action="read x", approved=2, asked=2, capability="read"),)
    ).ok
    assert check_repeated_approvals(
        (ApprovalTally(action="write x", approved=20, asked=20, capability="write"),)
    ).ok
    assert check_repeated_approvals(
        (ApprovalTally(action="read x", approved=11, asked=12, capability="read"),)
    ).ok


def test_check_anchors_pin_stale_is_finding() -> None:
    status = AnchorsStatus(
        ref="main", has_update=True, cached_commit="aaaa1111", remote_commit="bbbb2222"
    )
    result = check_anchors_pin(status)
    assert not result.ok
    assert "behind upstream" in result.message


def test_check_anchors_pin_current_is_ok() -> None:
    status = AnchorsStatus(ref="main", has_update=False, cached_commit="cccc3333")
    result = check_anchors_pin(status)
    assert result.ok
    assert "up to date" in result.message


def test_check_anchors_pin_offline_is_ok_no_false_finding() -> None:
    status = AnchorsStatus(ref="main", error="network down")
    result = check_anchors_pin(status)
    assert result.ok  # offline never fabricates a finding


def test_check_anchors_pin_none_is_skipped_ok() -> None:
    result = check_anchors_pin(None)
    assert result.ok
    assert "skipped" in result.message


def test_run_checks_includes_stale_anchors_finding() -> None:
    stale = AnchorsStatus(ref="main", has_update=True, cached_commit="a1", remote_commit="b2")
    report = run_checks(
        settings_paths=(),
        package="amplifier-app-tui",
        executable="python3",
        anchors_status=stale,
    )
    assert any("anchors" in f.text for f in report.findings)


def test_report_headline_and_healthy_join() -> None:
    report = DoctorReport(
        checks=(
            _ok("install", "install healthy"),
            _ok("path", "PATH clean"),
            _ok("settings", "settings parse"),
            _finding("mcp", "2 MCP servers unused in 30 days · cost 4.1k tok/session"),
            _finding(
                "approvals", "14 identical read-only approvals this week · candidate allowlist"
            ),
        )
    )
    assert report.headline() == "2 findings · nothing changed yet"
    assert report.healthy_summary == "install healthy · PATH clean · settings parse"
    assert [f.number for f in report.findings] == [1, 2]


def test_single_finding_headline_singular() -> None:
    report = DoctorReport(checks=(_finding("mcp", "x"),))
    assert report.headline() == "1 finding · nothing changed yet"


def test_build_doctor_block() -> None:
    report = DoctorReport(checks=(_ok("install", "install healthy"), _finding("mcp", "unused")))
    block = build_doctor_block("b3", report)
    assert block.kind == "doctor"
    assert block.headline == "1 finding · nothing changed yet"
    assert block.healthy == ("install healthy",)
    assert block.findings[0].number == 1
    assert block.findings[0].text == "unused"


def test_run_checks_end_to_end(tmp_path: Path) -> None:
    report = run_checks(
        mcp_stats=(),
        approval_tallies=(),
        settings_paths=(tmp_path / "settings.yaml",),
        package="amplifier-app-tui",
        executable="python3",
    )
    assert report.finding_count == 0
    assert "install healthy" in report.healthy_summary
    assert "PATH clean" in report.healthy_summary
    assert "settings parse" in report.healthy_summary


def test_run_checks_includes_composed_launch_preflight() -> None:
    report = run_checks(
        settings_paths=(),
        package="amplifier-app-tui",
        executable="python3",
        additional_checks=(
            CheckResult(
                name="launch-preflight",
                ok=False,
                message="launch blocked: provider failed to import · run init",
            ),
        ),
    )
    assert report.finding_count == 1
    assert report.findings[0].text == "launch blocked: provider failed to import · run init"


def test_render_text_matches_mockup_row_shapes() -> None:
    report = DoctorReport(
        checks=(
            _ok("install", "install healthy"),
            _ok("path", "PATH clean"),
            _ok("settings", "settings parse"),
            _finding("mcp", "2 MCP servers unused in 30 days · cost 4.1k tok/session"),
        )
    )
    text = render_text(report)
    lines = text.splitlines()
    assert lines[0] == "amplifier-tui doctor"
    assert "Doctor  1 finding · nothing changed yet" in lines
    assert "  ✔ install healthy · PATH clean · settings parse" in lines
    assert "  1 2 MCP servers unused in 30 days · cost 4.1k tok/session" in lines


def test_run_standalone_exit_codes(tmp_path: Path) -> None:
    printed: list[str] = []
    code = run_standalone(
        mcp_stats=(McpServerStats(name="dead", last_used_days_ago=None, tokens_per_session=500),),
        settings_paths=(tmp_path / "settings.yaml",),
        package="amplifier-app-tui",
        executable="python3",
        echo=printed.append,
    )
    assert code == 1
    assert "amplifier-tui doctor" in printed[0]
    assert "✔" in printed[0]

    printed.clear()
    code = run_standalone(
        settings_paths=(tmp_path / "settings.yaml",),
        package="amplifier-app-tui",
        executable="python3",
        echo=printed.append,
    )
    assert code == 0
    assert "0 findings · nothing changed yet" in printed[0]


def test_run_standalone_fails_when_composed_preflight_fails(tmp_path: Path) -> None:
    printed: list[str] = []
    code = run_standalone(
        settings_paths=(tmp_path / "settings.yaml",),
        package="amplifier-app-tui",
        executable="python3",
        additional_checks=(
            CheckResult(name="launch-preflight", ok=False, message="launch blocked: bad source"),
        ),
        echo=printed.append,
    )
    assert code == 1
    assert "launch blocked: bad source" in printed[0]


# --------------------------------------------------------------------------
# check_mounts — the target of the degraded notice's "run doctor for details"
# --------------------------------------------------------------------------


def test_check_mounts_skipped_without_a_session() -> None:
    # Standalone `amplifier-tui doctor` runs outside a session: say the check
    # was skipped rather than imply the mounts were verified healthy.
    result = check_mounts(None)
    assert result.ok
    assert result.message == "mount check skipped (no session)"


def test_check_mounts_green_when_everything_mounted() -> None:
    assert check_mounts(MountReport()).ok


def test_check_mounts_names_the_failed_modules() -> None:
    result = check_mounts(
        MountReport(missing_providers=("vllm",), missing_tools=("tool-team-pulse",))
    )
    assert not result.ok
    assert "provider(s) unavailable: vllm" in result.message
    assert "tool module(s) failed to mount: tool-team-pulse" in result.message
    assert "amplifier-tui update --force" in result.message


def test_run_checks_surfaces_a_degraded_mount_as_a_finding() -> None:
    # The gap this closes: before, a degraded boot still reported "0 findings",
    # so the notice's `run doctor for details` pointer led nowhere.
    report = run_checks(mount_report=MountReport(missing_tools=("tool-team-pulse",)))
    assert report.finding_count == 1
    assert "tool-team-pulse" in report.findings[0].text


def test_check_unused_mcp_empty_is_honest() -> None:
    """Zero configured servers must not claim "MCP servers in use"."""
    from amplifier_app_tui.commands import doctor

    result = doctor.check_unused_mcp(())
    assert result.ok
    assert result.message == "no MCP servers configured"
