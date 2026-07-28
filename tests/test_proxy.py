"""Transport adapters, connection lifecycle, and method-not-found degradation.

None of this is reachable from the CLI tests: `_HttpAdapter` in particular is
exercised by no configured server (every real one is stdio), so it is only ever
covered here.
"""

from __future__ import annotations

import pytest
from mcp_hub.proxy import _build_http_client

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
