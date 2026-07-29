"""Handler wiring in `build_app`.

Under mcp 1.x the decorators on the lowlevel `Server` decided which
capabilities were advertised. mcp 2.0 replaced them with `on_*` constructor
kwargs, so *supplying a handler* is now what advertises the capability. That
makes `any_exposed` load-bearing in a way it wasn't before, and these tests
pin it — a host reconnect is required to change advertised capabilities, so
getting this wrong is not something a user can work around.
"""

from __future__ import annotations

import pytest
from mcp import types
from mcp.client import Client
from mcp.server.context import ServerRequestContext
from mcp_hub.catalog import Catalog
from mcp_hub.config import ServerSpec
from mcp_hub.proxy import ProxyClient
from mcp_hub.server import build_app
from mcp_hub.state import HubState


@pytest.fixture
def build(tmp_path):
    """Build an app from a single server, exposed or not.

    Returns (app, init_options, state) — the state so tests can assert on the
    host session the handlers capture.
    """

    def _build(*, exposed: bool):
        spec = ServerSpec(
            name="child",
            transport="stdio",
            command="true",
            expose_prompts=exposed,
            expose_resources=exposed,
        )
        servers = {"child": spec}
        proxy = ProxyClient(servers)
        state = HubState(
            servers=servers,
            catalog=Catalog(tmp_path / "catalog.json"),
            proxy=proxy,
        )
        app, init_options = build_app(servers, proxy, state, "instructions")
        return app, init_options, state

    return _build


# --- capability gating ---


def test_unexposed_config_advertises_tools_only(build) -> None:
    """With nothing exposed the hub must stay a tools-only server.

    Everything else is reachable through the meta-tools, and advertising empty
    prompt/resource lists would put dead entries in the host's UI.
    """
    _, init_options, _ = build(exposed=False)
    caps = init_options.capabilities

    assert caps.tools is not None
    assert caps.logging is not None
    assert caps.prompts is None
    assert caps.resources is None
    assert caps.completions is None


def test_exposed_config_advertises_prompts_and_resources(build) -> None:
    """One opted-in server flips prompts, resources and completions on.

    Completions ride along because they only make sense against exposed
    prompts and resource templates.
    """
    _, init_options, _ = build(exposed=True)
    caps = init_options.capabilities

    assert caps.tools is not None
    assert caps.logging is not None
    assert caps.prompts is not None
    assert caps.resources is not None
    assert caps.completions is not None


def test_notification_options_track_the_same_flag(build) -> None:
    """list_changed must agree with whether the capability is advertised.

    These are computed from one `any_exposed` inside `build_app` precisely so
    they cannot drift; before the extraction they were set 25 lines apart.
    """
    _, exposed, _ = build(exposed=True)
    _, bare, _ = build(exposed=False)

    assert exposed.capabilities.prompts.list_changed is True
    assert exposed.capabilities.resources.list_changed is True
    assert bare.capabilities.prompts is None
    assert bare.capabilities.resources is None


# --- handler behaviour ---


class FakeHostSession:
    """Duck-typed host ServerSession — only its identity matters here."""


def _ctx(session=None, method="tools/call"):
    return ServerRequestContext(
        session=session or FakeHostSession(),
        lifespan_context=None,
        protocol_version="2025-06-18",
        method=method,
    )


def _handler(app, method: str):
    entry = app.get_request_handler(method)
    assert entry is not None, f"no handler registered for {method}"
    return entry.handler


async def test_call_tool_captures_the_host_session(build) -> None:
    """The host session is only reachable from a request context.

    Background notifications (prompts/list_changed and friends) need it, and
    the SDK doesn't create it until the first request lands — so every handler
    grabs it on the way in.
    """
    app, _, state = build(exposed=False)
    host = FakeHostSession()

    await _handler(app, "tools/call")(
        _ctx(host), types.CallToolRequestParams(name="list_servers", arguments={})
    )

    assert state.host_session is host


async def test_handler_exception_becomes_a_tool_error(build) -> None:
    """A raising handler must come back as a failed tool call, not a dead request.

    mcp 1.x's `@call_tool()` decorator wrapped every handler exception into an
    `isError` result. 2.0's `on_call_tool` does not, so the hub re-adds it —
    without which an unexpected raise reaches the host as a JSON-RPC error,
    which hosts surface far more harshly than a tool that simply failed.
    """
    app, _, state = build(exposed=False)

    # `arguments` is missing the required "server" key, so dispatch raises.
    result = await _handler(app, "tools/call")(
        _ctx(), types.CallToolRequestParams(name="get_server_tools", arguments={})
    )

    assert result.is_error is True
    assert result.content, "the failure reason must reach the model"


async def test_unroutable_completion_becomes_an_empty_result(build) -> None:
    """`handle_complete` returns None when a ref isn't ours; the wire needs a result.

    1.x's decorator turned None into an empty completion. Sending None over the
    wire would be a protocol error over an optional feature.
    """
    app, _, state = build(exposed=True)

    result = await _handler(app, "completion/complete")(
        _ctx(method="completion/complete"),
        types.CompleteRequestParams(
            ref=types.PromptReference(type="ref/prompt", name="not-namespaced"),
            argument=types.CompletionArgument(name="a", value=""),
        ),
    )

    assert result.completion.values == []


# --- end to end ---


async def test_hub_serves_a_client_over_real_jsonrpc(build) -> None:
    """Drive the built app the way a host does, in-process.

    The tests above call handlers directly, which skips the initialize
    handshake and JSON-RPC framing — exactly the layer that broke on the 2.0
    bump. `mode="legacy"` is required: the default routes through a dispatcher
    that bypasses framing entirely and would not exercise any of it.
    """
    app, _, state = build(exposed=False)

    async with Client(app, mode="legacy") as client:
        capabilities = client.server_capabilities
        tools = await client.list_tools()
        result = await client.call_tool("list_servers", {})

    assert capabilities.tools is not None
    assert capabilities.prompts is None, "gating must survive a real handshake"
    assert len(tools.tools) == 8
    assert result.is_error is False
    assert state.host_session is not None, "captured through the real request path"
