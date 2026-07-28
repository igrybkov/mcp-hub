#!/usr/bin/env python3
"""MCP Hub server entry point.

Loads server configs, builds a dynamic instructions string that lists the
configured servers (so the LLM is aware of available capabilities without
having to call a discovery tool), and serves the hub's tools + optionally
proxied prompts and resources over stdio.

Prompts and resources are only surfaced for servers with `expose_prompts: true`
or `expose_resources: true` in config. Everything else stays opaque behind
the meta-tools (list_servers, get_server_tools, call_tool, search).
"""

from __future__ import annotations

import asyncio
import logging
import os

from dotenv import load_dotenv
from mcp import types
from mcp.server import NotificationOptions, Server
from mcp.server.context import ServerRequestContext
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import UrlElicitationRequiredError

from mcp_hub.catalog import DEFAULT_CATALOG_PATH, Catalog
from mcp_hub.completions import handle_complete
from mcp_hub.config import compute_config_hash, load_servers
from mcp_hub.instructions import build_instructions
from mcp_hub.logging_relay import handle_set_logging_level, make_logging_callback
from mcp_hub.prompts import handle_get_prompt, handle_list_prompts
from mcp_hub.proxy import ProxyClient
from mcp_hub.relay import make_elicitation_callback, make_sampling_callback
from mcp_hub.resources import (
    handle_list_resource_templates,
    handle_list_resources,
    handle_read_resource,
)
from mcp_hub.roots import handle_roots_list_changed, make_list_roots_callback
from mcp_hub.startup import run_startup
from mcp_hub.state import HubState
from mcp_hub.tools import get_hub_tools, handle_tool

load_dotenv()

logger = logging.getLogger(__name__)


def _configure_logging(verbose: bool = False) -> None:
    """Set up file + stderr logging for the server process.

    Done in ``run()`` rather than at import time so that importing this module
    (e.g. lazily from the ``mcp-hub server`` CLI subcommand) has no side
    effects, and so we override any logging the CLI front-end already
    configured (hence ``force=True``). The stream handler targets stderr,
    keeping stdout clean for the stdio JSON-RPC channel.

    ``verbose`` (propagated from the CLI's -v/--verbose flag) logs at DEBUG
    instead of the default INFO.
    """
    log_file = os.getenv("MCP_HUB_LOG_FILE", os.path.expanduser("~/Library/Logs/mcp-hub.log"))
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[logging.FileHandler(log_file), logging.StreamHandler()],
        force=True,
    )


async def _main() -> None:
    servers = load_servers()
    logger.info("Loaded %d server(s)", len(servers))
    instructions = build_instructions(servers)

    config_hash = compute_config_hash()
    catalog = Catalog(DEFAULT_CATALOG_PATH)
    warm = catalog.load(config_hash)
    catalog.set_config_hash(config_hash)

    any_exposed = any(s.is_exposed for s in servers.values())

    async with ProxyClient(servers) as proxy:
        state = HubState(servers=servers, catalog=catalog, proxy=proxy)

        # Install per-child callbacks now that state exists. Must be set
        # before any session() call — nothing has opened a session yet at
        # this point (holders spawn from handlers + startup, both below).
        proxy.set_session_callbacks(
            lambda name: {
                "logging_callback": make_logging_callback(state, name),
                "list_roots_callback": make_list_roots_callback(state, name),
                "sampling_callback": make_sampling_callback(state, name),
                "elicitation_callback": make_elicitation_callback(state, name),
            }
        )

        # Warm start: serve cached catalog immediately, refresh in background.
        # Cold start: handlers will wait up to the soft timeout for the
        # background enumeration to settle.
        if warm:
            state.mark_enumeration_done()
            logger.info(
                "warm start: loaded catalog for %d server(s)",
                len(catalog.server_names()),
            )
        else:
            logger.info("cold start: no cache (or config changed) — enumerating")

        # mcp 2.0 hands every handler a ServerRequestContext instead of
        # exposing the old `request_ctx` contextvar. `ctx.session` is the host
        # ServerSession we need for background notifications, so each handler
        # captures it on the way in — idempotent, and it lets the handler
        # modules stay free of SDK context plumbing.
        def _capture(ctx: ServerRequestContext) -> None:
            state.capture_host_session(ctx.session)

        async def on_list_tools(
            ctx: ServerRequestContext, params: types.PaginatedRequestParams | None
        ) -> types.ListToolsResult:
            _capture(ctx)
            return types.ListToolsResult(tools=get_hub_tools())

        async def on_call_tool(
            ctx: ServerRequestContext, params: types.CallToolRequestParams
        ) -> types.CallToolResult:
            _capture(ctx)
            try:
                content = await handle_tool(
                    params.name, params.arguments or {}, servers, proxy, state=state
                )
            except UrlElicitationRequiredError:
                # Carries its own protocol-level handling (error code -32042).
                raise
            except Exception as exc:
                # 1.x's `@call_tool()` decorator turned any handler exception
                # into an isError result; 2.0's `on_call_tool` does not. Without
                # this an unexpected raise reaches the host as a JSON-RPC error
                # — a failed *request* rather than a failed *tool call* — which
                # hosts surface far more harshly. Keep the old contract.
                logger.exception("call_tool(%s) failed", params.name)
                return types.CallToolResult(
                    content=[types.TextContent(type="text", text=str(exc))],
                    is_error=True,
                )
            return types.CallToolResult(content=list(content))

        async def on_set_logging_level(
            ctx: ServerRequestContext, params: types.SetLevelRequestParams
        ) -> types.EmptyResult:
            _capture(ctx)
            await handle_set_logging_level(state, params.level)
            return types.EmptyResult()

        # Fan the host's roots/list_changed out to every connected child.
        async def on_roots_list_changed(
            ctx: ServerRequestContext, params: types.NotificationParams | None
        ) -> None:
            _capture(ctx)
            await handle_roots_list_changed(state)

        async def on_list_prompts(
            ctx: ServerRequestContext, params: types.PaginatedRequestParams | None
        ) -> types.ListPromptsResult:
            _capture(ctx)
            return types.ListPromptsResult(prompts=await handle_list_prompts(state))

        async def on_get_prompt(
            ctx: ServerRequestContext, params: types.GetPromptRequestParams
        ) -> types.GetPromptResult:
            _capture(ctx)
            return await handle_get_prompt(state, params.name, params.arguments)

        async def on_list_resources(
            ctx: ServerRequestContext, params: types.PaginatedRequestParams | None
        ) -> types.ListResourcesResult:
            _capture(ctx)
            return types.ListResourcesResult(resources=await handle_list_resources(state))

        async def on_list_resource_templates(
            ctx: ServerRequestContext, params: types.PaginatedRequestParams | None
        ) -> types.ListResourceTemplatesResult:
            _capture(ctx)
            return types.ListResourceTemplatesResult(
                resource_templates=await handle_list_resource_templates(state)
            )

        async def on_read_resource(
            ctx: ServerRequestContext, params: types.ReadResourceRequestParams
        ) -> types.ReadResourceResult:
            _capture(ctx)
            return await handle_read_resource(state, params.uri)

        async def on_completion(
            ctx: ServerRequestContext, params: types.CompleteRequestParams
        ) -> types.CompleteResult:
            _capture(ctx)
            completion = await handle_complete(state, params.ref, params.argument, params.context)
            # `handle_complete` returns None when the ref isn't ours to route
            # or the child has no completion support. 1.x's decorator turned
            # that into an empty completion; 2.0 wants a result object, so do
            # the same explicitly rather than sending None over the wire.
            if completion is None:
                return types.CompleteResult(completion=types.Completion(values=[]))
            return types.CompleteResult(completion=completion)

        # Prompt/resource handlers are only wired when at least one server opts
        # in. If none do, the hub advertises only tools (exactly as before), so
        # the host's UI stays clean. Under mcp 2.0 the advertised capabilities
        # follow from which `on_*` handlers are supplied, so passing None here
        # is what gates them — the same role the decorators played in 1.x.
        exposed_handlers = (
            {
                "on_list_prompts": on_list_prompts,
                "on_get_prompt": on_get_prompt,
                "on_list_resources": on_list_resources,
                "on_list_resource_templates": on_list_resource_templates,
                "on_read_resource": on_read_resource,
                "on_completion": on_completion,
            }
            if any_exposed
            else {}
        )

        app: Server = Server(
            "mcp-hub",
            instructions=instructions,
            on_list_tools=on_list_tools,
            on_call_tool=on_call_tool,
            # Logging relay is always on — child log events surface to the host
            # regardless of whether prompts/resources are exposed, so long as
            # the child is connected for some reason.
            on_set_logging_level=on_set_logging_level,
            on_roots_list_changed=on_roots_list_changed,
            **exposed_handlers,
        )

        # Start the recovery daemon that enumerates exposed servers,
        # retries on failure, and emits list_changed when entries update.
        await run_startup(state)

        try:
            init_options = app.create_initialization_options(
                notification_options=NotificationOptions(
                    prompts_changed=any_exposed,
                    resources_changed=any_exposed,
                    tools_changed=False,
                ),
            )
            logger.info("Starting mcp-hub MCP server")
            async with stdio_server() as (read_stream, write_stream):
                await app.run(read_stream, write_stream, init_options)
        finally:
            await state.shutdown()


def run(verbose: bool = False) -> None:
    _configure_logging(verbose=verbose)
    asyncio.run(_main())


if __name__ == "__main__":
    run()
