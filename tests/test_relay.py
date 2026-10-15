"""Sampling and elicitation relays.

These forward a child's request to the host, and the sampling forward reads
six fields off `CreateMessageRequestParams` in a single call. mcp 2.0 renamed
every one of them from camelCase to snake_case and dropped the old spelling,
so a missed rename here is an AttributeError that the callback's own catch-all
converts into a bland "sampling forward failed" — the child sees a hub error
and nothing points at the real cause. One assertion per field.
"""

from __future__ import annotations

import pytest
from mcp import types
from mcp_hub.relay import make_elicitation_callback, make_sampling_callback


class FakeHost:
    """Stand-in for the host ServerSession, recording what it was called with."""

    def __init__(self, *, raises: bool = False) -> None:
        self.raises = raises
        self.calls: list[tuple[str, dict]] = []

    async def create_message(self, messages, **kwargs):
        if self.raises:
            raise RuntimeError("host refused")
        self.calls.append(("create_message", {"messages": messages, **kwargs}))
        return types.CreateMessageResult(
            role="assistant",
            content=types.TextContent(type="text", text="ok"),
            model="test-model",
        )

    async def elicit_form(self, **kwargs):
        self.calls.append(("elicit_form", kwargs))
        return types.ElicitResult(action="accept", content={"answer": "yes"})

    async def elicit_url(self, **kwargs):
        self.calls.append(("elicit_url", kwargs))
        return types.ElicitResult(action="accept")


class FakeState:
    def __init__(self, host) -> None:
        self.host_session = host


@pytest.fixture
def sampling_params() -> types.CreateMessageRequestParams:
    """Built with camelCase aliases, i.e. the way a child sends it on the wire."""
    return types.CreateMessageRequestParams(
        messages=[
            types.SamplingMessage(role="user", content=types.TextContent(type="text", text="hi"))
        ],
        maxTokens=100,
        systemPrompt="be brief",
        includeContext="thisServer",
        temperature=0.5,
        stopSequences=["STOP"],
        modelPreferences=types.ModelPreferences(costPriority=0.1),
    )


# --- sampling ---


async def test_sampling_forwards_every_renamed_field(sampling_params) -> None:
    """Each of the six renamed fields must survive the hop to the host.

    Asserted individually so a failure names the field that regressed rather
    than just reporting that the forward is wrong.
    """
    host = FakeHost()
    callback = make_sampling_callback(FakeState(host), "child")

    result = await callback(None, sampling_params)

    # A renamed field raises AttributeError inside the callback, which its own
    # catch-all turns into ErrorData — so check that first, or this test fails
    # with an unhelpful IndexError on the empty call list below.
    assert not isinstance(result, types.ErrorData), (
        f"forward failed instead of reaching the host: {result}"
    )
    _, kwargs = host.calls[0]
    assert kwargs["max_tokens"] == 100
    assert kwargs["system_prompt"] == "be brief"
    assert kwargs["include_context"] == "thisServer"
    assert kwargs["stop_sequences"] == ["STOP"]
    assert kwargs["model_preferences"].cost_priority == pytest.approx(0.1)
    assert kwargs["tool_choice"] is None
    assert kwargs["temperature"] == pytest.approx(0.5)


async def test_sampling_without_host_returns_error(sampling_params) -> None:
    """A child may ask before the host has connected; that must not hang."""
    callback = make_sampling_callback(FakeState(None), "child")

    result = await callback(None, sampling_params)

    assert isinstance(result, types.ErrorData)
    assert result.code == types.INTERNAL_ERROR


async def test_sampling_host_failure_becomes_error_data(sampling_params) -> None:
    """Host errors come back as ErrorData so the SDK sends the child a clean
    JSON-RPC error instead of tearing down the hub."""
    callback = make_sampling_callback(FakeState(FakeHost(raises=True)), "child")

    result = await callback(None, sampling_params)

    assert isinstance(result, types.ErrorData)
    assert "sampling forward failed" in result.message


# --- elicitation ---


async def test_elicitation_form_mode_forwards_schema() -> None:
    host = FakeHost()
    callback = make_elicitation_callback(FakeState(host), "child")
    params = types.ElicitRequestFormParams(
        mode="form", message="need creds", requestedSchema={"type": "object"}
    )

    result = await callback(None, params)

    method, kwargs = host.calls[0]
    assert method == "elicit_form"
    assert kwargs["requested_schema"] == {"type": "object"}
    assert result.action == "accept"


async def test_elicitation_url_mode_routes_to_elicit_url() -> None:
    """URL-mode must reach `elicit_url`, not `elicit_form`.

    `ElicitRequestParams` is a union and only the form variant carries
    `requested_schema`. Reading it unconditionally raised AttributeError on
    every URL-mode request, which the catch-all reported to the child as a
    generic forward failure.
    """
    host = FakeHost()
    callback = make_elicitation_callback(FakeState(host), "child")
    params = types.ElicitRequestURLParams(
        mode="url",
        message="authorize here",
        url="https://example.com/auth",
        elicitationId="elicit-1",
    )

    result = await callback(None, params)

    method, kwargs = host.calls[0]
    assert method == "elicit_url"
    assert kwargs["url"] == "https://example.com/auth"
    assert kwargs["elicitation_id"] == "elicit-1"
    assert result.action == "accept"


async def test_elicitation_without_host_returns_error() -> None:
    callback = make_elicitation_callback(FakeState(None), "child")
    params = types.ElicitRequestFormParams(
        mode="form", message="need creds", requestedSchema={"type": "object"}
    )

    result = await callback(None, params)

    assert isinstance(result, types.ErrorData)
    assert result.code == types.INTERNAL_ERROR
