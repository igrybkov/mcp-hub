"""`list_resources`, `list_resource_templates`, `read_resource` handlers.

Resource URIs are rewritten so the host sees:

    mcphub://<server>/<percent-encoded-original-uri>

On `read_resource`, the hub-URI is decoded back to (server, original_uri)
and forwarded to the correct child. The child's `ReadResourceResult` is
passed back to the host as-is apart from its URIs, which are re-stamped to
the hub URI the host addressed (see `_restamp_uri`).

Resource templates also have their `uri_template` rewritten so the host can
construct hub URIs directly; the hub decodes them on read.
"""

from __future__ import annotations

import logging
import urllib.parse

from mcp import types

from mcp_hub.namespace import (
    RESOURCE_PREFIX,
    NamespaceError,
    decode_resource_uri,
    encode_resource_uri,
)
from mcp_hub.state import HubState

logger = logging.getLogger(__name__)


async def handle_list_resources(state: HubState) -> list[types.Resource]:
    await state.wait_for_cold_start_settle()

    resources: list[types.Resource] = []
    for server_name, raw in state.catalog.all_resources():
        try:
            original = types.Resource.model_validate(raw)
        except Exception as exc:
            logger.warning(
                "catalog entry for %s resource is invalid, skipping: %s",
                server_name,
                exc,
            )
            continue
        rewritten_uri = encode_resource_uri(server_name, str(original.uri))
        resources.append(original.model_copy(update={"uri": rewritten_uri}))
    return resources


async def handle_list_resource_templates(
    state: HubState,
) -> list[types.ResourceTemplate]:
    await state.wait_for_cold_start_settle()

    templates: list[types.ResourceTemplate] = []
    for server_name, raw in state.catalog.all_resource_templates():
        try:
            original = types.ResourceTemplate.model_validate(raw)
        except Exception as exc:
            logger.warning(
                "catalog entry for %s resource_template is invalid, skipping: %s",
                server_name,
                exc,
            )
            continue
        rewritten = _rewrite_template_uri(server_name, original.uri_template)
        templates.append(original.model_copy(update={"uri_template": rewritten}))
    return templates


async def handle_read_resource(state: HubState, uri: str) -> types.ReadResourceResult:
    try:
        server_name, original_uri = decode_resource_uri(str(uri))
    except NamespaceError as exc:
        raise ValueError(f"not a hub resource URI: {uri}") from exc

    if server_name not in state.servers:
        raise ValueError(f"unknown server: {server_name!r}")

    result = await state.proxy.read_resource(server_name, original_uri)
    return _restamp_uri(result, uri)


def _rewrite_template_uri(server_name: str, template: str) -> str:
    """Wrap the child's URI template so expansion yields a valid hub URI.

    Percent-encode the child's template as a single path segment of the hub
    URI. RFC 6570 expansion substitutes variables in-place, and since `{foo}`
    characters are safe-encoded they remain intact for the host-side expander,
    but `:` and `/` in literal parts are encoded so the final expanded URI is
    valid. The hub decodes the full path segment on read, recovering the
    expanded child URI.
    """
    # Preserve `{}` so the template remains expandable by the host. Everything
    # else is percent-encoded.
    quoted = urllib.parse.quote(template, safe="{}")
    return f"{RESOURCE_PREFIX}{server_name}/{quoted}"


def _restamp_uri(result: types.ReadResourceResult, uri: str) -> types.ReadResourceResult:
    """Re-address the child's contents to the hub URI the host asked for.

    The child answers with its own URI. Under mcp 1.x this never surfaced: the
    `@read_resource()` decorator took a bare `Iterable[ReadResourceContents]`
    and stamped the *requested* URI on the way out. mcp 2.0 hands the result
    back verbatim, so without this the host would see the child's private URI
    for a resource it addressed through the hub — and any re-read of that URI
    would fail to route.
    """
    contents = [entry.model_copy(update={"uri": uri}) for entry in result.contents]
    return result.model_copy(update={"contents": contents})
