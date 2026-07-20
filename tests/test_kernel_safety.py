"""Approval and execution path policy are independent safety axes."""

from __future__ import annotations

from pathlib import Path

from amplifier_app_newtui.kernel.directory_permissions import DirectoryPolicy
from amplifier_app_newtui.kernel.safety import resolve_safety
from amplifier_app_newtui.model.trust import CapabilityClass, resolve


def test_allowlisted_write_still_cannot_cross_path_policy(tmp_path: Path) -> None:
    policy = DirectoryPolicy(tmp_path / "project")
    approval = resolve("auto", "write_file", {"path": "../outside.txt"})
    assert approval.decision == "allow"
    safety = resolve_safety(
        approval,
        action="write_file · ../outside.txt",
        target="../outside.txt",
        directory_policy=policy,
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
    )
    assert safety.approval.decision == "ask"
    assert safety.execution_policy == "within-policy"


def test_outside_read_is_not_constrained_by_write_roots(tmp_path: Path) -> None:
    policy = DirectoryPolicy(tmp_path / "project")
    approval = resolve("chat", "read_file", {"path": "/tmp/outside.txt"})
    safety = resolve_safety(
        approval,
        action="read_file · /tmp/outside.txt",
        target="/tmp/outside.txt",
        directory_policy=policy,
    )
    assert safety.execution_policy == "not-applicable"
    assert safety.approval.capability == CapabilityClass.READ
    assert safety.approval.decision == "allow"


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
    )
    assert safety.execution_policy == "blocked"
