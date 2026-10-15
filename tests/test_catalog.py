"""On-disk catalog: serialization round-trip, cache invalidation, change detection.

The catalog is what makes warm start possible, and it is the only place the
hub persists SDK model objects. It dumps with `by_alias=True` and reads back
with `model_validate`, so this file is where an SDK alias change would show up
as a test failure rather than as prompts silently vanishing from the host.
"""

from __future__ import annotations

import json

import pytest
from mcp import types
from mcp_hub.catalog import CATALOG_VERSION, Catalog, _dump_models, _payload_differs


@pytest.fixture
def catalog(tmp_path) -> Catalog:
    return Catalog(tmp_path / "catalog.json")


# --- serialization round-trip ---


@pytest.mark.parametrize(
    ("model", "kwargs"),
    [
        (types.Prompt, {"name": "daily", "description": "Daily note"}),
        (types.Resource, {"name": "note", "uri": "obsidian://n.md"}),
        (types.ResourceTemplate, {"name": "day", "uriTemplate": "obs://{d}.md"}),
    ],
)
def test_dumped_models_validate_back(model, kwargs) -> None:
    """What we write must be readable by the same SDK that wrote it.

    `_dump_models` uses `by_alias=True`, so the file holds wire names while the
    handlers read snake_case fields. If the SDK ever changed an alias, a cached
    entry would fail `model_validate` and be skipped — the host would just see
    fewer prompts, with only a warning in the log. This asserts the loop closes.
    """
    original = model(**kwargs)

    [dumped] = _dump_models([original])
    restored = model.model_validate(dumped)

    assert restored == original
    assert json.loads(json.dumps(dumped)) == dumped, "must be JSON round-trippable"


# --- cache invalidation ---


def test_load_returns_false_when_no_file(catalog) -> None:
    assert catalog.load("hash-a") is False


def test_load_rejects_a_stale_config_hash(catalog) -> None:
    """Config edits must invalidate the cache, or the hub serves dead entries."""
    catalog.set_config_hash("hash-a")
    catalog.upsert_server("obs", status="ok", prompts=[types.Prompt(name="p")])

    assert catalog.load("hash-b") is False
    assert catalog.server_names() == []


def test_load_rejects_a_version_mismatch(catalog) -> None:
    """A format change must not be read with the new code's assumptions."""
    catalog.path.write_text(
        json.dumps({"version": CATALOG_VERSION + 1, "config_hash": "h", "servers": {}})
    )

    assert catalog.load("h") is False


@pytest.mark.parametrize("payload", ["not json at all", '"a string"', "[]"])
def test_load_survives_a_corrupt_file(catalog, payload: str) -> None:
    """A truncated or hand-edited cache degrades to cold start, never a crash."""
    catalog.path.write_text(payload)

    assert catalog.load("h") is False
    assert catalog.server_names() == []


def test_load_accepts_a_matching_snapshot(catalog) -> None:
    catalog.set_config_hash("hash-a")
    catalog.upsert_server("obs", status="ok", prompts=[types.Prompt(name="p")])

    fresh = Catalog(catalog.path)

    assert fresh.load("hash-a") is True
    assert fresh.server_names() == ["obs"]


# --- change detection ---


def test_upsert_reports_only_material_changes(catalog) -> None:
    """`changed` drives list_changed notifications, so churn must not trigger it.

    `last_seen` moves on every enumeration pass; if that counted as a change
    the hub would spam the host on every recovery tick.
    """
    prompts = [types.Prompt(name="p")]

    assert catalog.upsert_server("obs", status="ok", prompts=prompts) is True
    assert catalog.upsert_server("obs", status="ok", prompts=prompts) is False
    assert catalog.upsert_server("obs", status="ok", prompts=[types.Prompt(name="q")]) is True


def test_upsert_preserves_fields_it_was_not_given(catalog) -> None:
    """A prompts-only refresh must not wipe previously enumerated resources."""
    catalog.upsert_server("obs", status="ok", resources=[types.Resource(name="r", uri="o://r")])
    catalog.upsert_server("obs", status="ok", prompts=[types.Prompt(name="p")])

    assert [name for name, _ in catalog.all_resources()] == ["obs"]
    assert [name for name, _ in catalog.all_prompts()] == ["obs"]


def test_status_transition_counts_as_a_change(catalog) -> None:
    """ok -> degraded is worth re-rendering even with an identical payload."""
    catalog.upsert_server("obs", status="ok", prompts=[types.Prompt(name="p")])

    assert catalog.mark_degraded("obs", "connect failed") is True
    assert catalog.mark_degraded("obs", "connect failed") is False


def test_payload_differs_ignores_timestamp_churn() -> None:
    prior = {"status": "ok", "last_seen": "t1", "prompts": [{"name": "p"}]}
    current = {"status": "ok", "last_seen": "t2", "prompts": [{"name": "p"}]}

    assert _payload_differs(prior, current) is False
