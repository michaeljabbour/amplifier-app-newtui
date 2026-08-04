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
import os
import platform
import re
import shutil
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
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

APP_INSTALL_URI = "git+https://github.com/michaeljabbour/amplifier-app-tui"
"""The README's documented global-install source.

Kept as a literal here rather than imported (``commands/`` may not import
``kernel/`` -- ADR-0007 layering); pinned equal to
``kernel.updater.APP_REPO_URL`` by a cross-check test so the two copies can
never quietly drift apart.
"""

_UNSUPPORTED_MACHINES = frozenset({"i386", "i486", "i586", "i686", "x86"})
"""32-bit archs: uv and this app's dependencies (textual, httpx[socks], ...)
publish only 64-bit wheels/binaries today (x86_64/amd64, arm64/aarch64)."""

_SHELL_RC_FILES: dict[str, str] = {
    "zsh": "~/.zshrc",
    "bash": "~/.bashrc (~/.bash_profile on macOS)",
    "fish": "~/.config/fish/config.fish",
    "ksh": "~/.kshrc",
    "dash": "~/.profile",
    "sh": "~/.profile",
}
"""Startup file per shell, keyed by the ``$SHELL`` basename (see
:func:`_detect_shell`) -- the exact file ``check_path``'s PATH-repair
guidance names."""

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


def _detect_shell(shell_env: str) -> str:
    """Shell name from a ``$SHELL``-shaped path; ``"unknown"`` when unclear."""
    name = Path(shell_env).name if shell_env else ""
    return name if name in _SHELL_RC_FILES else "unknown"


def _path_export_line(shell: str, directory: Path) -> str:
    """The exact line to paste into the shell's startup file."""
    if shell == "fish":
        return f"fish_add_path {directory}"
    return f'export PATH="{directory}:$PATH"'


def _find_on_disk(executable: str, dirs: Sequence[Path]) -> Path | None:
    """The first *executable* found under *dirs* — real file check, never raises."""
    for directory in dirs:
        candidate = directory / executable
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def _default_bin_dirs(home: Path, env: Mapping[str, str]) -> tuple[Path, ...]:
    """Best-effort guess at where ``uv tool install`` puts executables.

    Mirrors uv's own resolution order (``UV_TOOL_BIN_DIR`` override →
    XDG_BIN_HOME → platform default) closely enough to explain a PATH miss
    or a permissions problem — never used to decide whether the app IS
    installed. *env* is threaded explicitly (never reads ``os.environ``
    itself) so callers/tests control it precisely instead of depending on
    the machine running them.
    """
    override = env.get("UV_TOOL_BIN_DIR")
    if override:
        return (Path(override),)
    if sys.platform == "win32":
        appdata = env.get("APPDATA")
        if appdata:
            return (Path(appdata) / "uv" / "tools" / "bin",)
        return (home / "AppData" / "Roaming" / "uv" / "tools" / "bin",)
    xdg_bin = env.get("XDG_BIN_HOME")
    return (Path(xdg_bin),) if xdg_bin else (home / ".local" / "bin",)


def check_path(
    executable: str = EXECUTABLE_NAME,
    *,
    path_env: str | None = None,
    shell_env: str | None = None,
    home: Path | None = None,
    search_dirs: Sequence[Path] | None = None,
) -> CheckResult:
    """The console script is reachable on PATH — and if not, exactly how to
    fix it: where the executable actually landed (if findable), which
    directory needs to be on PATH, and the precise shell-specific line to
    add (shell detected from ``$SHELL``).

    Every environment-derived input (``path_env``/``shell_env``/``home``/
    ``search_dirs``) is overridable so tests never depend on the PATH,
    shell, or home directory of the machine running the suite. Only the
    OK-path message (``"PATH clean"``) is a stable contract other
    tests/docs pin to; the failure message is free to be as helpful as
    possible.
    """
    resolved_path = os.environ.get("PATH", "") if path_env is None else path_env
    if shutil.which(executable, path=resolved_path) is not None:
        return CheckResult(name="path", ok=True, message="PATH clean")

    resolved_home = home if home is not None else Path.home()
    dirs = (
        tuple(search_dirs)
        if search_dirs is not None
        else _default_bin_dirs(resolved_home, os.environ)
    )
    shell = _detect_shell(os.environ.get("SHELL", "") if shell_env is None else shell_env)
    landed = _find_on_disk(executable, dirs)

    if landed is not None:
        directory = landed.parent
        export_line = _path_export_line(shell, directory)
        rc_file = _SHELL_RC_FILES.get(shell, "your shell's startup file (e.g. ~/.profile)")
        message = (
            f"{executable} not on PATH · found at {landed} · add {directory} to PATH: run "
            f"`uv tool update-shell` and restart your terminal, or add `{export_line}` to {rc_file}"
        )
    elif executable == EXECUTABLE_NAME:
        looked = ", ".join(str(d) for d in dirs)
        message = (
            f"{executable} not on PATH and not found in the usual install dir(s) ({looked}) · "
            f"install it: `uv tool install {APP_INSTALL_URI}`, then `uv tool update-shell` and "
            "restart your terminal"
        )
    else:
        looked = ", ".join(str(d) for d in dirs)
        message = f"{executable} not on PATH and not found in the usual install dir(s) ({looked})"
    return CheckResult(name="path", ok=False, message=message)


# --- platform: OS/arch support -------------------------------------------


def detect_platform() -> tuple[str, str]:
    """The real ``(system, machine)`` — impure edge; never raises."""
    return platform.system(), platform.machine()


def check_platform(system: str, machine: str) -> CheckResult:
    """OS/arch, flagging combinations this app genuinely doesn't support.

    Native Windows is flagged — the README documents macOS, Linux, and WSL
    only, and WSL reports its kernel honestly as ``Linux`` (it IS one), so
    this never conflates the two or penalizes a WSL user. 32-bit CPUs are
    flagged because uv itself ships no 32-bit builds. Pure: same inputs,
    same result on any machine — :func:`detect_platform` resolves the real
    values for the production caller.
    """
    label = f"{system} ({machine})" if machine else system
    if system == "Windows":
        return CheckResult(
            name="platform",
            ok=False,
            message=(
                f"{label} is not a supported platform · amplifier-tui is tested on macOS, "
                "Linux, and WSL · install WSL2 (`wsl --install` in an admin PowerShell), then "
                "reinstall from inside the WSL shell"
            ),
        )
    if machine.lower() in _UNSUPPORTED_MACHINES:
        return CheckResult(
            name="platform",
            ok=False,
            message=(
                f"{label} — 32-bit CPUs are not supported (uv publishes no 32-bit builds) · "
                "use a 64-bit OS/CPU (x86_64/amd64 or arm64/aarch64)"
            ),
        )
    return CheckResult(name="platform", ok=True, message=f"platform {label} supported")


# --- python / uv: versions found + pyproject's declared minimum ----------


class PythonUvFacts(BaseModel):
    """Python/uv facts the ``python_uv`` check reasons about.

    Populated by :func:`detect_python_uv` (the impure edge) for real use;
    tests construct one directly so the check's logic never depends on the
    interpreter, uv install, or PATH of the machine running the suite.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    python_version: str
    min_python: str | None = None
    uv_version: str | None = None


def _min_from_requires_python(specifier: str) -> str | None:
    """Extract the floor from a ``>=3.12``-shaped specifier.

    This project declares a single floor with no upper bound, so the first
    ``>=`` clause is enough; an unparseable specifier degrades to ``None``
    (the check just stops comparing, never crashes).
    """
    match = re.search(r">=\s*([0-9]+(?:\.[0-9]+)*)", specifier)
    return match.group(1) if match else None


def _declared_min_python(package: str = PACKAGE_NAME) -> str | None:
    """This project's declared ``requires-python`` floor.

    Read from the INSTALLED distribution's own metadata — hatchling copies
    ``pyproject.toml``'s ``requires-python`` into the wheel's
    ``Requires-Python`` field at build time — never hardcoded here. Works
    for a real end-user install with no ``pyproject.toml`` file on disk,
    not just a dev checkout. ``None`` when the package/field can't be found.
    """
    try:
        raw = metadata.metadata(package).get("Requires-Python")
    except metadata.PackageNotFoundError:
        return None
    return _min_from_requires_python(raw) if raw else None


def _version_at_least(actual: str, minimum: str) -> bool:
    """Is *actual* >= *minimum*? PEP 440 compare when ``packaging`` is
    importable (a transitive dep here), else a tolerant numeric-tuple
    fallback — never raises, never blocks a doctor run on a parse quirk.
    """
    try:
        from packaging.version import Version

        return Version(actual) >= Version(minimum)
    except Exception:  # noqa: BLE001 — fall back to a plain numeric compare

        def _parts(value: str) -> tuple[int, ...]:
            return tuple(int(p) for p in re.findall(r"\d+", value))

        return _parts(actual) >= _parts(minimum)


def _uv_version(timeout: float = 2.0) -> str | None:
    """``uv --version``'s version token, or ``None`` if uv is missing/hung.

    Short timeout: this can run synchronously on the Textual UI thread (the
    in-session ``/doctor``), so a hung subprocess must not stall the app.
    """
    exe = shutil.which("uv")
    if exe is None:
        return None
    try:
        result = subprocess.run(
            [exe, "--version"], capture_output=True, text=True, timeout=timeout, check=False
        )
    except Exception:  # noqa: BLE001 — never crash the doctor over a subprocess hiccup
        return None
    if result.returncode != 0:
        return None
    match = re.search(r"(\d+\.\d+(?:\.\d+)?)", result.stdout)
    return match.group(1) if match else None


def detect_python_uv(package: str = PACKAGE_NAME) -> PythonUvFacts:
    """The real, current-machine Python/uv facts (impure edge; never raises)."""
    return PythonUvFacts(
        python_version=platform.python_version(),
        min_python=_declared_min_python(package),
        uv_version=_uv_version(),
    )


def check_python_uv(facts: PythonUvFacts) -> CheckResult:
    """Python + uv health: versions found, whether Python meets pyproject's
    declared floor (never hardcoded — see :func:`_declared_min_python`), and
    the exact command to fix whichever is short. Pure: same *facts*, same
    result on any machine — :func:`detect_python_uv` resolves the real
    values for the production caller.
    """
    parts: list[str] = []
    healthy: list[str] = []

    if facts.min_python is not None and not _version_at_least(
        facts.python_version, facts.min_python
    ):
        parts.append(
            f"Python {facts.python_version} is older than the {facts.min_python}+ this app "
            f"requires · upgrade: `uv python install {facts.min_python}` (uv manages its own "
            f"Pythons), then `uv tool install --reinstall {APP_INSTALL_URI}` to rebuild against it"
        )
    else:
        floor = f" (>={facts.min_python})" if facts.min_python else ""
        healthy.append(f"Python {facts.python_version}{floor}")

    if facts.uv_version is None:
        parts.append(
            "uv not found · install: `curl -LsSf https://astral.sh/uv/install.sh | sh` (see "
            "https://astral.sh/uv for other platforms)"
        )
    else:
        healthy.append(f"uv {facts.uv_version}")

    if parts:
        return CheckResult(name="python_uv", ok=False, message=" · ".join(parts))
    return CheckResult(name="python_uv", ok=True, message=" · ".join(healthy))


# --- permissions: the common "install dir not writable" failure ----------


class PermissionFacts(BaseModel):
    """Writability facts the ``permissions`` check reasons about.

    Populated by :func:`detect_permissions` for real use; tests construct
    one directly (an explicit bool, never a chmod'd path — unreliable when
    a suite runs as root) so the check's logic never depends on the actual
    filesystem permissions of the machine running the suite.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    bin_dir: Path
    bin_dir_writable: bool
    amplifier_home: Path
    amplifier_home_writable: bool


def _writable(path: Path) -> bool:
    """Can we write to *path*, or (if it doesn't exist yet) create it?

    Walks up to the nearest existing ancestor so a not-yet-created app home
    or bin dir is judged by whether it COULD be created, not by permission
    bits that don't exist yet. Any stat failure degrades to "not writable"
    (the conservative answer) rather than raising.
    """
    try:
        current = path
        seen: set[Path] = set()
        while not current.exists():
            if current in seen:  # pathological loop guard; never spin forever
                return False
            seen.add(current)
            parent = current.parent
            if parent == current:  # reached the filesystem root, found nothing
                return False
            current = parent
        return os.access(current, os.W_OK)
    except OSError:
        return False


def detect_permissions() -> PermissionFacts:
    """The real bin-dir / app-home writability facts (impure edge)."""
    home = Path.home()
    bin_dir = _default_bin_dirs(home, os.environ)[0]
    amplifier_home = Path(os.environ.get("AMPLIFIER_HOME") or (home / ".amplifier"))
    return PermissionFacts(
        bin_dir=bin_dir,
        bin_dir_writable=_writable(bin_dir),
        amplifier_home=amplifier_home,
        amplifier_home_writable=_writable(amplifier_home),
    )


def check_permissions(facts: PermissionFacts) -> CheckResult:
    """The common real-world failure: an install/state dir you can't write to.

    Checks the uv-tool bin dir (where the executable/symlink installs) and
    the app's own state dir (settings, keys, cache — ``AMPLIFIER_HOME`` or
    ``~/.amplifier``). Pure: same *facts*, same result —
    :func:`detect_permissions` resolves the real values for the production
    caller.
    """
    problems: list[str] = []
    if not facts.bin_dir_writable:
        problems.append(
            f"install dir {facts.bin_dir} is not writable · fix: "
            f"`sudo chown -R $(whoami) {facts.bin_dir}` or point `UV_TOOL_BIN_DIR` at a "
            "writable directory"
        )
    if not facts.amplifier_home_writable:
        problems.append(
            f"app home {facts.amplifier_home} is not writable · fix: "
            f"`sudo chown -R $(whoami) {facts.amplifier_home}` or set `AMPLIFIER_HOME` to a "
            "writable directory"
        )
    if problems:
        return CheckResult(name="permissions", ok=False, message=" · ".join(problems))
    return CheckResult(name="permissions", ok=True, message="install/app-home dirs writable")


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
            check_platform(*detect_platform()),
            check_python_uv(detect_python_uv(package)),
            check_permissions(detect_permissions()),
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
    "APP_INSTALL_URI",
    "AnchorsPinStatus",
    "CheckResult",
    "DoctorReport",
    "EXECUTABLE_NAME",
    "McpServerStats",
    "MountHealth",
    "PACKAGE_NAME",
    "PermissionFacts",
    "PythonUvFacts",
    "REPEATED_APPROVAL_THRESHOLD",
    "UNUSED_MCP_THRESHOLD_DAYS",
    "build_doctor_block",
    "check_anchors_pin",
    "check_install",
    "check_mounts",
    "check_path",
    "check_permissions",
    "check_platform",
    "check_python_uv",
    "check_repeated_approvals",
    "check_settings",
    "check_unused_mcp",
    "detect_permissions",
    "detect_platform",
    "detect_python_uv",
    "render_text",
    "run_checks",
    "run_standalone",
]
