"""Enumeration of exposed servers at startup.

Only the deterministic parts: which primitives get enumerated for a given
opt-in, and that a hub with nothing exposed unblocks immediately. The retry
daemon's backoff loop is left alone on purpose — testing it means racing real
sleeps for a failure mode that has never bitten.
"""

from __future__ import annotations

import pytest
from mcp import types
from mcp_hub.catalog import Catalog
from mcp_hub.config import ServerSpec
from mcp_hub.startup import enumerate_once, run_startup
from mcp_hub.state import HubState


class StubProxy:
    def __init__(self) -> None:
        self.asked: list[str] = []
        self._holders: dict[str, object] = {}

    async def list_prompts(self, name):
        self.asked.append("prompts")
        return [types.Prompt(name="p")]

    async def list_resources(self, name):
        self.asked.append("resources")
        return [types.Resource(name="r", uri="obs://r")]

    async def list_resource_templates(self, name):
        self.asked.append("resource_templates")
        return []


@pytest.fixture
def make_state(tmp_path):
    def _make(**expose):
        spec = ServerSpec(name="obs", transport="stdio", command="true", **expose)
        proxy = StubProxy()
        state = HubState(
            servers={"obs": spec},
            catalog=Catalog(tmp_path / "catalog.json"),
            proxy=proxy,
        )
        return state, spec, proxy

    return _make


# --- enumeration ---


async def test_prompts_only_server_is_not_asked_for_resources(make_state) -> None:
    """Opting into prompts must not drag in a resource round-trip."""
    state, spec, proxy = make_state(expose_prompts=True)

    await enumerate_once(state, spec)

    assert proxy.asked == ["prompts"]
    assert [n for n, _ in state.catalog.all_prompts()] == ["obs"]


async def test_resources_only_server_also_gets_templates(make_state) -> None:
    """Templates ride with resources — a host needs both to address anything."""
    state, spec, proxy = make_state(expose_resources=True)

    await enumerate_once(state, spec)

    assert proxy.asked == ["resources", "resource_templates"]


# --- cold-start gating ---


async def test_nothing_exposed_unblocks_immediately(make_state) -> None:
    """With no exposed servers there is nothing to wait for.

    Handlers block on this event up to the cold-start soft timeout; leaving it
    unset would add that delay to the first list call for no reason.
    """
    state, _, _ = make_state()

    await run_startup(state)

    await state.wait_for_cold_start_settle()  # must not stall
    assert state._first_enumeration_done.is_set()
