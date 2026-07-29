"""Transport adapters, connection lifecycle, and method-not-found degradation.

None of this is reachable from the CLI tests: `_HttpAdapter` in particular is
exercised by no configured server (every real one is stdio), so it is only ever
covered here.
"""

from __future__ import annotations

import pytest
from mcp import MCPError, types
from mcp_hub.config import ServerSpec
from mcp_hub.proxy import (
    METHOD_NOT_FOUND,
    ProxyClient,
    _build_http_client,
    _HttpAdapter,
    _is_method_not_found,
    _open_transport,
    _SessionHolder,
)


class FakeSession:
    """Duck-typed ClientSession. `holder.session` is never isinstance-checked."""

    def __init__(self, *, raises: BaseException | None = None) -> None:
        self.raises = raises

    async def list_tools(self):
        return types.ListToolsResult(tools=[types.Tool(name="t", inputSchema={})])

    async def list_prompts(self):
        if self.raises:
            raise self.raises
        return types.ListPromptsResult(prompts=[types.Prompt(name="p")])

    async def list_resources(self):
        if self.raises:
            raise self.raises
        return types.ListResourcesResult(resources=[])

    async def list_resource_templates(self):
        if self.raises:
            raise self.raises
        return types.ListResourceTemplatesResult(resourceTemplates=[])


@pytest.fixture
def seeded():
    """A ProxyClient with a live-looking session, bypassing all transport code.

    Pre-seeding `_holders` is the seam that makes the proxy testable without
    spawning a child: `session()` returns any holder already in the registry.
    """

    def _seed(**session_kwargs):
        spec = ServerSpec(name="child", transport="stdio", command="true")
        proxy = ProxyClient({"child": spec})
        holder = _SessionHolder(spec)
        holder.session = FakeSession(**session_kwargs)
        holder.ready.set()
        proxy._holders["child"] = holder
        return proxy

    return _seed


# --- httpx client defaults ---


def test_http_client_matches_sdk_defaults() -> None:
    """`_build_http_client` must stay in step with the SDK's private factory.

    mcp 2.0 dropped `headers=` from the streamable-http transport, so we build
    the client ourselves rather than importing `create_mcp_http_client` from
    `mcp.shared._httpx_utils` (a private module). That copies three settings we
    do not own, and a silently stale copy would mean wrong timeouts in
    production. Comparing whole clients — not just our two constants — also
    catches the SDK *adding* a default we don't mirror.

    The private import lives in this body on purpose: if the SDK removes that
    module, exactly this test fails instead of the whole file failing to
    collect.
    """
    from mcp.shared._httpx_utils import create_mcp_http_client

    headers = {"Authorization": "Bearer token", "X-Tenant": "acme"}
    mine = _build_http_client(headers)
    theirs = create_mcp_http_client(headers=headers)

    assert mine.follow_redirects == theirs.follow_redirects
    assert mine.timeout == theirs.timeout
    for key in headers:
        assert mine.headers[key] == theirs.headers[key]


def test_http_client_overrides_httpx_defaults() -> None:
    """Pin the two settings whose httpx2 defaults would break MCP.

    httpx2 defaults `follow_redirects` to False and reads to 5s; an SSE stream
    held open for minutes would be cut off. Asserting the values directly means
    this still fails loudly if the SDK comparison above is ever deleted.
    """
    client = _build_http_client({"X-Test": "1"})

    assert client.follow_redirects is True
    assert client.timeout.read == pytest.approx(300.0)
    assert client.timeout.connect == pytest.approx(30.0)


# --- method-not-found degradation ---


def test_is_method_not_found_reads_the_public_code() -> None:
    """Five call sites depend on this to degrade gracefully to an empty list."""
    assert _is_method_not_found(MCPError(METHOD_NOT_FOUND, "Method not found")) is True
    assert _is_method_not_found(MCPError(-32000, "Connection closed")) is False


@pytest.mark.parametrize("method", ["list_prompts", "list_resources", "list_resource_templates"])
async def test_unsupported_primitive_degrades_to_empty(seeded, method: str) -> None:
    """A child that doesn't implement a primitive is normal, not an error.

    Most servers expose tools only. Surfacing -32601 would make every such
    server look broken during enumeration.
    """
    proxy = seeded(raises=MCPError(METHOD_NOT_FOUND, "Method not found"))

    assert await getattr(proxy, method)("child") == []


@pytest.mark.parametrize("method", ["list_prompts", "list_resources", "list_resource_templates"])
async def test_other_errors_still_propagate(seeded, method: str) -> None:
    """Only -32601 is swallowed; a real failure must not look like an empty list."""
    proxy = seeded(raises=MCPError(-32000, "Connection closed"))

    with pytest.raises(MCPError):
        await getattr(proxy, method)("child")


# --- session registry ---


async def test_tool_list_is_cached(seeded) -> None:
    proxy = seeded()

    first = await proxy.list_tools("child")
    proxy._holders["child"].session = None  # a second fetch would now explode

    assert await proxy.list_tools("child") == first


async def test_unknown_server_raises_key_error(seeded) -> None:
    with pytest.raises(KeyError, match="unknown server"):
        await seeded().session("ghost")


async def test_closed_proxy_refuses_new_sessions(seeded) -> None:
    proxy = seeded()
    async with proxy:
        pass

    with pytest.raises(RuntimeError, match="closed"):
        await proxy.session("child")


# --- transport selection ---


def test_stdio_without_command_is_rejected() -> None:
    with pytest.raises(ValueError, match="missing 'command'"):
        _open_transport(ServerSpec(name="x", transport="stdio"))


@pytest.mark.parametrize("transport", ["streamable-http", "sse"])
def test_url_transports_require_a_url(transport: str) -> None:
    with pytest.raises(ValueError, match="missing 'url'"):
        _open_transport(ServerSpec(name="x", transport=transport))


def test_unknown_transport_is_rejected() -> None:
    with pytest.raises(ValueError, match="unsupported transport"):
        _open_transport(ServerSpec(name="x", transport="carrier-pigeon"))


# --- http adapter lifecycle ---


class _Boom:
    """A transport whose __aenter__ fails, as an unreachable endpoint would."""

    async def __aenter__(self):
        raise RuntimeError("transport refused")

    async def __aexit__(self, *exc):
        return False


class _FakeTransport:
    """A transport that opens cleanly, standing in for a reachable endpoint.

    Faked rather than real so the suite stays offline: the real transport
    sends a DELETE on close when `terminate_on_close` is set.
    """

    async def __aenter__(self):
        return ("read", "write")

    async def __aexit__(self, *exc):
        return False


async def test_http_adapter_releases_its_client_on_close(monkeypatch) -> None:
    """We own the client we hand the transport — the SDK won't close it."""
    monkeypatch.setattr(
        "mcp_hub.proxy.streamable_http_client", lambda url, http_client=None: _FakeTransport()
    )
    adapter = _HttpAdapter("https://example.com/mcp", {"X-Test": "1"})

    async with adapter as streams:
        assert streams == ("read", "write"), "the 2-tuple must pass through unchanged"
        client = adapter._client
        assert client is not None and not client.is_closed

    assert client.is_closed
    assert adapter._client is None


async def test_http_adapter_releases_its_client_when_opening_fails(monkeypatch) -> None:
    """`async with` skips __aexit__ when __aenter__ raises, so the adapter has
    to release the client itself.

    Connect failures are routine — an unreachable URL, a rotated token — and
    the holder retries on the next call, so a client leaked per attempt would
    accumulate for as long as the server stays down.
    """
    monkeypatch.setattr(
        "mcp_hub.proxy.streamable_http_client", lambda url, http_client=None: _Boom()
    )
    adapter = _HttpAdapter("https://example.com/mcp", {"X-Test": "1"})
    opened = []
    real = _build_http_client
    monkeypatch.setattr(
        "mcp_hub.proxy._build_http_client", lambda h: opened.append(real(h)) or opened[-1]
    )

    with pytest.raises(RuntimeError, match="transport refused"):
        async with adapter:
            pass

    assert opened[0].is_closed, "the client we opened must be closed"
    assert adapter._client is None


async def test_http_adapter_without_headers_lets_the_sdk_own_the_client(monkeypatch) -> None:
    """No headers means no custom client — the transport manages its own."""
    seen = {}

    def fake(url, http_client=None):
        seen["http_client"] = http_client
        return _Boom()

    monkeypatch.setattr("mcp_hub.proxy.streamable_http_client", fake)

    with pytest.raises(RuntimeError):
        async with _HttpAdapter("https://example.com/mcp", None):
            pass

    assert seen["http_client"] is None
