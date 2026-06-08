"""Sampling-backed `recommend_servers` tool.

Given a natural-language task description, asks the host's own LLM (via
`sampling/createMessage`) to rank configured servers by relevance. Uses the
catalog's metadata (description, tags, prompt names/descriptions, resource
names/descriptions) as the ranking context.

Unlike a hardcoded keyword dictionary, this adapts automatically as the
user adds/removes servers or their metadata evolves. The tradeoff: it costs
one host LLM call per invocation, so it's a "when I'm stuck" tool, not a
hot-path one.

Falls back to a catalog-metadata summary + client-side hint if the host
doesn't support sampling or the call errors — the LLM already has the tool
output, so even a raw dump beats an error.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from mcp import types

from mcp_hub.state import HubState

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are a routing assistant embedded inside an MCP hub. Given a user "
    "task description and a catalog of available MCP servers (with their "
    "descriptions, tags, prompts, and resources), pick the 1-{max_results} "
    "servers most likely to help. Be decisive — this list is shown to the "
    "primary assistant so it can focus on the right tools.\n\n"
    "Reply ONLY with a JSON object of the shape:\n"
    '  {{"recommendations": [{{"server": "<id>", "score": <0-100 int>, '
    '"reason": "<one sentence>"}}]}}\n'
    "No prose outside the JSON. Use only server IDs that appear in the "
    "provided catalog. Order by score descending."
)


def _catalog_digest(state: HubState) -> str:
    """Compact human-readable summary of every configured server + catalog entry."""
    lines: list[str] = []
    for name in sorted(state.servers):
        spec = state.servers[name]
        parts = [f"- {name}"]
        if spec.description:
            parts.append(f"desc={spec.description!r}")
        if spec.tags:
            parts.append(f"tags={spec.tags}")
        entry = state.catalog.server_entry(name)
        if entry:
            prompts = entry.get("prompts") or []
            resources = entry.get("resources") or []
            if prompts:
                names = ", ".join(p.get("name", "?") for p in prompts[:5])
                parts.append(f"prompts=[{names}]")
            if resources:
                names = ", ".join(r.get("name") or r.get("uri", "?") for r in resources[:5])
                parts.append(f"resources=[{names}]")
        lines.append(" ".join(parts))
    return "\n".join(lines)


async def handle_recommend_servers(
    state: HubState, arguments: dict[str, Any]
) -> list[types.TextContent]:
    task = (arguments or {}).get("task_description", "").strip()
    max_results = int((arguments or {}).get("max_results", 5))
    if not task:
        return [
            types.TextContent(
                type="text",
                text=json.dumps({"error": "task_description is required"}),
            )
        ]

    host = state.host_session
    digest = _catalog_digest(state)
    known_ids = set(state.servers.keys())

    if host is None:
        # No host yet — return the catalog digest so the caller can fall back
        # to local reasoning. This shouldn't happen in practice since the tool
        # is only callable from within an active session.
        return _fallback(
            task,
            digest,
            reason="host session not available (sampling unsupported)",
        )

    system = _SYSTEM_PROMPT.format(max_results=max_results)
    user_text = f"Task:\n{task}\n\nCatalog:\n{digest}\n\nRecommend up to {{n}} servers.".format(
        n=max_results
    )
    messages = [
        types.SamplingMessage(
            role="user",
            content=types.TextContent(type="text", text=user_text),
        )
    ]
    try:
        result = await host.create_message(
            messages=messages,
            max_tokens=800,
            system_prompt=system,
            temperature=0.0,
        )
    except Exception as exc:
        logger.warning("recommend_servers: sampling failed: %s", exc)
        return _fallback(task, digest, reason=f"sampling failed: {exc}")

    content = result.content
    text: str | None = None
    if isinstance(content, types.TextContent):
        text = content.text
    elif isinstance(content, list) and content:  # some clients wrap in a list
        first = content[0]
        text = getattr(first, "text", None)

    if not text:
        return _fallback(task, digest, reason="host returned non-text content")

    parsed = _parse_ranking(text, known_ids, max_results)
    if parsed is None:
        return _fallback(
            task,
            digest,
            reason="host response was not valid JSON of the expected shape",
            raw_host_reply=text,
        )
    return [
        types.TextContent(
            type="text",
            text=json.dumps(
                {
                    "task": task,
                    "source": "sampling",
                    "model": getattr(result, "model", None),
                    "recommendations": parsed,
                },
                indent=2,
            ),
        )
    ]


def _parse_ranking(text: str, known_ids: set[str], max_results: int) -> list[dict[str, Any]] | None:
    """Extract the JSON payload and normalize into a validated recommendation list."""
    payload = _extract_json(text)
    if payload is None:
        return None
    recs = payload.get("recommendations")
    if not isinstance(recs, list):
        return None
    cleaned: list[dict[str, Any]] = []
    for item in recs:
        if not isinstance(item, dict):
            continue
        server = item.get("server")
        if not isinstance(server, str) or server not in known_ids:
            continue
        score_raw = item.get("score", 0)
        try:
            score_int = max(0, min(100, int(score_raw)))
        except Exception:
            # Any coercion failure — missing, None, nested object, bad string —
            # treats as zero. Better than dropping the recommendation outright.
            score_int = 0
        reason = item.get("reason")
        cleaned.append(
            {
                "server": server,
                "score": score_int,
                "reason": reason if isinstance(reason, str) else None,
            }
        )
    cleaned.sort(key=lambda r: r["score"], reverse=True)
    return cleaned[:max_results]


_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(text: str) -> dict[str, Any] | None:
    """Best-effort JSON extraction — strips code fences, grabs the outermost object."""
    stripped = text.strip()
    # Common wrapping: ```json { ... } ```
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\n?", "", stripped)
        stripped = re.sub(r"\n?```$", "", stripped)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    match = _JSON_OBJECT.search(stripped)
    if match is None:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def _fallback(
    task: str,
    digest: str,
    *,
    reason: str,
    raw_host_reply: str | None = None,
) -> list[types.TextContent]:
    """Return the catalog digest so the caller can reason locally.

    Sampling is never guaranteed — hosts may not support it, cap tokens too
    low, or rate-limit. When that happens, the assistant can still pick the
    right server from the raw catalog text, it just spends a bit more context.
    """
    body: dict[str, Any] = {
        "task": task,
        "source": "fallback",
        "reason": reason,
        "catalog": digest,
    }
    if raw_host_reply is not None:
        body["raw_host_reply"] = raw_host_reply
    return [types.TextContent(type="text", text=json.dumps(body, indent=2))]
