"""Prompt listing and routing.

Prompt names are flattened to `<server>__<prompt>` so the host sees one list.
`get_prompt` has to reverse that exactly, or a prompt the host can see becomes
one it cannot call.
"""

from __future__ import annotations

import pytest
from mcp import types
from mcp_hub.catalog import Catalog
from mcp_hub.config import ServerSpec
from mcp_hub.prompts import handle_get_prompt, handle_list_prompts
from mcp_hub.state import HubState


class StubProxy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self._holders: dict[str, object] = {}

    async def get_prompt(self, name, prompt_name, arguments):
        self.calls.append((name, prompt_name, arguments))
        return types.GetPromptResult(messages=[])


@pytest.fixture
def make_state(tmp_path):
    def _make(*, prompts=None, raw=None):
        catalog = Catalog(tmp_path / "catalog.json")
        catalog.upsert_server("obs", status="ok", prompts=prompts or [])
        if raw is not None:
            catalog._servers["obs"]["prompts"] = raw
        proxy = StubProxy()
        state = HubState(
            servers={"obs": ServerSpec(name="obs", transport="stdio", command="true")},
            catalog=catalog,
            proxy=proxy,
        )
        state.mark_enumeration_done()
        return state, proxy

    return _make


# --- listing ---


async def test_prompt_names_are_namespaced(make_state) -> None:
    state, _ = make_state(prompts=[types.Prompt(name="daily", description="Daily note")])

    listed = await handle_list_prompts(state)

    assert listed[0].name == "obs__daily"
    assert listed[0].description == "Daily note", "metadata must survive the rename"


async def test_invalid_prompt_entry_is_skipped(make_state) -> None:
    """A corrupt cache entry must not take down the whole listing."""
    state, _ = make_state(
        prompts=[types.Prompt(name="good")],
        raw=[{"name": "good"}, {"not": "a prompt"}],
    )

    listed = await handle_list_prompts(state)

    assert [p.name for p in listed] == ["obs__good"]


# --- routing ---


async def test_get_prompt_routes_to_child_name(make_state) -> None:
    """The child is asked for its own name, not the namespaced one."""
    state, proxy = make_state()

    await handle_get_prompt(state, "obs__daily", {"date": "today"})

    assert proxy.calls == [("obs", "daily", {"date": "today"})]


async def test_get_prompt_rejects_unnamespaced_name(make_state) -> None:
    state, _ = make_state()

    with pytest.raises(ValueError, match="unknown prompt"):
        await handle_get_prompt(state, "daily", None)


async def test_get_prompt_rejects_unknown_server(make_state) -> None:
    state, _ = make_state()

    with pytest.raises(ValueError, match="unknown server"):
        await handle_get_prompt(state, "ghost__daily", None)
