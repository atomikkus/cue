# cue.bash — Readline integration for cue
# Source this file in your .bashrc:  source ~/.config/cue/cue.bash
#
# BUFFER-ALWAYS INVARIANT: These bindings NEVER call `eval` on generated commands
# or append to history and execute. They set READLINE_LINE + READLINE_POINT only.

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_cue_resolve_config_dir() {
    if [[ -n "${CUE_CONFIG_DIR:-}" ]]; then
        printf '%s' "$CUE_CONFIG_DIR"
        return
    fi
    local dir="${HOME}/.config/cue"
    if [[ -f /proc/version ]] && grep -qiE 'microsoft|wsl' /proc/version && [[ "$dir" == /mnt/* ]]; then
        dir="/home/$(id -un)/.config/cue"
    fi
    printf '%s' "$dir"
}

CUE_CONFIG_DIR="${CUE_CONFIG_DIR:-$(_cue_resolve_config_dir)}"
CUE_SOCKET="${CUE_SOCKET:-${CUE_CONFIG_DIR}/daemon.sock}"
CUE_TIMEOUT="${CUE_TIMEOUT:-45}"

# ---------------------------------------------------------------------------
# Internal: Python helpers for JSON socket I/O
# ---------------------------------------------------------------------------

_cue_python() {
    python3 "$@"
}

_cue_send() {
    local json="$1"
    if [[ ! -S "$CUE_SOCKET" ]]; then
        echo "cue: daemon socket not found (${CUE_SOCKET}). Run: cue-daemon start" >&2
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
    local response="$1"
    _cue_python -c "
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
" "$response"
}

_cue_make_request() {
    local op="$1"
    local -a env_args=()
    [[ -n "${CUE_REQ_QUERY:-}" ]] && env_args+=(CUE_REQ_QUERY="$CUE_REQ_QUERY")
    [[ -n "${CUE_REQ_BUFFER:-}" ]] && env_args+=(CUE_REQ_BUFFER="$CUE_REQ_BUFFER")
    [[ -n "${CUE_REQ_COMMAND:-}" ]] && env_args+=(CUE_REQ_COMMAND="$CUE_REQ_COMMAND")
    env_args+=(CUE_LAST_EXIT="${CUE_LAST_EXIT:-0}")
    env "${env_args[@]}" python3 - "$op" <<'PYEOF'
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
        "shell": os.environ.get("SHELL", "bash"),
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
}

_cue_set_buffer() {
    local text="$1"
    READLINE_LINE="$text"
    READLINE_POINT=${#READLINE_LINE}
}

# Read a line from /dev/tty without readline.  bind -x handlers must not use
# readline-backed input — Enter would accept READLINE_LINE and auto-execute.
_cue_read_line() {
    local prompt="${1:-cue> }" line="" char tty=/dev/tty
    [[ -r "$tty" ]] || tty=/dev/stdin
    printf '%s' "$prompt" >"$tty"
    while true; do
        if ! IFS= read -r -n 1 char <"$tty"; then
            printf '\n' >"$tty"
            return 1
        fi
        case "$char" in
            ''|$'\n'|$'\r') break ;;
            $'\003') printf '^C\n' >"$tty"; return 1 ;;
            $'\033') return 1 ;;
            $'\177'|$'\b')
                if [[ -n "$line" ]]; then
                    line="${line%?}"
                    printf '\b \b' >"$tty"
                fi
                ;;
            $'\025')
                while [[ -n "$line" ]]; do
                    line="${line%?}"
                    printf '\b \b' >"$tty"
                done
                ;;
            *)
                line+="$char"
                printf '%s' "$char" >"$tty"
                ;;
        esac
    done
    printf '\n' >"$tty"
    REPLY="$line"
    return 0
}

# ---------------------------------------------------------------------------
# Ctrl+K — generate command from natural language
# ---------------------------------------------------------------------------

_cue_generate() {
    local query response command saved_buffer="${READLINE_LINE:-}"

    READLINE_LINE=""
    READLINE_POINT=0
    if ! _cue_read_line "cue> "; then
        _cue_set_buffer "$saved_buffer"
        return 0
    fi
    query="$REPLY"
    query="${query#"${query%%[![:space:]]*}"}"
    if [[ -z "$query" ]]; then
        _cue_set_buffer "$saved_buffer"
        return 0
    fi

    CUE_REQ_QUERY="$query"
    unset CUE_REQ_BUFFER CUE_REQ_COMMAND
    local req_json
    req_json="$(_cue_make_request generate)"

    if ! response="$(_cue_send "$req_json" 2>&1)"; then
        _cue_set_buffer "# cue: daemon error — run: cue-daemon start"
        return 0
    fi
    if [[ -z "$response" ]]; then
        _cue_set_buffer "# cue: empty response from daemon"
        return 0
    fi
    command="$(_cue_parse_command "$response")"
    if [[ -z "$command" ]]; then
        _cue_set_buffer "# cue: no command returned (check: cue doctor)"
        return 0
    fi
    _cue_set_buffer "$command"
}

# ---------------------------------------------------------------------------
# Ctrl+E — explain the current buffer command
# ---------------------------------------------------------------------------

_cue_explain() {
    local buffer_cmd="${READLINE_LINE:-}"
    if [[ -z "$buffer_cmd" ]]; then
        echo "cue: buffer is empty" >&2
        return 0
    fi

    local saved="$buffer_cmd"
    _cue_set_buffer "⏳ explaining..."

    unset CUE_REQ_QUERY CUE_REQ_COMMAND
    CUE_REQ_BUFFER="$buffer_cmd"
    local req_json response explanation
    req_json="$(_cue_make_request explain)"

    response="$(_cue_send "$req_json" 2>/dev/null)"
    explanation="$(_cue_parse_command "$response")"

    echo "" >&2
    echo "cue explain: $explanation" >&2
    _cue_set_buffer "$saved"
}

# ---------------------------------------------------------------------------
# Ctrl+F — fix the last failed command
# ---------------------------------------------------------------------------

_cue_fix_last() {
    local last_cmd
    last_cmd="$(history 1 2>/dev/null | sed 's/^[[:space:]]*[0-9]*[[:space:]]*//')"
    if [[ -z "$last_cmd" ]]; then
        last_cmd="$(fc -ln -1 2>/dev/null | sed 's/^[[:space:]]*//')"
    fi

    if [[ -z "$last_cmd" ]]; then
        echo "cue: no last command" >&2
        return 0
    fi

    _cue_set_buffer "⏳ fixing..."

    unset CUE_REQ_COMMAND
    CUE_REQ_QUERY="fix the failed command"
    CUE_REQ_BUFFER="$last_cmd"
    local req_json response command
    req_json="$(_cue_make_request fix_last)"

    response="$(_cue_send "$req_json" 2>/dev/null)"
    command="$(_cue_parse_command "$response")"
    _cue_set_buffer "$command"
}

# ---------------------------------------------------------------------------
# PROMPT_COMMAND — capture exit code and index history
# ---------------------------------------------------------------------------

_cue_prompt_command() {
    CUE_LAST_EXIT=$?

    local last_cmd
    last_cmd="$(history 1 2>/dev/null | sed 's/^[[:space:]]*[0-9]*[[:space:]]*//')"
    if [[ -n "$last_cmd" && -S "$CUE_SOCKET" ]]; then
        unset CUE_REQ_QUERY CUE_REQ_BUFFER
        CUE_REQ_COMMAND="$last_cmd"
        local req_json
        req_json="$(_cue_make_request index_cmd)"
        # Must be synchronous — background subshell often never completes.
        _cue_send "$req_json" >/dev/null 2>&1 || true
    fi
}

if [[ "$PROMPT_COMMAND" != *"_cue_prompt_command"* ]]; then
    if [[ -z "$PROMPT_COMMAND" ]]; then
        PROMPT_COMMAND="_cue_prompt_command"
    else
        PROMPT_COMMAND="_cue_prompt_command; $PROMPT_COMMAND"
    fi
fi

# ---------------------------------------------------------------------------
# Key bindings (override via CUE_KEY_* env vars before sourcing)
# ---------------------------------------------------------------------------

bind -x "${CUE_KEY_GENERATE:-\"\\C-k\"}:_cue_generate"
bind -x "${CUE_KEY_EXPLAIN:-\"\\C-e\"}:_cue_explain"
bind -x "${CUE_KEY_FIX:-\"\\C-f\"}:_cue_fix_last"
