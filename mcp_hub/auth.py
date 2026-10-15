"""Keychain-native auth manager for mcp-hub.

Secrets live in macOS Keychain (via keyring), not Ansible Vault.
Schema-as-source-of-truth: secrets are injected only if a schema entry names the env var.
Learned schemas (Tier-2) are persisted to XDG_STATE_HOME/mcp-hub/learned-auth.json.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeGuard

import keyring
from keyring.errors import PasswordDeleteError

logger = logging.getLogger(__name__)

KEYCHAIN_SERVICE = "mcp-hub"
COMMAND_SOURCE_SERVICE = "mcp-hub:credential-sources"
COMMAND_TIMEOUT_SECONDS = 10
COMMAND_OUTPUT_MAX = 64 * 1024

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


def _parse_secrets_raw(raw: Any) -> list[dict[str, Any]]:
    """Parse secrets from dict (new) or list (legacy) format into a flat list.

    New format:  {ENV_VAR: {label, create_url, sensitive, state}}
    Legacy format: [{env_var, label, create_url, sensitive, state}]
    """
    if isinstance(raw, dict):
        return [{"env_var": k, **v} for k, v in raw.items() if isinstance(v, dict)]
    if isinstance(raw, list):
        return [s for s in raw if isinstance(s, dict) and "env_var" in s]
    return []


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


def _valid_command(command: Any) -> TypeGuard[list[str]]:
    return (
        isinstance(command, list)
        and bool(command)
        and all(isinstance(arg, str) and "\0" not in arg for arg in command)
        and bool(command[0])
    )


def get_command_source(server: str, env_var: str) -> list[str] | None:
    try:
        raw = keyring.get_password(COMMAND_SOURCE_SERVICE, keychain_key(server, env_var))
    except Exception:
        raise RuntimeError(f"Cannot read credential source for {server}/{env_var}") from None
    if raw is None:
        return None
    try:
        command = json.loads(raw)
    except (ValueError, TypeError):
        raise RuntimeError(f"Invalid credential source for {server}/{env_var}") from None
    if not _valid_command(command):
        raise RuntimeError(f"Invalid credential source for {server}/{env_var}")
    return command


def set_command_source(server: str, env_var: str, command: list[str]) -> None:
    if not _valid_command(command):
        raise ValueError("Expected a nonempty command argument list without NUL characters")
    try:
        keyring.set_password(
            COMMAND_SOURCE_SERVICE, keychain_key(server, env_var), json.dumps(command)
        )
    except Exception:
        raise RuntimeError(f"Cannot store credential source for {server}/{env_var}") from None


def delete_command_source(server: str, env_var: str) -> None:
    try:
        keyring.delete_password(COMMAND_SOURCE_SERVICE, keychain_key(server, env_var))
    except PasswordDeleteError:
        pass
    except Exception:
        raise RuntimeError(f"Cannot delete credential source for {server}/{env_var}") from None


def delete_secret(server: str, env_var: str) -> None:
    delete_command_source(server, env_var)
    try:
        keyring.delete_password(KEYCHAIN_SERVICE, keychain_key(server, env_var))
    except PasswordDeleteError:
        pass
    except Exception:
        raise RuntimeError(f"Cannot delete secret for {server}/{env_var}") from None


async def _run_credential_command(server: str, env_var: str, command: list[str]) -> str:
    error = f"Credential command for {server}/{env_var}"
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=os.name == "posix",
        )
    except (OSError, ValueError):
        raise RuntimeError(f"{error} could not start") from None
    try:
        async with asyncio.timeout(COMMAND_TIMEOUT_SECONDS):
            assert process.stdout is not None
            output = bytearray()
            while chunk := await process.stdout.read(4096):
                output.extend(chunk)
                if len(output) > COMMAND_OUTPUT_MAX:
                    raise RuntimeError(f"{error} exceeded the output limit")
            code = await process.wait()
            if code != 0:
                raise RuntimeError(f"{error} exited with status {code}")
            try:
                value = output.decode("utf-8").rstrip("\r\n")
            except UnicodeDecodeError:
                raise RuntimeError(f"{error} returned invalid UTF-8") from None
            if not value or "\0" in value:
                raise RuntimeError(f"{error} returned an empty or invalid value")
            return value
    except TimeoutError:
        raise RuntimeError(f"{error} timed out") from None
    finally:
        # Kill the group too: descendants can keep stdout open after the command exits.
        with suppress(ProcessLookupError):
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            elif process.returncode is None:
                process.kill()
        await process.wait()


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
                for s in _parse_secrets_raw(auth_data.get("secrets", {}))
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
        "secrets": {
            s.env_var: {
                "label": s.label,
                **({"create_url": s.create_url} if s.create_url else {}),
                "sensitive": s.sensitive,
                "state": s.state,
            }
            for s in auth.secrets
        }
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
            secrets = data[server].get("secrets", {})
            if isinstance(secrets, dict):
                secrets.pop(env_var, None)
                data[server]["secrets"] = secrets
            else:
                data[server]["secrets"] = [s for s in secrets if s.get("env_var") != env_var]
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


async def resolve_secrets(server: str, auth: AuthConfig) -> dict[str, str]:
    """Resolve present schema entries from local commands or the keychain."""
    result: dict[str, str] = {}
    for s in auth.secrets:
        if s.state != "present":
            continue
        command = get_command_source(server, s.env_var)
        value = (
            await _run_credential_command(server, s.env_var, command)
            if command is not None
            else get_secret(server, s.env_var)
        )
        if value is not None:
            result[s.env_var] = value
    return result


def auth_status(server: str, auth: AuthConfig) -> dict[str, Any]:
    present_secrets = [s for s in auth.secrets if s.state == "present"]
    secret_statuses = []
    for s in present_secrets:
        command = get_command_source(server, s.env_var)
        stored = command is None and get_secret(server, s.env_var) is not None
        secret_statuses.append(
            {
                "env_var": s.env_var,
                "label": s.label,
                "stored": stored,
                "source": "command" if command is not None else "keychain",
                "configured": command is not None or stored,
                **({"create_url": s.create_url} if s.create_url else {}),
            }
        )
    configured_count = sum(1 for s in secret_statuses if s["configured"])
    total = len(secret_statuses)
    if total == 0:
        status = "unauthenticated"
    elif configured_count == 0:
        status = "unauthenticated"
    elif configured_count < total:
        status = "partial"
    elif any(s["source"] == "command" for s in secret_statuses):
        status = "configured"
    else:
        status = "authenticated"
    return {"status": status, "secrets": secret_statuses}


def reconcile_absent(server: str, auth: AuthConfig) -> None:
    """Delete stored values and command sources for absent schema entries."""
    for s in auth.secrets:
        if s.state == "absent":
            delete_secret(server, s.env_var)
