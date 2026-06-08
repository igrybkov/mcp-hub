"""`completion/complete` relay.

The host sends a completion request referencing either a namespaced prompt
(`server__prompt`) or a rewritten resource-template URI
(`mcphub://server/<encoded>`). The hub decodes the ref, rewrites it to the
child's native form, forwards to the correct child via `session.complete()`,
and returns the child's completion to the host unchanged.

If the child doesn't advertise completions, treat it as "no suggestions"
rather than bubbling an error — the host's UX degrades gracefully.
"""

from __future__ import annotations

import logging

from mcp import McpError, types

from mcp_hub.namespace import (
    NamespaceError,
    decode_prompt_name,
    decode_resource_uri,
)
from mcp_hub.proxy import _is_method_not_found
from mcp_hub.state import HubState

logger = logging.getLogger(__name__)


async def handle_complete(
    state: HubState,
    ref: types.PromptReference | types.ResourceTemplateReference,
    argument: types.CompletionArgument,
    context: types.CompletionContext | None,
) -> types.Completion | None:
    try:
        server_name, child_ref = _route_ref(ref)
    except NamespaceError as exc:
        logger.debug("completion ref not namespaced (dropping): %s", exc)
        return None

    if server_name not in state.servers:
        logger.debug("completion for unknown server %r — dropping", server_name)
        return None

    session = await state.proxy.session(server_name)
    arg_payload = {"name": argument.name, "value": argument.value}
    context_args = context.arguments if context is not None else None
    try:
        result = await session.complete(child_ref, arg_payload, context_args)
    except McpError as exc:
        if _is_method_not_found(exc):
            return None
        raise
    return result.completion


def _route_ref(
    ref: types.PromptReference | types.ResourceTemplateReference,
) -> tuple[str, types.PromptReference | types.ResourceTemplateReference]:
    """Extract (server_name, child-native-ref) from a host-namespaced ref."""
    if isinstance(ref, types.PromptReference):
        server, prompt = decode_prompt_name(ref.name)
        return server, types.PromptReference(type="ref/prompt", name=prompt)
    if isinstance(ref, types.ResourceTemplateReference):
        server, original_uri = decode_resource_uri(ref.uri)
        return server, types.ResourceTemplateReference(type="ref/resource", uri=original_uri)
    raise NamespaceError(f"unexpected completion ref type: {type(ref).__name__}")
