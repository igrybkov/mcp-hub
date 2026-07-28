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
from mcp_hub.catalog import Catalog
from mcp_hub.config import ServerSpec
from mcp_hub.proxy import ProxyClient
from mcp_hub.server import build_app
from mcp_hub.state import HubState


@pytest.fixture
def build(tmp_path):
    """Build an app from a single server, exposed or not."""

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
        return build_app(servers, proxy, state, "instructions")

    return _build


# --- capability gating ---


def test_unexposed_config_advertises_tools_only(build) -> None:
    """With nothing exposed the hub must stay a tools-only server.

    Everything else is reachable through the meta-tools, and advertising empty
    prompt/resource lists would put dead entries in the host's UI.
    """
    _, init_options = build(exposed=False)
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
    _, init_options = build(exposed=True)
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
    _, exposed = build(exposed=True)
    _, bare = build(exposed=False)

    assert exposed.capabilities.prompts.list_changed is True
    assert exposed.capabilities.resources.list_changed is True
    assert bare.capabilities.prompts is None
    assert bare.capabilities.resources is None
