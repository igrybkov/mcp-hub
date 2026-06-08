"""Basic smoke tests to verify the package imports and core modules load."""

from mcp_hub import __version__


def test_version_present() -> None:
    assert __version__
    assert "." in __version__


def test_imports() -> None:
    from mcp_hub import config, search, tools  # noqa: F401


def test_search_empty_query() -> None:
    from mcp_hub.search import search

    hits = search("", {}, {})
    assert hits == []
