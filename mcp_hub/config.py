"""Config loader — merges multiple JSON/YAML files into a unified server registry.

Sources are taken from CONFIG_FILE env var (comma-separated paths).
When unset, defaults to ~/.config/mcp-hub/servers.json, ~/.config/mcp-hub/servers.yml,
.mcp.local.json, and .mcp.local.yml (relative paths resolved from CWD at startup,
which is typically the project root when spawned by Claude Code).
Both JSON and YAML formats are supported. Later files override earlier ones on
matching server name.

Supported server shapes:

  # stdio
  {
    "mcpServers": {
      "github": {
        "command": "gh-mcp",
        "args": ["--flag"],
        "env": {"GH_TOKEN": "..."},
        "description": "optional",
        "tags": ["optional"]
      }
    }
  }

  # streamable-http / sse
  {
    "mcpServers": {
      "everything": {
        "url": "https://everything.mcp.run/mcp",
        "transport": "streamable-http",
        "headers": {"Authorization": "Bearer ..."}
      }
    }
  }

YAML files use the same shape without the outer `mcpServers` wrapper required —
top-level mapping is accepted either way.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from mcp_hub.auth import AuthConfig, SecretSpec, reconcile_absent, resolve_auth

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_FILES = [
    "~/.config/mcp-hub/servers.json",
    "~/.config/mcp-hub/servers.yml",
    ".mcp.local.json",
    ".mcp.local.yml",
]


@dataclass
class ServerSpec:
    """Unified server configuration."""

    name: str
    transport: str  # "stdio" | "streamable-http" | "sse"
    # stdio fields
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    # http fields
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    # metadata
    description: str | None = None
    tags: list[str] = field(default_factory=list)
    disabled: bool = False
    # opt-in native enumeration (prompts/resources). When false (default), the
    # server stays fully lazy and is only spawned on demand via the meta-tools.
    expose_prompts: bool = False
    expose_resources: bool = False
    # Per-server connect + enumerate budget. Raise for Docker-backed or uvx
    # cold-start servers. Applies to exposed servers only.
    connect_timeout_seconds: float = 5.0
    auth: AuthConfig | None = None

    @property
    def is_exposed(self) -> bool:
        return self.expose_prompts or self.expose_resources

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> ServerSpec:
        common_kwargs: dict[str, Any] = {
            "description": data.get("description"),
            "tags": list(data.get("tags", [])),
            "disabled": bool(data.get("disabled", False)),
            "expose_prompts": bool(data.get("expose_prompts", False)),
            "expose_resources": bool(data.get("expose_resources", False)),
            "connect_timeout_seconds": float(data.get("connect_timeout_seconds", 5.0)),
        }
        # Parse auth block
        auth: AuthConfig | None = None
        auth_data = data.get("auth")
        if auth_data and isinstance(auth_data, dict):
            secrets = []
            for s in auth_data.get("secrets", []):
                if isinstance(s, dict) and "env_var" in s:
                    secrets.append(
                        SecretSpec(
                            env_var=s["env_var"],
                            label=s.get("label", s["env_var"]),
                            create_url=s.get("create_url"),
                            sensitive=bool(s.get("sensitive", True)),
                            state=s.get("state", "present"),
                        )
                    )
            auth = AuthConfig(secrets=secrets) if secrets else None
        if "url" in data:
            transport = data.get("transport", "streamable-http")
            return cls(
                name=name,
                transport=transport,
                url=data["url"],
                headers=dict(data.get("headers", {})),
                auth=auth,
                **common_kwargs,
            )
        return cls(
            name=name,
            transport="stdio",
            command=data.get("command"),
            args=list(data.get("args", [])),
            env=dict(data.get("env", {})),
            auth=auth,
            **common_kwargs,
        )


def _expand(path: str) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(path))).resolve()


def _load_file(path: Path) -> dict[str, Any]:
    raw = path.read_text()
    if path.suffix in (".yml", ".yaml"):
        data = yaml.safe_load(raw) or {}
    else:
        data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top-level must be a mapping, got {type(data).__name__}")
    # Accept both wrapped ({"mcpServers": {...}}) and unwrapped shapes
    if "mcpServers" in data and isinstance(data["mcpServers"], dict):
        return dict(data["mcpServers"])
    return data


def config_paths() -> list[Path]:
    """Resolve configured source paths from CONFIG_FILE env, or defaults."""
    raw = os.environ.get("CONFIG_FILE")
    sources = raw.split(",") if raw else DEFAULT_CONFIG_FILES
    return [_expand(p.strip()) for p in sources if p.strip()]


def compute_config_hash() -> str:
    """SHA256 over the raw bytes of every existing config source.

    Stable across runs when the files are unchanged; flips when any tracked
    source is added, removed, or edited. Used by the catalog to decide whether
    a stored enumeration is still valid.
    """
    h = hashlib.sha256()
    for path in config_paths():
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            data = b""
        h.update(str(path).encode("utf-8"))
        h.update(b"\0")
        h.update(data)
        h.update(b"\0\0")
    return h.hexdigest()


def load_servers() -> dict[str, ServerSpec]:
    """Load and merge server specs from all configured sources.

    Later sources override earlier ones by server name (shallow override).
    Missing files are skipped (logged at debug). Malformed files raise.
    """
    merged: dict[str, dict[str, Any]] = {}
    for path in config_paths():
        if not path.exists():
            logger.debug("Config file missing, skipping: %s", path)
            continue
        try:
            servers = _load_file(path)
        except Exception as e:
            logger.error("Failed to parse %s: %s", path, e)
            raise
        for name, spec in servers.items():
            if not isinstance(spec, dict):
                logger.warning("%s: skipping non-mapping entry %r", path, name)
                continue
            merged[name] = spec
        logger.info("Loaded %d server(s) from %s", len(servers), path)

    result: dict[str, ServerSpec] = {}
    for name, spec in merged.items():
        try:
            server = ServerSpec.from_dict(name, spec)
        except Exception as e:
            logger.error("Skipping invalid server spec %r: %s", name, e)
            continue
        if server.disabled:
            logger.info("Server %r is disabled — skipping", name)
            continue
        result[name] = server

    # Merge learned schemas and reconcile absent entries
    from mcp_hub.auth import load_learned

    learned_all = load_learned()
    for name, spec in result.items():
        learned = learned_all.get(name)
        if learned is not None or spec.auth is not None:
            merged_auth = resolve_auth(name, spec.auth)
            if merged_auth is not None:
                spec.auth = merged_auth
                reconcile_absent(name, merged_auth)
    return result
