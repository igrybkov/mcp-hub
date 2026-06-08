"""Keyword search across server metadata and tool descriptions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mcp import types

from mcp_hub.config import ServerSpec


@dataclass
class SearchHit:
    server: str
    tool: str | None  # None = matched on server metadata only
    name: str
    description: str
    score: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "server": self.server,
            "tool": self.tool,
            "name": self.name,
            "description": self.description,
            "score": self.score,
        }


def _score(haystack: str, terms: list[str]) -> int:
    haystack = haystack.lower()
    return sum(haystack.count(t) for t in terms)


def search(
    query: str,
    servers: dict[str, ServerSpec],
    tools_by_server: dict[str, list[types.Tool]],
    limit: int = 20,
) -> list[SearchHit]:
    """Return hits sorted by relevance.

    tools_by_server may be partial; servers missing from it are only matched on
    their own metadata (description/tags).
    """
    terms = [t for t in query.lower().split() if t]
    if not terms:
        return []
    hits: list[SearchHit] = []
    for name, spec in servers.items():
        haystack = " ".join([name, spec.description or "", " ".join(spec.tags)])
        score = _score(haystack, terms)
        if score:
            hits.append(
                SearchHit(
                    server=name,
                    tool=None,
                    name=name,
                    description=spec.description or "",
                    score=score * 2,  # server-level hits weigh more
                )
            )
        for tool in tools_by_server.get(name, []):
            haystack = " ".join([tool.name, tool.description or ""])
            score = _score(haystack, terms)
            if score:
                hits.append(
                    SearchHit(
                        server=name,
                        tool=tool.name,
                        name=tool.name,
                        description=tool.description or "",
                        score=score,
                    )
                )

    hits.sort(key=lambda h: h.score, reverse=True)
    return hits[:limit]
