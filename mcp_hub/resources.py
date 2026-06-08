"""`list_resources`, `list_resource_templates`, `read_resource` handlers.

Resource URIs are rewritten so the host sees:

    mcphub://<server>/<percent-encoded-original-uri>

On `read_resource`, the hub-URI is decoded back to (server, original_uri)
and forwarded to the correct child. The child's `ReadResourceResult` is
returned to the host as `Iterable[ReadResourceContents]`, which the SDK's
`@read_resource` decorator expects.

Resource templates also have their `uriTemplate` rewritten so the host can
construct hub URIs directly; the hub decodes them on read.
"""

from __future__ import annotations

import logging
import urllib.parse
from collections.abc import Iterable

from mcp import types
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.lowlevel.server import request_ctx
from pydantic import AnyUrl

from mcp_hub.namespace import (
    RESOURCE_PREFIX,
    NamespaceError,
    decode_resource_uri,
    encode_resource_uri,
)
from mcp_hub.state import HubState

logger = logging.getLogger(__name__)


async def handle_list_resources(state: HubState) -> list[types.Resource]:
    _capture_session(state)
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
        resources.append(original.model_copy(update={"uri": AnyUrl(rewritten_uri)}))
    return resources


async def handle_list_resource_templates(
    state: HubState,
) -> list[types.ResourceTemplate]:
    _capture_session(state)
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
        rewritten = _rewrite_template_uri(server_name, original.uriTemplate)
        templates.append(original.model_copy(update={"uriTemplate": rewritten}))
    return templates


async def handle_read_resource(state: HubState, uri: AnyUrl) -> Iterable[ReadResourceContents]:
    _capture_session(state)
    try:
        server_name, original_uri = decode_resource_uri(str(uri))
    except NamespaceError as exc:
        raise ValueError(f"not a hub resource URI: {uri}") from exc

    if server_name not in state.servers:
        raise ValueError(f"unknown server: {server_name!r}")

    result = await state.proxy.read_resource(server_name, original_uri)
    return _convert_contents(result)


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


def _convert_contents(
    result: types.ReadResourceResult,
) -> list[ReadResourceContents]:
    """Flatten ReadResourceResult's typed contents to the SDK helper type."""
    out: list[ReadResourceContents] = []
    for entry in result.contents:
        if isinstance(entry, types.TextResourceContents):
            out.append(
                ReadResourceContents(
                    content=entry.text,
                    mime_type=entry.mimeType,
                )
            )
        elif isinstance(entry, types.BlobResourceContents):
            import base64

            out.append(
                ReadResourceContents(
                    content=base64.b64decode(entry.blob),
                    mime_type=entry.mimeType or "application/octet-stream",
                )
            )
        else:
            logger.warning("unknown ReadResourceResult content type: %r", entry)
    return out


def _capture_session(state: HubState) -> None:
    try:
        ctx = request_ctx.get()
    except LookupError:
        return
    state.capture_host_session(ctx.session)
