"""Keep stdio connections on the initialize-handshake path for probing clients.

The mcp 2.x server fixes a connection's protocol era from its first request:
a first request carrying the 2026-07-28 `_meta` envelope locks the connection
to the modern era, after which a fallback `initialize` is refused with
UNSUPPORTED_PROTOCOL_VERSION. Some clients (e.g. Copilot CLI) probe with an
enveloped request and then fall back to `initialize` on the same connection.

`guard_handshake` refuses such a probe itself - with the same error a modern
server gives for an unserved version, listing the handshake versions - and
never forwards it, so the era stays undecided until the client's real opening
request. Only requests seen before `initialize` are affected.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import anyio
from mcp.shared.message import SessionMessage
from mcp_types import (
    ErrorData,
    JSONRPCError,
    JSONRPCRequest,
)
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS

PROTOCOL_VERSION_META_KEY = "io.modelcontextprotocol/protocolVersion"
UNSUPPORTED_PROTOCOL_VERSION_CODE = -32022


def _is_modern_probe(message: Any) -> bool:
    if not isinstance(message, JSONRPCRequest) or message.method == "initialize":
        return False
    meta = (message.params or {}).get("_meta")
    return isinstance(meta, dict) and PROTOCOL_VERSION_META_KEY in meta


@asynccontextmanager
async def guard_handshake(read_stream: Any, write_stream: Any) -> AsyncIterator[Any]:
    """Yield a read stream that withholds modern-envelope probes sent before `initialize`."""
    send, guarded = anyio.create_memory_object_stream[Any](0)

    async def pump() -> None:
        initialized = False
        async with send:
            async for item in read_stream:
                message = getattr(item, "message", None)
                if not initialized:
                    if _is_modern_probe(message):
                        error = JSONRPCError(
                            jsonrpc="2.0",
                            id=message.id,
                            error=ErrorData(
                                code=UNSUPPORTED_PROTOCOL_VERSION_CODE,
                                message="initialize handshake required; this connection serves handshake protocol versions",
                                data={
                                    "supported": list(HANDSHAKE_PROTOCOL_VERSIONS),
                                    "requested": message.params["_meta"][PROTOCOL_VERSION_META_KEY],
                                },
                            ),
                        )
                        await write_stream.send(SessionMessage(error))
                        continue
                    if isinstance(message, JSONRPCRequest) and message.method == "initialize":
                        initialized = True
                await send.send(item)

    async with anyio.create_task_group() as tg:
        tg.start_soon(pump)
        try:
            yield guarded
        finally:
            tg.cancel_scope.cancel()
