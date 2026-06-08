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
from typing import Any

from dotenv import load_dotenv
from mcp import types
from mcp.server import NotificationOptions, Server
from mcp.server.stdio import stdio_server
from pydantic import AnyUrl

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

    app: Server = Server("mcp-hub", instructions=instructions)

    @app.list_tools()
    async def list_tools() -> list[types.Tool]:
        return get_hub_tools()

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

        # Observe host roots/list_changed notifications and fan out to children.
        # This is a notification handler (not a request handler), so we plug
        # directly into the Server's notification_handlers dict.
        async def _on_roots_list_changed(_: types.RootsListChangedNotification) -> None:
            await handle_roots_list_changed(state)

        app.notification_handlers[types.RootsListChangedNotification] = _on_roots_list_changed

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

        @app.call_tool()
        async def call_tool(name: str, arguments: dict[str, Any]) -> list[types.TextContent]:
            return await handle_tool(name, arguments or {}, servers, proxy, state=state)

        # Prompt/resource handlers are only registered when at least one
        # server opts in. If none opt in, the hub advertises only tools
        # (exactly as before), so the host's UI stays clean.
        if any_exposed:

            @app.list_prompts()
            async def list_prompts() -> list[types.Prompt]:
                return await handle_list_prompts(state)

            @app.get_prompt()
            async def get_prompt(
                name: str, arguments: dict[str, str] | None
            ) -> types.GetPromptResult:
                return await handle_get_prompt(state, name, arguments)

            @app.list_resources()
            async def list_resources() -> list[types.Resource]:
                return await handle_list_resources(state)

            @app.list_resource_templates()
            async def list_resource_templates() -> list[types.ResourceTemplate]:
                return await handle_list_resource_templates(state)

            @app.read_resource()
            async def read_resource(uri: AnyUrl):
                return await handle_read_resource(state, uri)

            @app.completion()
            async def complete(
                ref,
                argument: types.CompletionArgument,
                context: types.CompletionContext | None,
            ):
                return await handle_complete(state, ref, argument, context)

        # Logging relay is always on — child log events surface to the host
        # regardless of whether prompts/resources are exposed, so long as the
        # child is connected for some reason (exposed enumeration, tool call).
        @app.set_logging_level()
        async def set_logging_level(level: types.LoggingLevel) -> None:
            await handle_set_logging_level(state, level)

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
