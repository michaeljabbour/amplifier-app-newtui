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
    )
    assert policy.check_write(project / "ok.txt")[0]
    assert policy.check_write(outside / "ok.txt")[0]
    assert not policy.check_write(blocked / "no.txt")[0]
    assert not policy.check_write(tmp_path / "elsewhere" / "no.txt")[0]


def test_shell_path_signal_respects_allowed_and_parent_escape(tmp_path: Path) -> None:
    project = tmp_path / "project"
    shared = tmp_path / "shared"
    policy = DirectoryPolicy(project, allowed=(str(shared),))
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


def test_write_shaped_shell_is_flagged_outside_project(tmp_path: Path) -> None:
    policy = DirectoryPolicy(tmp_path / "project")
    assert policy.shell_outside_target("rm /tmp/elsewhere/x.txt") is not None
    assert policy.shell_outside_target("echo x > /tmp/elsewhere/x.txt") is not None
    assert policy.shell_outside_target("cd /tmp && rm /tmp/elsewhere/x.txt") is not None


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
