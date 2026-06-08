"""Encoding rules for per-server prompt names and resource URIs.

The hub exposes prompts and resources from multiple child servers as a single
flat namespace to its host. Child names/URIs must be stably round-trippable so
`get_prompt` and `read_resource` can route to the right child.

    Prompt:   child "obsidian" prompt "daily-note"
              ↔ hub-visible name "obsidian__daily-note"

    Resource: child "obsidian" URI "obsidian://daily/2026-04-22.md"
              ↔ hub-visible URI "mcphub://obsidian/obsidian%3A%2F%2Fdaily%2F2026-04-22.md"

The resource URI is percent-encoded so the hub-side URI is always a single
valid `scheme://host/path` that Pydantic's AnyUrl accepts.
"""

from __future__ import annotations

import urllib.parse

PROMPT_SEP = "__"
RESOURCE_SCHEME = "mcphub"
RESOURCE_PREFIX = f"{RESOURCE_SCHEME}://"


class NamespaceError(ValueError):
    """Raised when a namespaced name or URI cannot be parsed."""


def encode_prompt_name(server: str, prompt: str) -> str:
    if PROMPT_SEP in server:
        raise NamespaceError(f"server name {server!r} contains reserved separator {PROMPT_SEP!r}")
    return f"{server}{PROMPT_SEP}{prompt}"


def decode_prompt_name(encoded: str) -> tuple[str, str]:
    """Split "server__prompt" back into (server, prompt). First split wins."""
    parts = encoded.split(PROMPT_SEP, 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise NamespaceError(f"not a namespaced prompt name: {encoded!r}")
    return parts[0], parts[1]


def encode_resource_uri(server: str, uri: str) -> str:
    if "/" in server:
        raise NamespaceError(f"server name {server!r} contains reserved '/'")
    # `safe=""` percent-encodes every reserved character, including `:` and `/`,
    # so the original URI becomes a single opaque path segment.
    quoted = urllib.parse.quote(uri, safe="")
    return f"{RESOURCE_PREFIX}{server}/{quoted}"


def decode_resource_uri(encoded: str) -> tuple[str, str]:
    """Parse "mcphub://server/<percent-encoded-uri>" back into (server, uri)."""
    if not encoded.startswith(RESOURCE_PREFIX):
        raise NamespaceError(f"not a hub resource URI: {encoded!r}")
    rest = encoded[len(RESOURCE_PREFIX) :]
    parts = rest.split("/", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise NamespaceError(f"malformed hub resource URI: {encoded!r}")
    server, quoted = parts
    return server, urllib.parse.unquote(quoted)
