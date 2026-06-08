---
name: mcp-hub
description: Manage MCP servers with mcp-hub — discover/search servers, add and configure them from their docs, wire secrets to the OS keychain, reload, verify, and troubleshoot. Use when adding/removing/configuring an MCP server in mcp-hub, when the user says "add <X> mcp to mcp hub", or asks how to find or manage MCP servers.
---

# Managing MCP servers with mcp-hub

`mcp-hub` is a meta-MCP server: your host connects to **one** hub, and child
servers are spawned lazily on first use. You manage that fleet two ways, which
do the same things:

- **Meta-tools** (inside an MCP session): `list_servers`, `search`,
  `recommend_servers`, `get_server_tools`, `call_tool`, `reload`,
  `authenticate`, `auth_status`.
- **CLI** (in a shell): `mcp-hub list | search | tools | call | add | validate |
  config path | auth | reload-equivalent (restart) | skill`.

Secrets never go in config files — they live in the OS keychain. This is the
single most important rule when adding a server.

For exhaustive field docs, worked examples, and a troubleshooting matrix, read
[reference.md](reference.md) (or run `mcp-hub skill show mcp-hub`).

## 1. Discover what already exists (in the hub)

Use the cheap-to-expensive funnel; only load detail when you need it.

```text
list_servers(filter?)                  → which servers exist
search(query) / recommend_servers(task)→ find by keyword / let the LLM rank
get_server_tools(server, summary_only=true) → tool names + descriptions (~100 tokens)
get_server_tools(server, tools=[...])  → full input schema for the 1–2 tools you'll call
call_tool(server, tool, arguments)     → run it (spawns the child if needed)
```

CLI equivalents: `mcp-hub list [--filter X]`, `mcp-hub search "deploy"`,
`mcp-hub tools <server> --summary`, `mcp-hub tools <server> --tool <name>`,
`mcp-hub call <server> <tool> --args '{...}'`.

## 2. Find a *new* server to add

When the user wants a capability the hub doesn't have yet:

- **Vendor docs first.** Most tools publish an `mcpServers` JSON snippet — that
  is exactly what you translate in step 3.
- **Official servers repo:** `github.com/modelcontextprotocol/servers`.
- **MCP registry:** `registry.modelcontextprotocol.io` (and community lists).

Quick eval checklist before adding: **transport** (stdio vs http/sse), **auth**
needs (API key? OAuth?), **runtime** (`npx`/`uvx`/`docker` — affects cold-start
time), and **maintenance** (recent commits, official vs community).

## 3. Add a server (the main event)

Translate the vendor's docs snippet into mcp-hub's shape, moving every secret to
the keychain. `mcp-hub add` bakes in the secret-vs-env rule, so prefer it over
hand-editing.

**Step 1 — find the write target:**

```bash
mcp-hub config path        # shows config sources + the file `add` will write to
```

**Step 2 — translate the docs.** Given a vendor snippet like:

```json
{ "mcpServers": { "MongoDB": {
  "command": "npx",
  "args": ["-y", "mongodb-mcp-server@latest"],
  "env": { "MDB_MCP_CONNECTION_STRING": "mongodb+srv://user:pass@host/db" }
}}}
```

pass it straight to `add`. Likely-secret env vars (matching
`*TOKEN/KEY/SECRET/PASSWORD*` or `*CONNECTION_STRING*`, with a real value) are
auto-moved into `auth.secrets` and their raw values dropped:

```bash
mcp-hub add mongodb \
  --from-json '{"mcpServers":{"MongoDB":{"command":"npx","args":["-y","mongodb-mcp-server@latest"],"env":{"MDB_MCP_CONNECTION_STRING":"mongodb+srv://user:pass@host/db"}}}}' \
  --arg --readOnly \
  --description "MongoDB / Atlas — query collections, schema, aggregate." \
  --tag database --tag mongodb --tag atlas
```

The resulting YAML (secret in keychain schema, not inline):

```yaml
mongodb:
  command: npx
  args: ["-y", "mongodb-mcp-server@latest", "--readOnly"]
  auth:
    secrets:
      - env_var: MDB_MCP_CONNECTION_STRING
        label: "MongoDB connection string"
        create_url: "https://www.mongodb.com/docs/mcp-server/configuration/connection-string/"
  description: "MongoDB / Atlas — query collections, schema, aggregate."
  tags: [database, mongodb, atlas]
```

> Secret-vs-env rule: API keys, tokens, passwords, client secrets, and
> connection strings with embedded creds → `auth.secrets` (keychain). Base URLs,
> hostnames, account/team IDs, emails, regions, log paths, and flags → plaintext
> `env`. Per-user-but-not-secret values you still won't hardcode →
> `auth.secrets` with `sensitive: false`. See [reference.md](reference.md).

Use `--dry-run` to preview without writing. Use flag-only input when there's no
snippet (`--command`, `--arg`, `--env K=V`, `--url`, `--transport`,
`--header K=V`, `--secret ENV_VAR[:Label[:create_url]]`, …).

**Step 3 — store the secret(s):**

```bash
mcp-hub auth provision mongodb        # prompts for each secret, stores in keychain
```

In an MCP session use the `authenticate` tool instead (prompts via elicitation;
the value never enters the model's context).

**Step 4 — validate, then make it live:**

```bash
mcp-hub validate                      # lints specs; flags any raw secrets left in env/headers
```

Then call the **`reload`** tool (no host restart needed) — or restart the host.
Verify with `list_servers` / `mcp-hub list`, then `get_server_tools` /
`mcp-hub tools <name> --summary`, then a real `call_tool`.

## 4. Authenticate

```bash
mcp-hub auth status [--server X]          # what's stored / missing / partial
mcp-hub auth provision <server> [--force] # store (or re-store rotated/expired) secrets
mcp-hub auth promote <server>             # print YAML for a learned schema to paste into config
```

If you provision a server with no declared schema, the hub records a **learned
schema** (`~/.local/state/mcp-hub/learned-auth.json`); `auth promote` turns it
into canonical config. In-session, the `authenticate` tool collects secrets via
elicitation and refreshes the server session automatically.

## 5. Manage lifecycle

All by editing the server's config entry (find the file with `mcp-hub config
path`), then `reload`:

- **Disable temporarily:** add `disabled: true` (keeps the entry, skips the
  server).
- **Edit/update:** change `command`/`args`/`url`/`env`/`headers`/metadata, then
  `reload`.
- **Remove:** delete the entry; `reload`. Drop its secrets with
  `mcp-hub auth rm <server>`.
- **Expose prompts/resources:** add `expose_prompts: true` and/or
  `expose_resources: true`. Note: adding the *first* exposed server needs a host
  reconnect to register the capability.
- **Slow cold starts** (docker/uvx): raise `connect_timeout_seconds` (e.g. 20).

`reload` semantics: with no argument it re-reads all config files and reconciles
added/removed/changed servers; with `server: <name>` it only drops that server's
cached session/schemas (faster, no config re-read).

## 6. Troubleshoot

- **Child won't start:** run its `command` by hand; then `mcp-hub tools <server>`
  surfaces the child's stderr. Logs: `~/Library/Logs/mcp-hub.log`.
- **Auth says "partial":** a declared secret isn't stored — `mcp-hub auth
  provision <server>` (or `authenticate`).
- **Edited config not picked up:** call `reload` (or restart the host).
- **Slow startup / timeouts:** raise `connect_timeout_seconds`.
- **Validation fails on a raw secret:** move it to `auth.secrets` (re-run
  `mcp-hub add` or hand-edit) and provision it.

## 7. More detail

`mcp-hub skill show mcp-hub` prints this guide; the companion
[reference.md](reference.md) has the full field reference, more worked YAML
examples (stdio/http/docker), a troubleshooting matrix, and links for finding
and evaluating servers.
