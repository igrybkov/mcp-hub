"""Roots fan-out.

Two directions need wiring:

1. **Child → Hub (roots/list request)**: a child server asks "what filesystem
   roots can I see?" The hub's `ClientSession` to that child has a
   `list_roots_callback` that proxies the question to the hub's own host
   session via `host_session.list_roots()` and returns the host's answer.

2. **Host → Hub → Children (roots/list_changed notification)**: when the
   host's roots change, it sends `notifications/roots/list_changed` to the
   hub. The hub observes this via a notification handler and calls
   `child_session.send_roots_list_changed()` on every currently-connected
   child so they can re-fetch.

Children that don't ask for roots (never call `list_roots()` on the hub) are
unaffected — the fan-out only triggers a notification, not a synchronous
request.
"""

from __future__ import annotations

import logging

from mcp import types
from mcp.shared.context import RequestContext

from mcp_hub.state import HubState

logger = logging.getLogger(__name__)


def make_list_roots_callback(state: HubState, server_name: str):
    """Return a `list_roots_callback` closure for a child ClientSession.

    When the child asks for roots, forward to the host session and return
    whatever the host provides. If the host isn't captured yet, or doesn't
    advertise roots support, return an empty list (safer than an error —
    children that proactively call list_roots shouldn't crash because the
    host happens to not support it).
    """

    async def callback(
        ctx: RequestContext,
    ) -> types.ListRootsResult | types.ErrorData:
        host = state.host_session
        if host is None:
            logger.debug(
                "child %r asked for roots before host session was captured; returning empty",
                server_name,
            )
            return types.ListRootsResult(roots=[])
        try:
            return await host.list_roots()
        except Exception as exc:
            logger.debug("list_roots failed forwarding from %r to host: %s", server_name, exc)
            return types.ListRootsResult(roots=[])

    return callback


async def handle_roots_list_changed(state: HubState) -> None:
    """Fan the host's `roots/list_changed` notification out to every connected child."""
    forwarded = 0
    for name in list(state.servers):
        holder = state.proxy._holders.get(name)  # noqa: SLF001 (intentional)
        if holder is None or holder.session is None:
            continue
        try:
            await holder.session.send_roots_list_changed()
            forwarded += 1
        except Exception as exc:
            logger.debug("roots/list_changed fan-out to %r failed: %s", name, exc)
    logger.info("forwarded roots/list_changed to %d connected child(ren)", forwarded)
