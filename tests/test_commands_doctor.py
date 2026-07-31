"""/doctor named checks, report shape, block build, standalone CLI."""

from __future__ import annotations

from pathlib import Path

from amplifier_app_tui.commands.doctor import (
    CheckResult,
    DoctorReport,
    McpServerStats,
    build_doctor_block,
    check_anchors_pin,
    check_install,
    check_path,
    check_repeated_approvals,
    check_settings,
    check_unused_mcp,
    render_text,
    run_checks,
    run_standalone,
)
from amplifier_app_tui.commands.improve import ApprovalTally
from amplifier_app_tui.kernel.updater import AnchorsStatus


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
    missing = check_path("no-such-binary-xyz")
    assert not missing.ok
    assert missing.message == "no-such-binary-xyz not on PATH"


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
