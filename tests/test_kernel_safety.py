"""Approval and execution path policy are independent safety axes."""

from __future__ import annotations

from pathlib import Path

from amplifier_app_newtui.kernel.directory_permissions import DirectoryPolicy
from amplifier_app_newtui.kernel.safety import resolve_safety
from amplifier_app_newtui.model.trust import CapabilityClass, resolve, resolve_capability


def _capability(capability: CapabilityClass):
    return resolve_capability("build", capability)


def test_allowlisted_write_still_cannot_cross_path_policy(tmp_path: Path) -> None:
    policy = DirectoryPolicy(tmp_path / "project")
    approval = resolve("auto", "write_file", {"path": "../outside.txt"})
    assert approval.decision == "allow"
    safety = resolve_safety(
        approval,
        action="write_file · ../outside.txt",
        target="../outside.txt",
        directory_policy=policy,
        resolve_capability=_capability,
    )
    assert safety.approval.decision == "allow"
    assert safety.execution_policy == "blocked"


def test_inside_write_preserves_approval_and_satisfies_path_policy(tmp_path: Path) -> None:
    project = tmp_path / "project"
    policy = DirectoryPolicy(project)
    approval = resolve("build", "write_file", {"path": "src/app.py"})
    safety = resolve_safety(
        approval,
        action="write_file · src/app.py",
        target="src/app.py",
        directory_policy=policy,
        resolve_capability=_capability,
    )
    assert safety.approval.decision == "ask"
    assert safety.execution_policy == "within-policy"


def test_outside_read_roams_within_denylist(tmp_path: Path) -> None:
    """Reads are denylist-bounded, not allowlist-bounded — amplifier may read
    wherever it needs (matching amplifier-app-cli's permissive read defaults)
    without the outside-project gate."""
    policy = DirectoryPolicy(tmp_path / "project")
    approval = resolve("chat", "read_file", {"path": "/tmp/outside.txt"})
    safety = resolve_safety(
        approval,
        action="read_file · /tmp/outside.txt",
        target="/tmp/outside.txt",
        directory_policy=policy,
        resolve_capability=_capability,
    )
    assert safety.execution_policy == "within-policy"
    assert safety.approval.capability == CapabilityClass.READ
    assert safety.approval.decision == "allow"


def test_denied_directory_read_is_blocked(tmp_path: Path) -> None:
    """The 'within reason' boundary: user-denied directories gate reads too."""
    secrets = tmp_path / "secrets"
    policy = DirectoryPolicy(tmp_path / "project", denied=(str(secrets),))
    approval = resolve("chat", "read_file", {"path": str(secrets / "k.txt")})
    safety = resolve_safety(
        approval,
        action=f"read_file · {secrets / 'k.txt'}",
        target=str(secrets / "k.txt"),
        directory_policy=policy,
        resolve_capability=_capability,
    )
    assert safety.execution_policy == "blocked"
    assert "denied" in safety.policy_reason


def test_read_shaped_shell_outside_project_roams(tmp_path: Path) -> None:
    """Read-shaped commands may roam outside the project; only write-shaped
    commands (write heads, redirect targets) inherit the outside-project gate."""
    policy = DirectoryPolicy(tmp_path / "project")
    approval = resolve("auto", "bash", {"command": "ls -la /tmp/elsewhere 2>/dev/null"})
    safety = resolve_safety(
        approval,
        action="ls -la /tmp/elsewhere 2>/dev/null",
        target="",
        directory_policy=policy,
        resolve_capability=_capability,
    )
    assert safety.execution_policy == "within-policy"


def test_write_shaped_shell_outside_project_is_gated(tmp_path: Path) -> None:
    policy = DirectoryPolicy(tmp_path / "project")
    approval = resolve("auto", "bash", {"command": "rm /tmp/elsewhere/x.txt"})
    safety = resolve_safety(
        approval,
        action="rm /tmp/elsewhere/x.txt",
        target="",
        directory_policy=policy,
        resolve_capability=_capability,
    )
    assert safety.execution_policy == "outside-policy"
    assert safety.approval.capability == CapabilityClass.OUTSIDE_PROJECT


def test_protected_shell_target_is_blocked_even_when_exec_is_allowlisted(
    tmp_path: Path,
) -> None:
    project = tmp_path / "project"
    policy = DirectoryPolicy(project)
    approval = resolve("auto", "bash", {"command": "echo bad > ./AGENTS.md"})
    safety = resolve_safety(
        approval,
        action="echo bad > ./AGENTS.md",
        target="",
        directory_policy=policy,
        resolve_capability=_capability,
    )
    assert safety.execution_policy == "blocked"
    assert "protected" in safety.policy_reason


def test_bare_protected_shell_target_is_also_blocked(tmp_path: Path) -> None:
    policy = DirectoryPolicy(tmp_path / "project")
    approval = resolve("auto", "bash", {"command": "git config -f .git/config x y"})
    safety = resolve_safety(
        approval,
        action="git config -f .git/config x y",
        target="",
        directory_policy=policy,
        resolve_capability=_capability,
    )
    assert safety.execution_policy == "blocked"
