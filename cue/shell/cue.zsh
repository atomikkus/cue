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
CUE_TIMEOUT="${CUE_TIMEOUT:-45}"

# ---------------------------------------------------------------------------
# Internal: Python helpers for JSON socket I/O
# ---------------------------------------------------------------------------

_cue_python() {
    python3 "$@"
}

_cue_send() {
    # Usage: _cue_send <json-string>
    # Writes daemon response to stdout. Returns 1 on failure.
    local json="$1"
    if [[ ! -S "$CUE_SOCKET" ]]; then
        print -u2 "cue: daemon socket not found (${CUE_SOCKET}). Run: cue-daemon start"
        return 1
    fi
    _cue_python - "$CUE_SOCKET" "$CUE_TIMEOUT" "$json" <<'PYEOF'
import json, socket, sys
sock_path, timeout_s, payload = sys.argv[1], float(sys.argv[2]), sys.argv[3]
try:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout_s)
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

_cue_parse_command() {
    # Usage: echo "$response" | _cue_parse_command
    # Sets REPLY to command field or error comment.
    local response="$1"
    REPLY="$(_cue_python -c "
import json, sys
try:
    d = json.loads(sys.argv[1])
    if d.get('ok') and d.get('command'):
        print(d['command'])
    elif d.get('error'):
        print('# cue error: ' + str(d['error'])[:80])
    else:
        print('')
except Exception:
    print('')
" "$response")"
}

# ---------------------------------------------------------------------------
# Widget: ^K — generate command from natural language
# ---------------------------------------------------------------------------

_cue_read_line() {
    emulate -L zsh
    local char line="$1" prompt="$2"
    zle -R "${prompt}${line}"
    while true; do
        read -k 1 char || return 1
        case "$char" in
            $'\n'|$'\r') break ;;
            $'\x03'|$'\x1b') return 1 ;;
            $'\x7f'|$'\b') line="${line%?}" ;;
            $'\x15') line="" ;;
            *) line+="$char" ;;
        esac
        zle -R "${prompt}${line}"
    done
    REPLY="$line"
    return 0
}

_cue_make_request() {
    local op="$1"
    local -a env_args=()
    [[ -n "${CUE_REQ_QUERY:-}" ]] && env_args+=(CUE_REQ_QUERY="$CUE_REQ_QUERY")
    [[ -n "${CUE_REQ_BUFFER:-}" ]] && env_args+=(CUE_REQ_BUFFER="$CUE_REQ_BUFFER")
    [[ -n "${CUE_REQ_COMMAND:-}" ]] && env_args+=(CUE_REQ_COMMAND="$CUE_REQ_COMMAND")
    env_args+=(CUE_LAST_EXIT="${CUE_LAST_EXIT:-0}")
    REPLY="$(env "${env_args[@]}" _cue_python - "$op" <<'PYEOF'
import json, os, subprocess, sys

op = sys.argv[1]
cwd = os.getcwd()
os_name = __import__("platform").system().lower()
git_branch = ""
git_remote = ""
project_root = cwd

for cmd, attr in (
    (["git", "rev-parse", "--abbrev-ref", "HEAD"], "git_branch"),
    (["git", "remote", "get-url", "origin"], "git_remote"),
    (["git", "rev-parse", "--show-toplevel"], "project_root"),
):
    try:
        val = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, text=True).strip()
        if attr == "git_branch":
            git_branch = val
        elif attr == "git_remote":
            git_remote = val
        else:
            project_root = val
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

req = {
    "op": op,
    "context": {
        "cwd": cwd,
        "git_branch": git_branch,
        "git_remote": git_remote,
        "project_root": project_root,
        "last_exit_code": int(os.environ.get("CUE_LAST_EXIT", "0") or 0),
        "shell": os.environ.get("SHELL", "zsh"),
        "os": os_name,
    },
}
if os.environ.get("CUE_REQ_QUERY"):
    req["query"] = os.environ["CUE_REQ_QUERY"]
if os.environ.get("CUE_REQ_BUFFER"):
    req["context"]["buffer"] = os.environ["CUE_REQ_BUFFER"]
    req["buffer"] = os.environ["CUE_REQ_BUFFER"]
if os.environ.get("CUE_REQ_COMMAND"):
    req["command"] = os.environ["CUE_REQ_COMMAND"]
print(json.dumps(req))
PYEOF
)"
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

    BUFFER="⏳ generating..."
    zle redisplay

    CUE_REQ_QUERY="$query"
    unset CUE_REQ_BUFFER CUE_REQ_COMMAND
    local req_json
    req_json="$(_cue_make_request generate)"

    response="$(_cue_send "$req_json" 2>/dev/null)"
    _cue_parse_command "$response"
    command="$REPLY"

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

    unset CUE_REQ_QUERY CUE_REQ_COMMAND
    CUE_REQ_BUFFER="$buffer_cmd"
    local req_json response explanation
    req_json="$(_cue_make_request explain)"

    response="$(_cue_send "$req_json" 2>/dev/null)"
    _cue_parse_command "$response"
    explanation="$REPLY"

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

    if [[ -z "$last_cmd" ]]; then
        zle -R "cue: no last command"
        zle redisplay
        return
    fi

    BUFFER="⏳ fixing..."
    zle redisplay

    unset CUE_REQ_COMMAND
    CUE_REQ_QUERY="fix the failed command"
    CUE_REQ_BUFFER="$last_cmd"
    local req_json response command
    req_json="$(_cue_make_request fix_last)"

    response="$(_cue_send "$req_json" 2>/dev/null)"
    _cue_parse_command "$response"
    command="$REPLY"

    BUFFER="$command"
    zle end-of-line
    zle redisplay
}

# ---------------------------------------------------------------------------
# precmd hook — incrementally index each new command
# ---------------------------------------------------------------------------

_cue_precmd() {
    CUE_LAST_EXIT=$?

    local last_cmd="${history[1]}"
    if [[ -n "$last_cmd" && -S "$CUE_SOCKET" ]]; then
        unset CUE_REQ_QUERY CUE_REQ_BUFFER
        CUE_REQ_COMMAND="$last_cmd"
        local req_json
        req_json="$(_cue_make_request index_cmd)"
        ( _cue_send "$req_json" &>/dev/null ) &!
    fi
}

# ---------------------------------------------------------------------------
# Register ZLE widgets and key bindings
# ---------------------------------------------------------------------------

zle -N _cue_generate
zle -N _cue_explain
zle -N _cue_fix_last

bindkey "${CUE_KEY_GENERATE:-^K}" _cue_generate
bindkey "${CUE_KEY_EXPLAIN:-^E}"  _cue_explain
bindkey "${CUE_KEY_FIX:-^F}"      _cue_fix_last

autoload -Uz add-zsh-hook
add-zsh-hook precmd _cue_precmd
