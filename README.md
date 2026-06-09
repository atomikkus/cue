# ctrlk

AI-native **Ctrl+K for the terminal**. Type intent in plain language → a shell command appears in your buffer. You review it and press Enter. `ctrlk` never runs anything for you.

Most queries resolve locally (history, semantic cache) at zero API cost. The LLM is only used when local tiers miss.

## Requirements

- **Python 3.11+**
- **zsh** (bash support planned)
- At least one LLM provider API key (for novel queries that miss local tiers)

## Quick install

```bash
git clone <your-repo-url> ctrlk
cd ctrlk
chmod +x install.sh
./install.sh
```

The installer will:

1. Create an isolated Python venv at `~/.config/ctrlk/venv` (works on Homebrew/macOS PEP 668 Python)
2. Install the `ctrlk` package into that venv
3. Create `~/.config/ctrlk/` with default `config.toml`
4. Copy the zsh widget to `~/.config/ctrlk/ctrlk.zsh`
5. Append shell hooks to `~/.zshrc` (including PATH to the venv)
6. Start the background daemon

Reload your shell:

```bash
source ~/.zshrc
```

No manual `pip install` or `~/.local/bin` setup needed — the installer handles the venv for you.

After install, verify everything:

```bash
source ~/.zshrc
ctrlk doctor
ctrlk generate "list files here"
```

`ctrlk doctor` checks the daemon, shell widget, zsh hooks, and Ctrl+K binding.

### Install options

```bash
./install.sh --python /path/to/python3.11   # use a specific Python
./install.sh --no-daemon                    # install hooks only; start daemon manually
./install.sh --uninstall                    # remove hooks and optionally config/cache
```

## API keys

Set at least one provider key before using Tier-3 (LLM) generation:

```bash
export ANTHROPIC_API_KEY='sk-ant-...'
export OPENROUTER_API_KEY='sk-or-...'
export OPENAI_API_KEY='sk-...'
export MISTRAL_API_KEY='...'
```

`ctrlk` checks `CTRLK_<PROVIDER>_API_KEY` first, then the provider's canonical env var (e.g. `ANTHROPIC_API_KEY`).

For a local model via Ollama, configure the `[providers.custom]` section in `config.toml` and point `providers.primary` at `custom` — no cloud key required.

## Usage

| Keybinding | Action |
|------------|--------|
| **Ctrl+K** | Generate a command from natural language |
| **Ctrl+E** | Explain the command currently in the buffer |
| **Ctrl+F** | Fix the last failed command |

At any zsh prompt, press **Ctrl+K**, type your intent (e.g. `find large files in this directory`), and the suggested command lands in your buffer. Edit if needed, then press **Enter**.

Keybindings are configurable in `~/.config/ctrlk/config.toml` under `[keys]`.

## CLI

```bash
ctrlk install-shell         # install/update zsh widget from the Python package
ctrlk doctor                # verify daemon, widget, hooks, and keybindings
ctrlk-daemon start          # start in background (returns to shell when ready)
ctrlk-daemon start -f       # foreground mode for debugging (blocks terminal)
ctrlk-daemon stop           # stop the daemon
ctrlk-daemon health         # check daemon status
ctrlk health                # same, via main CLI
ctrlk stats                 # hit rates, tier breakdown, token usage
ctrlk generate "list git branches"   # test from the terminal
```

Re-run `ctrlk install-shell` after upgrading the package to refresh the zsh widget without a full reinstall.

Reload config without restarting:

```bash
kill -HUP $(cat ~/.config/ctrlk/daemon.pid)
```

## Manual install

If you prefer not to use `install.sh`, use a dedicated venv (required on Homebrew Python due to [PEP 668](https://peps.python.org/pep-0668/)):

```bash
mkdir -p ~/.config/ctrlk
python3 -m venv ~/.config/ctrlk/venv
~/.config/ctrlk/venv/bin/pip install /path/to/ctrlk

# Start daemon
~/.config/ctrlk/venv/bin/ctrlk-daemon start

# Add to ~/.zshrc:
export PATH="${HOME}/.config/ctrlk/venv/bin:$PATH"
source "${HOME}/.config/ctrlk/ctrlk.zsh"
(ctrlk-daemon start &>/dev/null &)
```

Copy `shell/ctrlk.zsh` to `~/.config/ctrlk/ctrlk.zsh` if it does not exist yet. On first daemon start, default config is written to `~/.config/ctrlk/config.toml`.

## Configuration

All settings live in `~/.config/ctrlk/config.toml`:

- **Providers** — primary and escalate models (`openrouter`, `anthropic`, `openai`, `mistral`, `custom`)
- **Cache** — similarity thresholds, SQLite path
- **Context** — git branch, pwd, exit code sent to the resolver
- **Embeddings** — local model for semantic cache/history (default: `all-MiniLM-L6-v2`)
- **Safety** — danger scan, secret redaction

See `CLAUDE.md` for architecture and design details.

## Development

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check ctrlk tests
```

## How it works

```
Query
 ├─ Tier 0  Exact history / alias match     (0 tokens, ~3ms)
 ├─ Tier 1  Semantic cache hit              (0 tokens, ~30ms)
 ├─ Tier 2  History semantic search         (0 tokens, ~40ms)
 └─ Tier 3  LLM generation                  (~150 tokens, ~400–800ms)
```

A warm Python daemon holds the embedding model, SQLite cache, and provider clients. The zsh widget sends JSON over a Unix socket; responses are injected into the ZLE buffer only — never executed automatically.

## License

MIT
