# mcp-hub

**One MCP connection. Every server. Loaded only when needed.**

`mcp-hub` is a meta-[MCP](https://modelcontextprotocol.io) server that sits between your MCP host (Claude Code, Claude Desktop, Cursor, …) and all of your individual MCP servers. Instead of wiring a dozen servers directly into your client — each one spawning a process at launch and flooding the model's context with tool definitions — you connect to **a single hub** that exposes a handful of discovery tools and spawns child servers **lazily, on first use**.

[![Build & Test](https://github.com/igrybkov/mcp-hub/actions/workflows/test.yml/badge.svg)](https://github.com/igrybkov/mcp-hub/actions/workflows/test.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](pyproject.toml)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![semantic-release](https://img.shields.io/badge/semantic--release-conventional-e10079?logo=semantic-release)](https://github.com/semantic-release/python-semantic-release)
[![MCP](https://img.shields.io/badge/MCP-compatible-000?logo=anthropic)](https://modelcontextprotocol.io)

---

## Table of contents

- [Quick start](#quick-start)
- [Why mcp-hub?](#why-mcp-hub)
- [Architecture](#architecture)
- [Features](#features)
- [Installation](#installation)
- [Use mcp-hub in your client](#use-mcp-hub-in-your-client)
- [Agent skill](#agent-skill)
- [Configuration](#configuration)
- [Managing servers (agent workflow)](#managing-servers-agent-workflow)
- [Meta-tools](#meta-tools)
- [Authentication](#authentication)
- [Prompts & resources](#prompts--resources)
- [Advanced MCP support](#advanced-mcp-support)
- [CLI reference](#cli-reference)
- [How it works](#how-it-works)
- [File locations](#file-locations)
- [Development](#development)
- [Contributing](#contributing)
- [Releasing](#releasing)
- [Troubleshooting](#troubleshooting)
- [License](#license)

## Quick start

```bash
# 1. Install (from Git — see Installation for options)
uv tool install git+https://github.com/igrybkov/mcp-hub.git

# 2. Describe your servers
mkdir -p ~/.config/mcp-hub
cat > ~/.config/mcp-hub/servers.yml <<'YAML'
everything:
  command: npx
  args: ["-y", "@modelcontextprotocol/server-everything"]
  description: "Reference MCP server for testing"
  tags: [example]
YAML

# 3. Verify from the shell
mcp-hub list
mcp-hub tools everything --summary

# 4. Register the hub with your client (writes .mcp.json by default)
mcp-hub install
```

That's it — your host now talks to one server (`mcp-hub`), and `everything` only starts when the model actually calls one of its tools.

## Why mcp-hub?

Connecting many MCP servers directly to a host has two costs that grow with every server you add:

- **Context bloat.** Every server's full tool schema is injected into the model's context up front. Twenty servers can burn tens of thousands of tokens before the user types a word.
- **Process bloat.** Every server is spawned at startup, even the ones you won't touch this session — slow launches, idle Docker containers, wasted memory.

`mcp-hub` collapses all of that behind one connection:

| | Direct wiring | With mcp-hub |
| --- | --- | --- |
| Connections in the host | one per server | **one, total** |
| Tools in context at startup | all tools, all servers | **~8 meta-tools** |
| Child process spawn | eager, at launch | **lazy, on first use** |
| Add/remove a server | edit + restart the host | edit config + `reload` |
| Secrets | per-client env plumbing | **central OS keychain** |

The model discovers what it needs through cheap, progressive tool calls — and the hub only spawns the child servers a task actually touches.

## Architecture

```mermaid
flowchart LR
    Host["MCP Host<br/>Claude Code · Desktop · Cursor"]

    subgraph HUB["mcp-hub (single connection)"]
        direction TB
        Meta["Discovery meta-tools<br/>list · search · get · call · recommend"]
        Cat["Catalog cache<br/>prompts + resources"]
        Auth["Keychain auth"]
    end

    Host <==>|"stdio · ~8 tools"| HUB

    HUB -.->|spawn on first use| G["github"]
    HUB -.->|spawn on first use| J["jira"]
    HUB -.->|lazy| S["slack"]
    HUB -.->|lazy| N["… N servers"]
```

Child servers stay dormant until a tool call (or an opt-in prompt/resource enumeration) reaches them. The hub also relays the full duplex of MCP capabilities — sampling, elicitation, roots, logging, and completions — between the host and each child, so wrapping a server in the hub doesn't take features away.

## Features

- **Lazy proxying** — child servers spawn on first use, each in its own supervised connection task.
- **Progressive discovery** — `list_servers`, `get_server_tools` (summary or full schema), `search`, and `call_tool` let the model drill down without paying for every schema up front.
- **LLM-backed routing** — `recommend_servers` asks the host's own model (via MCP sampling) which servers fit a task, with a graceful fallback when sampling is unavailable.
- **Keychain-native auth** — secrets live in your OS keychain (via [`keyring`](https://github.com/jaraco/keyring)), are injected into child environments on spawn, and are collected through MCP elicitation so they never enter the model's context.
- **Opt-in prompts & resources** — surface a child's prompts/resources through the hub with a flat, namespaced view, backed by an on-disk catalog for instant warm starts and a self-healing recovery daemon for cold ones.
- **Full capability relay** — bidirectional sampling, elicitation, roots, logging, and completions pass through transparently.
- **Three transports** — `stdio`, `streamable-http`, and `sse` children.
- **Hot reload** — add, remove, or edit servers and pick up the change with a single `reload`, no host restart.
- **First-class CLI** — script everything (`list`, `tools`, `call`, `search`, `auth`, `add`, `validate`, `install`) with JSON output.
- **Bundled agent skill** — ships a "managing MCP servers" skill so an assistant can add, configure, and troubleshoot servers from a vendor's docs; install it with `mcp-hub skill install`.

## Installation

> **Note:** `mcp-hub` is not yet published to PyPI. Install from Git for now.

Requires **Python 3.11+**.

```bash
# Recommended: install as an isolated tool with uv
uv tool install git+https://github.com/igrybkov/mcp-hub.git

# Run ephemerally without installing (great for trying it out)
uvx --from git+https://github.com/igrybkov/mcp-hub.git mcp-hub list

# Or with pip / pipx
pip install git+https://github.com/igrybkov/mcp-hub.git
pipx install git+https://github.com/igrybkov/mcp-hub.git
```

A single `mcp-hub` entry point is installed, with two roles:

- `mcp-hub <command>` — the CLI (`list`, `tools`, `call`, `auth`, `install`, …).
- `mcp-hub server` — the MCP server (stdio) your host launches.

### From source

```bash
git clone https://github.com/igrybkov/mcp-hub.git
cd mcp-hub
uv sync --dev
uv run mcp-hub list
```

## Use mcp-hub in your client

### With the `install` command

`install` writes (or updates) an `mcpServers` entry and **auto-detects the runner** from how you launched it. If you ran via `uvx --from <spec>`, it reuses the same `--from` spec so the generated entry matches exactly; otherwise it writes a plain `mcp-hub server`.

```bash
# Claude Code — project-level (.mcp.json in CWD, checked into the repo)
mcp-hub install

# User-level / other clients — point at any config file
mcp-hub install --config ~/.mcp.json
mcp-hub install --config ~/Library/Application\ Support/Claude/claude_desktop_config.json
mcp-hub install --config ~/.cursor/mcp.json

# Preview without writing
mcp-hub install --config .mcp.json --dry-run

# Force a specific runner ("mcp-hub server" is appended automatically)
mcp-hub install --runner 'uvx --from git+https://github.com/igrybkov/mcp-hub.git'
```

### Manual configuration

If you installed `mcp-hub` as a tool, the entry is simply:

```json
{
  "mcpServers": {
    "mcp-hub": {
      "command": "mcp-hub",
      "args": ["server"]
    }
  }
}
```

To run straight from Git without a prior install:

```json
{
  "mcpServers": {
    "mcp-hub": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/igrybkov/mcp-hub.git", "mcp-hub", "server"]
    }
  }
}
```

## Agent skill

`mcp-hub` ships a bundled [agent skill](https://docs.claude.com/en/docs/agents-and-tools/agent-skills/overview) that teaches an assistant the full lifecycle of managing servers — discover/search, add & configure from a vendor's docs (secrets to the keychain, never inline), authenticate, reload, verify, and troubleshoot. With it installed, you can just say *"add the MongoDB MCP to mcp-hub: <docs URL>"* or *"find me an MCP server for Postgres"* and the agent knows the rest.

```bash
# Install for Claude (default) — writes ./.claude/skills/mcp-hub/
mcp-hub skill install

# Install for Cursor — writes ./.cursor/skills/mcp-hub/
mcp-hub skill install --client cursor

# Or an explicit directory
mcp-hub skill install --dir ~/.config/skills

# Print the guide to stdout (no install) — pull it on demand or pipe to a file
mcp-hub skill show
mcp-hub skill list
```

The skill travels inside the package, so every install has it. It's also surfaced to the model automatically: the hub's instructions always point at `mcp-hub skill show`.

## Configuration

Servers are described in JSON or YAML. By default the hub merges these sources, in order, with **later sources overriding earlier ones by server name**:

1. `~/.config/mcp-hub/servers.json`
2. `~/.config/mcp-hub/servers.yml`
3. `./.mcp.local.json` (project-level, resolved from the working directory)
4. `./.mcp.local.yml`

Point the hub at different files with the `CONFIG_FILE` environment variable (comma-separated paths):

```bash
export CONFIG_FILE="~/.config/mcp-hub/servers.yml,./team-servers.yml"
```

Both the wrapped (`{"mcpServers": {…}}`) and unwrapped (top-level mapping) shapes are accepted, so you can reuse an existing `.mcp.json`-style file as-is.

### Examples

```yaml
# stdio child
github:
  command: gh-mcp
  args: ["--stdio"]
  env:
    GH_HOST: github.com
  description: "GitHub issues, PRs, and repos"
  tags: [dev, vcs]

# streamable-http child (default transport when `url` is set)
everything:
  url: https://everything.mcp.run/mcp
  headers:
    Authorization: "Bearer ${TOKEN}"

# sse child
metrics:
  url: https://metrics.example.com/sse
  transport: sse

# opt in to prompts/resources and give a slow (Docker) server more time
obsidian:
  command: docker
  args: ["run", "-i", "--rm", "obsidian-mcp"]
  expose_prompts: true
  expose_resources: true
  connect_timeout_seconds: 20

# temporarily turn a server off without deleting it
legacy:
  command: old-mcp
  disabled: true
```

### Field reference

| Field | Type | Applies to | Default | Description |
| --- | --- | --- | --- | --- |
| `command` | string | stdio | — | Executable to launch. |
| `args` | string[] | stdio | `[]` | Arguments passed to `command`. |
| `env` | map | stdio | `{}` | Extra environment for the child (merged over the hub's own env). |
| `url` | string | http/sse | — | Endpoint URL. Presence selects an HTTP transport. |
| `transport` | string | http/sse | `streamable-http` | `streamable-http` or `sse` (only when `url` is set). |
| `headers` | map | http/sse | `{}` | Headers sent with each request. |
| `description` | string | all | — | Shown in discovery and used for search/recommendations. |
| `tags` | string[] | all | `[]` | Free-form labels, matched by `list`/`search`. |
| `disabled` | bool | all | `false` | Skip this server entirely. |
| `expose_prompts` | bool | all | `false` | Surface the child's prompts through the hub. |
| `expose_resources` | bool | all | `false` | Surface the child's resources/templates through the hub. |
| `connect_timeout_seconds` | number | exposed | `5.0` | Per-server connect + enumerate budget. Raise for slow/Docker cold starts. |
| `auth.secrets` | list | all | — | Secret schema for keychain injection (see [Authentication](#authentication)). |

## Managing servers (agent workflow)

You rarely need to hand-edit config. `mcp-hub add` translates a vendor's docs snippet into the hub's shape and **moves likely-secret env vars into the keychain schema automatically** (the rule below), `validate` lints before you reload, and `config path` shows where things get written. The bundled [agent skill](#agent-skill) drives the whole flow.

```bash
mcp-hub config path        # resolved sources + the file `add` writes to

# Translate a docs snippet (wrapped or single-entry). Secret env vars matching
# *TOKEN/KEY/SECRET/PASSWORD/CONNECTION_STRING* (with a real value) are moved to
# auth.secrets and their raw values dropped; --keep-env-secrets opts out.
mcp-hub add mongodb \
  --from-json '{"mcpServers":{"MongoDB":{"command":"npx","args":["-y","mongodb-mcp-server@latest"],"env":{"MDB_MCP_CONNECTION_STRING":"mongodb+srv://user:pass@host/db"}}}}' \
  --arg --readOnly --description "MongoDB / Atlas" --tag database

# Or build from flags (no snippet):
mcp-hub add linear --command npx --arg -y --arg linear-mcp-server \
  --secret 'LINEAR_API_KEY:Linear API key:https://linear.app/settings/api'

mcp-hub auth provision mongodb   # store the secret(s) in the keychain
mcp-hub validate                 # lint; flags any raw secrets left in env/headers
# …then call the `reload` tool (or restart the host) and verify with `list`/`tools`/`call`.
```

**Secret-vs-env rule** — API keys, tokens, passwords, client secrets, and connection strings with embedded credentials belong in `auth.secrets` (keychain). Base URLs, hostnames, account/team IDs, emails, regions, and flags stay in plaintext `env`/`headers`. Run `mcp-hub skill show` for the full guide.

## Meta-tools

The hub exposes a small, fixed set of tools to the host. The model uses them to discover and reach everything else.

| Tool | What it does |
| --- | --- |
| `list_servers` | List configured servers with descriptions, tags, transport, and auth status. Optional substring `filter`. |
| `get_server_tools` | List a server's tools. `summary_only: true` for cheap discovery (~100 tokens); `tools: [names]` for full schemas of specific tools. Connects lazily. |
| `call_tool` | Invoke `tool` on `server` with `arguments`. Spawns the child on first call. |
| `search` | Keyword-rank across server metadata and *already-loaded* tool descriptions. |
| `recommend_servers` | Ask the host LLM to rank servers for a `task_description` (via sampling). Falls back to a catalog dump if sampling is unsupported. |
| `reload` | Re-read config and reconcile the server set, or reload a single `server`. Drops cached schemas and refreshes exposed catalogs. |
| `authenticate` | Collect and store a server's secrets in the OS keychain via elicitation, then refresh the session. |
| `auth_status` | Report auth state (authenticated / partial / unauthenticated) for one or all servers. |

### The discovery funnel

The intended flow keeps token usage low by only loading detail when it's needed:

```text
list_servers(filter?)              → which servers exist
        │
get_server_tools(server,           → which tools exist (names + descriptions only)
                 summary_only=true)
        │
get_server_tools(server,           → full input schema for the 1–2 tools you'll call
                 tools=[names])
        │
call_tool(server, tool, arguments) → run it (spawns the child if needed)
```

When you're unsure which server fits, `recommend_servers("deploy the staging branch")` returns a ranked shortlist with one-line rationales.

## Authentication

Secrets are **never** stored in config files or passed through the model. Instead the hub uses a *schema-as-source-of-truth* model:

1. A server declares which environment variables it needs in an `auth.secrets` block.
2. Values are stored in your OS keychain (`keyring`; macOS Keychain, Windows Credential Locker, Secret Service, …) under the service name `mcp-hub`.
3. On spawn, the hub injects **only** the declared secrets into the child's environment.

```yaml
linear:
  command: linear-mcp
  auth:
    secrets:
      - env_var: LINEAR_API_KEY
        label: "Linear API key"
        create_url: "https://linear.app/settings/api"
        sensitive: true        # default; masks terminal input
```

Each secret supports `env_var`, `label`, `create_url`, `sensitive` (default `true`), and `state` (`present` or `absent`; `absent` reconciles the value out of the keychain).

### Storing secrets

**From the assistant** (in-session, no terminal): call `authenticate` with the server name. The hub asks the host to prompt you via MCP elicitation, stores the answer in the keychain, and refreshes the session — the value never touches the model's context.

**From the shell:**

```bash
mcp-hub auth status                 # what's stored, what's missing
mcp-hub auth provision linear       # prompt for and store linear's secrets
mcp-hub auth provision linear --force  # overwrite stored secrets (rotated/expired keys)
mcp-hub auth provision --all        # provision every server with a schema
mcp-hub auth rm linear              # delete linear's stored secrets
mcp-hub auth rm linear LINEAR_API_KEY
```

By default `provision` skips secrets that are already in the keychain. Pass `--force` to re-prompt and overwrite them — use this to rotate an expired or revoked key. In-session, the `authenticate` tool takes an equivalent `force: true`.

### Learned schemas

If you provision secrets for a server that has no declared schema, the hub records a **learned schema** at `~/.local/state/mcp-hub/learned-auth.json` (honoring `XDG_STATE_HOME`). Promote it into your config to make it canonical:

```bash
mcp-hub auth promote linear   # prints the YAML auth block to paste into your config
```

## Prompts & resources

By default, child prompts and resources stay hidden behind the meta-tools — the host's UI stays clean. Set `expose_prompts: true` and/or `expose_resources: true` on a server to surface them natively, where they appear in one flat, namespaced list:

- Prompts: `obsidian__daily-note` (`<server>__<prompt>`)
- Resources: `mcphub://obsidian/<percent-encoded-original-uri>`

The hub decodes these on `get_prompt` / `read_resource` and routes back to the right child. Resource templates and argument **completions** are proxied too.

To make this fast and resilient, exposed metadata is cached on disk at `~/.cache/mcp-hub/catalog.json`:

- **Warm start** (cache valid for the current config): prompts/resources are served instantly.
- **Cold start** (no cache or config changed): the hub serves whatever has enumerated so far, then a background recovery daemon keeps trying — with exponential backoff (5s → 5min, jittered) for slow or flaky children — and emits `list_changed` as servers come online. Degraded servers keep serving their last-known-good entries.

The cache key is a hash of your config files, so editing config (or running `reload`) automatically invalidates stale entries.

## Advanced MCP support

Wrapping a server in the hub keeps its full feature set. The hub relays every MCP capability in both directions:

| Capability | Direction | Behaviour |
| --- | --- | --- |
| **Tools** | host → child | Proxied on demand via `call_tool`; child spawned lazily. |
| **Sampling** | child → host | Forwarded to the host LLM (`createMessage`). Also powers `recommend_servers`. |
| **Elicitation** | child → host | Forwarded to the host (`elicit`). Also powers `authenticate`. |
| **Roots** | child → host & host → children | `list_roots` proxied to the host; `roots/list_changed` fanned out to connected children. |
| **Logging** | child → host & host → children | Child log messages forwarded (prefixed with the server name); `setLevel` fanned out to connected children. |
| **Prompts** | child → host | Opt-in (`expose_prompts`); namespaced and cached. |
| **Resources** | child → host | Opt-in (`expose_resources`); namespaced and cached, templates included. |
| **Completions** | host → child | Proxied for exposed prompts and resource templates. |

Errors from children are mapped to clean JSON-RPC errors rather than crashing the hub, and capabilities a child doesn't implement degrade gracefully (e.g. "method not found" becomes "no suggestions").

## CLI reference

Run any command with `-h/--help` for details. Add `-v/--verbose` for debug logging to stderr — this also raises `mcp-hub server` from INFO to DEBUG (e.g. `mcp-hub -v server`, or `"args": ["-v", "server"]` in a client config). Most commands print JSON to stdout, so they compose well with `jq`.

```bash
# Discover
mcp-hub list                          # all configured servers
mcp-hub list --filter monitoring      # substring filter on name/description/tags
mcp-hub list --names-only             # one name per line (scripting)

mcp-hub tools <server>                # full tool schemas for a server
mcp-hub tools <server> --summary      # names + descriptions only
mcp-hub tools <server> --tool <name>  # full schema for specific tool(s)

mcp-hub search "deploy"               # search metadata + loaded tools
mcp-hub search "deploy" --load        # load every server's tools first (slow)

# Invoke
mcp-hub call <server> <tool> --args '{"key": "value"}'
mcp-hub call <server> <tool> --args-file ./args.json

# Auth
mcp-hub auth status [--server <name>]
mcp-hub auth provision <server> | --all [--force]
mcp-hub auth rm <server> [<ENV_VAR>]
mcp-hub auth promote <server>

# Manage config (see "Managing servers" above)
mcp-hub config path                   # resolved sources + write target
mcp-hub add <name> [--from-json '<snippet>'] [flags] [--dry-run]
mcp-hub validate [--config PATH]      # lint specs; non-zero exit on error

# Bundled agent skill
mcp-hub skill show [name]             # print SKILL.md to stdout (default: mcp-hub)
mcp-hub skill list                    # list bundled skills
mcp-hub skill install [name] [--client claude|cursor] [--dir PATH] [--force]

# Install into a client config
mcp-hub install [--config PATH] [--name KEY] [--runner CMD] [--dry-run]
```

## How it works

A few design choices worth knowing:

- **One task per connection.** Each child connection is owned by a dedicated supervisor task that opens the transport, initializes the `ClientSession`, parks until shutdown, then tears everything down in the *same* task. This respects an anyio constraint (cancel scopes must be exited in the task that entered them) and prevents orphaned child processes.
- **Schema-driven secret injection.** Only environment variables named in a server's (declared or learned) auth schema are pulled from the keychain and injected — nothing implicit leaks into a child.
- **Atomic, self-describing catalog.** Exposed prompts/resources are serialized (with metadata) and written atomically (tempfile + `os.replace`), so the hub never needs to re-call a child to reconstruct a listing, and a crash can't corrupt the cache.
- **Buffered notifications.** The host's `ServerSession` only exists once a request arrives, so background notifications produced during startup (e.g. a late server finishing enumeration) are buffered in a bounded queue and flushed the moment the host connects.

## File locations

| Path | Purpose | Override |
| --- | --- | --- |
| `~/.config/mcp-hub/servers.{json,yml}` | Default global config | `CONFIG_FILE` |
| `./.mcp.local.{json,yml}` | Project-level config (CWD) | `CONFIG_FILE` |
| `~/.cache/mcp-hub/catalog.json` | Cached exposed prompts/resources | — |
| `~/.local/state/mcp-hub/learned-auth.json` | Learned auth schemas | `XDG_STATE_HOME` |
| `~/Library/Logs/mcp-hub.log` | Server log (created on start) | `MCP_HUB_LOG_FILE` |
| OS keychain, service `mcp-hub` | Stored secrets | keyring backend |

## Development

```bash
uv sync --dev          # install dev dependencies
uv run pytest          # run the test suite
uv run ruff check .    # lint
uv run ruff format .   # format

# optional: install git hooks (ruff lint + format on commit)
uv run pre-commit install
```

The **Build & Test** workflow (GitHub Actions) runs `ruff check`, `ruff format --check`, and `pytest` on every push and pull request to `main`.

## Contributing

Contributions are welcome! Please:

1. Open an issue to discuss substantial changes first.
2. Keep PRs focused, and add or update tests where it makes sense.
3. Use Conventional Commit messages (they drive the automated release).
4. Make sure `uv run ruff check .`, `uv run ruff format --check .`, and `uv run pytest` pass before opening a PR.

## Releasing

Releases are fully automated with [python-semantic-release](https://python-semantic-release.readthedocs.io/) driven by [Conventional Commits](https://www.conventionalcommits.org/). When **Build & Test** passes on `main`, the **Publish** workflow computes the next version from commit messages, updates the changelog and version, tags the release, publishes a GitHub release, and attaches the built wheel and sdist to it as release assets.

Commit message prefixes that affect versioning:

- `fix:` → patch release
- `feat:` → minor release
- `feat!:` / `BREAKING CHANGE:` → major release
- `chore:`, `docs:`, `refactor:`, `test:`, … → no release

## Troubleshooting

- **A child won't start / connection error.** Run the child's `command` by hand to confirm it works, then `mcp-hub tools <server>` — the child's own stderr is surfaced. Detailed logs are at `~/Library/Logs/mcp-hub.log` (or `$MCP_HUB_LOG_FILE`).
- **Edited config isn't picked up.** Call the `reload` tool (or restart the host). Adding the *first* exposed server requires a host reconnect to register the prompts/resources capability.
- **A server needs more startup time.** Raise its `connect_timeout_seconds` (slow Docker images especially).
- **Auth says "partial".** One or more declared secrets aren't stored yet — run `mcp-hub auth provision <server>` or the `authenticate` tool.
- **A stored key expired or was rotated.** `provision` skips secrets that already exist; re-store with `mcp-hub auth provision <server> --force` (or `authenticate` with `force: true`).

## License

[MIT](LICENSE) © Illia Grybkov
