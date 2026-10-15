"""The meta-tool surface: schemas and dispatch.

`get_hub_tools()` is the hub's entire model-facing contract, and `handle_tool`
is the switchboard behind it. Both are pure enough to test without a child
server — `proxy` is just a parameter.
"""

from __future__ import annotations

import json

import pytest
from mcp import types
from mcp_hub.config import ServerSpec
from mcp_hub.tools import _text, _tool_full, _tool_summary, get_hub_tools, handle_tool


class StubProxy:
    def __init__(self, result=None) -> None:
        self.result = result
        self.calls: list[tuple] = []
        self._tool_cache: dict[str, list[types.Tool]] = {}

    async def call_tool(self, server, tool, arguments):
        self.calls.append((server, tool, arguments))
        return self.result

    async def list_tools(self, server):
        return [types.Tool(name="child_tool", description="d", inputSchema={"type": "object"})]


@pytest.fixture
def servers() -> dict[str, ServerSpec]:
    return {"child": ServerSpec(name="child", transport="stdio", command="true")}


@pytest.fixture(autouse=True)
def isolate_learned(monkeypatch, tmp_path):
    """Keep `list_servers` off the real learned-auth store and keychain."""
    monkeypatch.setattr("mcp_hub.auth.LEARNED_AUTH_PATH", tmp_path / "learned-auth.json")


def _payload(blocks: list[types.TextContent]) -> dict:
    return json.loads(blocks[0].text)


# --- tool schemas ---


def test_every_hub_tool_has_a_usable_schema() -> None:
    """Each tool is built with the `inputSchema` alias and read back by field.

    Construction by alias still works in mcp 2.0 but the camelCase attribute is
    gone, so this exercises both halves of that asymmetry in one pass.
    """
    tools = get_hub_tools()

    assert {t.name for t in tools} == {
        "list_servers",
        "get_server_tools",
        "call_tool",
        "search",
        "reload",
        "authenticate",
        "auth_status",
        "recommend_servers",
    }
    for tool in tools:
        assert _tool_full(tool)["inputSchema"]["type"] == "object"
        assert tool.description, f"{tool.name} needs a description — the model reads it"


def test_tool_summary_omits_the_schema() -> None:
    """Summary mode exists to keep discovery cheap; a schema would defeat it."""
    tool = get_hub_tools()[0]

    assert set(_tool_summary(tool)) == {"name", "description"}


def test_text_wraps_objects_as_json_and_strings_as_is() -> None:
    assert _text("hello")[0].text == "hello"
    assert json.loads(_text({"a": 1})[0].text) == {"a": 1}


# --- dispatch ---


async def test_unknown_tool_reports_an_error(servers) -> None:
    blocks = await handle_tool("no_such_tool", {}, servers, StubProxy())

    assert _payload(blocks)["error"] == "unknown tool: no_such_tool"


async def test_list_servers_returns_configured_servers(servers) -> None:
    blocks = await handle_tool("list_servers", {}, servers, StubProxy())
    payload = _payload(blocks)

    assert payload["count"] == 1
    assert payload["servers"][0]["name"] == "child"


async def test_call_tool_rejects_an_unknown_server(servers) -> None:
    blocks = await handle_tool(
        "call_tool", {"server": "ghost", "tool": "x", "arguments": {}}, servers, StubProxy()
    )

    assert "unknown server" in _payload(blocks)["error"]


async def test_call_tool_passes_child_content_through(servers) -> None:
    proxy = StubProxy(
        types.CallToolResult(content=[types.TextContent(type="text", text="child said hi")])
    )

    blocks = await handle_tool(
        "call_tool", {"server": "child", "tool": "t", "arguments": {"a": 1}}, servers, proxy
    )

    assert proxy.calls == [("child", "t", {"a": 1})]
    assert blocks[0].text == "child said hi"


async def test_child_tool_errors_are_marked_for_the_model(servers) -> None:
    """A failed child call must be visibly flagged, not silently returned.

    The hub flattens the child's result into text, so without this marker the
    model cannot tell a failure from a normal answer.
    """
    proxy = StubProxy(
        types.CallToolResult(content=[types.TextContent(type="text", text="boom")], isError=True)
    )

    blocks = await handle_tool("call_tool", {"server": "child", "tool": "t"}, servers, proxy)

    assert blocks[0].text == "[tool reported error]"
    assert blocks[1].text == "boom"
