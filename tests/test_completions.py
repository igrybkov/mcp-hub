"""Completion routing.

Completions arrive referencing a namespaced prompt name or hub resource URI
and have to be re-addressed to the child's own naming before forwarding. When
the hub can't route one it returns None rather than raising — the host is
merely offering autocomplete, and an error there would surface as a broken
request for something optional.
"""

from __future__ import annotations

import pytest
from mcp import types
from mcp_hub.completions import _route_ref, handle_complete
from mcp_hub.config import ServerSpec
from mcp_hub.namespace import NamespaceError


class FakeSession:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def complete(self, ref, argument, context_arguments=None):
        self.calls.append((ref, argument, context_arguments))
        return types.CompleteResult(completion=types.Completion(values=["done"]))


class StubProxy:
    def __init__(self, session) -> None:
        self._session = session
        self._holders: dict[str, object] = {}

    async def session(self, name):
        return self._session


class FakeState:
    def __init__(self, session, *, servers=("obs",)) -> None:
        self.servers = {n: ServerSpec(name=n, transport="stdio", command="true") for n in servers}
        self.proxy = StubProxy(session)


@pytest.fixture
def argument() -> types.CompletionArgument:
    return types.CompletionArgument(name="date", value="2026-")


# --- ref routing ---


def test_prompt_ref_is_rewritten_to_the_child_name() -> None:
    server, ref = _route_ref(types.PromptReference(type="ref/prompt", name="obs__daily"))

    assert server == "obs"
    assert ref.name == "daily", "the child never sees the hub's namespacing"


def test_resource_template_ref_is_rewritten_to_the_child_uri() -> None:
    server, ref = _route_ref(
        types.ResourceTemplateReference(
            type="ref/resource", uri="mcphub://obs/obsidian%3A%2F%2Fd%2F{date}.md"
        )
    )

    assert server == "obs"
    assert str(ref.uri) == "obsidian://d/{date}.md"


def test_unroutable_ref_is_rejected() -> None:
    with pytest.raises(NamespaceError):
        _route_ref(types.PromptReference(type="ref/prompt", name="not-namespaced"))


# --- forwarding ---


async def test_completion_forwards_to_the_child(argument) -> None:
    session = FakeSession()
    state = FakeState(session)

    result = await handle_complete(
        state, types.PromptReference(type="ref/prompt", name="obs__daily"), argument, None
    )

    assert result.values == ["done"]
    ref, arg, _ = session.calls[0]
    assert ref.name == "daily"
    assert arg == {"name": "date", "value": "2026-"}


async def test_unnamespaced_ref_is_dropped(argument) -> None:
    """Not ours to route — drop it rather than erroring on an optional feature."""
    state = FakeState(FakeSession())

    result = await handle_complete(
        state, types.PromptReference(type="ref/prompt", name="daily"), argument, None
    )

    assert result is None


async def test_unknown_server_is_dropped(argument) -> None:
    session = FakeSession()
    state = FakeState(session)

    result = await handle_complete(
        state, types.PromptReference(type="ref/prompt", name="ghost__daily"), argument, None
    )

    assert result is None
    assert session.calls == [], "must not connect to a server that isn't configured"
