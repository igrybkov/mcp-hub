"""Resource listing, template rewriting, and read routing.

The hub rewrites every child URI into `mcphub://<server>/<encoded>` so a flat
list still routes back to the right child. Getting a rewrite wrong does not
raise — the host simply receives the child's private URI and any read of it
fails to route — so these assert on the rewritten values themselves.
"""

from __future__ import annotations

import pytest
from mcp import types
from mcp_hub.catalog import Catalog
from mcp_hub.config import ServerSpec
from mcp_hub.resources import (
    _restamp_uri,
    handle_list_resource_templates,
    handle_list_resources,
    handle_read_resource,
)
from mcp_hub.state import HubState


class StubProxy:
    """Duck-typed ProxyClient: HubState never type-checks it."""

    def __init__(self, result=None) -> None:
        self.result = result
        self.reads: list[tuple[str, str]] = []
        self._holders: dict[str, object] = {}

    async def read_resource(self, name: str, uri: str) -> types.ReadResourceResult:
        self.reads.append((name, uri))
        return self.result


@pytest.fixture
def make_state(tmp_path):
    def _make(*, resources=None, templates=None, proxy=None, raw=None):
        catalog = Catalog(tmp_path / "catalog.json")
        catalog.upsert_server(
            "obs",
            status="ok",
            resources=resources or [],
            resource_templates=templates or [],
        )
        if raw is not None:
            catalog._servers["obs"]["resources"] = raw
        state = HubState(
            servers={"obs": ServerSpec(name="obs", transport="stdio", command="true")},
            catalog=catalog,
            proxy=proxy or StubProxy(),
        )
        state.mark_enumeration_done()
        return state

    return _make


# --- listing ---


async def test_resource_uri_is_rewritten_to_hub_scheme(make_state) -> None:
    state = make_state(resources=[types.Resource(name="note", uri="obsidian://vault/note.md")])

    listed = await handle_list_resources(state)

    assert str(listed[0].uri).startswith("mcphub://obs/")
    assert listed[0].name == "note"


async def test_resource_template_uri_is_rewritten(make_state) -> None:
    """The rewrite must land on the field, not on an alias-named attribute.

    `model_copy(update=...)` takes *field* names and validates nothing. Passing
    the wire alias `uriTemplate` silently attaches a stray attribute, leaves
    `uri_template` at the child's original value, and ships that to the host —
    no exception anywhere. Only asserting the output value catches it.
    """
    state = make_state(
        templates=[types.ResourceTemplate(name="daily", uriTemplate="obsidian://daily/{date}.md")]
    )

    listed = await handle_list_resource_templates(state)

    assert listed[0].uri_template.startswith("mcphub://obs/")
    assert "{date}" in listed[0].uri_template, "RFC 6570 variables must survive encoding"


async def test_invalid_catalog_entry_is_skipped(make_state) -> None:
    """A corrupt cache entry must not take down the whole listing."""
    state = make_state(
        resources=[types.Resource(name="good", uri="obsidian://a.md")],
        raw=[{"name": "good", "uri": "obsidian://a.md"}, {"nonsense": True}],
    )

    listed = await handle_list_resources(state)

    assert [r.name for r in listed] == ["good"]


# --- read routing ---


async def test_read_resource_routes_to_child_uri(make_state) -> None:
    """The hub URI decodes back to the child's own URI before forwarding."""
    proxy = StubProxy(
        types.ReadResourceResult(
            contents=[types.TextResourceContents(uri="obsidian://vault/n.md", text="hi")]
        )
    )
    state = make_state(proxy=proxy)
    hub_uri = "mcphub://obs/obsidian%3A%2F%2Fvault%2Fn.md"

    await handle_read_resource(state, hub_uri)

    assert proxy.reads == [("obs", "obsidian://vault/n.md")]


async def test_read_resource_rejects_foreign_uri(make_state) -> None:
    state = make_state()

    with pytest.raises(ValueError, match="not a hub resource URI"):
        await handle_read_resource(state, "https://example.com/x")


async def test_read_resource_rejects_unknown_server(make_state) -> None:
    state = make_state()

    with pytest.raises(ValueError, match="unknown server"):
        await handle_read_resource(state, "mcphub://ghost/obsidian%3A%2F%2Fa.md")


def test_restamp_uri_readdresses_contents() -> None:
    """The host must see the URI it asked for, not the child's private one.

    mcp 1.x's `@read_resource()` decorator stamped the requested URI on the way
    out; 2.0 returns the child's result verbatim, so the hub does it. Without
    this the host gets a URI it cannot re-read.
    """
    child = types.ReadResourceResult(
        contents=[
            types.TextResourceContents(
                uri="obsidian://vault/n.md", text="body", mimeType="text/markdown"
            )
        ]
    )

    out = _restamp_uri(child, "mcphub://obs/obsidian%3A%2F%2Fvault%2Fn.md")

    assert str(out.contents[0].uri) == "mcphub://obs/obsidian%3A%2F%2Fvault%2Fn.md"
    assert out.contents[0].text == "body", "payload must be untouched"
    assert out.contents[0].mime_type == "text/markdown"
