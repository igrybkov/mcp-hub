"""Bidirectional relay for sampling and elicitation requests.

A child MCP server that needs the LLM (sampling) or the user (elicitation)
sends its request to the hub. The hub's `ClientSession` to that child has
`sampling_callback` / `elicitation_callback` set to the closures returned
from this module. The closures forward the request to the host via the
hub's own `ServerSession.create_message()` / `elicit_form()`, then return
the host's answer back to the child.

Errors are mapped to `ErrorData` rather than raised so the SDK's
ClientSession sends a clean JSON-RPC error to the child instead of
hard-crashing the hub.
"""

from __future__ import annotations

import logging

from mcp import types
from mcp.shared.context import RequestContext

from mcp_hub.state import HubState

logger = logging.getLogger(__name__)


def make_sampling_callback(state: HubState, server_name: str):
    """Return a `sampling_callback` for a child ClientSession.

    Forwards `sampling/createMessage` requests to the host. If the host isn't
    captured yet, returns an error rather than hanging — children shouldn't
    issue sampling requests before the hub has a host.
    """

    async def callback(
        ctx: RequestContext,
        params: types.CreateMessageRequestParams,
    ) -> types.CreateMessageResult | types.CreateMessageResultWithTools | types.ErrorData:
        host = state.host_session
        if host is None:
            return types.ErrorData(
                code=types.INTERNAL_ERROR,
                message="mcp-hub: host session not yet connected",
            )
        try:
            return await host.create_message(
                messages=list(params.messages),
                max_tokens=params.maxTokens,
                system_prompt=params.systemPrompt,
                include_context=params.includeContext,
                temperature=params.temperature,
                stop_sequences=params.stopSequences,
                metadata=params.metadata,
                model_preferences=params.modelPreferences,
                tools=params.tools,
                tool_choice=params.toolChoice,
            )
        except Exception as exc:
            logger.warning("sampling relay failed for %r: %s", server_name, exc)
            return types.ErrorData(
                code=types.INTERNAL_ERROR,
                message=f"mcp-hub: sampling forward failed: {exc}",
            )

    return callback


def make_elicitation_callback(state: HubState, server_name: str):
    """Return an `elicitation_callback` for a child ClientSession.

    Forwards `elicitation/create` requests to the host. Same failure-mode
    rules as sampling.
    """

    async def callback(
        ctx: RequestContext,
        params: types.ElicitRequestParams,
    ) -> types.ElicitResult | types.ErrorData:
        host = state.host_session
        if host is None:
            return types.ErrorData(
                code=types.INTERNAL_ERROR,
                message="mcp-hub: host session not yet connected",
            )
        try:
            return await host.elicit_form(
                message=params.message,
                requestedSchema=params.requestedSchema,
            )
        except Exception as exc:
            logger.warning("elicitation relay failed for %r: %s", server_name, exc)
            return types.ErrorData(
                code=types.INTERNAL_ERROR,
                message=f"mcp-hub: elicitation forward failed: {exc}",
            )

    return callback
