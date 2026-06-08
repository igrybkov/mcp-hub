"""On-disk cache of exposed prompts and resources, keyed by child server.

The catalog lets the hub serve `list_prompts` / `list_resources` instantly on
warm start (cache hit) and return whatever subset of servers has completed
enumeration on cold start. Late-arriving servers update the catalog as they
come online; the hub then emits `list_changed` to the host.

File layout (~/.cache/mcp-hub/catalog.json):

    {
      "version": 1,
      "config_hash": "sha256-hex",
      "servers": {
        "obsidian": {
          "status": "ok" | "degraded",
          "last_seen": "2026-04-22T12:34:56Z",
          "error": null | "<reason>",
          "prompts": [ {name, description, arguments}, ... ],
          "resources": [ {uri, name, description, mimeType, size}, ... ],
          "resource_templates": [ {uriTemplate, name, description, mimeType}, ... ]
        }
      }
    }

Writes are atomic (tempfile + os.replace). All prompt/resource payloads are
Pydantic-serialized dicts so the catalog is self-contained — we never need
to re-call the child to reconstruct full metadata.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp import types

logger = logging.getLogger(__name__)

CATALOG_VERSION = 1
DEFAULT_CATALOG_PATH = Path(os.path.expanduser("~/.cache/mcp-hub/catalog.json"))


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _dump_models(items: list[Any]) -> list[dict[str, Any]]:
    """Pydantic v2 model_dump with aliases — preserves _meta and by-alias fields."""
    return [item.model_dump(mode="json", by_alias=True, exclude_none=True) for item in items]


class Catalog:
    """Thread-safe on-disk catalog of exposed prompts/resources per server.

    All reads serve from the in-memory snapshot loaded at construction time.
    `upsert_server` mutates in memory and schedules an atomic disk write.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or DEFAULT_CATALOG_PATH
        self._lock = threading.RLock()
        self._config_hash: str = ""
        # server_name -> entry dict
        self._servers: dict[str, dict[str, Any]] = {}

    # --------------------------------------------------------------- lifecycle

    def load(self, expected_hash: str) -> bool:
        """Populate the in-memory snapshot from disk if the config hash matches.

        Returns True if the disk catalog was loaded and is still valid (warm
        start), False otherwise (cold start — caller should enumerate from
        scratch).
        """
        with self._lock:
            self._config_hash = expected_hash
            if not self.path.exists():
                self._servers = {}
                return False
            try:
                payload = json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("catalog unreadable at %s: %s", self.path, exc)
                self._servers = {}
                return False
            if not isinstance(payload, dict):
                self._servers = {}
                return False
            if payload.get("version") != CATALOG_VERSION:
                logger.info(
                    "catalog version mismatch (%r != %d) — discarding",
                    payload.get("version"),
                    CATALOG_VERSION,
                )
                self._servers = {}
                return False
            if payload.get("config_hash") != expected_hash:
                logger.info("catalog config-hash mismatch — treating as cold start")
                self._servers = {}
                return False
            raw_servers = payload.get("servers", {})
            if not isinstance(raw_servers, dict):
                self._servers = {}
                return False
            self._servers = {
                name: entry for name, entry in raw_servers.items() if isinstance(entry, dict)
            }
            return True

    def save(self) -> None:
        """Atomically write the current snapshot to disk."""
        with self._lock:
            snapshot = {
                "version": CATALOG_VERSION,
                "config_hash": self._config_hash,
                "servers": dict(self._servers),
            }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=self.path.name + ".", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(snapshot, f, indent=2, default=str)
            os.replace(tmp_path, self.path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    # ---------------------------------------------------------------- mutation

    def upsert_server(
        self,
        name: str,
        *,
        status: str,
        prompts: list[types.Prompt] | None = None,
        resources: list[types.Resource] | None = None,
        resource_templates: list[types.ResourceTemplate] | None = None,
        error: str | None = None,
    ) -> bool:
        """Merge a server's enumeration result into the catalog.

        Returns True if the entry materially changed (prompts or resources
        differ from the prior snapshot), signalling to the caller that a
        `list_changed` notification is warranted.
        """
        entry: dict[str, Any] = {
            "status": status,
            "last_seen": _now_iso(),
            "error": error,
        }
        if prompts is not None:
            entry["prompts"] = _dump_models(prompts)
        if resources is not None:
            entry["resources"] = _dump_models(resources)
        if resource_templates is not None:
            entry["resource_templates"] = _dump_models(resource_templates)

        with self._lock:
            prior = self._servers.get(name, {})
            # Preserve fields not provided in this upsert (e.g., resources
            # when only prompts were refreshed).
            for key in ("prompts", "resources", "resource_templates"):
                if key not in entry and key in prior:
                    entry[key] = prior[key]
            changed = _payload_differs(prior, entry)
            self._servers[name] = entry
        if changed:
            self.save()
        return changed

    def mark_degraded(self, name: str, error: str) -> bool:
        """Record a failed enumeration attempt without clobbering prior data.

        Returns True if this is a fresh transition into degraded (so the caller
        may choose to notify). Prior prompt/resource payloads are retained so
        the host keeps seeing the last-known-good set until recovery.
        """
        with self._lock:
            prior = self._servers.get(name, {})
            entry = dict(prior)
            entry["status"] = "degraded"
            entry["error"] = error
            entry["last_seen"] = _now_iso()
            was_degraded = prior.get("status") == "degraded"
            self._servers[name] = entry
        if not was_degraded:
            self.save()
            return True
        return False

    def drop_server(self, name: str) -> None:
        """Remove a server (used when config no longer contains it)."""
        with self._lock:
            if name in self._servers:
                del self._servers[name]
                self.save()

    def set_config_hash(self, config_hash: str) -> None:
        with self._lock:
            self._config_hash = config_hash

    # ---------------------------------------------------------------- queries

    def all_prompts(self) -> list[tuple[str, dict[str, Any]]]:
        """Return [(server_name, prompt_dict), ...] for every exposed prompt."""
        out: list[tuple[str, dict[str, Any]]] = []
        with self._lock:
            for server, entry in self._servers.items():
                for prompt in entry.get("prompts") or []:
                    out.append((server, prompt))
        return out

    def all_resources(self) -> list[tuple[str, dict[str, Any]]]:
        out: list[tuple[str, dict[str, Any]]] = []
        with self._lock:
            for server, entry in self._servers.items():
                for resource in entry.get("resources") or []:
                    out.append((server, resource))
        return out

    def all_resource_templates(self) -> list[tuple[str, dict[str, Any]]]:
        out: list[tuple[str, dict[str, Any]]] = []
        with self._lock:
            for server, entry in self._servers.items():
                for tpl in entry.get("resource_templates") or []:
                    out.append((server, tpl))
        return out

    def server_entry(self, name: str) -> dict[str, Any] | None:
        with self._lock:
            return self._servers.get(name)

    def server_names(self) -> list[str]:
        with self._lock:
            return list(self._servers.keys())


def _payload_differs(prior: dict[str, Any], current: dict[str, Any]) -> bool:
    """Compare just the payload-shaped fields — ignore status/last_seen churn."""
    for key in ("prompts", "resources", "resource_templates"):
        if prior.get(key) != current.get(key):
            return True
    # Transition between ok and degraded is noteworthy even without payload
    # changes, because the UI may want to re-render.
    return prior.get("status") != current.get("status")
