"""Flat-namespace encoding for prompt names and resource URIs.

Everything the hub exposes from a child is flattened into one namespace, so
these encodings are the only thing that lets `get_prompt` and `read_resource`
find their way back. They are pure string functions — the cheapest place in
the codebase to pin behaviour that everything else depends on.
"""

from __future__ import annotations

import pytest
from mcp_hub.namespace import (
    NamespaceError,
    decode_prompt_name,
    decode_resource_uri,
    encode_prompt_name,
    encode_resource_uri,
)

# --- prompt names ---


def test_prompt_name_round_trips() -> None:
    encoded = encode_prompt_name("obsidian", "daily-note")

    assert encoded == "obsidian__daily-note"
    assert decode_prompt_name(encoded) == ("obsidian", "daily-note")


def test_prompt_name_may_contain_the_separator() -> None:
    """Only the first separator splits, so a child may use `__` in its names.

    Vault-namespaced prompts like `garden__weekly` are real; splitting on the
    last separator instead would route them to the wrong server.
    """
    encoded = encode_prompt_name("obsidian", "garden__weekly")

    assert decode_prompt_name(encoded) == ("obsidian", "garden__weekly")


def test_server_name_with_separator_is_rejected() -> None:
    """Ambiguity is refused at encode time rather than mis-routed at decode."""
    with pytest.raises(NamespaceError, match="reserved separator"):
        encode_prompt_name("my__server", "daily")


@pytest.mark.parametrize("bad", ["daily", "__daily", "obsidian__", ""])
def test_unnamespaced_prompt_names_are_rejected(bad: str) -> None:
    with pytest.raises(NamespaceError, match="not a namespaced prompt name"):
        decode_prompt_name(bad)


# --- resource URIs ---


def test_resource_uri_round_trips_through_encoding() -> None:
    """`:` and `/` must survive so the hub URI stays a single valid URI."""
    original = "obsidian://daily/2026-04-22.md"

    encoded = encode_resource_uri("obsidian", original)

    assert encoded.startswith("mcphub://obsidian/")
    assert "://daily" not in encoded[len("mcphub://obsidian/") :], "child scheme must be escaped"
    assert decode_resource_uri(encoded) == ("obsidian", original)


def test_resource_uri_round_trips_a_literal_percent() -> None:
    """A child URI containing `%` must not be corrupted by the decode."""
    original = "file:///notes/100%25-done.md"

    assert decode_resource_uri(encode_resource_uri("obs", original)) == ("obs", original)


def test_server_name_with_slash_is_rejected() -> None:
    with pytest.raises(NamespaceError, match="reserved '/'"):
        encode_resource_uri("a/b", "file:///x")


@pytest.mark.parametrize(
    ("bad", "match"),
    [
        ("https://example.com/x", "not a hub resource URI"),
        ("mcphub://obsidian", "malformed hub resource URI"),
        ("mcphub:///encoded", "malformed hub resource URI"),
        ("mcphub://obsidian/", "malformed hub resource URI"),
    ],
)
def test_malformed_resource_uris_are_rejected(bad: str, match: str) -> None:
    with pytest.raises(NamespaceError, match=match):
        decode_resource_uri(bad)
