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
CTRLK_ZSH_HOOK_LINE='source "${HOME}/.config/ctrlk/ctrlk.zsh"'
CTRLK_DAEMON_LAUNCH_LINE='(ctrlk-daemon start &>/dev/null &)'
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

# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------

if [[ "$UNINSTALL" == "true" ]]; then
    log "Uninstalling ctrlk..."

    # Stop daemon
    if command -v ctrlk-daemon &>/dev/null; then
        ctrlk-daemon stop 2>/dev/null || true
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
        read -r -p "  Remove $CTRLK_CONFIG_DIR (cache, config, history index)? [y/N] " confirm
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

# 1. Install Python package
log "Installing ctrlk Python package..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"$PYTHON" -m pip install --quiet --user "$SCRIPT_DIR"
ok "ctrlk package installed."

# 2. Create config directory
mkdir -p "$CTRLK_CONFIG_DIR"
ok "Config dir: $CTRLK_CONFIG_DIR"

# 3. Write default config (if not exists)
if [[ ! -f "${CTRLK_CONFIG_DIR}/config.toml" ]]; then
    "$PYTHON" -c "from ctrlk.config import load; load()" 2>/dev/null || true
    ok "Default config written to ${CTRLK_CONFIG_DIR}/config.toml"
else
    ok "Config already exists, skipping."
fi

# 4. Install shell widget
cp "${SCRIPT_DIR}/shell/ctrlk.zsh" "${CTRLK_CONFIG_DIR}/ctrlk.zsh"
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

if [[ -f "$ZSHRC" || ! -e "$ZSHRC" ]]; then
    touch "$ZSHRC"
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
    echo "    $CTRLK_ZSH_HOOK_LINE"
    [[ "$START_DAEMON" == "true" ]] && echo "    $CTRLK_DAEMON_LAUNCH_LINE"
fi

# 6. Start the daemon now
if [[ "$START_DAEMON" == "true" ]]; then
    log "Starting ctrlk daemon..."
    # Find ctrlk-daemon in user bin or PATH
    DAEMON_BIN=""
    for candidate in \
        "${HOME}/.local/bin/ctrlk-daemon" \
        "$(command -v ctrlk-daemon 2>/dev/null || true)"
    do
        if [[ -x "$candidate" ]]; then
            DAEMON_BIN="$candidate"
            break
        fi
    done

    if [[ -n "$DAEMON_BIN" ]]; then
        "$DAEMON_BIN" stop 2>/dev/null || true
        "$DAEMON_BIN" start &>/dev/null &
        sleep 1
        if "$DAEMON_BIN" health &>/dev/null 2>&1; then
            ok "Daemon started."
        else
            warn "Daemon may still be loading. Check with: ctrlk-daemon health"
        fi
    else
        warn "ctrlk-daemon not found in PATH. You may need to add ~/.local/bin to PATH."
        warn "Then run: ctrlk-daemon start"
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
echo "  3. Press Ctrl+K at any prompt and type your intent."
echo ""
echo "  Check daemon status:  ctrlk-daemon health"
echo "  View stats:           ctrlk stats"
echo "  Reload config:        kill -HUP \$(cat ${CTRLK_CONFIG_DIR}/daemon.pid)"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
