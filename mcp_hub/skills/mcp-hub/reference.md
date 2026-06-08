# mcp-hub reference — adding & managing servers

Companion to [SKILL.md](SKILL.md). Everything here is detail you pull in only
when you need it.

## Config file format

Servers are described in JSON or YAML. The hub merges these sources in order,
**later overriding earlier by server name**:

1. `~/.config/mcp-hub/servers.json`
2. `~/.config/mcp-hub/servers.yml`
3. `./.mcp.local.json` (project-level, from CWD)
4. `./.mcp.local.yml`

Override with the `CONFIG_FILE` env var (comma-separated paths). Both the wrapped
(`{"mcpServers": {…}}`) and unwrapped (top-level mapping) shapes are accepted.
`mcp-hub config path` prints the resolved sources and the **write target** (the
highest-precedence existing file, or `~/.config/mcp-hub/servers.yml` if none
exist yet). `mcp-hub add` writes YAML by default and preserves an existing
file's format.

## Field reference

| Field | Type | Applies to | Default | Description |
| --- | --- | --- | --- | --- |
| `command` | string | stdio | — | Executable to launch. Mutually exclusive with `url`. |
| `args` | string[] | stdio | `[]` | Arguments passed to `command`. |
| `env` | map | stdio | `{}` | Extra environment (merged over the hub's env). **Plaintext only — no secrets.** |
| `url` | string | http/sse | — | Endpoint URL. Its presence selects an HTTP transport. |
| `transport` | string | http/sse | `streamable-http` | `streamable-http` or `sse` (only meaningful with `url`). |
| `headers` | map | http/sse | `{}` | Headers sent with each request. **No raw secrets.** |
| `description` | string | all | — | Shown in discovery; used for search/recommendations. Write a good one. |
| `tags` | string[] | all | `[]` | Free-form labels, matched by `list`/`search`. |
| `disabled` | bool | all | `false` | Skip this server entirely (keeps the entry). |
| `expose_prompts` | bool | all | `false` | Surface the child's prompts natively through the hub. |
| `expose_resources` | bool | all | `false` | Surface the child's resources/templates natively. |
| `connect_timeout_seconds` | number | exposed | `5.0` | Connect + enumerate budget. Raise for docker/uvx cold starts. |
| `auth.secrets` | list | all | — | Secret schema for keychain injection (see below). |

### `auth.secrets` entry

| Key | Required | Default | Description |
| --- | --- | --- | --- |
| `env_var` | yes | — | Env var name injected into the child on spawn. |
| `label` | no | `env_var` | Human label shown when prompting. |
| `create_url` | no | — | Where the user gets the value (shown in prompts). |
| `sensitive` | no | `true` | `true` masks terminal input; set `false` for per-user-but-not-secret values. |
| `state` | no | `present` | `absent` reconciles the value out of the keychain. |

## Secret-vs-env decision rules

**→ `auth.secrets` (keychain):** API keys, access tokens, refresh tokens,
passwords, client secrets, signing keys, and connection strings that embed
credentials (e.g. `mongodb+srv://user:pass@…`).

**→ plaintext `env` / `headers`:** base URLs, hostnames, account IDs, project/
team IDs (`SLACK_TEAM_ID`), emails, regions, log file paths, feature flags, and
API *base* URLs (`JIRA_API_BASE_URL`). Not secret, safe to commit.

**→ `auth.secrets` with `sensitive: false`:** per-user values you still don't
want hardcoded but that aren't secret (some account IDs, org slugs). Matches how
`newrelic`/`argo` are configured.

`mcp-hub add` auto-applies the first rule: env keys matching
`*TOKEN/KEY/SECRET/PASSWORD/CONNECTION_STRING/CREDENTIAL*` with a real
(non-placeholder) value are moved into `auth.secrets` and their values dropped.
`--keep-env-secrets` opts out. `mcp-hub validate` flags any that remain.

## Transport rules

- `url` present → HTTP. Leave `transport` unset for `streamable-http` (the
  default); add `transport: sse` **only** if the docs say SSE.
- `command` present → stdio. `transport` is ignored.
- `docker`/`uvx` cold starts can exceed the default 5s connect budget — raise
  `connect_timeout_seconds` when the server is exposed (prompts/resources).

## Worked examples

### stdio via npx, with a secret

```yaml
linear:
  command: npx
  args: ["-y", "linear-mcp-server"]
  auth:
    secrets:
      - env_var: LINEAR_API_KEY
        label: "Linear API key"
        create_url: "https://linear.app/settings/api"
  description: "Linear — issues, projects, cycles."
  tags: [project-management, issues]
```

```bash
mcp-hub add linear --command npx --arg -y --arg linear-mcp-server \
  --secret 'LINEAR_API_KEY:Linear API key:https://linear.app/settings/api' \
  --description "Linear — issues, projects, cycles." --tag issues
mcp-hub auth provision linear
```

### streamable-http with non-secret env + a secret header

```yaml
acme:
  url: https://acme.example.com/mcp
  description: "Acme platform API."
  tags: [saas]
  auth:
    secrets:
      - env_var: ACME_TOKEN
        label: "Acme API token"
```

For HTTP servers the hub injects declared secrets into the child's environment;
keep literal `headers` free of raw credentials (use `${VAR}`-style placeholders
or move auth to the child's own env handling).

### sse transport

```yaml
metrics:
  url: https://metrics.example.com/sse
  transport: sse
  description: "Streaming metrics."
```

### docker with a longer cold-start budget, exposing prompts/resources

```yaml
obsidian:
  command: docker
  args: ["run", "-i", "--rm", "obsidian-mcp"]
  expose_prompts: true
  expose_resources: true
  connect_timeout_seconds: 20
  description: "Obsidian vault — notes, daily-note prompt, resources."
  tags: [notes, knowledge]
```

### Secret inference from a docs snippet

Input:

```json
{ "mcpServers": { "Stripe": {
  "command": "npx",
  "args": ["-y", "@stripe/mcp"],
  "env": { "STRIPE_SECRET_KEY": "sk_live_abc123", "STRIPE_API_BASE": "https://api.stripe.com" }
}}}
```

`mcp-hub add stripe --from-json '<snippet>'` produces:

```yaml
stripe:
  command: npx
  args: ["-y", "@stripe/mcp"]
  env:
    STRIPE_API_BASE: https://api.stripe.com   # not secret → stays in env
  auth:
    secrets:
      - env_var: STRIPE_SECRET_KEY            # secret → moved to keychain, value dropped
        label: Stripe Secret Key
```

## Troubleshooting matrix

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `failed to connect — see server output` | Child binary missing / bad args | Run the `command` by hand; check `npx`/`docker` is installed; re-check `args`. |
| Connection times out | Slow cold start (docker/uvx) | Raise `connect_timeout_seconds`. |
| `auth_status` = `partial` | A declared secret isn't stored | `mcp-hub auth provision <server>` or `authenticate`. |
| `auth_status` = `unauthenticated` | No secrets stored | Same as above. |
| Child starts but tool calls 401/403 | Wrong/expired key | `mcp-hub auth provision <server> --force`. |
| Config edits ignored | Hub holds old config | Call `reload` (or restart host). |
| New exposed server's prompts missing | Capability not registered | Reconnect the host once (first exposed server only). |
| `validate` reports `raw_secret` | Secret left in `env`/`headers` | Move to `auth.secrets` (re-run `add` or hand-edit) + provision. |
| Edited a child's tools, hub shows old ones | Cached schemas | `reload` with `server: <name>`. |

Logs: `~/Library/Logs/mcp-hub.log` (override with `MCP_HUB_LOG_FILE`). Run any
CLI command with `-v` for debug output.

## Finding & evaluating servers

- **Official servers:** https://github.com/modelcontextprotocol/servers
- **Registry:** https://registry.modelcontextprotocol.io
- **Spec / concepts:** https://modelcontextprotocol.io

Evaluate before adding: transport (stdio simplest), auth (key vs OAuth flow),
runtime & cold-start cost (`npx`/`uvx`/`docker`), scope (read-only flags like
`--readOnly` are safer defaults), and maintenance (official vs community, recent
activity).

## Command quick reference

```bash
mcp-hub config path                          # sources + write target
mcp-hub add <name> [--from-json '<snippet>'] [flags] [--dry-run]
mcp-hub validate [--config PATH]             # lint; non-zero exit on error
mcp-hub auth status | provision <s> [--force] | rm <s> | promote <s>
mcp-hub list [--filter X] | search "<q>" | tools <s> --summary | call <s> <t> --args '{}'
mcp-hub skill show [name] | list | install [name] [--client claude|cursor] [--dir PATH]
```

In-session meta-tools mirror these: `list_servers`, `search`,
`recommend_servers`, `get_server_tools`, `call_tool`, `reload`, `authenticate`,
`auth_status`.
