"""``/doctor`` — named setup checks with OK / finding rows (DESIGN-SPEC §6).

Pattern ported from amplifier-app-opencode's ``doctor`` subcommand
(RESEARCH-BRIEF §5): a list of named checks, each returning an OK or a
finding; CI-friendly exit codes when run standalone. Mockup output:

    · Doctor  3 findings · nothing changed yet
      ✔ install healthy · PATH clean · settings parse
      1 2 MCP servers unused in 30 days · cost 4.1k tok/session
      2 14 identical read-only approvals this week · candidate allowlist

Healthy checks collapse into ONE green ``✔`` line (messages joined with
`` · ``); each failing check becomes a numbered orange finding. /doctor
reports only — fixes happen on explicit confirm, elsewhere.

Runnable standalone: :func:`run_standalone` prints a plain-text report
and returns an exit code (0 = no findings, 1 = findings) so the
integrator can wire ``amplifier-tui doctor`` straight to it.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterable, Sequence
from importlib import metadata
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from ..model.blocks import DoctorBlock, DoctorFinding
from ..model.formatting import format_tokens_compact
from .improve import ApprovalTally

PACKAGE_NAME = "amplifier-app-tui"
EXECUTABLE_NAME = "amplifier-tui"
DEFAULT_SETTINGS_PATHS = (
    Path.home() / ".amplifier" / "settings.yaml",
    Path.home() / ".amplifier" / "settings.json",
)

UNUSED_MCP_THRESHOLD_DAYS = 30
REPEATED_APPROVAL_THRESHOLD = 10
"""Identical read-only approvals this session/week before /doctor flags
an allowlist candidate."""


class CheckResult(BaseModel):
    """One named check outcome: OK (joins the ✔ line) or a finding."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    ok: bool
    message: str


class McpServerStats(BaseModel):
    """Usage stats for one configured MCP server (input to the unused check).

    ``last_used_days_ago`` is ``None`` when the server has never been
    used; ``tokens_per_session`` is its schema/handshake overhead cost.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    last_used_days_ago: float | None = Field(default=None, ge=0)
    tokens_per_session: int = Field(default=0, ge=0)

    def unused_for(self, days: float) -> bool:
        return self.last_used_days_ago is None or self.last_used_days_ago >= days


class DoctorReport(BaseModel):
    """All check outcomes, split into the ✔ summary and numbered findings."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    checks: tuple[CheckResult, ...]

    @property
    def healthy_summary(self) -> str:
        """The single green line: OK messages joined with `` · ``."""
        return " · ".join(check.message for check in self.checks if check.ok)

    @property
    def findings(self) -> tuple[DoctorFinding, ...]:
        """Failing checks as numbered orange findings, in check order."""
        return tuple(
            DoctorFinding(number=index + 1, text=check.message)
            for index, check in enumerate([check for check in self.checks if not check.ok])
        )

    @property
    def finding_count(self) -> int:
        return sum(1 for check in self.checks if not check.ok)

    def headline(self) -> str:
        """``3 findings · nothing changed yet`` (mockup header suffix)."""
        count = self.finding_count
        noun = "finding" if count == 1 else "findings"
        return f"{count} {noun} · nothing changed yet"


# --- named checks ------------------------------------------------------


def check_install(package: str = PACKAGE_NAME) -> CheckResult:
    """The package resolves to an installed distribution."""
    try:
        metadata.version(package)
    except metadata.PackageNotFoundError:
        return CheckResult(
            name="install", ok=False, message=f"install broken · {package} not found"
        )
    return CheckResult(name="install", ok=True, message="install healthy")


def check_path(executable: str = EXECUTABLE_NAME) -> CheckResult:
    """The console script is reachable on PATH."""
    if shutil.which(executable) is None:
        return CheckResult(name="path", ok=False, message=f"{executable} not on PATH")
    return CheckResult(name="path", ok=True, message="PATH clean")


def check_settings(paths: Sequence[Path] = DEFAULT_SETTINGS_PATHS) -> CheckResult:
    """Every existing settings file parses (YAML or JSON).

    No settings file at all is healthy — defaults apply.
    """
    for path in paths:
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8")
            if path.suffix == ".json":
                json.loads(text)
            else:
                import yaml

                yaml.safe_load(text)
        except Exception as exc:  # noqa: BLE001 — any parse failure is the finding
            return CheckResult(
                name="settings",
                ok=False,
                message=f"settings parse failed · {path.name}: {exc}",
            )
    return CheckResult(name="settings", ok=True, message="settings parse")


def check_unused_mcp(
    stats: Iterable[McpServerStats],
    *,
    threshold_days: float = UNUSED_MCP_THRESHOLD_DAYS,
) -> CheckResult:
    """Configured MCP servers nobody has used lately still cost tokens."""
    stats = tuple(stats)
    if not stats:
        # Zero configured servers is healthy — but say so honestly instead of
        # the misleading "MCP servers in use" (the CLI doctor passes no stats).
        return CheckResult(name="mcp", ok=True, message="no MCP servers configured")
    unused = [server for server in stats if server.unused_for(threshold_days)]
    if not unused:
        return CheckResult(name="mcp", ok=True, message="MCP servers in use")
    cost = sum(server.tokens_per_session for server in unused)
    count = len(unused)
    noun = "server" if count == 1 else "servers"
    return CheckResult(
        name="mcp",
        ok=False,
        message=(
            f"{count} MCP {noun} unused in {round(threshold_days)} days "
            f"· cost {format_tokens_compact(cost)} tok/session"
        ),
    )


def check_repeated_approvals(
    tallies: Iterable[ApprovalTally],
    *,
    threshold: int = REPEATED_APPROVAL_THRESHOLD,
) -> CheckResult:
    """Repeated identical read-only approvals are an allowlist candidate."""
    repeated = sum(
        tally.asked for tally in tallies if tally.capability == "read" and tally.always_approved
    )
    if repeated < threshold:
        return CheckResult(name="approvals", ok=True, message="no repeated approvals")
    return CheckResult(
        name="approvals",
        ok=False,
        message=(f"{repeated} identical read-only approvals this week · candidate allowlist"),
    )


@runtime_checkable
class AnchorsPinStatus(Protocol):
    """Structural shape of ``kernel.updater.AnchorsStatus`` the check reads.

    Kept as a Protocol so ``commands/`` never imports ``kernel/`` (ADR-0007
    layering); the CLI computes the status and injects it here."""

    @property
    def is_stale(self) -> bool: ...

    @property
    def error(self) -> str | None: ...

    def describe(self) -> str: ...


def check_anchors_pin(status: AnchorsPinStatus | None) -> CheckResult:
    """The composed anchors bundle is not behind its upstream ref.

    Anchors is included (not a direct source), so ``update``'s per-bundle
    check skips it — this surfaces its freshness instead of leaving it silent.
    Green when current, when offline (``error`` set — never a false finding),
    or when no status was supplied. A confirmed-behind cache is the finding."""
    if status is None:
        return CheckResult(name="anchors", ok=True, message="anchors ref check skipped")
    if status.error is not None:
        return CheckResult(name="anchors", ok=True, message=status.describe())
    if status.is_stale:
        return CheckResult(name="anchors", ok=False, message=status.describe())
    return CheckResult(name="anchors", ok=True, message=status.describe())


@runtime_checkable
class MountHealth(Protocol):
    """The subset of ``session_factory.MountReport`` this check reads."""

    @property
    def missing_providers(self) -> tuple[str, ...]: ...

    @property
    def missing_tools(self) -> tuple[str, ...]: ...


def check_mounts(report: MountHealth | None) -> CheckResult:
    """Every configured provider and tool module registered something.

    This is what ``run doctor for details`` was always pointing at. The
    degraded-start notice (``session_factory.MountReport.degraded_notice``)
    names the failed modules and then sends the user here — but doctor had no
    mount check at all, so a degraded boot still reported "0 findings". Green
    when nothing failed, and green when no report was supplied (the standalone
    ``amplifier-tui doctor`` runs outside a session and has nothing to inspect
    — say so rather than imply health).
    """
    if report is None:
        return CheckResult(name="mounts", ok=True, message="mount check skipped (no session)")
    parts: list[str] = []
    if report.missing_providers:
        parts.append(f"provider(s) unavailable: {', '.join(report.missing_providers)}")
    if report.missing_tools:
        parts.append(f"tool module(s) failed to mount: {', '.join(report.missing_tools)}")
    if not parts:
        return CheckResult(name="mounts", ok=True, message="all modules mounted")
    return CheckResult(
        name="mounts",
        ok=False,
        message=f"{' · '.join(parts)} · reinstall with `amplifier-tui update --force`",
    )


def run_checks(
    *,
    mcp_stats: Iterable[McpServerStats] = (),
    approval_tallies: Iterable[ApprovalTally] = (),
    settings_paths: Sequence[Path] = DEFAULT_SETTINGS_PATHS,
    package: str = PACKAGE_NAME,
    executable: str = EXECUTABLE_NAME,
    anchors_status: AnchorsPinStatus | None = None,
    mount_report: MountHealth | None = None,
) -> DoctorReport:
    """Run the full named-check suite and return the report."""
    return DoctorReport(
        checks=(
            check_install(package),
            check_path(executable),
            check_settings(settings_paths),
            check_mounts(mount_report),
            check_unused_mcp(mcp_stats),
            check_repeated_approvals(approval_tallies),
            check_anchors_pin(anchors_status),
        )
    )


def build_doctor_block(block_id: str, report: DoctorReport) -> DoctorBlock:
    """Assemble the ``/doctor`` transcript block: the ``Doctor  <headline>``
    header, one joined ✔ healthy line, plus the numbered findings."""
    healthy = (report.healthy_summary,) if report.healthy_summary else ()
    return DoctorBlock(
        id=block_id,
        headline=report.headline(),
        healthy=healthy,
        findings=report.findings,
    )


# --- standalone CLI surface ---------------------------------------------


def render_text(report: DoctorReport) -> str:
    """Plain-text report for the ``amplifier-tui doctor`` subcommand."""
    lines = [f"{EXECUTABLE_NAME} doctor", "", f"Doctor  {report.headline()}"]
    if report.healthy_summary:
        lines.append(f"  ✔ {report.healthy_summary}")
    for finding in report.findings:
        lines.append(f"  {finding.number} {finding.text}")
    return "\n".join(lines)


def run_standalone(
    *,
    mcp_stats: Iterable[McpServerStats] = (),
    approval_tallies: Iterable[ApprovalTally] = (),
    settings_paths: Sequence[Path] = DEFAULT_SETTINGS_PATHS,
    package: str = PACKAGE_NAME,
    executable: str = EXECUTABLE_NAME,
    anchors_status: AnchorsPinStatus | None = None,
    mount_report: MountHealth | None = None,
    echo=print,
) -> int:
    """Run checks, print the plain report, return the CI exit code.

    0 = no findings; 1 = findings present (opencode doctor convention).
    """
    report = run_checks(
        mcp_stats=mcp_stats,
        approval_tallies=approval_tallies,
        settings_paths=settings_paths,
        package=package,
        executable=executable,
        anchors_status=anchors_status,
        mount_report=mount_report,
    )
    echo(render_text(report))
    return 0 if report.finding_count == 0 else 1


__all__ = [
    "AnchorsPinStatus",
    "CheckResult",
    "DoctorReport",
    "EXECUTABLE_NAME",
    "McpServerStats",
    "MountHealth",
    "PACKAGE_NAME",
    "REPEATED_APPROVAL_THRESHOLD",
    "UNUSED_MCP_THRESHOLD_DAYS",
    "build_doctor_block",
    "check_anchors_pin",
    "check_install",
    "check_mounts",
    "check_path",
    "check_repeated_approvals",
    "check_settings",
    "check_unused_mcp",
    "render_text",
    "run_checks",
    "run_standalone",
]
