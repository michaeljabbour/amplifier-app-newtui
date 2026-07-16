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
integrator can wire ``amplifier-newtui doctor`` straight to it.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterable, Sequence
from importlib import metadata
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ..model.blocks import DoctorBlock, DoctorFinding
from .context import format_tokens
from .improve import ApprovalTally

PACKAGE_NAME = "amplifier-app-newtui"
EXECUTABLE_NAME = "amplifier-newtui"
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
            for index, check in enumerate(
                [check for check in self.checks if not check.ok]
            )
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
        return CheckResult(
            name="path", ok=False, message=f"{executable} not on PATH"
        )
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
            f"· cost {format_tokens(cost)} tok/session"
        ),
    )


def check_repeated_approvals(
    tallies: Iterable[ApprovalTally],
    *,
    threshold: int = REPEATED_APPROVAL_THRESHOLD,
) -> CheckResult:
    """Repeated identical read-only approvals are an allowlist candidate."""
    repeated = sum(
        tally.asked
        for tally in tallies
        if tally.capability == "read" and tally.always_approved
    )
    if repeated < threshold:
        return CheckResult(name="approvals", ok=True, message="no repeated approvals")
    return CheckResult(
        name="approvals",
        ok=False,
        message=(
            f"{repeated} identical read-only approvals this week "
            "· candidate allowlist"
        ),
    )


def run_checks(
    *,
    mcp_stats: Iterable[McpServerStats] = (),
    approval_tallies: Iterable[ApprovalTally] = (),
    settings_paths: Sequence[Path] = DEFAULT_SETTINGS_PATHS,
    package: str = PACKAGE_NAME,
    executable: str = EXECUTABLE_NAME,
) -> DoctorReport:
    """Run the full named-check suite and return the report."""
    return DoctorReport(
        checks=(
            check_install(package),
            check_path(executable),
            check_settings(settings_paths),
            check_unused_mcp(mcp_stats),
            check_repeated_approvals(approval_tallies),
        )
    )


def build_doctor_block(block_id: str, report: DoctorReport) -> DoctorBlock:
    """Assemble the ``/doctor`` transcript block: one joined ✔ healthy line
    plus the numbered findings (the ``Doctor  <headline>`` header line is
    the renderer's, via :meth:`DoctorReport.headline`)."""
    healthy = (report.healthy_summary,) if report.healthy_summary else ()
    return DoctorBlock(id=block_id, healthy=healthy, findings=report.findings)


# --- standalone CLI surface ---------------------------------------------


def render_text(report: DoctorReport) -> str:
    """Plain-text report for the ``amplifier-newtui doctor`` subcommand."""
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
    )
    echo(render_text(report))
    return 0 if report.finding_count == 0 else 1


__all__ = [
    "CheckResult",
    "DoctorReport",
    "EXECUTABLE_NAME",
    "McpServerStats",
    "PACKAGE_NAME",
    "REPEATED_APPROVAL_THRESHOLD",
    "UNUSED_MCP_THRESHOLD_DAYS",
    "build_doctor_block",
    "check_install",
    "check_path",
    "check_repeated_approvals",
    "check_settings",
    "check_unused_mcp",
    "render_text",
    "run_checks",
    "run_standalone",
]
