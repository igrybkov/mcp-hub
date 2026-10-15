"""Host-to-children fan-out for logging level and roots changes.

Both walk `state.proxy._holders` directly so they can reach *already connected*
children without forcing a connect — fanning out must never spawn a server.
That private coupling is deliberate, and these tests pin it: if the holder
registry is ever restructured, this is what says so.
"""

from __future__ import annotations

import pytest
from mcp_hub.config import ServerSpec
from mcp_hub.logging_relay import handle_set_logging_level
from mcp_hub.roots import handle_roots_list_changed


class FakeChild:
    def __init__(self, *, raises: bool = False) -> None:
        self.raises = raises
        self.levels: list[str] = []
        self.roots_notifications = 0

    async def set_logging_level(self, level):
        if self.raises:
            raise RuntimeError("child died")
        self.levels.append(level)

    async def send_roots_list_changed(self):
        if self.raises:
            raise RuntimeError("child died")
        self.roots_notifications += 1


class FakeHolder:
    def __init__(self, session) -> None:
        self.session = session


class StubProxy:
    def __init__(self, holders) -> None:
        self._holders = holders


class FakeState:
    def __init__(self, holders) -> None:
        self.servers = {
            name: ServerSpec(name=name, transport="stdio", command="true") for name in holders
        }
        self.proxy = StubProxy(holders)
        self.current_log_level = None


@pytest.fixture
def children():
    """Three servers: one live, one never connected, one that will fail."""
    live, broken = FakeChild(), FakeChild(raises=True)
    holders = {
        "live": FakeHolder(live),
        "unconnected": FakeHolder(None),
        "broken": FakeHolder(broken),
    }
    return FakeState(holders), live, broken


# --- logging level ---


async def test_log_level_reaches_connected_children_only(children) -> None:
    """An unconnected child must not be spawned just to receive a log level."""
    state, live, _ = children

    await handle_set_logging_level(state, "debug")

    assert live.levels == ["debug"]
    assert state.current_log_level == "debug", "cached for children that connect later"


async def test_one_failing_child_does_not_stop_the_fan_out(children) -> None:
    """A child that dies mid-fan-out must not deprive the others."""
    state, live, _ = children

    await handle_set_logging_level(state, "warning")

    assert live.levels == ["warning"]


# --- roots ---


async def test_roots_change_reaches_connected_children_only(children) -> None:
    state, live, _ = children

    await handle_roots_list_changed(state)

    assert live.roots_notifications == 1


async def test_roots_fan_out_survives_a_failing_child(children) -> None:
    state, live, broken = children

    await handle_roots_list_changed(state)

    assert live.roots_notifications == 1
    assert broken.roots_notifications == 0
