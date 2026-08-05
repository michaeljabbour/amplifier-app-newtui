"""Contract tests for the macOS/Linux/WSL source installer.

The tests replace git and uv with small fakes and point uv's reported bin directory
at ``tmp_path``. They never install a package, edit a shell profile, or touch the
caller's home directory.
"""

from __future__ import annotations

import os
from pathlib import Path
import subprocess

from amplifier_app_tui.install_contract import (
    SOURCE_INSTALL_COMMAND,
    SOURCE_INSTALL_LAUNCH_COMMAND,
)


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install.sh"
SHA = "0123456789abcdef0123456789abcdef01234567"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/sh\nset -eu\n{body}", encoding="utf-8")
    path.chmod(0o755)


def _fake_environment(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    fake_bin = tmp_path / "fake-bin"
    tool_bin = tmp_path / "tool-bin"
    fake_bin.mkdir()
    tool_bin.mkdir()
    log = tmp_path / "calls.log"

    _write_executable(
        fake_bin / "git",
        f"""
printf 'git %s\\n' "$*" >> "$INSTALL_TEST_LOG"
case "${{1:-}}" in
    ls-remote)
        printf '{SHA}\\trefs/heads/main\\n'
        ;;
    init)
        source_dir=${{3:-}}
        if [ -z "$source_dir" ]; then
            source_dir=${{2:-}}
        fi
        mkdir -p "$source_dir"
        printf 'version = 1\\n' > "$source_dir/uv.lock"
        printf '[project]\\nname = "amplifier-app-tui"\\nversion = "0"\\n' > "$source_dir/pyproject.toml"
        ;;
    -C)
        if [ "${{3:-}} ${{4:-}}" = "rev-parse HEAD" ]; then
            printf '{SHA}\\n'
        fi
        ;;
esac
""",
    )
    _write_executable(
        fake_bin / "uv",
        """
printf 'uv %s\\n' "$*" >> "$INSTALL_TEST_LOG"
case "${1:-} ${2:-}" in
    "export --frozen")
        output=''
        while [ "$#" -gt 0 ]; do
            case "$1" in
                --output-file)
                    output=$2
                    shift 2
                    ;;
                *) shift ;;
            esac
        done
        [ -n "$output" ]
        printf 'textual==8.2.8\\n' > "$output"
        ;;
    "tool install")
        cat > "$INSTALL_TEST_TOOL_BIN/amplifier-tui" <<'APP'
#!/bin/sh
printf 'app %s\\n' "$*" >> "$INSTALL_TEST_LOG"
if [ "${1:-}" = "version" ]; then
    printf 'Amplifier TUI 0.1.0\\n'
fi
APP
        chmod +x "$INSTALL_TEST_TOOL_BIN/amplifier-tui"
        ;;
    "tool dir")
        printf '%s\\n' "$INSTALL_TEST_TOOL_BIN"
        ;;
    "tool update-shell")
        ;;
    *)
        if [ "${1:-}" = "--version" ]; then
            printf 'uv 0.test\\n'
        fi
        ;;
esac
""",
    )

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "INSTALL_TEST_LOG": str(log),
            "INSTALL_TEST_TOOL_BIN": str(tool_bin),
            "AMPLIFIER_TUI_REPO_URL": "https://example.test/amplifier-app-tui.git",
        }
    )
    return env, log, tool_bin


def _run(tmp_path: Path, *args: str, env_updates: dict[str, str] | None = None):
    env, log, tool_bin = _fake_environment(tmp_path)
    if env_updates:
        env.update(env_updates)
    result = subprocess.run(
        ["sh", str(INSTALLER), *args],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    return result, log, tool_bin


def test_source_install_resolves_main_to_immutable_sha(tmp_path: Path) -> None:
    result, log, tool_bin = _run(tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert f"source commit {SHA}" in result.stdout
    assert f"Verified {tool_bin}/amplifier-tui · Amplifier TUI 0.1.0" in result.stdout
    calls = log.read_text(encoding="utf-8")
    assert "git ls-remote --exit-code https://example.test/amplifier-app-tui.git" in calls
    assert ("uv tool install --reinstall --no-config --constraints ") in calls
    assert f"git+https://example.test/amplifier-app-tui.git@{SHA}" in calls
    assert "git init -q " in calls
    assert "git -C " in calls and f"fetch --quiet --depth=1 origin {SHA}" in calls
    assert (
        "uv export --frozen --no-dev --no-editable --no-emit-project --no-config --project "
    ) in calls
    assert f"Dependencies locked by uv.lock from {SHA}" in result.stdout
    assert "app version" in calls
    assert "app --help" in calls
    assert "uv tool update-shell" in calls


def test_full_sha_skips_remote_resolution_and_path_edit(tmp_path: Path) -> None:
    result, log, _tool_bin = _run(tmp_path, "--ref", SHA.upper(), "--no-update-shell")

    assert result.returncode == 0, result.stderr
    assert f"source commit {SHA}" in result.stdout
    calls = log.read_text(encoding="utf-8")
    assert "git ls-remote" not in calls
    assert "uv tool update-shell" not in calls
    assert f"@{SHA}" in calls


def test_invalid_ref_fails_before_install(tmp_path: Path) -> None:
    result, log, _tool_bin = _run(tmp_path, "--ref", "main;touch-bad")

    assert result.returncode == 1
    assert "invalid ref" in result.stderr
    assert not log.exists() or "uv tool install" not in log.read_text(encoding="utf-8")


def test_launch_hands_first_run_to_verified_executable(tmp_path: Path) -> None:
    launch_input = tmp_path / "tty-input"
    launch_input.write_text("", encoding="utf-8")
    result, log, _tool_bin = _run(
        tmp_path,
        "--launch",
        env_updates={"AMPLIFIER_TUI_TTY_PATH": str(launch_input)},
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert "Opening Amplifier TUI; first launch will guide provider setup" in result.stdout
    calls = log.read_text(encoding="utf-8")
    assert "app version" in calls
    assert calls.splitlines()[-1] == "app "


def test_missing_uv_uses_downloaded_astral_bootstrap_without_real_home(tmp_path: Path) -> None:
    env, log, tool_bin = _fake_environment(tmp_path)
    fake_bin = Path(env["PATH"].split(os.pathsep)[0])
    uv_template = tmp_path / "uv-template"
    (fake_bin / "uv").replace(uv_template)
    bootstrap_bin = tmp_path / "bootstrap-bin"
    bootstrap_bin.mkdir()
    installed_uv = bootstrap_bin / "uv"

    _write_executable(
        fake_bin / "curl",
        """
printf 'curl %s\\n' "$*" >> "$INSTALL_TEST_LOG"
output=''
while [ "$#" -gt 0 ]; do
    case "$1" in
        -o)
            output=$2
            shift 2
            ;;
        *) shift ;;
    esac
done
[ -n "$output" ]
cat > "$output" <<'BOOTSTRAP'
#!/bin/sh
set -eu
cp "$INSTALL_TEST_UV_TEMPLATE" "$AMPLIFIER_TUI_UV_BIN"
chmod +x "$AMPLIFIER_TUI_UV_BIN"
BOOTSTRAP
""",
    )
    env.update(
        {
            "AMPLIFIER_TUI_UV_BIN": str(installed_uv),
            "INSTALL_TEST_UV_TEMPLATE": str(uv_template),
        }
    )

    result = subprocess.run(
        ["sh", str(INSTALLER)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert "Installing uv from the official Astral installer" in result.stdout
    assert f"Verified {tool_bin}/amplifier-tui · Amplifier TUI 0.1.0" in result.stdout
    calls = log.read_text(encoding="utf-8")
    assert "curl --proto =https --tlsv1.2 -fsSL https://astral.sh/uv/install.sh" in calls
    assert "uv tool install --reinstall --no-config --constraints" in calls


def test_help_is_available_without_git_or_uv(tmp_path: Path) -> None:
    result = subprocess.run(
        ["sh", str(INSTALLER), "--help"],
        env={"PATH": "/usr/bin:/bin"},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stderr == ""
    assert "Usage: install.sh" in result.stdout
    assert "--ref REF" in result.stdout


def test_documented_install_and_update_commands_use_the_shared_contract() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    guide = (ROOT / "docs" / "INSTALL.md").read_text(encoding="utf-8")
    assert SOURCE_INSTALL_LAUNCH_COMMAND in readme
    assert SOURCE_INSTALL_LAUNCH_COMMAND in guide
    assert SOURCE_INSTALL_COMMAND in readme


def test_documented_pipefail_wrapper_propagates_download_failure(tmp_path: Path) -> None:
    """The one-line bootstrap must not report success for an empty download."""
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    _write_executable(fake_bin / "curl", "exit 22\n")

    result = subprocess.run(
        [
            "bash",
            "-o",
            "pipefail",
            "-c",
            "curl -fsSL https://example.invalid/install.sh | bash -s -- --launch",
        ],
        env={"PATH": f"{fake_bin}:/usr/bin:/bin"},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 22


def test_broken_existing_uv_fails_with_specific_probe_error(tmp_path: Path) -> None:
    env, log, _tool_bin = _fake_environment(tmp_path)
    uv_path = Path(env["PATH"].split(os.pathsep)[0]) / "uv"
    _write_executable(uv_path, "exit 7\n")

    result = subprocess.run(
        ["sh", str(INSTALLER)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "could not report its version" in result.stderr
    assert not log.exists() or "tool install" not in log.read_text(encoding="utf-8")


def test_repository_url_with_userinfo_is_rejected_without_leaking_secret(tmp_path: Path) -> None:
    secret_url = "https://installer-user:do-not-print@example.test/amplifier-app-tui.git"
    result, log, _tool_bin = _run(
        tmp_path,
        env_updates={"AMPLIFIER_TUI_REPO_URL": secret_url},
    )

    assert result.returncode == 1
    assert "must not contain embedded credentials" in result.stderr
    assert "do-not-print" not in result.stdout
    assert "do-not-print" not in result.stderr
    assert not log.exists()


def _replace_uv_with_install_failure(env: dict[str, str], message: str) -> None:
    uv_path = Path(env["PATH"].split(os.pathsep)[0]) / "uv"
    _write_executable(
        uv_path,
        f"""
if [ "${{1:-}}" = "--version" ]; then
    printf 'uv 0.test\\n'
    exit 0
fi
if [ "${{1:-}} ${{2:-}}" = "export --frozen" ]; then
    output=''
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --output-file)
                output=$2
                shift 2
                ;;
            *) shift ;;
        esac
    done
    [ -n "$output" ]
    printf 'textual==8.2.8\\n' > "$output"
    exit 0
fi
if [ "${{1:-}} ${{2:-}}" = "tool install" ]; then
    printf '%s\\n' {message!r} >&2
    exit 2
fi
exit 0
""",
    )


def test_permission_failure_explains_the_pre_doctor_repair(tmp_path: Path) -> None:
    env, _log, _tool_bin = _fake_environment(tmp_path)
    _replace_uv_with_install_failure(env, "Permission denied: tool directory is not writable")

    result = subprocess.run(
        ["sh", str(INSTALLER)], env=env, text=True, capture_output=True, check=False
    )

    assert result.returncode == 1
    assert "uv tool directory is not writable" in result.stderr
    assert "fix its ownership/permissions" in result.stderr


def test_python_failure_explains_the_pre_doctor_repair(tmp_path: Path) -> None:
    env, _log, _tool_bin = _fake_environment(tmp_path)
    _replace_uv_with_install_failure(env, "No interpreter found; package requires Python >=3.12")

    result = subprocess.run(
        ["sh", str(INSTALLER)], env=env, text=True, capture_output=True, check=False
    )

    assert result.returncode == 1
    assert "compatible Python 3.12+" in result.stderr
    assert "uv python install 3.12" in result.stderr


def test_launch_without_a_terminal_fails_after_verified_install(tmp_path: Path) -> None:
    missing_tty = tmp_path / "does-not-exist"
    result, log, _tool_bin = _run(
        tmp_path,
        "--launch",
        env_updates={"AMPLIFIER_TUI_TTY_PATH": str(missing_tty)},
    )

    assert result.returncode == 1
    assert "--launch needs an interactive terminal" in result.stderr
    assert "app version" in log.read_text(encoding="utf-8")


def test_unsupported_os_is_refused_before_install(tmp_path: Path) -> None:
    env, log, _tool_bin = _fake_environment(tmp_path)
    fake_bin = Path(env["PATH"].split(os.pathsep)[0])
    _write_executable(fake_bin / "uname", "printf 'FreeBSD\\n'\n")

    result = subprocess.run(
        ["sh", str(INSTALLER)], env=env, text=True, capture_output=True, check=False
    )

    assert result.returncode == 1
    assert "supports macOS, Linux, and WSL" in result.stderr
    assert not log.exists() or "tool install" not in log.read_text(encoding="utf-8")
