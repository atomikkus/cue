# cue.zsh — ZLE widgets for cue
# Source this file in your .zshrc:  source ~/.config/cue/cue.zsh
#
# BUFFER-ALWAYS INVARIANT: These widgets NEVER call `zle accept-line` or any
# equivalent. They end at BUFFER= + zle redisplay. The user presses Enter.
# This is architectural — do not add execute modes.

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CUE_SOCKET="${CUE_SOCKET:-${HOME}/.config/cue/daemon.sock}"
CUE_TIMEOUT="${CUE_TIMEOUT:-15}"

# ---------------------------------------------------------------------------
# Internal: send a JSON request to the daemon, return raw response
# ---------------------------------------------------------------------------

_cue_send() {
    # Usage: _cue_send '<json>'
    # Writes daemon response to stdout. Returns 1 on failure.
    local json="$1"
    if [[ ! -S "$CUE_SOCKET" ]]; then
        print -u2 "cue: daemon socket not found (${CUE_SOCKET}). Run: cue-daemon start"
        return 1
    fi
    # Use Python for the socket call — it's already in PATH and handles the
    # newline-framed protocol correctly across platforms.
    python3 - "$CUE_SOCKET" "$json" <<'PYEOF'
import json, socket, sys
sock_path, payload = sys.argv[1], sys.argv[2]
try:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(15)
        s.connect(sock_path)
        s.sendall((payload + "\n").encode())
        data = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
            if b"\n" in data:
                break
    print(data.decode().strip())
except Exception as e:
    print(json.dumps({"ok": False, "error": str(e)}))
PYEOF
}

# ---------------------------------------------------------------------------
# Internal: build context JSON from current shell state
# ---------------------------------------------------------------------------

_cue_context_json() {
    local cwd git_branch git_remote project_root os_name
    cwd="$(pwd)"
    os_name="$(uname -s | tr '[:upper:]' '[:lower:]')"
    git_branch=""
    git_remote=""
    project_root="$cwd"

    if command -v git &>/dev/null; then
        git_branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
        git_remote="$(git remote get-url origin 2>/dev/null || true)"
        project_root="$(git rev-parse --show-toplevel 2>/dev/null || echo "$cwd")"
    fi

    # Escape for JSON (basic — no control chars in these values normally)
    local esc_cwd esc_branch esc_remote esc_root
    esc_cwd="${cwd//\"/\\\"}"
    esc_branch="${git_branch//\"/\\\"}"
    esc_remote="${git_remote//\"/\\\"}"
    esc_root="${project_root//\"/\\\"}"

    printf '{"cwd":"%s","git_branch":"%s","git_remote":"%s","project_root":"%s","last_exit_code":%d,"shell":"%s","os":"%s"}' \
        "$esc_cwd" "$esc_branch" "$esc_remote" "$esc_root" \
        "${CUE_LAST_EXIT:-0}" "${SHELL:-zsh}" "$os_name"
}

# ---------------------------------------------------------------------------
# Widget: ^K — generate command from natural language
# ---------------------------------------------------------------------------

_cue_read_line() {
    # ZLE cannot call read/vared (recursive ZLE error) — collect keys directly
    emulate -L zsh
    local char line="$1" prompt="$2"
    zle -R "${prompt}${line}"
    while true; do
        read -k 1 char || return 1
        case "$char" in
            $'\n'|$'\r') break ;;
            $'\x03'|$'\x1b') return 1 ;;  # Ctrl+C / Escape
            $'\x7f'|$'\b') line="${line%?}" ;;
            $'\x15') line="" ;;  # Ctrl+U — clear line
            *) line+="$char" ;;
        esac
        zle -R "${prompt}${line}"
    done
    REPLY="$line"
    return 0
}

_cue_generate() {
    emulate -L zsh
    local query response command
    local saved_buffer="$BUFFER"

    if ! _cue_read_line "" "cue> "; then
        BUFFER="$saved_buffer"
        zle redisplay
        return 0
    fi
    query="$REPLY"
    query="${query#"${query%%[![:space:]]*}"}"
    if [[ -z "$query" ]]; then
        BUFFER="$saved_buffer"
        zle redisplay
        return 0
    fi

    # Show spinner while waiting
    BUFFER="⏳ generating..."
    zle redisplay

    # Build and send request
    local ctx_json
    ctx_json="$(_cue_context_json)"
    local esc_query="${query//\"/\\\"}"
    local req_json
    req_json="$(printf '{"op":"generate","query":"%s","context":%s}' "$esc_query" "$ctx_json")"

    response="$(_cue_send "$req_json" 2>/dev/null)"

    if [[ -z "$response" ]]; then
        BUFFER="$saved_buffer"
        zle redisplay
        return
    fi

    # Extract command field from JSON response using Python (reliable, no jq dep)
    command="$(python3 -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    if d.get('ok') and d.get('command'):
        print(d['command'])
    elif d.get('error'):
        print('# cue error: ' + d['error'][:80])
    else:
        print('')
except Exception as e:
    print('')
" <<< "$response")"

    # BUFFER-ALWAYS: place command in buffer; user reviews and presses Enter
    # Never call zle accept-line — this is an architectural invariant
    BUFFER="$command"
    zle end-of-line
    zle redisplay
}

# ---------------------------------------------------------------------------
# Widget: ^E — explain the current buffer command
# ---------------------------------------------------------------------------

_cue_explain() {
    local buffer_cmd="$BUFFER"
    if [[ -z "$buffer_cmd" ]]; then
        zle -R "cue: buffer is empty"
        zle redisplay
        return
    fi

    BUFFER="⏳ explaining..."
    zle redisplay

    local ctx_json
    ctx_json="$(_cue_context_json)"
    local esc_cmd="${buffer_cmd//\"/\\\"}"
    local req_json
    req_json="$(printf '{"op":"explain","context":%s,"buffer":"%s"}' "$ctx_json" "$esc_cmd")"

    local response explanation
    response="$(_cue_send "$req_json" 2>/dev/null)"
    explanation="$(python3 -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    print(d.get('command', '') or d.get('error', 'no explanation'))
except:
    print('parse error')
" <<< "$response")"

    # Show explanation above the prompt line, restore original buffer
    print ""
    print "cue explain: $explanation"
    BUFFER="$buffer_cmd"
    zle redisplay
}

# ---------------------------------------------------------------------------
# Widget: ^F — fix the last failed command
# ---------------------------------------------------------------------------

_cue_fix_last() {
    local last_cmd="${history[1]}"
    local last_exit="${CUE_LAST_EXIT:-1}"

    if [[ -z "$last_cmd" ]]; then
        zle -R "cue: no last command"
        zle redisplay
        return
    fi

    BUFFER="⏳ fixing..."
    zle redisplay

    local ctx_json
    ctx_json="$(_cue_context_json)"
    local esc_cmd="${last_cmd//\"/\\\"}"
    local req_json
    req_json="$(printf '{"op":"fix_last","context":%s,"buffer":"%s","query":"fix the failed command"}' "$ctx_json" "$esc_cmd")"

    local response command
    response="$(_cue_send "$req_json" 2>/dev/null)"
    command="$(python3 -c "
import json, sys
try:
    d = json.loads(sys.stdin.read())
    print(d.get('command', '') or d.get('error', ''))
except:
    print('')
" <<< "$response")"

    # BUFFER-ALWAYS invariant — place fixed command in buffer
    BUFFER="$command"
    zle end-of-line
    zle redisplay
}

# ---------------------------------------------------------------------------
# precmd hook — incrementally index each new command
# ---------------------------------------------------------------------------

_cue_precmd() {
    # Capture exit code before anything else modifies it
    CUE_LAST_EXIT=$?

    # Index the most recent command asynchronously (fire and forget)
    local last_cmd="${history[1]}"
    if [[ -n "$last_cmd" && -S "$CUE_SOCKET" ]]; then
        local esc_cmd="${last_cmd//\"/\\\"}"
        local req_json="$(printf '{"op":"index_cmd","command":"%s"}' "$esc_cmd")"
        # Run in background; ignore output; do not block the prompt
        ( _cue_send "$req_json" &>/dev/null ) &!
    fi
}

# ---------------------------------------------------------------------------
# Register ZLE widgets and key bindings
# ---------------------------------------------------------------------------

zle -N _cue_generate
zle -N _cue_explain
zle -N _cue_fix_last

# Default bindings (user can override in config before sourcing this file)
bindkey "${CUE_KEY_GENERATE:-^K}" _cue_generate
bindkey "${CUE_KEY_EXPLAIN:-^E}"  _cue_explain
bindkey "${CUE_KEY_FIX:-^F}"      _cue_fix_last

# Register precmd hook
autoload -Uz add-zsh-hook
add-zsh-hook precmd _cue_precmd
