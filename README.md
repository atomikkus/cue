<p align="center">
  <img src="docs/cue_vaporwave_transparent.png" alt="Cue" width="280">
</p>

<h1 align="center">Cue</h1>

<p align="center">
  <strong>AI-native Ctrl+K for your terminal.</strong><br>
  Type intent in plain language → a shell command appears in your buffer.<br>
  You review it. You press Enter. Cue never runs anything for you.
</p>

<p align="center">
  <code>Python 3.11+</code> · <code>zsh</code> · <code>bash</code> · <code>local-first</code> · <code>multi-provider</code>
</p>

---

## Why Cue

Most terminal AI tools burn tokens on every keystroke. Cue doesn't. A warm daemon resolves your intent through a tiered ladder — history, semantic cache, then LLM — and stops at the first confident hit.

| Tier | Source | Cost | Speed |
|:----:|--------|------|-------|
| **0** | Exact cache / alias | free | ~3 ms |
| **1** | Semantic cache | free | ~30 ms |
| **2** | Your shell history | free | ~40 ms |
| **3** | LLM generation | ~150 tokens | ~400–800 ms |

Embeddings run locally. The network is touched only when local tiers miss.

---

## Install

Works on **macOS and Linux**. One command:

```bash
curl -fsSL https://raw.githubusercontent.com/atomikkus/cue/main/install.sh | bash
```

Then **open a new terminal** and press **Ctrl+K**.

Inline Ctrl+K works in **zsh** and **bash**. On **Linux and WSL**, the installer bootstraps **uv** and installs into `~/.config/cue/venv` via a pre-built wheel (fast, visible progress). On **macOS**, it uses **pipx** when available, then falls back to uv or venv.

<details>
<summary><strong>Install from a clone</strong></summary>

```bash
git clone https://github.com/atomikkus/cue.git ~/cue
cd ~/cue
./install.sh
```

On **WSL**, clone inside Linux home (`~/cue`), not under `/mnt/c/...` — Windows drives make Python installs very slow.

</details>

### Prerequisites

| Platform | What you need |
|----------|----------------|
| **macOS** | Python 3.11+ (`brew install python@3.11` if needed). zsh is usually already your login shell. |
| **Debian/Ubuntu / WSL** | `sudo apt install python3 python3-venv python3-pip` — clone to `~/cue`, not `/mnt/c` |
| **Fedora** | `sudo dnf install python3 python3-pip` |
| **Arch** | `sudo pacman -S python python-pip` |

The installer handles Python deps, shell hooks, and the background daemon automatically. Config and widgets live in `~/.config/cue/`. If your login shell is neither zsh nor bash, use `cue generate "..."` from any terminal.

```bash
cue doctor                      # verify daemon, widget, hooks, keybindings
cue generate "list files here"  # smoke test from the terminal
```

<details>
<summary><strong>Install options</strong></summary>

```bash
./install.sh --method venv                  # force venv (default on Linux: uv + venv)
./install.sh --method pipx                  # force pipx (default on macOS)
./install.sh --shell bash                   # force bash hooks (default: auto from $SHELL)
./install.sh --no-daemon                    # hooks only; start daemon manually
./install.sh --uninstall                    # remove hooks and optionally ~/.config/cue
```

</details>

---

## API keys

Add at least one key to your shell profile (`~/.zshrc` or `~/.bashrc`), then restart the daemon:

```bash
export OPENROUTER_API_KEY='sk-or-...'
# export ANTHROPIC_API_KEY='sk-ant-...'
# export OPENAI_API_KEY='sk-...'

cue-daemon restart
```

Cue checks `CUE_<PROVIDER>_API_KEY` first, then the provider's canonical env var. For a fully local setup, point `providers.primary` at `[providers.custom]` (Ollama, vLLM, etc.) in `~/.config/cue/config.toml`.

---

## Usage

Press a key at any zsh or bash prompt, type your intent, get a command in the buffer.

| Key | Action |
|-----|--------|
| **Ctrl+K** | Generate a command from natural language |
| **Ctrl+E** | Explain what's in the buffer |
| **Ctrl+F** | Fix the last failed command |

```
you>  [Ctrl+K]
cue>  find large files in this directory
you>  find . -type f -size +100M          ← lands here, editable, not executed
```

### Cursor / macOS tip

If **Ctrl+K** does nothing, Cursor or your terminal may be stealing it. Rebind in your shell profile before sourcing `cue.zsh` or `cue.bash`:

```bash
export CUE_KEY_GENERATE='^X^K'   # Ctrl+X, then Ctrl+K
```

---

## CLI

```bash
cue install-shell              # refresh zsh/bash widget after upgrades
cue doctor                     # full install health check
cue stats                      # hit rates, tier breakdown, token usage
cue generate "show git status" # test without keybindings

cue-daemon start               # background (returns when ready)
cue-daemon stop
cue-daemon health
```

Reload config without restarting:

```bash
kill -HUP $(cat ~/.config/cue/daemon.pid)
```

---

## Configuration

Everything lives in `~/.config/cue/config.toml`:

| Section | What it controls |
|---------|------------------|
| `[keys]` | Keybindings (`^K`, `^E`, `^F`) |
| `[providers.primary]` | First LLM attempt |
| `[providers.escalate]` | Retry model on validation failure |
| `[cache]` | Similarity thresholds, SQLite path |
| `[embeddings]` | Local ONNX embeddings via fastembed (default: `BAAI/bge-small-en-v1.5`) |
| `[safety]` | Danger scan, secret redaction |

---

## How it works

```
┌─────────────┐     Unix socket      ┌──────────────────────────────┐
│ shell widget│  ─────────────────►  │  cue-daemon (warm Python)    │
│  Ctrl+K     │  ◄─────────────────  │  embedder · cache · resolver │
└─────────────┘   command in buffer  └──────────────────────────────┘
                                              │ Tier 3 only
                                              ▼
                                     Anthropic · OpenAI · OpenRouter · Ollama …
```

The shell layer is thin. All intelligence lives in the daemon. Commands are injected into the ZLE buffer only — there is no execute mode.

---

## Development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest
ruff check cue tests
```

See [`CLAUDE.md`](docs/CLAUDE.md) for architecture, design principles, and the implementation roadmap.

---

## License

MIT
