"""Forward child log messages to the host, fan out log-level changes.

Each child ClientSession is constructed with a `logging_callback` that calls
into this module. The relay:

1. Captures every child `notifications/message` event.
2. Prefixes the `logger` name with the child-server name so the host can
   tell them apart (`[obsidian] vault sync error`).
3. Forwards via `host_session.send_log_message(...)`.

For the opposite direction, the host sends `logging/setLevel`; the hub
registers a handler that fans out to every *connected* child session. Not-yet-
connected children pick up the level when their session initializes — we
cache the level and (TODO in a future phase) reapply on connect.
"""

from __future__ import annotations

import logging

from mcp import McpError, types

from mcp_hub.proxy import _is_method_not_found
from mcp_hub.state import HubState

logger = logging.getLogger(__name__)


def make_logging_callback(state: HubState, server_name: str):
    """Return a per-child `logging_callback` closure.

    Passed to `ClientSession(..., logging_callback=...)` when the hub opens a
    connection. Fires on every `notifications/message` from the child.
    """

    async def callback(params: types.LoggingMessageNotificationParams) -> None:
        prefixed_logger = f"{server_name}:{params.logger}" if params.logger else server_name

        async def send(host):
            await host.send_log_message(
                level=params.level,
                data=params.data,
                logger=prefixed_logger,
            )

        # If host isn't captured yet, `enqueue_or_send` buffers the call until
        # the first handler invocation and then flushes — so logs emitted
        # during startup enumeration aren't dropped.
        state.enqueue_or_send(send)

    return callback


async def handle_set_logging_level(state: HubState, level: types.LoggingLevel) -> None:
    """Fan the host's requested log level out to every connected child.

    Children that aren't connected yet are skipped — they'll come online with
    their default level. A follow-up could cache the current level in
    HubState and apply on connect.
    """
    state.current_log_level = level  # cached for future connects (see proxy)
    forwarded = 0
    for name in list(state.servers):
        # Reach into the proxy's holder registry without forcing a connect —
        # only fan out to servers that already have a live session.
        holder = state.proxy._holders.get(name)  # noqa: SLF001 (intentional)
        if holder is None or holder.session is None:
            continue
        try:
            await holder.session.set_logging_level(level)
            forwarded += 1
        except McpError as exc:
            if _is_method_not_found(exc):
                logger.debug("%r has no logging capability, skipping", name)
                continue
            logger.warning("set_logging_level failed on %r: %s", name, exc)
        except Exception as exc:
            logger.warning("set_logging_level failed on %r: %s", name, exc)
    logger.info("forwarded logging/setLevel=%s to %d child(ren)", level, forwarded)
