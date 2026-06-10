#!/usr/bin/env bash
# install.sh — Install cue: daemon, shell hooks, default config
#
# Usage:
#   ./install.sh              # default install (Python from PATH)
#   ./install.sh --python /usr/local/bin/python3.11
#   ./install.sh --no-daemon  # install shell hooks only, start daemon manually
#   ./install.sh --uninstall  # remove everything

set -euo pipefail

CUE_VERSION="0.1.0"
CUE_CONFIG_DIR="${CUE_CONFIG_DIR:-${HOME}/.config/cue}"
CUE_VENV_DIR="${CUE_CONFIG_DIR}/venv"
CUE_PATH_LINE='export PATH="${HOME}/.config/cue/venv/bin:$PATH"'
CUE_ZSH_HOOK_LINE='source "${HOME}/.config/cue/cue.zsh"'
CUE_DAEMON_LAUNCH_LINE='cue-daemon start &>/dev/null'
ZSHRC="${ZDOTDIR:-$HOME}/.zshrc"

PYTHON="${PYTHON:-python3}"
START_DAEMON=true
UNINSTALL=false

# ---------------------------------------------------------------------------
# Parse args
# ---------------------------------------------------------------------------

while [[ $# -gt 0 ]]; do
    case "$1" in
        --python)     PYTHON="$2"; shift 2 ;;
        --no-daemon)  START_DAEMON=false; shift ;;
        --uninstall)  UNINSTALL=true; shift ;;
        -h|--help)
            echo "Usage: $0 [--python PATH] [--no-daemon] [--uninstall]"
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

require_python() {
    if ! command -v "$PYTHON" &>/dev/null; then
        echo "Error: Python not found at '$PYTHON'. Install Python 3.11+ first."
        exit 1
    fi
    local ver
    ver=$("$PYTHON" -c "import sys; print(sys.version_info >= (3,11))")
    if [[ "$ver" != "True" ]]; then
        echo "Error: Python 3.11+ required. Found: $("$PYTHON" --version)"
        exit 1
    fi
    ok "Python: $("$PYTHON" --version)"
}

daemon_bin() {
    if [[ -x "${CUE_VENV_DIR}/bin/cue-daemon" ]]; then
        echo "${CUE_VENV_DIR}/bin/cue-daemon"
    elif command -v cue-daemon &>/dev/null; then
        command -v cue-daemon
    fi
}

# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------

if [[ "$UNINSTALL" == "true" ]]; then
    log "Uninstalling cue..."

    # Stop daemon (prefer venv binary from a prior install)
    local_daemon="$(daemon_bin || true)"
    if [[ -n "$local_daemon" ]]; then
        "$local_daemon" stop 2>/dev/null || true
    fi

    # Remove shell hook lines from .zshrc
    if [[ -f "$ZSHRC" ]]; then
        tmp=$(mktemp)
        grep -v "cue" "$ZSHRC" > "$tmp" || true
        mv "$tmp" "$ZSHRC"
        ok "Removed cue lines from $ZSHRC"
    fi

    # Remove config dir (ask first)
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

echo ""
echo "Installing cue v${CUE_VERSION}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

require_python

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 1. Create config directory
mkdir -p "$CUE_CONFIG_DIR"
ok "Config dir: $CUE_CONFIG_DIR"

# 2. Install into an isolated venv (avoids PEP 668 on Homebrew/system Python)
log "Creating virtual environment at ${CUE_VENV_DIR}..."
if [[ ! -x "${CUE_VENV_DIR}/bin/python" ]]; then
    "$PYTHON" -m venv "$CUE_VENV_DIR"
fi
VENV_PYTHON="${CUE_VENV_DIR}/bin/python"
VENV_PIP="${CUE_VENV_DIR}/bin/pip"
ok "Virtual environment ready."

log "Installing cue Python package..."
"$VENV_PIP" install --quiet --upgrade pip
"$VENV_PIP" install --quiet "$SCRIPT_DIR"
ok "cue package installed in venv."

# 3. Write default config (if not exists)
if [[ ! -f "${CUE_CONFIG_DIR}/config.toml" ]]; then
    "$VENV_PYTHON" -c "from cue.config import load; load()" 2>/dev/null || true
    ok "Default config written to ${CUE_CONFIG_DIR}/config.toml"
else
    ok "Config already exists, skipping."
fi

# 4. Install shell widget from the Python package (always matches installed version)
log "Installing zsh widget..."
"$VENV_PYTHON" -m cue.shell_install
ok "Shell widget installed to ${CUE_CONFIG_DIR}/cue.zsh"

# 5. Add to .zshrc if not already present
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

upgrade_zshrc_line() {
    local old="$1" new="$2" file="$3"
    if [[ -f "$file" ]] && grep -qF "$old" "$file" 2>/dev/null; then
        tmp=$(mktemp)
        sed "s|$(printf '%s' "$old" | sed 's/[&/\]/\\&/g')|$(printf '%s' "$new" | sed 's/[&/\]/\\&/g')|g" "$file" > "$tmp"
        mv "$tmp" "$file"
        ok "Updated stale hook in $file"
    fi
}

if [[ -f "$ZSHRC" || ! -e "$ZSHRC" ]]; then
    touch "$ZSHRC"
    # Upgrade older installs (ctrlk rename, pip --user, background-only daemon start)
    upgrade_zshrc_line 'export PATH="${HOME}/.config/ctrlk/venv/bin:$PATH"' "$CUE_PATH_LINE" "$ZSHRC"
    upgrade_zshrc_line 'source "${HOME}/.config/ctrlk/ctrlk.zsh"' "$CUE_ZSH_HOOK_LINE" "$ZSHRC"
    upgrade_zshrc_line '(ctrlk-daemon start &>/dev/null &)' "$CUE_DAEMON_LAUNCH_LINE" "$ZSHRC"
    upgrade_zshrc_line 'ctrlk-daemon start &>/dev/null' "$CUE_DAEMON_LAUNCH_LINE" "$ZSHRC"
    upgrade_zshrc_line '(cue-daemon start &>/dev/null &)' "$CUE_DAEMON_LAUNCH_LINE" "$ZSHRC"
    if add_line_if_missing "$CUE_PATH_LINE" "$ZSHRC"; then
        ok "Added cue venv to PATH in $ZSHRC"
    else
        ok "cue PATH already in $ZSHRC"
    fi
    if add_line_if_missing "$CUE_ZSH_HOOK_LINE" "$ZSHRC"; then
        ok "Added shell hook to $ZSHRC"
    else
        ok "Shell hook already in $ZSHRC"
    fi
    if [[ "$START_DAEMON" == "true" ]]; then
        if add_line_if_missing "$CUE_DAEMON_LAUNCH_LINE" "$ZSHRC"; then
            ok "Added daemon auto-start to $ZSHRC"
        else
            ok "Daemon auto-start already in $ZSHRC"
        fi
    fi
else
    warn "$ZSHRC is not a regular file. Add these lines manually:"
    echo "    $CUE_PATH_LINE"
    echo "    $CUE_ZSH_HOOK_LINE"
    [[ "$START_DAEMON" == "true" ]] && echo "    $CUE_DAEMON_LAUNCH_LINE"
fi

# 6. Start the daemon now
if [[ "$START_DAEMON" == "true" ]]; then
    log "Starting cue daemon..."
    DAEMON_BIN="$(daemon_bin || true)"

    if [[ -n "$DAEMON_BIN" ]]; then
        "$DAEMON_BIN" stop 2>/dev/null || true
        "$DAEMON_BIN" start
        if "$DAEMON_BIN" health &>/dev/null 2>&1; then
            ok "Daemon started."
        else
            warn "Daemon may still be loading. Check with: cue-daemon health"
        fi
    else
        warn "cue-daemon not found. Run: ${CUE_VENV_DIR}/bin/cue-daemon start"
    fi
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  cue installed successfully!"
echo ""
echo "  Next steps:"
echo "  1. Configure provider, API key, and model:"
echo "     cue setup"
echo "     # or: cue key set openrouter && cue config set providers.primary.model <model>"
echo ""
echo "  2. Reload your shell:"
echo "     source ~/.zshrc"
echo ""
echo "  3. Verify install:      cue doctor"
echo "  4. Press Ctrl+K at any prompt and type your intent."
echo "     (In Cursor, rebind if ^K is stolen: export CUE_KEY_GENERATE='^X^K')"
echo ""
echo "  Binaries:             ${CUE_VENV_DIR}/bin/"
echo "  Update shell widget:  cue install-shell"
echo "  Check daemon status:  cue-daemon health"
echo "  View stats:           cue stats"
echo "  Reload config:        kill -HUP \$(cat ${CUE_CONFIG_DIR}/daemon.pid)"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
