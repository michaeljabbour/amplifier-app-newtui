"""One source-install contract shared by setup, repair, and update guidance.

The shell bootstrap itself lives in ``scripts/install.sh``.  Keeping its public URL and
the exact fail-closed wrapper here prevents the TUI from suggesting several subtly
different floating ``uv tool install`` commands.  This module is dependency-free so both
``commands/`` and ``kernel/`` can import it without crossing the ADR-0007 layer boundary.
"""

from __future__ import annotations

APP_REPO_URL = "https://github.com/michaeljabbour/amplifier-app-tui"
APP_INSTALL_URI = f"git+{APP_REPO_URL}"
SOURCE_INSTALL_URL = (
    "https://raw.githubusercontent.com/michaeljabbour/amplifier-app-tui/main/scripts/install.sh"
)

_CURL_INSTALLER = f"curl --proto '=https' --tlsv1.2 -fsSL {SOURCE_INSTALL_URL}"


def source_install_pipeline(*, launch: bool = False) -> str:
    """The inner Bash pipeline, optionally handing setup to the verified executable."""
    launch_args = " -s -- --launch" if launch else ""
    return f"{_CURL_INSTALLER} | bash{launch_args}"


def source_install_command(*, launch: bool = False) -> str:
    """Copy/paste command whose status preserves a failed bootstrap download."""
    return f'bash -o pipefail -c "{source_install_pipeline(launch=launch)}"'


def source_install_argv(*, launch: bool = False) -> list[str]:
    """Argument vector for invoking the same contract without another shell parse."""
    return ["bash", "-o", "pipefail", "-c", source_install_pipeline(launch=launch)]


SOURCE_INSTALL_COMMAND = source_install_command()
SOURCE_INSTALL_LAUNCH_COMMAND = source_install_command(launch=True)


__all__ = [
    "APP_INSTALL_URI",
    "APP_REPO_URL",
    "SOURCE_INSTALL_COMMAND",
    "SOURCE_INSTALL_LAUNCH_COMMAND",
    "SOURCE_INSTALL_URL",
    "source_install_argv",
    "source_install_command",
    "source_install_pipeline",
]
