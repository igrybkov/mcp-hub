"""Keychain-native auth manager for mcp-hub.

Secrets live in macOS Keychain (via keyring), not Ansible Vault.
Schema-as-source-of-truth: secrets are injected only if a schema entry names the env var.
Learned schemas (Tier-2) are persisted to XDG_STATE_HOME/mcp-hub/learned-auth.json.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import keyring
from keyring.errors import PasswordDeleteError

logger = logging.getLogger(__name__)

KEYCHAIN_SERVICE = "mcp-hub"

LEARNED_AUTH_PATH = (
    Path(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")))
    / "mcp-hub"
    / "learned-auth.json"
)


@dataclass
class SecretSpec:
    env_var: str
    label: str
    create_url: str | None = None
    sensitive: bool = True
    state: str = "present"  # "present" | "absent"


@dataclass
class AuthConfig:
    secrets: list[SecretSpec] = field(default_factory=list)


def keychain_key(server: str, env_var: str) -> str:
    return f"{server}:{env_var}"


def get_secret(server: str, env_var: str) -> str | None:
    try:
        return keyring.get_password(KEYCHAIN_SERVICE, keychain_key(server, env_var))
    except Exception as exc:
        logger.debug("keyring get failed for %s/%s: %s", server, env_var, exc)
        return None


def set_secret(server: str, env_var: str, value: str) -> None:
    keyring.set_password(KEYCHAIN_SERVICE, keychain_key(server, env_var), value)


def delete_secret(server: str, env_var: str) -> None:
    try:
        keyring.delete_password(KEYCHAIN_SERVICE, keychain_key(server, env_var))
    except PasswordDeleteError:
        pass
    except Exception as exc:
        logger.debug("keyring delete failed for %s/%s: %s", server, env_var, exc)


# --- learned schema store (Tier-2) ---


def load_learned() -> dict[str, AuthConfig]:
    if not LEARNED_AUTH_PATH.exists():
        return {}
    try:
        data = json.loads(LEARNED_AUTH_PATH.read_text())
        result: dict[str, AuthConfig] = {}
        for server_name, auth_data in data.items():
            secrets = [
                SecretSpec(
                    env_var=s["env_var"],
                    label=s.get("label", s["env_var"]),
                    create_url=s.get("create_url"),
                    sensitive=s.get("sensitive", True),
                    state=s.get("state", "present"),
                )
                for s in auth_data.get("secrets", [])
            ]
            result[server_name] = AuthConfig(secrets=secrets)
        return result
    except Exception as exc:
        logger.warning("Failed to load learned auth schemas: %s", exc)
        return {}


def save_learned(server: str, auth: AuthConfig) -> None:
    LEARNED_AUTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing = {}
    if LEARNED_AUTH_PATH.exists():
        try:
            existing = json.loads(LEARNED_AUTH_PATH.read_text())
        except Exception:
            pass
    existing[server] = {
        "secrets": [
            {
                "env_var": s.env_var,
                "label": s.label,
                **({"create_url": s.create_url} if s.create_url else {}),
                "sensitive": s.sensitive,
                "state": s.state,
            }
            for s in auth.secrets
        ]
    }
    LEARNED_AUTH_PATH.write_text(json.dumps(existing, indent=2))


def delete_learned(server: str, env_var: str | None = None) -> None:
    if not LEARNED_AUTH_PATH.exists():
        return
    try:
        data = json.loads(LEARNED_AUTH_PATH.read_text())
        if server not in data:
            return
        if env_var is None:
            del data[server]
        else:
            data[server]["secrets"] = [
                s for s in data[server].get("secrets", []) if s.get("env_var") != env_var
            ]
            if not data[server]["secrets"]:
                del data[server]
        LEARNED_AUTH_PATH.write_text(json.dumps(data, indent=2))
    except Exception as exc:
        logger.warning("Failed to update learned auth schemas: %s", exc)


def resolve_auth(server: str, declared: AuthConfig | None) -> AuthConfig | None:
    """Merge declared ∪ learned; declared wins on env_var collision."""
    learned_all = load_learned()
    learned = learned_all.get(server)

    if declared is None and learned is None:
        return None
    if declared is None:
        return learned
    if learned is None:
        return declared

    # Merge: declared wins on env_var collision
    declared_vars = {s.env_var for s in declared.secrets}
    merged = list(declared.secrets) + [s for s in learned.secrets if s.env_var not in declared_vars]
    return AuthConfig(secrets=merged)


def resolve_secrets(server: str, auth: AuthConfig) -> dict[str, str]:
    """{env_var: value} for present secrets found in Keychain — schema-driven only."""
    result: dict[str, str] = {}
    for s in auth.secrets:
        if s.state != "present":
            continue
        value = get_secret(server, s.env_var)
        if value is not None:
            result[s.env_var] = value
    return result


def auth_status(server: str, auth: AuthConfig) -> dict[str, Any]:
    present_secrets = [s for s in auth.secrets if s.state == "present"]
    secret_statuses = [
        {
            "env_var": s.env_var,
            "label": s.label,
            "stored": get_secret(server, s.env_var) is not None,
            **({"create_url": s.create_url} if s.create_url else {}),
        }
        for s in present_secrets
    ]
    stored_count = sum(1 for s in secret_statuses if s["stored"])
    total = len(secret_statuses)
    if total == 0:
        status = "unauthenticated"
    elif stored_count == 0:
        status = "unauthenticated"
    elif stored_count < total:
        status = "partial"
    else:
        status = "authenticated"
    return {"status": status, "secrets": secret_statuses}


def reconcile_absent(server: str, auth: AuthConfig) -> None:
    """Delete every Keychain value whose schema entry has state: absent. Idempotent."""
    for s in auth.secrets:
        if s.state == "absent":
            delete_secret(server, s.env_var)
