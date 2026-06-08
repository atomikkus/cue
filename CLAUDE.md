# CLAUDE.md — `ctrlk`: An AI-Native Terminal Command Layer

> A fast, token-frugal, provider-agnostic Ctrl+K for the terminal.
> Type intent in plain language → the correct shell command appears in your buffer.
> You review it. You press Enter. `ctrlk` never runs anything for you.
> Most queries never touch an LLM. The ones that do are lean.

This file is the single source of truth for the project's architecture, design
philosophy, and implementation plan. It is written to be read by both humans and
coding agents (Claude Code, Cursor, etc.). Keep it current.

---

## 1. Vision & Scope

`ctrlk` is a terminal utility that turns natural-language intent into executable
shell commands, inline, without leaving the prompt. It is inspired by Cursor's
terminal Ctrl+K but is **standalone, shell-native, multi-provider, and learns from
your own history**.

### Non-negotiable properties (the "winning proposition")
1. **Fewest tokens** — the LLM is the last resort, not the default. Local resolution
   tiers answer the majority of queries at zero API cost.
2. **Best results** — quality comes from cheap local signals (CWD, git state, exit
   code, your own history) and cached few-shot examples, not from bloated prompts.
3. **Fast** — cache/history hits resolve in tens of milliseconds; a warm daemon
   eliminates interpreter cold-start.
4. **Configurable** — keybindings, providers, models, thresholds all live in one
   declarative config file.
5. **Provider-agnostic** — Anthropic, OpenAI, Mistral, OpenRouter, or any custom
   OpenAI-compatible endpoint, swappable via config.
6. **Buffer-always** — commands are injected into the editable ZLE buffer and
   nothing more. `ctrlk` has no execute mode, no confirm-and-run flag, no auto-run
   shortcut. The user presses Enter. This is an architectural invariant, not a default.

### Explicit non-goals (v1)
- Not an autonomous agent. It generates a single command for review; it does not
  run multi-step workflows on its own.
- Not a terminal emulator. It augments your existing shell (zsh first, bash later).
- Not a RAG-over-documents system. History + semantic cache is the only retrieval.

---

## 2. Design Principles

### 2.1 Tiered resolution (the core idea)
Every query descends a ladder and stops at the first confident hit. Cost and latency
increase down the ladder; hit rate is highest at the top.

```
Query
  ├─ Tier 0  Exact history / alias match      0 tokens     ~3ms
  ├─ Tier 1  Semantic cache hit               0 tokens     ~30ms
  ├─ Tier 2  History semantic search          0 tokens     ~40ms
  │          (high-confidence only)
  └─ Tier 3  LLM generation                   ~150 tokens  ~400-800ms
             (cached prefix + compressed context + small model first)
```

Embeddings are computed **locally**, so the "have we answered this already?" decision
costs zero API tokens. Tokens are spent only on genuinely novel intent.

### 2.2 Tokens are spent on signal, never boilerplate
- Static prefix (system prompt + few-shot) is cached at the provider; amortized ~free.
- Dynamic suffix carries only high-signal context: `$PWD`, git branch, last exit code,
  and the *single* most relevant history line (retrieved locally).
- Output is constrained: command only, no prose, low `max_tokens`.

### 2.3 Small model first, escalate on failure
Generate with a cheap/fast model → validate locally → if validation fails, retry once
with a stronger model. Most shell commands never need escalation.

### 2.4 Local-first, cloud-optional
Everything that can run locally (embeddings, cache, history search, validation) does.
The network is touched only for Tier-3 generation, and even that can be pointed at a
local model (Ollama / LocalAI) via the custom provider.

### 2.5 Personal-first, public-ready
The v1 runs as a single-user local daemon. Every interface is designed so the same
core can later back a multi-user server without rewrites (see §10).

---

## 3. System Architecture

### 3.1 Component map

```
┌─────────────────────────────────────────────────────────────┐
│  Shell layer (zsh ZLE widgets) — thin, ~40 lines             │
│   ^K generate   ^E explain   ^F fix-last                     │
└───────────────┬─────────────────────────────────────────────┘
                │ Unix domain socket (newline-framed JSON)
┌───────────────▼─────────────────────────────────────────────┐
│  Daemon (Python, warm process)                               │
│                                                              │
│   Request Router ──► Resolver (tier engine)                  │
│                        ├─ Tier 0  exact match                │
│                        ├─ Tier 1  semantic cache             │
│                        ├─ Tier 2  history search             │
│                        └─ Tier 3  Provider call              │
│                                                              │
│   Embedder (local model)   Cache/Store (SQLite)              │
│   Validator (safety+parse) Provider Layer (pluggable)        │
└───────────────┬─────────────────────────────────────────────┘
                │ HTTPS (only on Tier 3)
┌───────────────▼─────────────────────────────────────────────┐
│  Provider Layer                                              │
│   Anthropic │ OpenAI │ Mistral │ OpenRouter │ Custom(OAI-compat)│
└──────────────────────────────────────────────────────────────┘
```

### 3.2 Why a daemon
Python's import cost (numpy, embedding model) is 100–300ms — unacceptable on every
keypress. The daemon loads everything once at shell startup and answers over a Unix
socket. Cache hits return in tens of ms. This mirrors how Fig / Amazon Q and the
GitHub CLI work internally.

The shell widget is deliberately dumb: capture query → send to socket → drop the
returned command into the ZLE buffer. All intelligence lives in the daemon.

### 3.3 Request lifecycle
1. User presses the bound key; ZLE widget prompts for intent inline.
2. Widget sends `{op, query, context}` to the socket. `context` = CWD, git branch,
   last exit code, shell, OS.
3. Router dispatches to the Resolver.
4. Resolver walks Tiers 0→3, returning the first confident result.
5. On a Tier-3 miss-then-generate, the result is validated and written to the cache.
6. Daemon returns `{command, source_tier, confidence, tokens_used}`.
7. Widget places `command` in the buffer, cursor at end. Nothing runs.
   The user edits if needed and presses Enter themselves.

---

## 4. Provider Layer (multi-provider abstraction)

The defining requirement: **one interface, many backends**. OpenRouter, Anthropic,
OpenAI, Mistral, and arbitrary custom OpenAI-compatible endpoints are all first-class.

### 4.1 Strategy
Most providers (OpenAI, Mistral, OpenRouter, Ollama, LocalAI, vLLM) expose an
**OpenAI-compatible** `/chat/completions` API. We implement **one** generic
OpenAI-compatible adapter and parameterize it by base URL, auth header, and model.
Anthropic uses its own `/v1/messages` shape, so it gets a dedicated adapter. This
keeps the surface area tiny: effectively two adapters cover everything.

```
Provider (abstract)
  ├─ generate(messages, *, model, max_tokens, stream, cache_prefix) -> Result
  ├─ supports_prompt_caching: bool
  └─ name: str

  ├─ OpenAICompatProvider   # OpenAI, Mistral, OpenRouter, Ollama, LocalAI, custom
  └─ AnthropicProvider      # native /v1/messages, prompt caching via cache_control
```

### 4.2 Unified provider interface (Python sketch)

```python
from dataclasses import dataclass
from typing import Iterator, Protocol

@dataclass
class GenResult:
    text: str
    tokens_in: int
    tokens_out: int
    cached_tokens: int
    model: str
    provider: str

class Provider(Protocol):
    name: str
    supports_prompt_caching: bool
    def generate(
        self,
        system: str,
        few_shot: list[dict],   # static, cacheable prefix
        user: str,              # dynamic suffix (context + query)
        *,
        model: str,
        max_tokens: int = 100,
        stop: list[str] | None = None,
        stream: bool = False,
    ) -> GenResult | Iterator[str]: ...
```

### 4.3 OpenAI-compatible adapter (covers most providers)

```python
class OpenAICompatProvider:
    """OpenAI, Mistral, OpenRouter, Ollama, LocalAI, vLLM, any OAI-compatible URL."""
    def __init__(self, base_url, api_key, name, extra_headers=None):
        self.base_url = base_url            # e.g. https://openrouter.ai/api/v1
        self.api_key = api_key
        self.name = name
        self.extra_headers = extra_headers or {}
        self.supports_prompt_caching = True  # provider-dependent; treated as best-effort

    def generate(self, system, few_shot, user, *, model, max_tokens=100, stop=None, stream=False):
        messages = [{"role": "system", "content": system}, *few_shot,
                    {"role": "user", "content": user}]
        headers = {"Authorization": f"Bearer {self.api_key}",
                   "Content-Type": "application/json", **self.extra_headers}
        # OpenRouter convention: optionally set HTTP-Referer / X-Title for routing/attribution
        payload = {"model": model, "messages": messages,
                   "max_tokens": max_tokens, "stop": stop, "stream": stream}
        # ... POST {base_url}/chat/completions, parse choices[0].message.content ...
```

### 4.4 Anthropic adapter (native, with prompt caching)

```python
class AnthropicProvider:
    name = "anthropic"
    supports_prompt_caching = True

    def generate(self, system, few_shot, user, *, model, max_tokens=100, stop=None, stream=False):
        # System + few-shot marked with cache_control so the static prefix is billed once.
        # POST https://api.anthropic.com/v1/messages
        # headers: x-api-key, anthropic-version
        # body: model, system (with cache_control), messages=[*few_shot, {user}], max_tokens
        ...
```

### 4.5 Provider registry & selection
Providers are instantiated from config at daemon startup. The Resolver picks the
`primary` provider/model for first attempt and the `escalate` provider/model for
retries. A provider can be overridden per-operation (e.g. use a local model for
`explain` to save cost).

```python
REGISTRY = {
    "anthropic":  lambda c: AnthropicProvider(api_key=c.key),
    "openai":     lambda c: OpenAICompatProvider("https://api.openai.com/v1", c.key, "openai"),
    "mistral":    lambda c: OpenAICompatProvider("https://api.mistral.ai/v1", c.key, "mistral"),
    "openrouter": lambda c: OpenAICompatProvider("https://openrouter.ai/api/v1", c.key, "openrouter",
                                                 extra_headers={"HTTP-Referer": c.referer or "",
                                                                "X-Title": "ctrlk"}),
    "custom":     lambda c: OpenAICompatProvider(c.base_url, c.key, c.name or "custom"),
}
```

### 4.6 Key management
- Keys are read from **environment variables first**, then the config file, then an
  OS keychain (optional, via `keyring`). Never logged, never sent anywhere except the
  provider's own endpoint.
- Env var convention: `CTRLK_<PROVIDER>_API_KEY` (e.g. `CTRLK_OPENROUTER_API_KEY`),
  falling back to the provider's canonical var (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
  `MISTRAL_API_KEY`, `OPENROUTER_API_KEY`).

### 4.7 Note on model names
Model identifiers change over time. They are **config values, not hardcoded**. The
examples in §8 are starting points; verify the current model id with your provider.

---

## 5. Tiered Resolution Engine

### 5.1 Tier 0 — exact match
Normalize the query (lowercase, collapse whitespace) and hash-lookup against an
`exact_cache` table and known aliases. Hit → return immediately. This catches the
"I literally asked this verbatim" case and aliases.

### 5.2 Tier 1 — semantic cache
Embed the query locally, cosine-compare against cached `(query_embedding → command)`
rows. If best similarity ≥ `similarity_threshold` (default 0.92), return the cached
command. Optionally key by a coarse context bucket (see §5.5).

### 5.3 Tier 2 — history semantic search
Embed the query, compare against embeddings of the user's shell history. If best match
≥ `history_threshold` (default 0.88), surface that command. This returns *your* idioms,
*your* aliases, *your* cluster-specific invocations — often better than an LLM and free.

### 5.4 Tier 3 — LLM generation
On a miss:
1. Build the **cached prefix**: system prompt + curated few-shot pairs.
2. Build the **dynamic suffix**: compressed context + the single most relevant history
   line (from the Tier-2 search, even if below threshold) + the query.
3. Call `primary` provider/model.
4. Validate (§6). If invalid, retry once with `escalate` provider/model, feeding the
   validation error back in.
5. Store the validated result in the semantic cache.

### 5.5 Context-sensitivity guard
Some intents are CWD-dependent ("restart the service", "delete the last migration").
For queries flagged context-sensitive (heuristic: presence of deictic words like
"this/that/the last/here", or no concrete noun), Tier-1/2 hits require a matching
context bucket (hash of project root + git remote). This prevents returning a cached
command that was correct in a different project.

---

## 6. Validation & Safety

Generated and cached commands pass through a validator before reaching the buffer:
- **Parse check** — must tokenize as a valid command line (use `shlex`).
- **Binary existence** — first token resolves on `$PATH` (warn, don't block, for
  commands that create their own context).
- **Danger scan** — flag destructive patterns (`rm -rf /`, `mkfs`, `dd of=/dev/...`,
  fork bombs, curl-pipe-to-shell). Dangerous commands are surfaced with a visible
  `⚠` warning prefix in the buffer so the user sees them before pressing Enter.
- **Buffer-always, unconditionally** — there is no code path in `ctrlk` that calls
  `zle accept-line` or any equivalent. The ZLE widget ends after `BUFFER=` and
  `zle redisplay`. That is the complete execution surface.
- **Secret redaction** — context sent to providers is scrubbed of obvious secrets
  (tokens, key-like strings, `.env` values) before transmission.

---

## 7. Storage & Embeddings

### 7.1 SQLite schema
Single file at `~/.config/ctrlk/cache.db`. SQLite + on-the-fly cosine in numpy is more
than enough at this scale (≤10k history rows, ≤1k cache rows). No external vector DB.

```sql
CREATE TABLE exact_cache (
    query_norm   TEXT PRIMARY KEY,
    command      TEXT NOT NULL,
    context_hash TEXT,
    hits         INTEGER DEFAULT 1,
    created_at   INTEGER,
    last_used    INTEGER
);

CREATE TABLE semantic_cache (
    id           INTEGER PRIMARY KEY,
    query        TEXT NOT NULL,
    embedding    BLOB NOT NULL,       -- float32 vector
    command      TEXT NOT NULL,
    context_hash TEXT,
    provider     TEXT,
    model        TEXT,
    hits         INTEGER DEFAULT 1,
    created_at   INTEGER,
    last_used    INTEGER
);

CREATE TABLE history_index (
    id         INTEGER PRIMARY KEY,
    command    TEXT NOT NULL,
    embedding  BLOB NOT NULL,
    source     TEXT,                  -- zsh_history | bash_history
    freq       INTEGER DEFAULT 1,
    indexed_at INTEGER
);

CREATE TABLE telemetry (             -- local only, opt-in, never transmitted in v1
    ts          INTEGER,
    op          TEXT,
    tier        INTEGER,
    tokens_in   INTEGER,
    tokens_out  INTEGER,
    latency_ms  INTEGER
);
```

### 7.2 Embedding model
- Default: a small CPU-friendly sentence-embedding model (e.g. `all-MiniLM-L6-v2`
  class, ~22MB, ~80ms/query on CPU). Loaded once in the daemon.
- Alternative: a provider embedding endpoint for zero local deps (costs tokens; off by
  default since it undermines the "free local routing" property).
- Embeddings are L2-normalized at write time so similarity is a dot product.

### 7.3 History ingestion
On first run and periodically, parse `~/.zsh_history` (and bash later), dedupe,
weight by frequency/recency, embed, and populate `history_index`. Incremental updates
hook the shell's `precmd` to index newly run commands.

---

## 8. Configuration

One declarative TOML file. Keybindings, providers, models, thresholds, and feature
flags all live here. The widget reads keybindings at shell startup; the daemon reads
the rest at launch and on `SIGHUP`.

```toml
# ~/.config/ctrlk/config.toml

[keys]                       # fully configurable; rebind + reload
generate = "^K"              # Ctrl+K  natural language -> command
explain  = "^E"              # Ctrl+E  explain current buffer
fix      = "^F"              # Ctrl+F  fix the last failed command

[providers.primary]
provider   = "openrouter"    # anthropic | openai | mistral | openrouter | custom
model      = "anthropic/claude-haiku-4-5"   # provider-namespaced for openrouter
max_tokens = 100

[providers.escalate]
provider   = "anthropic"
model      = "claude-sonnet-4-6"
max_tokens = 200

[providers.openrouter]
# key resolved from CTRLK_OPENROUTER_API_KEY or OPENROUTER_API_KEY
referer = "https://github.com/<you>/ctrlk"

[providers.custom]           # any OpenAI-compatible endpoint (Ollama, vLLM, LocalAI)
name     = "local-ollama"
base_url = "http://localhost:11434/v1"
model    = "qwen2.5-coder:1.5b"
# key optional for local

[cache]
similarity_threshold = 0.92
history_threshold    = 0.88
db_path = "~/.config/ctrlk/cache.db"

[context]
include_git  = true
include_pwd  = true
include_exit = true
history_lines = 1            # most-relevant lines injected into Tier-3 prompt

[embeddings]
backend = "local"           # local | provider
model   = "all-MiniLM-L6-v2"

[safety]
danger_scan      = true      # prefix dangerous commands with ⚠ in buffer
redact_secrets   = true      # scrub key-like strings from context before LLM calls

[telemetry]
enabled = false             # local-only stats; opt-in
```

---

## 9. Directory Layout

```
ctrlk/
├── CLAUDE.md                  # this file
├── README.md
├── pyproject.toml
├── install.sh                 # installs daemon, shell hooks, default config
├── shell/
│   ├── ctrlk.zsh              # ZLE widgets + socket client (sourced in .zshrc)
│   └── ctrlk.bash             # bash port (Phase 4)
├── ctrlk/
│   ├── __init__.py
│   ├── daemon.py              # Unix socket server, lifecycle
│   ├── router.py              # op dispatch
│   ├── resolver.py            # tier engine
│   ├── store.py               # SQLite access
│   ├── embedder.py            # local/provider embeddings
│   ├── validator.py           # parse + safety
│   ├── context.py             # context capture + secret redaction
│   ├── config.py              # TOML load/validate, SIGHUP reload
│   ├── history.py             # history ingestion + indexing
│   └── providers/
│       ├── base.py            # Provider protocol, GenResult
│       ├── anthropic.py       # native /v1/messages + caching
│       ├── openai_compat.py   # OpenAI/Mistral/OpenRouter/custom
│       └── registry.py        # config -> provider instances
└── tests/
    ├── test_resolver.py
    ├── test_providers.py
    ├── test_validator.py
    └── fixtures/
```

---

## 10. Personal → Public Extensibility

The v1 is single-user and local. The path to public/multi-user use is designed in from
the start so it's additive, not a rewrite.

### What stays identical
- Provider layer, resolver, validator, embedder, store interfaces. These are pure
  functions of `(query, context, config)` and don't care who's calling.

### What changes for public use
| Concern | Personal (v1) | Public / Multi-user |
|---|---|---|
| Transport | Unix domain socket | HTTP/gRPC server, authenticated |
| Identity | implicit (one user) | API tokens / OAuth, per-user namespaces |
| Storage | one SQLite file | per-user rows; Postgres + pgvector if scale demands |
| Cache scope | personal | personal + opt-in shared/global cache layer |
| History | local `.zsh_history` | per-user, encrypted at rest, never shared by default |
| Keys | user's own keys | per-user keys, or org gateway (OpenRouter) with quotas |
| Rate/cost | n/a | per-user budgets, usage metering, escalation caps |
| Telemetry | local, opt-in | aggregated, anonymized, opt-in, privacy policy |
| Distribution | `install.sh` + pip | pip + Homebrew + prebuilt daemon; optional hosted backend |

### Server-mode sketch
Swap `daemon.py`'s socket server for an ASGI app (FastAPI) exposing the same router.
The Resolver gains a `user_id` and namespaces all store queries by it. Shared cache
becomes a separate table consulted after the user's personal cache. Everything else is
untouched — the abstractions already isolate transport and identity from logic.

### Scaling the cache
SQLite is correct up to a single power user. For a hosted multi-tenant service, move
`semantic_cache`/`history_index` to Postgres + pgvector (or a managed vector store).
The `store.py` interface is the only thing that changes; the resolver calls the same
methods.

---

## 11. Implementation Plan (phased)

### Phase 0 — Skeleton (foundation)
- [ ] Repo, `pyproject.toml`, config loader with schema validation.
- [ ] Daemon with Unix socket server; newline-framed JSON protocol.
- [ ] Minimal `ctrlk.zsh` widget: `^K` → prompt → socket → buffer.
- [ ] Echo round-trip working end to end (no intelligence yet).
**Exit criteria:** pressing Ctrl+K, typing text, and seeing it returned into the buffer.

### Phase 1 — Provider layer + Tier 3
- [ ] `Provider` protocol, `GenResult`.
- [ ] `AnthropicProvider` and `OpenAICompatProvider`.
- [ ] Registry + config-driven instantiation; env-var key resolution.
- [ ] System prompt + few-shot prefix; prompt caching where supported.
- [ ] Output constraints (command-only, low max_tokens, stop sequences).
- [ ] Validator: shlex parse + binary existence + danger scan.
**Exit criteria:** novel queries produce correct, validated commands via any configured
provider (test with OpenRouter, Anthropic, and a local Ollama endpoint).

### Phase 2 — Local resolution tiers (the token savings)
- [ ] Local embedder loaded in daemon.
- [ ] SQLite store; `exact_cache`, `semantic_cache`, `history_index`.
- [ ] Tier 0/1/2 implemented; thresholds from config.
- [ ] History ingestion + incremental indexing via `precmd` hook.
- [ ] Cache write-back on Tier-3 success.
- [ ] Context-sensitivity guard with context-bucket hashing.
**Exit criteria:** measured ≥60% of a real workday's queries resolve in Tiers 0–2 at
zero API cost; cache/history hits return < 60ms.

### Phase 3 — Quality + UX polish
- [ ] Small-model-first with escalate-on-validation-failure.
- [ ] Streaming Tier-3 output into the buffer.
- [ ] Spinner during network calls; cancel on Esc.
- [ ] `^E` explain and `^F` fix-last operations.
- [ ] Secret redaction in context.
- [ ] Local telemetry (opt-in) + a `ctrlk stats` command (hit rates, tokens saved).
**Exit criteria:** feels instant on hits, smooth on misses; explain/fix usable daily.

### Phase 4 — Distribution & breadth
- [ ] bash port (`ctrlk.bash`); fish stretch goal.
- [ ] `install.sh` + Homebrew formula + pip package.
- [ ] OS keychain integration for keys.
- [ ] Docs, README, recorded demo.
**Exit criteria:** a stranger can install and use it in under five minutes.

### Phase 5 — Public/multi-user (optional)
- [ ] ASGI server mode mirroring the router.
- [ ] Per-user identity, namespacing, budgets, metering.
- [ ] Optional shared cache layer; Postgres + pgvector backend.
- [ ] Privacy policy, opt-in aggregated telemetry.
**Exit criteria:** multi-tenant deployment serving real users with per-user isolation.

---

## 12. Tech Stack Decisions (and rationale)

| Layer | Choice | Why |
|---|---|---|
| Shell glue | zsh ZLE (bash later) | Native keybindings; same mechanism as fzf/autosuggestions |
| Daemon | Python | Warm process kills cold-start; best embedding ecosystem; fast iteration |
| IPC | Unix domain socket | Local, fast, no ports; trivially swappable for HTTP in server mode |
| Store | SQLite + numpy cosine | Zero infra; correct at single-user scale; pgvector only if hosted |
| Embeddings | local MiniLM-class | Free local routing is the whole token-saving thesis |
| Providers | 2 adapters (Anthropic + OAI-compat) | Covers Anthropic, OpenAI, Mistral, OpenRouter, custom/local with minimal code |
| Config | TOML | Human-friendly, typed, comments; one file for everything |

---

## 13. Testing Strategy
- **Unit:** resolver tier logic with mocked store/providers; validator danger scan;
  config parsing; provider request shaping (assert payloads, don't hit network).
- **Contract:** record/replay fixtures for each provider's response shape.
- **Integration:** real socket round-trip with a fake provider; cache write-back; the
  context-sensitivity guard.
- **Eval harness:** a fixed set of NL→command pairs; track accuracy, tier distribution,
  and tokens-per-query as a regression metric across model/provider changes.

---

## 14. Open Questions / Future Work
- Per-directory learned aliases (project-scoped cache promotion).
- Multi-command suggestions (offer 2–3 candidates, arrow to pick) vs. single best.
- "Why this command?" inline explanation on demand without a second round-trip.
- Optional shared community cache (privacy-preserving, opt-in) for cold-start quality.
- fish shell support; Windows/PowerShell consideration for public release.

---

## 15. Conventions for Agents Editing This Repo
- Keep the shell layer thin. New logic goes in the daemon.
- **The buffer-always invariant is not negotiable.** The ZLE widget must never call
  `zle accept-line`, `zle execute-named-cmd`, or any equivalent. The widget ends at
  `BUFFER=` + `zle redisplay`. Period. A PR that adds an execute mode will be rejected.
- Any new provider must implement the `Provider` protocol; do not special-case call
  sites. If it's OpenAI-compatible, configure `custom` — don't write a new adapter.
- Tokens are a tracked metric. A change that increases tokens-per-query must justify it
  with a measurable quality gain in the eval harness.
- Update this file when architecture changes. It is the contract.