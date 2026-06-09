#!/usr/bin/env bash
# install.sh — Install ctrlk: daemon, shell hooks, default config
#
# Usage:
#   ./install.sh              # default install (Python from PATH)
#   ./install.sh --python /usr/local/bin/python3.11
#   ./install.sh --no-daemon  # install shell hooks only, start daemon manually
#   ./install.sh --uninstall  # remove everything

set -euo pipefail

CTRLK_VERSION="0.1.0"
CTRLK_CONFIG_DIR="${CTRLK_CONFIG_DIR:-${HOME}/.config/ctrlk}"
CTRLK_VENV_DIR="${CTRLK_CONFIG_DIR}/venv"
CTRLK_PATH_LINE='export PATH="${HOME}/.config/ctrlk/venv/bin:$PATH"'
CTRLK_ZSH_HOOK_LINE='source "${HOME}/.config/ctrlk/ctrlk.zsh"'
CTRLK_DAEMON_LAUNCH_LINE='ctrlk-daemon start &>/dev/null'
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

log()  { echo "  [ctrlk] $*"; }
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
    if [[ -x "${CTRLK_VENV_DIR}/bin/ctrlk-daemon" ]]; then
        echo "${CTRLK_VENV_DIR}/bin/ctrlk-daemon"
    elif command -v ctrlk-daemon &>/dev/null; then
        command -v ctrlk-daemon
    fi
}

# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------

if [[ "$UNINSTALL" == "true" ]]; then
    log "Uninstalling ctrlk..."

    # Stop daemon (prefer venv binary from a prior install)
    local_daemon="$(daemon_bin || true)"
    if [[ -n "$local_daemon" ]]; then
        "$local_daemon" stop 2>/dev/null || true
    fi

    # Remove shell hook lines from .zshrc
    if [[ -f "$ZSHRC" ]]; then
        tmp=$(mktemp)
        grep -v "ctrlk" "$ZSHRC" > "$tmp" || true
        mv "$tmp" "$ZSHRC"
        ok "Removed ctrlk lines from $ZSHRC"
    fi

    # Remove config dir (ask first)
    if [[ -d "$CTRLK_CONFIG_DIR" ]]; then
        read -r -p "  Remove $CTRLK_CONFIG_DIR (venv, cache, config, history index)? [y/N] " confirm
        if [[ "$confirm" == "y" || "$confirm" == "Y" ]]; then
            rm -rf "$CTRLK_CONFIG_DIR"
            ok "Removed $CTRLK_CONFIG_DIR"
        fi
    fi

    ok "ctrlk uninstalled."
    exit 0
fi

# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------

echo ""
echo "Installing ctrlk v${CTRLK_VERSION}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

require_python

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 1. Create config directory
mkdir -p "$CTRLK_CONFIG_DIR"
ok "Config dir: $CTRLK_CONFIG_DIR"

# 2. Install into an isolated venv (avoids PEP 668 on Homebrew/system Python)
log "Creating virtual environment at ${CTRLK_VENV_DIR}..."
if [[ ! -x "${CTRLK_VENV_DIR}/bin/python" ]]; then
    "$PYTHON" -m venv "$CTRLK_VENV_DIR"
fi
VENV_PYTHON="${CTRLK_VENV_DIR}/bin/python"
VENV_PIP="${CTRLK_VENV_DIR}/bin/pip"
ok "Virtual environment ready."

log "Installing ctrlk Python package..."
"$VENV_PIP" install --quiet --upgrade pip
"$VENV_PIP" install --quiet "$SCRIPT_DIR"
ok "ctrlk package installed in venv."

# 3. Write default config (if not exists)
if [[ ! -f "${CTRLK_CONFIG_DIR}/config.toml" ]]; then
    "$VENV_PYTHON" -c "from ctrlk.config import load; load()" 2>/dev/null || true
    ok "Default config written to ${CTRLK_CONFIG_DIR}/config.toml"
else
    ok "Config already exists, skipping."
fi

# 4. Install shell widget from the Python package (always matches installed version)
log "Installing zsh widget..."
"$VENV_PYTHON" -m ctrlk.shell_install
ok "Shell widget installed to ${CTRLK_CONFIG_DIR}/ctrlk.zsh"

# 5. Add to .zshrc if not already present
add_line_if_missing() {
    local line="$1"
    local file="$2"
    if ! grep -qF "$line" "$file" 2>/dev/null; then
        echo "" >> "$file"
        echo "# ctrlk" >> "$file"
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
    # Upgrade older installs that used pip --user or background-only daemon start
    upgrade_zshrc_line '(ctrlk-daemon start &>/dev/null &)' "$CTRLK_DAEMON_LAUNCH_LINE" "$ZSHRC"
    if add_line_if_missing "$CTRLK_PATH_LINE" "$ZSHRC"; then
        ok "Added ctrlk venv to PATH in $ZSHRC"
    else
        ok "ctrlk PATH already in $ZSHRC"
    fi
    if add_line_if_missing "$CTRLK_ZSH_HOOK_LINE" "$ZSHRC"; then
        ok "Added shell hook to $ZSHRC"
    else
        ok "Shell hook already in $ZSHRC"
    fi
    if [[ "$START_DAEMON" == "true" ]]; then
        if add_line_if_missing "$CTRLK_DAEMON_LAUNCH_LINE" "$ZSHRC"; then
            ok "Added daemon auto-start to $ZSHRC"
        else
            ok "Daemon auto-start already in $ZSHRC"
        fi
    fi
else
    warn "$ZSHRC is not a regular file. Add these lines manually:"
    echo "    $CTRLK_PATH_LINE"
    echo "    $CTRLK_ZSH_HOOK_LINE"
    [[ "$START_DAEMON" == "true" ]] && echo "    $CTRLK_DAEMON_LAUNCH_LINE"
fi

# 6. Start the daemon now
if [[ "$START_DAEMON" == "true" ]]; then
    log "Starting ctrlk daemon..."
    DAEMON_BIN="$(daemon_bin || true)"

    if [[ -n "$DAEMON_BIN" ]]; then
        "$DAEMON_BIN" stop 2>/dev/null || true
        "$DAEMON_BIN" start
        if "$DAEMON_BIN" health &>/dev/null 2>&1; then
            ok "Daemon started."
        else
            warn "Daemon may still be loading. Check with: ctrlk-daemon health"
        fi
    else
        warn "ctrlk-daemon not found. Run: ${CTRLK_VENV_DIR}/bin/ctrlk-daemon start"
    fi
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  ctrlk installed successfully!"
echo ""
echo "  Next steps:"
echo "  1. Set your API key (at least one provider required):"
echo "     export ANTHROPIC_API_KEY='sk-ant-...'"
echo "     export OPENROUTER_API_KEY='sk-or-...'"
echo "     export OPENAI_API_KEY='sk-...'"
echo ""
echo "  2. Reload your shell:"
echo "     source ~/.zshrc"
echo ""
echo "  3. Verify install:      ctrlk doctor"
echo "  4. Press Ctrl+K at any prompt and type your intent."
echo "     (In Cursor, rebind if ^K is stolen: export CTRLK_KEY_GENERATE='^X^K')"
echo ""
echo "  Binaries:             ${CTRLK_VENV_DIR}/bin/"
echo "  Update shell widget:  ctrlk install-shell"
echo "  Check daemon status:  ctrlk-daemon health"
echo "  View stats:           ctrlk stats"
echo "  Reload config:        kill -HUP \$(cat ${CTRLK_CONFIG_DIR}/daemon.pid)"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
