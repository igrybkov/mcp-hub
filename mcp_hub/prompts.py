"""`list_prompts` and `get_prompt` handlers backed by the hub catalog.

Prompt names are namespaced as "<server>__<prompt>" so the host sees a single
flat list but the hub can route `get_prompt` back to the correct child.
"""

from __future__ import annotations

import logging

from mcp import types

from mcp_hub.namespace import (
    NamespaceError,
    decode_prompt_name,
    encode_prompt_name,
)
from mcp_hub.state import HubState

logger = logging.getLogger(__name__)


async def handle_list_prompts(state: HubState) -> list[types.Prompt]:
    """Return every cataloged prompt with a namespaced name.

    Waits up to the cold-start soft timeout on first call, then serves
    whatever the catalog contains — partial on cold start, complete on warm.
    Late-arriving servers push updates via `prompts/list_changed` so the host
    re-fetches and sees the full set.
    """
    await state.wait_for_cold_start_settle()

    prompts: list[types.Prompt] = []
    for server_name, raw in state.catalog.all_prompts():
        try:
            prompt = types.Prompt.model_validate(raw)
        except Exception as exc:
            logger.warning(
                "catalog entry for %s prompt is invalid, skipping: %s",
                server_name,
                exc,
            )
            continue
        # Clone with namespaced name; keep description/args intact.
        prompts.append(
            prompt.model_copy(update={"name": encode_prompt_name(server_name, prompt.name)})
        )
    return prompts


async def handle_get_prompt(
    state: HubState, encoded_name: str, arguments: dict[str, str] | None
) -> types.GetPromptResult:
    try:
        server_name, prompt_name = decode_prompt_name(encoded_name)
    except NamespaceError as exc:
        raise ValueError(f"unknown prompt {encoded_name!r}: {exc}") from exc

    if server_name not in state.servers:
        raise ValueError(f"unknown server: {server_name!r}")
    return await state.proxy.get_prompt(server_name, prompt_name, arguments)
