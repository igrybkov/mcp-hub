# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`mcp-hub` is a meta-MCP server: a single MCP connection that lazily proxies many child MCP servers. The host (Claude Code, Desktop, Cursor) connects only to the hub, which exposes ~8 discovery meta-tools and spawns child servers on first use. Goal: avoid the context bloat (every server's full tool schema injected up front) and process bloat (every server spawned at launch) of wiring servers directly.

## Commands

```bash
uv sync --dev                       # install deps (incl. dev group)
uv run pytest                       # run tests
uv run pytest tests/test_smoke.py::test_imports   # single test
uv run ruff check .                 # lint
uv run ruff format .                # format (check-only: --check)
uv run pre-commit install           # optional git hooks (ruff lint+format)

uv run mcp-hub list                 # exercise the CLI against your config
uv run mcp-hub server               # run the MCP server (stdio) — what hosts launch
uv run mcp-hub -v server            # DEBUG logging to stderr + log file

# Server-management helpers (the "agent workflow" for editing config)
uv run mcp-hub config path          # show resolved config sources + the YAML-first write target
uv run mcp-hub add <name> --from-json '<docs snippet>'   # scaffold an entry, auto-moving secrets to auth.secrets
uv run mcp-hub validate             # lint specs; non-zero exit if a raw secret is in env/headers
uv run mcp-hub skill show           # print the bundled "managing MCP servers" guide
uv run mcp-hub skill install        # copy the skill into .claude/skills/ (--client cursor for Cursor)
```

CI (`.github/workflows/test.yml`) runs `ruff check`, `ruff format --check`, and `pytest` on every push/PR to `main`. Releases are automated via python-semantic-release driven by Conventional Commits — `fix:` → patch, `feat:` → minor, `feat!:`/`BREAKING CHANGE:` → major; `chore:`/`docs:`/`refactor:`/`test:` → no release. The version lives in `mcp_hub/__init__.py:__version__` and is bumped by the release bot, not by hand. This package is **never** published to PyPI (the `Private :: Do Not Upload` classifier in `pyproject.toml` is a deliberate guard); distribution is Git + GitHub Releases only.

## Architecture

The single entry point is `mcp_hub.cli:main`. `mcp-hub <command>` is the Click CLI; `mcp-hub server` lazily imports and runs `server.py`. CLI and server share the same config loading, so `CONFIG_FILE` points both at the same sources.

**Request flow:** host → `server.py` (registers MCP handlers on the low-level `Server`) → `tools.py` (`handle_tool` dispatch for the meta-tools) → `proxy.py` (`ProxyClient` gets/spawns a child `ClientSession`) → child server.

### Key modules

- **`config.py`** — loads + merges JSON/YAML config sources into `dict[str, ServerSpec]`. Later sources override earlier by server name. Accepts both wrapped (`{"mcpServers": {...}}`) and unwrapped shapes. `compute_config_hash()` produces the catalog cache key. `ServerSpec.transport` is derived: `url` present → `streamable-http` (or `sse`), else `stdio`.
- **`proxy.py`** — `ProxyClient` is the lazy connection pool. **Critical invariant:** each child connection is owned by a dedicated `_SessionHolder` task that opens the transport, initializes the session, parks on a shutdown event, and tears down *in the same task*. This is required because anyio cancel scopes (used inside `stdio_client` etc.) must be exited in the task that entered them — do not refactor this into a shared `AsyncExitStack` across tasks or you'll orphan child processes. `session(name)` is the spawn-on-first-use entry; errors drop the holder so the next call retries fresh.
- **`tools.py`** — defines the 8 meta-tools (`get_hub_tools`) and dispatches them (`handle_tool`): `list_servers`, `get_server_tools`, `call_tool`, `search`, `reload`, `authenticate`, `auth_status`. This is the model-facing surface.
- **`state.py`** — `HubState` holds catalog, proxy, and the captured host `ServerSession`. The SDK only creates the host session once a request arrives, so background notifications produced before then are buffered in a bounded deque (`PENDING_BUFFER_MAX`) and flushed on `capture_host_session`. Also gates cold-start enumeration via `_first_enumeration_done`.
- **`auth.py`** — keychain-native secrets via `keyring` under service name `mcp-hub`, key `server:env_var`. **Schema-as-source-of-truth:** only env vars named in a server's declared or learned auth schema are injected into the child env on spawn (see `proxy._open_transport`). Learned schemas persist to `$XDG_STATE_HOME/mcp-hub/learned-auth.json`.

  **Secret-vs-env classification** (when translating a server's docs into config — what goes in `auth.secrets` vs. plaintext `env`/`headers`):
  - `auth.secrets` (keychain): API keys, tokens, passwords, client secrets, and connection strings with embedded credentials.
  - Plaintext `env`/`headers`: non-sensitive config like base URLs, hostnames, account/team IDs, emails, regions, log paths, feature flags (e.g. `JIRA_API_BASE_URL`, `SLACK_TEAM_ID`).
  - Per-user-but-not-secret values still belong in `auth.secrets` with `sensitive: false` (so they're per-user and kept out of the config file, but terminal input isn't masked) — see `newrelic`/`argo` in real configs.

  This rule is **enforced in code**, not just convention: `cli.py`'s `_SECRET_KEY_RE` (matches `TOKEN|SECRET|PASSWORD|PASSWD|PWD|KEY|CONNECTION_STRING|CREDENTIAL`) drives `mcp-hub add --from-json`, which auto-moves matching env vars into `auth.secrets`, and `mcp-hub validate`, which fails when a raw secret value sits in plaintext `env`/`headers`.
- **`cli.py`** — the Click front-end. Beyond discovery/invoke commands, it hosts the config-editing helpers (`config path`, `validate`, `add`) and the `skill` group (`list`/`show`/`install`). New config is written **YAML-first** to the highest-precedence existing source, or `~/.config/mcp-hub/servers.yml` if none exists; existing files keep their format. `skill install` defaults to Claude (`.claude/skills/`); Cursor is opt-in via `--client cursor`.
- **`skills/mcp-hub/` (package data)** — a bundled "managing MCP servers" agent skill (`SKILL.md` + `reference.md`) that ships in the wheel/sdist (hatchling includes everything under `mcp_hub/`). The `skill` CLI reads it via `importlib.resources`, and `build_instructions` (`instructions.py`) always points the model at `mcp-hub skill show`. There is intentionally **no** first-party MCP prompt/resource for this, since that would un-gate the `any_exposed` prompts/resources capability for every host.
- **`startup.py`** + **`catalog.py`** — prompt/resource enumeration is opt-in per server (`expose_prompts`/`expose_resources`). Exposed metadata is cached on disk (`~/.cache/mcp-hub/catalog.json`, keyed by config hash). Warm start serves the cache instantly; cold start runs a background recovery daemon with exponential backoff and emits `list_changed` as servers come online.
- **`namespace.py`** — flat-namespace encoding so exposed prompts/resources round-trip to the right child: prompts `server__prompt` (sep `__`), resources `mcphub://server/<percent-encoded-original-uri>`.
- **`relay.py` / `roots.py` / `logging_relay.py` / `completions.py`** — bidirectional capability relays (sampling, elicitation, roots, logging, completions) wired as per-child session callbacks in `server.py` via `proxy.set_session_callbacks`. `recommender.py` uses host sampling for `recommend_servers` with a catalog-dump fallback.

### Things to keep in mind when editing

- Prompt/resource MCP handlers are only wired when at least one server is exposed (`any_exposed` in `server.py`); otherwise the hub advertises tools only. Adding the *first* exposed server changes advertised capabilities and requires a host reconnect. Under mcp 2.0 the lowlevel `Server` takes handlers as `on_*` constructor kwargs (`(ctx, params)` in, a full result object out) rather than the 1.x decorators — advertised capabilities follow from which kwargs you pass, so the `any_exposed` conditional is what gates them.
- Handlers get the host `ServerSession` from the `ServerRequestContext` the SDK passes in; there is no `request_ctx` contextvar any more. `server.py` calls `capture_host_session(ctx.session)` at the handler boundary so the handler modules stay free of SDK context plumbing.
- `set_session_callbacks` must be installed before any `session()` call — existing holders don't pick up changes.
- stdout is the stdio JSON-RPC channel; all logging goes to stderr + `~/Library/Logs/mcp-hub.log` (`MCP_HUB_LOG_FILE`). Never `print` to stdout from the server path.
- Tests: `test_smoke.py` (imports/version) plus `test_cli_tooling.py` (the `add`/`validate`/`config path`/`skill` behavior, secret inference, and round-tripping through `load_servers()`). Async tests run under `asyncio_mode = "auto"` (pytest-asyncio).
- **YAML-first config.** Examples, generated config, and new-file creation default to YAML (`~/.config/mcp-hub/servers.yml`); existing files keep their format. JSON is still accepted and written when the target is a `.json` file.
