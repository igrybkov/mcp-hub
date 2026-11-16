"""A modern-envelope probe must not lock the connection out of `initialize`."""

from __future__ import annotations

import anyio
from mcp.shared.message import SessionMessage
from mcp_hub.handshake_guard import PROTOCOL_VERSION_META_KEY, guard_handshake
from mcp_types import JSONRPCError, JSONRPCRequest


def _request(id_: int, method: str, params: dict) -> SessionMessage:
    return SessionMessage(JSONRPCRequest(jsonrpc="2.0", id=id_, method=method, params=params))


async def test_probe_is_refused_and_initialize_still_forwarded() -> None:
    in_send, in_recv = anyio.create_memory_object_stream[SessionMessage](10)
    out_send, out_recv = anyio.create_memory_object_stream[SessionMessage](10)

    probe = _request(0, "server/discover", {"_meta": {PROTOCOL_VERSION_META_KEY: "2026-07-28"}})
    init = _request(1, "initialize", {"protocolVersion": "2025-11-25"})
    await in_send.send(probe)
    await in_send.send(init)

    async with guard_handshake(in_recv, out_send) as guarded:
        forwarded = await guarded.receive()
        reply = await out_recv.receive()

    assert forwarded.message.method == "initialize"
    assert isinstance(reply.message, JSONRPCError)
    assert reply.message.id == 0
    assert reply.message.error.code == -32022
    assert "2025-11-25" in reply.message.error.data["supported"]


async def test_enveloped_requests_after_initialize_pass_through() -> None:
    in_send, in_recv = anyio.create_memory_object_stream[SessionMessage](10)
    out_send, _out_recv = anyio.create_memory_object_stream[SessionMessage](10)

    await in_send.send(_request(1, "initialize", {}))
    await in_send.send(
        _request(2, "tools/list", {"_meta": {PROTOCOL_VERSION_META_KEY: "2026-07-28"}})
    )

    async with guard_handshake(in_recv, out_send) as guarded:
        assert (await guarded.receive()).message.method == "initialize"
        assert (await guarded.receive()).message.method == "tools/list"
