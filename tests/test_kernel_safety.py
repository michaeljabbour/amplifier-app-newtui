"""Approval and execution path policy are independent safety axes."""

from __future__ import annotations

from pathlib import Path

from amplifier_app_newtui.kernel.directory_permissions import DirectoryPolicy
from amplifier_app_newtui.kernel.safety import resolve_safety
from amplifier_app_newtui.model.trust import CapabilityClass, resolve, resolve_capability


def _capability(capability: CapabilityClass):
    return resolve_capability("build", capability)


def test_guarded_boundary_blocks_outside_write_preflight(tmp_path: Path) -> None:
    policy = DirectoryPolicy(tmp_path / "project", write_boundary="guarded")
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


def test_open_boundary_defers_outside_write_to_filesystem_tool(tmp_path: Path) -> None:
    """Default posture (app-cli parity): no governance pre-flight block for an
    outside write — the mounted filesystem tool remains the hard enforcement
    point and returns a graceful tool error instead of a governance denial."""
    policy = DirectoryPolicy(tmp_path / "project")
    approval = resolve("auto", "write_file", {"path": "../outside.txt"})
    safety = resolve_safety(
        approval,
        action="write_file · ../outside.txt",
        target="../outside.txt",
        directory_policy=policy,
        resolve_capability=_capability,
    )
    assert safety.execution_policy == "within-policy"
    assert "filesystem tool" in safety.policy_reason


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


def test_write_shaped_shell_outside_project_is_gated_when_guarded(tmp_path: Path) -> None:
    policy = DirectoryPolicy(tmp_path / "project", write_boundary="guarded")
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


def test_write_shaped_shell_outside_project_roams_when_open(tmp_path: Path) -> None:
    """Default posture (app-cli parity): bash writes are not path-confined —
    like amplifier-app-cli's unconfined bash tool."""
    policy = DirectoryPolicy(tmp_path / "project")
    approval = resolve("auto", "bash", {"command": "rm /tmp/elsewhere/x.txt"})
    safety = resolve_safety(
        approval,
        action="rm /tmp/elsewhere/x.txt",
        target="",
        directory_policy=policy,
        resolve_capability=_capability,
    )
    assert safety.execution_policy == "within-policy"


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


def test_embedded_protected_shell_target_escalates_to_ask(tmp_path: Path) -> None:
    """Audit H1 fail-closed: a protected path buried inside `python3 -c`
    escapes the command-list token pass (and the bash tool's own validator
    enforces no write-path list), so governance escalates it to *ask* rather
    than silently allowing it -- even in the default open posture."""
    policy = DirectoryPolicy(tmp_path / "project")  # open posture (default)
    command = "python3 -c \"open('.git/config','w').write('x')\""
    approval = resolve("build", "bash", {"command": command})
    safety = resolve_safety(
        approval,
        action=command,
        target="",
        directory_policy=policy,
        resolve_capability=_capability,
    )
    assert safety.execution_policy == "outside-policy"
    assert safety.approval.decision == "ask"
    assert safety.approval.capability == CapabilityClass.OUTSIDE_PROJECT
    assert safety.target == ".git"


def test_embedded_protected_shell_target_in_auto_is_classifier_gated(tmp_path: Path) -> None:
    """In auto mode the escalated ask is classifier-gated -- the reasoning-blind
    classifier adjudicates and denies-and-continue on refusal."""
    policy = DirectoryPolicy(tmp_path / "project")

    def auto_capability(capability: CapabilityClass):
        return resolve_capability("auto", capability)

    command = "sed -i 's/a/b/' vendored/.git/config"
    approval = resolve("auto", "bash", {"command": command})
    safety = resolve_safety(
        approval,
        action=command,
        target="",
        directory_policy=policy,
        resolve_capability=auto_capability,
    )
    assert safety.execution_policy == "outside-policy"
    assert safety.approval.classifier_gated is True


def test_embedded_outside_write_still_roams_when_open(tmp_path: Path) -> None:
    """Documented residual: a merely-outside write via python3 -c is not
    path-confined in the default open posture (the filesystem tool cannot see
    interpreter code). Only protected paths fail closed here."""
    policy = DirectoryPolicy(tmp_path / "project")
    command = "python3 -c \"open('/tmp/outside.txt','w')\""
    approval = resolve("auto", "bash", {"command": command})
    safety = resolve_safety(
        approval,
        action=command,
        target="",
        directory_policy=policy,
        resolve_capability=_capability,
    )
    assert safety.execution_policy == "within-policy"
