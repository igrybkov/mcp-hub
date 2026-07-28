"""MCP tool definitions and dispatch for mcp-hub."""

from __future__ import annotations

import json
import logging
from typing import Any

from mcp import types

from mcp_hub.config import ServerSpec, compute_config_hash, load_servers
from mcp_hub.proxy import ProxyClient
from mcp_hub.search import search as do_search
from mcp_hub.startup import enumerate_once

logger = logging.getLogger(__name__)


def get_hub_tools() -> list[types.Tool]:
    """Return the mcp-hub tool definitions."""
    return [
        types.Tool(
            name="list_servers",
            description=(
                "List configured MCP servers with their descriptions and tags. "
                "Optionally filter by substring match on name/description/tags."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "filter": {
                        "type": "string",
                        "description": "Optional substring to filter on name, description, or tags.",
                    }
                },
            },
        ),
        types.Tool(
            name="get_server_tools",
            description=(
                "Get tools from a specific server. Lazily connects if not already "
                "connected. Use summary_only=true for cheap discovery (~100 tokens), "
                "then fetch full schemas for specific tools you plan to call."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "server": {"type": "string", "description": "Server name"},
                    "summary_only": {
                        "type": "boolean",
                        "description": "If true, return only tool names and descriptions (no input schemas).",
                        "default": False,
                    },
                    "tools": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "If set, return full schemas only for the named tools.",
                    },
                },
                "required": ["server"],
            },
        ),
        types.Tool(
            name="call_tool",
            description=(
                "Call a tool on a specific server. The server is spawned on first call. "
                "The tool must exist on the server — use get_server_tools to discover first."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "server": {"type": "string", "description": "Server name"},
                    "tool": {"type": "string", "description": "Tool name"},
                    "arguments": {
                        "type": "object",
                        "description": "Tool arguments as a JSON object.",
                        "additionalProperties": True,
                    },
                },
                "required": ["server", "tool"],
            },
        ),
        types.Tool(
            name="search",
            description=(
                "Search all configured servers and their known tools for a keyword. "
                "Returns ranked hits across server metadata and tool descriptions. "
                "Only searches servers whose tools have been loaded via get_server_tools "
                "or call_tool — server-level metadata is always searched."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Keywords (space-separated).",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 20).",
                        "default": 20,
                    },
                },
                "required": ["query"],
            },
        ),
        types.Tool(
            name="reload",
            description=(
                "Reload mcp-hub: re-reads config files and reconciles the "
                "server set (added/removed/changed), tears down stale "
                "child connections, and drops cached tool schemas. "
                "Use after editing the hub's config, or after a child "
                "server's tools have changed (code edits, new tool registered). "
                "If `server` is given, only that server is reloaded (faster — "
                "no config re-read). For exposed servers, the catalog is "
                "re-enumerated and `prompts/list_changed` + "
                "`resources/list_changed` notifications are emitted so the "
                "host re-fetches. Tool schema changes on non-exposed servers "
                "are picked up on the next `get_server_tools` call. Caveat: "
                "if the hub started with no exposed servers, the prompts/"
                "resources capabilities aren't registered for the session — "
                "adding an exposed server via reload won't surface until "
                "the host reconnects."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "server": {
                        "type": "string",
                        "description": "If set, only reload this server. Otherwise, reconcile all servers against config files.",
                    },
                },
            },
        ),
        types.Tool(
            name="authenticate",
            description=(
                "Authenticate a server by collecting and storing its required secrets "
                "in macOS Keychain. If the server has no auth schema, asks Claude to "
                "infer it. Uses MCP elicitation so secrets never enter the assistant's "
                "context. After storing, the server session is refreshed automatically. "
                "Set force=true to re-collect and overwrite secrets that are already "
                "stored (e.g. rotated or expired keys)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "server": {
                        "type": "string",
                        "description": "Server name to authenticate",
                    },
                    "force": {
                        "type": "boolean",
                        "description": "Re-collect and overwrite already-stored secrets (e.g. expired keys).",
                        "default": False,
                    },
                },
                "required": ["server"],
            },
        ),
        types.Tool(
            name="auth_status",
            description=(
                "Show authentication status for one or all servers with auth schemas. "
                "Returns per-server status: authenticated, partial, or unauthenticated."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "server": {
                        "type": "string",
                        "description": "If set, return status for this server only. Otherwise all servers.",
                    },
                },
            },
        ),
        types.Tool(
            name="recommend_servers",
            description=(
                "Given a natural-language task description, asks the host's LLM "
                "(via MCP sampling) to rank configured servers by relevance. "
                "Returns up to max_results recommendations with scores and "
                "rationale. Falls back to a raw catalog dump if the host "
                "doesn't support sampling. Use this when the user's request "
                "spans domains and you're not sure which server(s) to reach for."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "task_description": {
                        "type": "string",
                        "description": "Plain-English description of what the user wants to do.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Max recommendations to return (default 5).",
                        "default": 5,
                    },
                },
                "required": ["task_description"],
            },
        ),
    ]


# --- dispatch ---


def _filter_match(spec: ServerSpec, needle: str) -> bool:
    needle = needle.lower()
    fields = [spec.name, spec.description or "", " ".join(spec.tags)]
    return any(needle in f.lower() for f in fields)


def _server_summary(spec: ServerSpec) -> dict[str, Any]:
    from mcp_hub.auth import auth_status as get_auth_status
    from mcp_hub.auth import resolve_auth

    summary: dict[str, Any] = {
        "name": spec.name,
        "description": spec.description,
        "tags": spec.tags,
        "transport": spec.transport,
    }
    auth = resolve_auth(spec.name, spec.auth)
    if auth is not None:
        summary["auth"] = get_auth_status(spec.name, auth)
    return summary


def _tool_summary(tool: types.Tool) -> dict[str, Any]:
    return {"name": tool.name, "description": tool.description or ""}


def _tool_full(tool: types.Tool) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description or "",
        "inputSchema": tool.input_schema,
    }


def _text(payload: Any) -> list[types.TextContent]:
    if isinstance(payload, str):
        return [types.TextContent(type="text", text=payload)]
    return [types.TextContent(type="text", text=json.dumps(payload, indent=2, default=str))]


async def _handle_reload(
    arguments: dict[str, Any],
    servers: dict[str, ServerSpec],
    proxy: ProxyClient,
    state,
) -> list[types.TextContent]:
    """Reconcile the live server set against config files and/or drop caches."""
    # `servers` is shared by reference with ProxyClient — mutating in place
    # is visible to subsequent tool calls.

    target = arguments.get("server")
    if target is not None:
        if target not in servers:
            return _text({"error": f"unknown server: {target}"})
        await proxy.invalidate_server(target)
        # Re-merge learned auth schema in case it changed out-of-process
        from mcp_hub.auth import (
            load_learned as _load_learned,
        )
        from mcp_hub.auth import (
            reconcile_absent as _reconcile_absent,
        )
        from mcp_hub.auth import (
            resolve_auth as _resolve_auth,
        )

        learned_all = _load_learned()
        learned = learned_all.get(target)
        spec = servers[target]
        if learned is not None or spec.auth is not None:
            spec.auth = _resolve_auth(target, spec.auth)
            if spec.auth is not None:
                _reconcile_absent(target, spec.auth)
        result: dict[str, Any] = {"reloaded": target}
        if state is not None and servers[target].is_exposed:
            # Refresh catalog before notifying so the host's re-fetch sees the new state.
            try:
                await enumerate_once(state, servers[target])
            except Exception as exc:
                state.catalog.mark_degraded(target, str(exc))
                result["enumerate_error"] = str(exc)
            state.enqueue_or_send(lambda s: s.send_prompt_list_changed())
            state.enqueue_or_send(lambda s: s.send_resource_list_changed())
        return _text(result)

    try:
        new_servers = load_servers()
    except Exception as exc:
        logger.exception("reload: config reload failed")
        return _text({"error": f"config reload failed: {exc}"})

    old_names = set(servers.keys())
    new_names = set(new_servers.keys())
    added = sorted(new_names - old_names)
    removed = sorted(old_names - new_names)
    kept = old_names & new_names
    changed = sorted(n for n in kept if servers[n] != new_servers[n])

    for n in list(removed) + changed:
        await proxy.invalidate_server(n)

    # Mutate in place — ProxyClient holds the same dict reference.
    for n in removed:
        servers.pop(n, None)
    for n in added + changed:
        servers[n] = new_servers[n]

    if state is not None:
        state.catalog.set_config_hash(compute_config_hash())
        for n in removed:
            state.catalog.drop_server(n)

        enumerate_errors: dict[str, str] = {}
        for n in added + changed:
            spec = servers[n]
            if not spec.is_exposed:
                continue
            try:
                await enumerate_once(state, spec)
            except Exception as exc:
                state.catalog.mark_degraded(n, str(exc))
                enumerate_errors[n] = str(exc)

        # Fire unconditionally — covers the "exposed server was removed" case too.
        state.enqueue_or_send(lambda s: s.send_prompt_list_changed())
        state.enqueue_or_send(lambda s: s.send_resource_list_changed())

        payload: dict[str, Any] = {
            "added": added,
            "removed": removed,
            "changed": changed,
            "unchanged": sorted(kept - set(changed)),
        }
        if enumerate_errors:
            payload["enumerate_errors"] = enumerate_errors
        return _text(payload)

    return _text(
        {
            "added": added,
            "removed": removed,
            "changed": changed,
            "unchanged": sorted(kept - set(changed)),
        }
    )


async def _handle_authenticate(
    arguments: dict[str, Any],
    servers: dict[str, ServerSpec],
    proxy: ProxyClient,
    state,
) -> list[types.TextContent]:
    from mcp_hub.auth import (
        auth_status as get_auth_status,
    )
    from mcp_hub.auth import (
        get_secret,
        resolve_auth,
        set_secret,
    )

    server_name = arguments.get("server", "")
    if server_name not in servers:
        return _text({"error": f"unknown server: {server_name}"})

    force = bool(arguments.get("force", False))
    spec = servers[server_name]

    auth = resolve_auth(server_name, spec.auth)
    source = "declared"

    if auth is None:
        # Tier-2: no schema — return instructions for now (full sampling inference is future work)
        return _text(
            {
                "error": "no_auth_schema",
                "server": server_name,
                "message": (
                    f"Server '{server_name}' has no auth schema. "
                    f"Run `mcp-hub auth {server_name}` in a terminal to provision secrets interactively, "
                    f"then `mcp-hub auth promote {server_name}` to see the config to add to your profile."
                ),
            }
        )

    # Elicitation path
    host = state.host_session if state is not None else None
    stored_count = 0
    failed_secrets = []

    for secret in auth.secrets:
        if secret.state != "present":
            continue
        if get_secret(server_name, secret.env_var) is not None:
            stored_count += 1
            continue  # already stored, skip unless we want to re-auth

    # Check if all secrets are already stored (force re-collects regardless)
    present_secrets = [s for s in auth.secrets if s.state == "present"]
    all_stored = all(get_secret(server_name, s.env_var) is not None for s in present_secrets)
    if all_stored and present_secrets and not force:
        # Still invalidate to pick up fresh creds
        await proxy.invalidate_server(server_name)
        return _text(
            {
                "status": "already_authenticated",
                "server": server_name,
                "message": "All secrets already stored. Server session refreshed.",
                "session": "refreshed",
            }
        )

    if host is None:
        return _text(
            {
                "status": "elicitation_unavailable",
                "server": server_name,
                "message": f"Run `mcp-hub auth {server_name}` in a terminal to store secrets.",
            }
        )

    # Try elicitation for each missing secret (force re-collects stored ones too)
    for secret in present_secrets:
        if get_secret(server_name, secret.env_var) is not None and not force:
            continue  # already have it

        schema: dict[str, Any] = {
            "type": "object",
            "properties": {
                "value": {
                    "type": "string",
                    "title": secret.label,
                    **(
                        {"description": f"Get it at: {secret.create_url}"}
                        if secret.create_url
                        else {}
                    ),
                }
            },
            "required": ["value"],
        }
        message = f"Enter {secret.label} for server '{server_name}'"
        if secret.create_url:
            message += f"\n\nCreate one at: {secret.create_url}"

        try:
            result = await host.elicit_form(message=message, requested_schema=schema)
            # result is ElicitResult with action and content
            action = getattr(result, "action", None)
            if action == "cancel":
                return _text(
                    {
                        "status": "cancelled",
                        "server": server_name,
                        "message": f"Authentication cancelled by user at secret '{secret.label}'",
                    }
                )
            content = getattr(result, "content", None)
            if content and isinstance(content, dict):
                value = content.get("value", "")
            else:
                value = ""
            if not value:
                failed_secrets.append(secret.env_var)
                continue
            set_secret(server_name, secret.env_var, value)
        except Exception as exc:
            logger.warning("elicitation failed for %s/%s: %s", server_name, secret.env_var, exc)
            return _text(
                {
                    "status": "elicitation_unavailable",
                    "server": server_name,
                    "message": f"Elicitation not supported by this client. Run `mcp-hub auth {server_name}` in a terminal.",
                    "error": str(exc),
                }
            )

    if failed_secrets:
        return _text(
            {
                "status": "partial",
                "server": server_name,
                "failed": failed_secrets,
                "message": "Some secrets could not be collected.",
            }
        )

    # Invalidate server so next call spawns with new creds
    await proxy.invalidate_server(server_name)

    final_status = get_auth_status(server_name, auth)
    return _text(
        {
            "status": "authenticated",
            "server": server_name,
            "source": source,
            "session": "refreshed",
            "auth": final_status,
        }
    )


def _handle_auth_status(
    arguments: dict[str, Any],
    servers: dict[str, ServerSpec],
) -> list[types.TextContent]:
    from mcp_hub.auth import auth_status as get_auth_status
    from mcp_hub.auth import resolve_auth

    target = (arguments or {}).get("server")
    results = []

    check_servers = {target: servers[target]} if target and target in servers else servers
    if target and target not in servers:
        return _text({"error": f"unknown server: {target}"})

    for name, spec in sorted(check_servers.items()):
        auth = resolve_auth(name, spec.auth)
        if auth is None:
            continue
        status = get_auth_status(name, auth)
        results.append({"server": name, **status})

    return _text({"count": len(results), "servers": results})


async def handle_tool(
    name: str,
    arguments: dict[str, Any],
    servers: dict[str, ServerSpec],
    proxy: ProxyClient,
    state=None,
) -> list[types.TextContent]:
    """Dispatch a meta-tool call.

    `state` is optional so the CLI (which has no hub state) can reuse this
    function for the non-sampling tools. The `recommend_servers` tool needs
    state to reach the host session and will error out if state is None.
    """
    if name == "list_servers":
        needle = (arguments or {}).get("filter")
        matches = [
            _server_summary(s) for s in servers.values() if not needle or _filter_match(s, needle)
        ]
        matches.sort(key=lambda x: x["name"])
        return _text({"count": len(matches), "servers": matches})

    if name == "get_server_tools":
        server = arguments["server"]
        if server not in servers:
            return _text({"error": f"unknown server: {server}"})
        summary_only = bool(arguments.get("summary_only", False))
        filter_names = arguments.get("tools")
        try:
            tools = await proxy.list_tools(server)
        except Exception as e:
            logger.exception("list_tools failed for %s", server)
            return _text({"error": f"list_tools failed: {e}"})
        if filter_names:
            wanted = set(filter_names)
            tools = [t for t in tools if t.name in wanted]
            return _text({"server": server, "tools": [_tool_full(t) for t in tools]})
        if summary_only:
            return _text({"server": server, "tools": [_tool_summary(t) for t in tools]})
        return _text({"server": server, "tools": [_tool_full(t) for t in tools]})

    if name == "call_tool":
        server = arguments["server"]
        tool = arguments["tool"]
        tool_args = arguments.get("arguments") or {}
        if server not in servers:
            return _text({"error": f"unknown server: {server}"})
        try:
            result = await proxy.call_tool(server, tool, tool_args)
        except Exception as e:
            logger.exception("call_tool failed for %s/%s", server, tool)
            return _text({"error": f"call_tool failed: {e}"})
        # Pass through the underlying tool result content (text blocks primarily).
        passthrough: list[types.TextContent] = []
        for block in result.content:
            if isinstance(block, types.TextContent):
                passthrough.append(block)
            else:
                passthrough.append(types.TextContent(type="text", text=str(block)))
        if result.is_error:
            passthrough.insert(0, types.TextContent(type="text", text="[tool reported error]"))
        return passthrough

    if name == "search":
        query = arguments["query"]
        limit = int(arguments.get("limit", 20))
        # Use already-loaded tool cache — don't force eager connections
        hits = do_search(query, servers, proxy._tool_cache, limit=limit)
        return _text({"count": len(hits), "hits": [h.to_dict() for h in hits]})

    if name == "reload":
        return await _handle_reload(arguments or {}, servers, proxy, state)

    if name == "recommend_servers":
        if state is None:
            return _text(
                {"error": "recommend_servers requires host state (not available in CLI mode)"}
            )
        from mcp_hub.recommender import handle_recommend_servers

        return await handle_recommend_servers(state, arguments)

    if name == "authenticate":
        if state is None:
            return _text({"error": "authenticate requires hub state (not available in CLI mode)"})
        return await _handle_authenticate(arguments or {}, servers, proxy, state)

    if name == "auth_status":
        return _handle_auth_status(arguments or {}, servers)

    return _text({"error": f"unknown tool: {name}"})
