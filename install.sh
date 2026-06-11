#!/usr/bin/env bash
# install.sh — Install cue: daemon, shell hooks, default config
#
# One-liner (from GitHub):
#   curl -fsSL https://raw.githubusercontent.com/atomikkus/cue/main/install.sh | bash
#
# Local:
#   ./install.sh
#   ./install.sh --method pipx|uv|venv|auto
#   ./install.sh --shell bash|zsh|auto
#   ./install.sh --no-daemon
#   ./install.sh --uninstall

set -euo pipefail

CUE_VERSION="0.1.0"
CUE_REPO_URL="${CUE_REPO_URL:-https://github.com/atomikkus/cue.git}"
CUE_ARCHIVE_URL="${CUE_ARCHIVE_URL:-https://github.com/atomikkus/cue/archive/refs/heads/main.zip}"
CUE_CONFIG_DIR=""
CUE_VENV_DIR=""
CUE_PATH_LINE=""
CUE_ZSH_HOOK_LINE=""
CUE_BASH_HOOK_LINE=""
CUE_DAEMON_LAUNCH_LINE='cue-daemon start &>/dev/null'

PYTHON="${PYTHON:-python3}"
START_DAEMON=true
UNINSTALL=false
SHELL_CHOICE="auto"
INSTALL_METHOD="${CUE_INSTALL_METHOD:-auto}"
INSTALL_BACKEND=""
SCRIPT_DIR=""

# ---------------------------------------------------------------------------
# Parse args
# ---------------------------------------------------------------------------

while [[ $# -gt 0 ]]; do
    case "$1" in
        --python)     PYTHON="$2"; shift 2 ;;
        --shell)      SHELL_CHOICE="$2"; shift 2 ;;
        --method)     INSTALL_METHOD="$2"; shift 2 ;;
        --no-daemon)  START_DAEMON=false; shift ;;
        --uninstall)  UNINSTALL=true; shift ;;
        -h|--help)
            cat <<'EOF'
Usage: ./install.sh [options]

Options:
  --python PATH           Python 3.11+ interpreter (default: python3)
  --shell auto|zsh|bash   Shell integration target (default: auto from $SHELL)
  --method auto|pipx|uv|venv   Install backend (default: auto — pipx, then uv, then venv)
  --no-daemon             Skip daemon auto-start; install hooks only
  --uninstall             Remove cue hooks and optionally ~/.config/cue
  -h, --help              Show this help
EOF
            exit 0
            ;;
        *)  echo "Unknown option: $1"; exit 1 ;;
    esac
done

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

log()  { echo "  [cue] $*"; }
ok()   { echo "  ✓ $*"; }
warn() { echo "  ! $*"; }

is_wsl() {
    [[ -f /proc/version ]] && grep -qiE 'microsoft|wsl' /proc/version
}

resolve_config_dir() {
    CUE_CONFIG_DIR="${CUE_CONFIG_DIR:-${HOME}/.config/cue}"
    if is_wsl && [[ "$CUE_CONFIG_DIR" == /mnt/* ]]; then
        local linux_home="/home/$(id -un)"
        if [[ -d "$linux_home" && -w "$linux_home" ]]; then
            warn "WSL: using Linux home for config (${linux_home}/.config/cue)"
            CUE_CONFIG_DIR="${linux_home}/.config/cue"
        else
            warn "WSL: ${CUE_CONFIG_DIR} may fail — use export CUE_CONFIG_DIR=/home/\$(whoami)/.config/cue"
        fi
    fi
    export CUE_CONFIG_DIR
    CUE_VENV_DIR="${CUE_CONFIG_DIR}/venv"
    set_hook_lines_for_backend
}

set_hook_lines_for_backend() {
    if [[ "$INSTALL_BACKEND" == "pipx" || "$INSTALL_BACKEND" == "uv" ]]; then
        CUE_PATH_LINE='export PATH="$HOME/.local/bin:$PATH"'
    else
        CUE_PATH_LINE="export PATH=\"${CUE_CONFIG_DIR}/venv/bin:\$PATH\""
    fi
    CUE_ZSH_HOOK_LINE="source \"${CUE_CONFIG_DIR}/cue.zsh\""
    CUE_BASH_HOOK_LINE="source \"${CUE_CONFIG_DIR}/cue.bash\""
}

cue_path() {
    export PATH="${HOME}/.local/bin:${CUE_CONFIG_DIR}/venv/bin:${PATH}"
}

resolve_install_source() {
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" 2>/dev/null && pwd)" || SCRIPT_DIR=""
    if [[ -n "$SCRIPT_DIR" && -f "${SCRIPT_DIR}/pyproject.toml" ]]; then
        echo "$SCRIPT_DIR"
    elif command -v git &>/dev/null; then
        echo "git+${CUE_REPO_URL}"
    else
        echo "${CUE_ARCHIVE_URL}"
    fi
}

ensure_pipx() {
    if command -v pipx &>/dev/null; then
        return 0
    fi
    local os distro
    os="$(detect_os)"
    distro="$(detect_distro_id)"
    log "Installing pipx..."
    if [[ "$os" == "linux" && "$distro" =~ ^(ubuntu|debian|pop|linuxmint)$ ]] && command -v apt-get &>/dev/null; then
        if sudo -n apt-get install -qq -y pipx 2>/dev/null; then
            command -v pipx &>/dev/null && return 0
        fi
    fi
    if "$PYTHON" -m pip install --user pipx &>/dev/null \
        || "$PYTHON" -m pip install --user pipx --break-system-packages &>/dev/null; then
        "$PYTHON" -m pipx ensurepath &>/dev/null || true
        export PATH="${HOME}/.local/bin:${PATH}"
    fi
    command -v pipx &>/dev/null
}

install_with_pipx() {
    local source="$1"
    ensure_pipx || return 1
    log "Installing cue with pipx..."
    if ! pipx install --force "$source"; then
        return 1
    fi
    INSTALL_BACKEND="pipx"
    set_hook_lines_for_backend
    ok "cue installed with pipx (~/.local/bin)."
    return 0
}

install_with_uv() {
    local source="$1"
    command -v uv &>/dev/null || return 1
    log "Installing cue with uv..."
    if ! uv tool install --force "$source"; then
        return 1
    fi
    INSTALL_BACKEND="uv"
    set_hook_lines_for_backend
    ok "cue installed with uv (~/.local/bin)."
    return 0
}

install_with_venv() {
    local source="$1"
    create_venv
    local py
    py="$(venv_python)"
    log "Installing cue into venv..."
    "$py" -m pip install --quiet --upgrade pip
    "$py" -m pip install --quiet "$source"
    INSTALL_BACKEND="venv"
    set_hook_lines_for_backend
    ok "cue installed in ${CUE_VENV_DIR}."
}

install_cue_package() {
    local source method
    source="$(resolve_install_source)"
    method="$INSTALL_METHOD"

    case "$method" in
        pipx) install_with_pipx "$source" ;;
        uv)   install_with_uv "$source" || install_with_venv "$source" ;;
        venv) install_with_venv "$source" ;;
        auto)
            install_with_pipx "$source" \
                || install_with_uv "$source" \
                || install_with_venv "$source"
            ;;
        *)
            echo "Error: --method must be auto, pipx, uv, or venv (got: $method)" >&2
            exit 1
            ;;
    esac
}

cue_python() {
    cue_path
    local venv=""
    if [[ "$INSTALL_BACKEND" == "pipx" ]]; then
        venv="$(pipx environment cue -e PIPX_VENV_DIR 2>/dev/null || true)"
    elif [[ "$INSTALL_BACKEND" == "uv" ]]; then
        if [[ -x "${HOME}/.local/share/uv/tools/cue/bin/python" ]]; then
            venv="${HOME}/.local/share/uv/tools/cue"
        fi
    fi
    if [[ -n "$venv" && -x "${venv}/bin/python" ]]; then
        echo "${venv}/bin/python"
        return 0
    fi
    if [[ "$INSTALL_BACKEND" == "venv" ]]; then
        venv_python
        return 0
    fi
    command -v python3 || command -v python
}

write_default_config() {
    if [[ -f "${CUE_CONFIG_DIR}/config.toml" ]]; then
        ok "Config already exists, skipping."
        return 0
    fi
    "$(cue_python)" -c "from cue.config import load; load()" 2>/dev/null || true
    ok "Default config written to ${CUE_CONFIG_DIR}/config.toml"
}

run_shell_install() {
    local shell_name="$1"
    cue_path
    "$(cue_python)" -m cue.shell_install "$shell_name"
}

detect_os() {
    case "$(uname -s)" in
        Darwin) echo "darwin" ;;
        Linux)  echo "linux" ;;
        *)      echo "unknown" ;;
    esac
}

detect_distro_id() {
    if [[ -f /etc/os-release ]]; then
        # shellcheck disable=SC1091
        . /etc/os-release
        echo "${ID:-unknown}"
        return
    fi
    echo "unknown"
}

python_venv_hint() {
    local os="$1" distro="$2"
    if [[ "$os" == "darwin" ]]; then
        echo "Install Python 3.11+ via Homebrew:  brew install python@3.11"
        return
    fi
    case "$distro" in
        ubuntu|debian|pop|linuxmint)
            echo "sudo apt install python3 python3-venv python3-pip"
            ;;
        fedora|rhel|centos|rocky|almalinux)
            echo "sudo dnf install python3 python3-pip"
            ;;
        arch|manjaro)
            echo "sudo pacman -S python python-pip"
            ;;
        *)
            echo "Install Python 3.11+ and the python3-venv package for your distro."
            ;;
    esac
}

detect_target_shell() {
    case "$SHELL_CHOICE" in
        auto)
            local name
            name="$(basename "${SHELL:-}")"
            case "$name" in
                zsh|bash) echo "$name" ;;
                *) echo "other" ;;
            esac
            ;;
        zsh|bash) echo "$SHELL_CHOICE" ;;
        *)
            echo "Error: --shell must be auto, zsh, or bash (got: $SHELL_CHOICE)" >&2
            exit 1
            ;;
    esac
}

profile_for_shell() {
    case "$1" in
        zsh)
            echo "${ZDOTDIR:-$HOME}/.zshrc"
            ;;
        bash)
            if [[ -n "${BASH_ENV:-}" && -f "${BASH_ENV}" ]]; then
                echo "$BASH_ENV"
            else
                echo "${HOME}/.bashrc"
            fi
            ;;
    esac
}

hook_line_for_shell() {
    case "$1" in
        zsh) echo "$CUE_ZSH_HOOK_LINE" ;;
        bash) echo "$CUE_BASH_HOOK_LINE" ;;
    esac
}

require_python() {
    local os distro
    os="$(detect_os)"
    distro="$(detect_distro_id)"

    if ! command -v "$PYTHON" &>/dev/null; then
        echo "Error: Python not found at '$PYTHON'."
        echo "  $(python_venv_hint "$os" "$distro")"
        exit 1
    fi
    local ver
    ver=$("$PYTHON" -c "import sys; print(sys.version_info >= (3,11))")
    if [[ "$ver" != "True" ]]; then
        echo "Error: Python 3.11+ required. Found: $("$PYTHON" --version)"
        echo "  $(python_venv_hint "$os" "$distro")"
        exit 1
    fi
    ok "Python: $("$PYTHON" --version)"
}

venv_python() {
    echo "${CUE_VENV_DIR}/bin/python"
}

venv_has_pip() {
    local py
    py="$(venv_python)"
    [[ -x "$py" ]] && "$py" -m pip --version &>/dev/null
}

create_venv() {
    local os distro py
    os="$(detect_os)"
    distro="$(detect_distro_id)"
    py="$(venv_python)"

    log "Creating virtual environment at ${CUE_VENV_DIR}..."

    # Stale/broken venv: bin/python exists but pip is missing (common on WSL /mnt/c).
    if [[ -x "$py" ]] && ! venv_has_pip; then
        warn "Removing broken venv (pip missing or unusable)..."
        rm -rf "$CUE_VENV_DIR"
    fi

    if [[ ! -x "$py" ]]; then
        if ! "$PYTHON" -m venv "$CUE_VENV_DIR"; then
            echo "Error: failed to create virtual environment."
            if [[ "$os" == "linux" && "$distro" =~ ^(ubuntu|debian|pop|linuxmint)$ ]]; then
                echo "  On Debian/Ubuntu, install: sudo apt install python3 python3-venv python3-pip"
            else
                echo "  $(python_venv_hint "$os" "$distro")"
            fi
            if is_wsl && [[ "$CUE_CONFIG_DIR" == /mnt/* ]]; then
                echo "  WSL: do not install on /mnt/c — use: export CUE_CONFIG_DIR=/home/\$(whoami)/.config/cue"
            fi
            exit 1
        fi
    fi

    py="$(venv_python)"
    if ! venv_has_pip; then
        log "Bootstrapping pip in venv..."
        if ! "$py" -m ensurepip --upgrade &>/dev/null; then
            echo "Error: venv has no pip. Install system packages first:"
            if [[ "$os" == "linux" && "$distro" =~ ^(ubuntu|debian|pop|linuxmint)$ ]]; then
                echo "  sudo apt install python3-venv python3-pip"
            else
                echo "  $(python_venv_hint "$os" "$distro")"
            fi
            exit 1
        fi
    fi

    if ! venv_has_pip; then
        echo "Error: pip is not usable in ${CUE_VENV_DIR}."
        echo "  Remove it and retry: rm -rf ${CUE_VENV_DIR} && ./install.sh"
        exit 1
    fi
    ok "Virtual environment ready."
}

daemon_bin() {
    if [[ -x "${CUE_VENV_DIR}/bin/cue-daemon" ]]; then
        echo "${CUE_VENV_DIR}/bin/cue-daemon"
    elif command -v cue-daemon &>/dev/null; then
        command -v cue-daemon
    fi
}

add_line_if_missing() {
    local line="$1"
    local file="$2"
    if ! grep -qF "$line" "$file" 2>/dev/null; then
        echo "" >> "$file"
        echo "# cue" >> "$file"
        echo "$line" >> "$file"
        return 0
    fi
    return 1
}

upgrade_profile_line() {
    local old="$1" new="$2" file="$3"
    if [[ -f "$file" ]] && grep -qF "$old" "$file" 2>/dev/null; then
        local tmp
        tmp=$(mktemp)
        sed "s|$(printf '%s' "$old" | sed 's/[&/\]/\\&/g')|$(printf '%s' "$new" | sed 's/[&/\]/\\&/g')|g" "$file" > "$tmp"
        mv "$tmp" "$file"
        ok "Updated stale hook in $file"
    fi
}

install_profile_hooks() {
    local shell_name="$1"
    local profile hook_line widget_file
    profile="$(profile_for_shell "$shell_name")"
    hook_line="$(hook_line_for_shell "$shell_name")"
    widget_file="${CUE_CONFIG_DIR}/cue.${shell_name}"

    if [[ ! -f "$profile" && ! -e "$profile" ]]; then
        touch "$profile"
    fi

    if [[ ! -f "$profile" ]]; then
        warn "$profile is not a regular file. Add these lines manually:"
        echo "    $CUE_PATH_LINE"
        echo "    $hook_line"
        [[ "$START_DAEMON" == "true" ]] && echo "    $CUE_DAEMON_LAUNCH_LINE"
        return
    fi

    upgrade_profile_line 'export PATH="${HOME}/.config/ctrlk/venv/bin:$PATH"' "$CUE_PATH_LINE" "$profile"
    upgrade_profile_line 'source "${HOME}/.config/ctrlk/ctrlk.zsh"' "$CUE_ZSH_HOOK_LINE" "$profile"
    upgrade_profile_line '(ctrlk-daemon start &>/dev/null &)' "$CUE_DAEMON_LAUNCH_LINE" "$profile"
    upgrade_profile_line 'ctrlk-daemon start &>/dev/null' "$CUE_DAEMON_LAUNCH_LINE" "$profile"
    upgrade_profile_line '(cue-daemon start &>/dev/null &)' "$CUE_DAEMON_LAUNCH_LINE" "$profile"

    if add_line_if_missing "$CUE_PATH_LINE" "$profile"; then
        ok "Added cue venv to PATH in $profile"
    else
        ok "cue PATH already in $profile"
    fi

    if [[ -f "$widget_file" ]]; then
        if add_line_if_missing "$hook_line" "$profile"; then
            ok "Added ${shell_name} shell hook to $profile"
        else
            ok "${shell_name} shell hook already in $profile"
        fi
    else
        warn "Widget missing at $widget_file — run: cue install-shell"
    fi

    if [[ "$START_DAEMON" == "true" ]]; then
        if add_line_if_missing "$CUE_DAEMON_LAUNCH_LINE" "$profile"; then
            ok "Added daemon auto-start to $profile"
        else
            ok "Daemon auto-start already in $profile"
        fi
    fi
}

remove_cue_lines_from_file() {
    local file="$1"
    if [[ -f "$file" ]]; then
        local tmp
        tmp=$(mktemp)
        grep -v "cue" "$file" > "$tmp" || true
        mv "$tmp" "$file"
        ok "Removed cue lines from $file"
    fi
}

resolve_config_dir

# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------

if [[ "$UNINSTALL" == "true" ]]; then
    log "Uninstalling cue..."

    cue_path
    local_daemon="$(daemon_bin || true)"
    if [[ -n "$local_daemon" ]]; then
        "$local_daemon" stop 2>/dev/null || true
    elif command -v cue-daemon &>/dev/null; then
        cue-daemon stop 2>/dev/null || true
    fi

    if command -v pipx &>/dev/null && pipx list 2>/dev/null | grep -qE '^package cue '; then
        pipx uninstall cue 2>/dev/null || true
        ok "Removed pipx package"
    fi
    if command -v uv &>/dev/null && uv tool list 2>/dev/null | grep -q cue; then
        uv tool uninstall cue 2>/dev/null || true
        ok "Removed uv tool"
    fi

    remove_cue_lines_from_file "${ZDOTDIR:-$HOME}/.zshrc"
    remove_cue_lines_from_file "${HOME}/.bashrc"
    if [[ -n "${BASH_ENV:-}" && -f "${BASH_ENV}" ]]; then
        remove_cue_lines_from_file "$BASH_ENV"
    fi

    if [[ -d "$CUE_CONFIG_DIR" ]]; then
        read -r -p "  Remove $CUE_CONFIG_DIR (venv, cache, config, history index)? [y/N] " confirm
        if [[ "$confirm" == "y" || "$confirm" == "Y" ]]; then
            rm -rf "$CUE_CONFIG_DIR"
            ok "Removed $CUE_CONFIG_DIR"
        fi
    fi

    ok "cue uninstalled."
    exit 0
fi

# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------

OS="$(detect_os)"
DISTRO="$(detect_distro_id)"
TARGET_SHELL="$(detect_target_shell)"

echo ""
echo "Installing cue v${CUE_VERSION}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
ok "OS: ${OS} (${DISTRO})"
ok "Shell integration: ${TARGET_SHELL}"

require_python

mkdir -p "$CUE_CONFIG_DIR"
ok "Config dir: $CUE_CONFIG_DIR"

install_cue_package
write_default_config

if [[ "$TARGET_SHELL" == "zsh" || "$TARGET_SHELL" == "bash" ]]; then
    log "Installing ${TARGET_SHELL} widget..."
    if ! run_shell_install "$TARGET_SHELL"; then
        echo "Error: failed to install ${TARGET_SHELL} widget." >&2
        exit 1
    fi
    widget_path="${CUE_CONFIG_DIR}/cue.${TARGET_SHELL}"
    if [[ -f "$widget_path" ]]; then
        ok "Shell widget installed to ${widget_path}"
    else
        echo "Error: widget not found at ${widget_path} after install." >&2
        exit 1
    fi
    install_profile_hooks "$TARGET_SHELL"
else
    warn "Unsupported shell '${SHELL:-unknown}' — installed CLI only (no inline Ctrl+K)."
    warn "Use zsh or bash for inline integration, or run: cue generate \"your intent\""
    profile="${ZDOTDIR:-$HOME}/.zshrc"
    if [[ -f "$profile" || ! -e "$profile" ]]; then
        touch "$profile"
        if add_line_if_missing "$CUE_PATH_LINE" "$profile"; then
            ok "Added cue venv to PATH in $profile"
        fi
    fi
fi

if [[ "$START_DAEMON" == "true" ]]; then
    log "Starting cue daemon..."
    cue_path
    if command -v cue-daemon &>/dev/null; then
        cue-daemon stop 2>/dev/null || true
        cue-daemon start
        if cue-daemon health &>/dev/null 2>&1; then
            ok "Daemon started."
        else
            warn "Daemon may still be starting. Check: cue-daemon health"
        fi
    else
        warn "cue-daemon not found on PATH. Open a new terminal and run: cue-daemon start"
    fi
fi

PROFILE="$(profile_for_shell "$TARGET_SHELL" 2>/dev/null || echo "$HOME/.bashrc")"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  cue installed!"
echo ""
if [[ "$TARGET_SHELL" == "zsh" || "$TARGET_SHELL" == "bash" ]]; then
    echo "  Open a new terminal (or: exec \$SHELL -l)"
    echo "  Press Ctrl+K at any prompt."
    if [[ "$OS" == "darwin" ]]; then
        echo "  Cursor steals ^K?  export CUE_KEY_GENERATE='^X^K' in ${PROFILE}"
    fi
else
    echo "  Open a new terminal, then: cue generate \"your intent\""
fi
echo ""
echo "  Optional — cloud LLM:  cue setup"
echo "  Verify:                cue doctor"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
