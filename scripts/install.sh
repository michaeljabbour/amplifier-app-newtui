#!/bin/sh
# Install amplifier-app-tui from source on macOS, Linux, or WSL.
#
# The bootstrap script may be fetched from the repository's source channel,
# but the installed environment never floats: a branch or tag is resolved once
# to a full 40-character commit, that commit's checked-in uv.lock is exported,
# and uv installs the exact application source under those locked constraints.

set -eu

REPO_URL_DEFAULT="https://github.com/michaeljabbour/amplifier-app-tui.git"
UV_INSTALLER_URL="https://astral.sh/uv/install.sh"

repo_url=${AMPLIFIER_TUI_REPO_URL:-$REPO_URL_DEFAULT}
requested_ref=${AMPLIFIER_TUI_REF:-main}
launch=${AMPLIFIER_TUI_LAUNCH:-0}
update_shell=${AMPLIFIER_TUI_UPDATE_SHELL:-1}
temp_dir=""

say() {
    printf '%s\n' "$*"
}

warn() {
    printf 'warning: %s\n' "$*" >&2
}

fail() {
    printf 'install failed: %s\n' "$*" >&2
    exit 1
}

usage() {
    cat <<'EOF'
Usage: install.sh [--ref REF] [--launch] [--no-update-shell]

Install amplifier-app-tui from its Git repository. REF defaults to main and
is resolved to a full commit SHA before installation. A 40-character commit
SHA may be supplied directly for a reviewed, immutable application-source install.

Options:
  --ref REF          branch, tag, or full 40-character commit
  --launch           open amplifier-tui after installation (uses /dev/tty)
  --no-update-shell  do not ask uv to add its tool bin directory to PATH
  -h, --help         show this help
EOF
}

cleanup() {
    if [ -n "$temp_dir" ] && [ -d "$temp_dir" ]; then
        rm -rf "$temp_dir"
    fi
}

ensure_temp_dir() {
    if [ -z "$temp_dir" ]; then
        temp_dir=$(mktemp -d "${TMPDIR:-/tmp}/amplifier-tui-install.XXXXXX") ||
            fail "could not create a temporary directory"
    fi
}

trap cleanup 0

while [ "$#" -gt 0 ]; do
    case "$1" in
        --ref)
            [ "$#" -ge 2 ] || fail "--ref requires a value"
            requested_ref=$2
            shift 2
            ;;
        --launch)
            launch=1
            shift
            ;;
        --no-update-shell)
            update_shell=0
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            fail "unknown option: $1"
            ;;
    esac
done

case "$launch" in
    0|1) ;;
    *) fail "AMPLIFIER_TUI_LAUNCH must be 0 or 1" ;;
esac

case "$update_shell" in
    0|1) ;;
    *) fail "AMPLIFIER_TUI_UPDATE_SHELL must be 0 or 1" ;;
esac

case "$(uname -s 2>/dev/null || true)" in
    Darwin|Linux) ;;
    *) fail "this source installer supports macOS, Linux, and WSL" ;;
esac

case "$repo_url" in
    https://*|file://*) ;;
    *) fail "repository URL must use https:// or file://" ;;
esac

# A URL supplied through the environment can otherwise expose credentials in
# git/uv diagnostics.  The public installer never needs URL userinfo; callers
# that need authenticated Git should use a credential helper instead.
case "$repo_url" in
    https://*@*) fail "repository URL must not contain embedded credentials; use a Git credential helper" ;;
esac

case "$requested_ref" in
    ""|-*|*[!A-Za-z0-9._/-]*)
        fail "invalid ref '$requested_ref' (use a branch, tag, or full commit SHA)"
        ;;
    refs/*)
        fail "use a branch or tag name without the refs/ prefix"
        ;;
esac

command -v git >/dev/null 2>&1 || fail "git is required; install git and run this command again"

is_full_sha() {
    [ "${#1}" -eq 40 ] || return 1
    case "$1" in
        *[!0-9A-Fa-f]*) return 1 ;;
        *) return 0 ;;
    esac
}

resolve_ref() {
    ref=$1
    if is_full_sha "$ref"; then
        printf '%s\n' "$ref" | tr 'A-F' 'a-f'
        return 0
    fi

    refs=$(git ls-remote --exit-code "$repo_url" \
        "refs/heads/$ref" "refs/tags/$ref" "refs/tags/$ref^{}" 2>/dev/null) ||
        fail "could not resolve '$ref' from $repo_url"

    head_ref="refs/heads/$ref"
    tag_ref="refs/tags/$ref"
    peeled_ref="refs/tags/$ref^{}"

    # A branch wins when a repository happens to use the same name for a
    # branch and a tag. For annotated tags, install the peeled commit rather
    # than the tag object.
    sha=$(printf '%s\n' "$refs" | awk -v target="$head_ref" '$2 == target { print $1; exit }')
    if [ -z "$sha" ]; then
        sha=$(printf '%s\n' "$refs" | awk -v target="$peeled_ref" '$2 == target { print $1; exit }')
    fi
    if [ -z "$sha" ]; then
        sha=$(printf '%s\n' "$refs" | awk -v target="$tag_ref" '$2 == target { print $1; exit }')
    fi

    is_full_sha "$sha" || fail "remote returned an invalid commit for '$ref'"
    printf '%s\n' "$sha" | tr 'A-F' 'a-f'
}

find_uv() {
    if [ -n "${AMPLIFIER_TUI_UV_BIN:-}" ]; then
        [ -x "$AMPLIFIER_TUI_UV_BIN" ] || return 1
        printf '%s\n' "$AMPLIFIER_TUI_UV_BIN"
        return 0
    fi
    if command -v uv >/dev/null 2>&1; then
        command -v uv
        return 0
    fi
    if [ -n "${HOME:-}" ] && [ -x "$HOME/.local/bin/uv" ]; then
        printf '%s\n' "$HOME/.local/bin/uv"
        return 0
    fi
    if [ -n "${HOME:-}" ] && [ -x "$HOME/.cargo/bin/uv" ]; then
        printf '%s\n' "$HOME/.cargo/bin/uv"
        return 0
    fi
    return 1
}

resolved_sha=$(resolve_ref "$requested_ref")
say "Installing Amplifier TUI source commit $resolved_sha"

if uv_bin=$(find_uv); then
    :
else
    command -v curl >/dev/null 2>&1 ||
        fail "curl is required to install uv; install curl and run this command again"
    ensure_temp_dir
    say "Installing uv from the official Astral installer"
    curl --proto '=https' --tlsv1.2 -fsSL "$UV_INSTALLER_URL" \
        -o "$temp_dir/uv-installer.sh" || fail "could not download the uv installer"
    sh "$temp_dir/uv-installer.sh" || fail "uv installation failed"
    uv_bin=$(find_uv) || fail "uv installed, but its executable could not be found"
fi

uv_version=$("$uv_bin" --version 2>/dev/null) ||
    fail "found uv at $uv_bin, but it could not report its version"
case "$uv_version" in
    "uv "[0-9]*) ;;
    *) fail "found uv at $uv_bin, but its version output was not recognized" ;;
esac
say "Using $uv_version"

ensure_temp_dir
source_dir="$temp_dir/source"
constraints_file="$temp_dir/runtime-constraints.txt"

say "Fetching the locked dependency manifest from source commit $resolved_sha"
git init -q "$source_dir" || fail "could not prepare the source checkout"
git -C "$source_dir" remote add origin "$repo_url" ||
    fail "could not configure the source checkout"
if ! git -C "$source_dir" fetch --quiet --depth=1 origin "$resolved_sha"; then
    fail "could not fetch source commit $resolved_sha; check the ref and network/proxy access"
fi
git -C "$source_dir" checkout --quiet --detach FETCH_HEAD ||
    fail "could not check out source commit $resolved_sha"
checked_out_sha=$(git -C "$source_dir" rev-parse HEAD 2>/dev/null || true)
[ "$checked_out_sha" = "$resolved_sha" ] ||
    fail "source checkout did not match requested commit $resolved_sha"
[ -f "$source_dir/uv.lock" ] ||
    fail "source commit $resolved_sha does not contain uv.lock; choose a release with a dependency lock"

say "Exporting the commit's locked runtime dependency set"
if ! "$uv_bin" export --frozen --no-dev --no-editable --no-emit-project \
    --no-config --project "$source_dir" --output-file "$constraints_file" >/dev/null; then
    fail "the checked-in uv.lock could not be exported; update uv or choose a source commit with a valid lock"
fi
[ -s "$constraints_file" ] || fail "uv exported an empty dependency lock"

package_url="git+$repo_url@$resolved_sha"
install_log="$temp_dir/uv-tool-install.log"
say "Creating the isolated Amplifier TUI tool environment from the locked dependency set"
if "$uv_bin" tool install --reinstall --no-config \
    --constraints "$constraints_file" "$package_url" >"$install_log" 2>&1; then
    :
else
    [ ! -s "$install_log" ] || cat "$install_log" >&2
    install_error=$(tr '[:upper:]' '[:lower:]' <"$install_log")
    case "$install_error" in
        *"permission denied"*|*"operation not permitted"*|*"not writable"*)
            fail "the uv tool directory is not writable; fix its ownership/permissions, then rerun this installer"
            ;;
        *"requires-python"*|*"requires python"*|*"no interpreter found"*|*"python version"*)
            fail "uv could not prepare a compatible Python 3.12+ tool environment; run 'uv python install 3.12', then rerun this installer"
            ;;
        *"failed to clone"*|*"could not resolve host"*|*"connection"*|*"network"*|*"timed out"*)
            fail "the Git source fetch failed; check network/proxy access to GitHub, then rerun this installer"
            ;;
        *)
            fail "uv could not install Amplifier TUI; the full uv error is printed above"
            ;;
    esac
fi

tool_bin_dir=$("$uv_bin" tool dir --bin 2>/dev/null) ||
    fail "uv did not report its tool executable directory"
[ -n "$tool_bin_dir" ] || fail "uv reported an empty tool executable directory"
app_bin="$tool_bin_dir/amplifier-tui"
[ -x "$app_bin" ] || fail "installation finished without an amplifier-tui executable"

installed_version=$("$app_bin" version 2>&1) ||
    fail "the installed amplifier-tui could not report its version"
"$app_bin" --help >/dev/null 2>&1 ||
    fail "the installed amplifier-tui command could not load its help surface"
say "Verified $app_bin · $installed_version"
say "Dependencies locked by uv.lock from $resolved_sha"

case ":${PATH:-}:" in
    *":$tool_bin_dir:"*) ;;
    *)
        if [ "$update_shell" -eq 1 ]; then
            if "$uv_bin" tool update-shell >/dev/null 2>&1; then
                say "Added $tool_bin_dir to your shell PATH (restart the shell to pick it up)"
            else
                warn "could not update PATH automatically; add $tool_bin_dir to PATH"
            fi
        else
            warn "$tool_bin_dir is not on PATH"
        fi
        ;;
esac

if [ "$launch" -eq 1 ]; then
    launch_input=${AMPLIFIER_TUI_TTY_PATH:-/dev/tty}
    [ -r "$launch_input" ] ||
        fail "--launch needs an interactive terminal; run $app_bin after installation"
    say "Opening Amplifier TUI; first launch will guide provider setup"
    cleanup
    temp_dir=""
    trap - 0
    exec "$app_bin" <"$launch_input"
fi

say "Installed. Run: $app_bin"
say "First launch will guide provider setup; use --demo for an offline tour."
