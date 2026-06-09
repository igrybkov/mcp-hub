#!/usr/bin/env python3
"""mcp-hub CLI — scriptable access to configured MCP servers.

Uses the same config loading as the server, so CONFIG_FILE points at the same
sources. Each subcommand spawns the needed child server on demand and prints
JSON to stdout.

Examples:
    mcp-hub list
    mcp-hub list --filter monitoring
    mcp-hub tools github --summary
    mcp-hub tools github --tool createIssue
    mcp-hub search "deploy"
    mcp-hub call github listIssues --args '{"repo": "my/repo"}'
    mcp-hub call github listIssues --args-file ./args.json
"""

from __future__ import annotations

import asyncio
import getpass
import importlib.resources
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import click
import yaml
from dotenv import load_dotenv

from mcp_hub.config import config_paths, load_servers
from mcp_hub.proxy import ProxyClient
from mcp_hub.search import search as do_search

load_dotenv()

logger = logging.getLogger("mcp-hub.cli")


def _print(payload: Any) -> None:
    click.echo(json.dumps(payload, indent=2, default=str))


def _die(msg: str, code: int = 1) -> None:
    click.echo(f"error: {msg}", err=True)
    sys.exit(code)


def _run_async(coro, *, server: str | None = None) -> Any:
    """Run a coroutine, converting unhandled exceptions to clean CLI errors.

    Re-raises SystemExit (from _die calls inside the coroutine) unchanged.
    Converts ExceptionGroups (anyio task-group failures) to a one-line error
    that points the user at the server's own stderr output.
    """
    try:
        return asyncio.run(coro)
    except SystemExit:
        raise
    except BaseException as exc:
        prefix = f"server '{server}': " if server else ""
        if hasattr(exc, "exceptions"):  # BaseExceptionGroup / ExceptionGroup
            _die(f"{prefix}failed to connect — see server output above")
        _die(f"{prefix}{exc}")


def _parse_args(args: str | None, args_file: str | None) -> dict[str, Any]:
    if args and args_file:
        _die("use either --args or --args-file, not both")
    if args_file:
        raw = Path(args_file).read_text()
    elif args:
        raw = args
    else:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        _die(f"invalid JSON: {e}")
    if not isinstance(parsed, dict):
        _die("arguments must be a JSON object")
    return parsed  # type: ignore[return-value]


# --- config-editing helpers (shared by `config path`, `validate`, `add`) ---

DEFAULT_WRITE_TARGET = "~/.config/mcp-hub/servers.yml"

# Keys whose values are treated as secrets (keychain), not plaintext env/headers.
# Matches the plan's `*TOKEN/KEY/SECRET/PASSWORD*` plus connection strings/creds.
_SECRET_KEY_RE = re.compile(
    r"(TOKEN|SECRET|PASSWORD|PASSWD|PWD|KEY|CONNECTION_STRING|CREDENTIAL)",
    re.IGNORECASE,
)


def _expand_path(path: str) -> Path:
    return Path(os.path.expanduser(os.path.expandvars(path))).resolve()


def _path_format(path: Path) -> str:
    return "yaml" if path.suffix in (".yml", ".yaml") else "json"


def _resolve_write_target() -> Path:
    """The file `add` writes to: highest-precedence existing source, else default.

    When no source exists yet, prefer the documented global YAML file if it's one
    of the configured sources; otherwise create the highest-precedence source so
    ``load_servers()`` will actually read it.
    """
    paths = config_paths()
    existing = [p for p in paths if p.exists()]
    if existing:
        return existing[-1]
    default = _expand_path(DEFAULT_WRITE_TARGET)
    if default in paths or not paths:
        return default
    return paths[-1]


def _is_secretish_key(key: str) -> bool:
    return bool(_SECRET_KEY_RE.search(key))


def _is_placeholder(value: str) -> bool:
    """True for empty values or env-var references (``${X}`` / ``$X``)."""
    v = (value or "").strip()
    return not v or "${" in v or v.startswith("$")


def _header_is_secretish(key: str, value: str) -> bool:
    if _is_secretish_key(key):
        return not _is_placeholder(value)
    if key.lower() in ("authorization", "proxy-authorization"):
        return not _is_placeholder(value)
    return False


def _titlecase_label(env_var: str) -> str:
    return env_var.replace("_", " ").strip().title() or env_var


def _parse_config_text(path: Path) -> dict[str, Any]:
    """Parse a config file to its top-level mapping. Raises on malformed input."""
    raw = path.read_text()
    if _path_format(path) == "yaml":
        data = yaml.safe_load(raw) or {}
    else:
        data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("top-level must be a mapping")
    return data


def _servers_map(top: dict[str, Any]) -> dict[str, Any]:
    """The sub-dict holding server entries (unwrapping ``mcpServers`` if present)."""
    if isinstance(top.get("mcpServers"), dict):
        return top["mcpServers"]
    return top


def _load_config_doc(path: Path) -> tuple[dict[str, Any], dict[str, Any], bool]:
    """Return (full_doc, servers_map, wrapped) for an existing config file.

    ``servers_map`` is the sub-dict holding server entries (the ``mcpServers``
    value when wrapped, otherwise ``full_doc`` itself), so mutating it mutates
    ``full_doc``.
    """
    try:
        data = _parse_config_text(path)
    except Exception as exc:
        _die(f"{path}: {exc}")
    wrapped = isinstance(data.get("mcpServers"), dict)
    return data, _servers_map(data), wrapped


def _dump_config_doc(path: Path, full_doc: dict[str, Any]) -> None:
    if _path_format(path) == "yaml":
        text = yaml.safe_dump(
            full_doc, sort_keys=False, default_flow_style=False, allow_unicode=True
        )
    else:
        text = json.dumps(full_doc, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "-v",
    "--verbose",
    is_flag=True,
    help="Enable debug logging to stderr (also raises the level of `mcp-hub server`).",
)
@click.pass_context
def main(ctx: click.Context, verbose: bool) -> None:
    """MCP Hub CLI — invoke configured MCP servers from the shell."""
    ctx.obj = {"verbose": verbose}
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


@main.command("server")
@click.pass_context
def cmd_server(ctx: click.Context) -> None:
    """Run the MCP Hub server over stdio.

    This is the entry point MCP clients launch. It speaks JSON-RPC on
    stdin/stdout, so nothing is printed to stdout here — all logging goes to
    stderr and the log file (see MCP_HUB_LOG_FILE). Pass the global -v/--verbose
    flag (`mcp-hub -v server`) to log at DEBUG instead of INFO.
    """
    # Imported lazily so ordinary CLI commands stay fast and free of the
    # server module's logging/dotenv configuration.
    from mcp_hub.server import run

    verbose = bool(ctx.obj and ctx.obj.get("verbose"))
    run(verbose=verbose)


@main.command("list")
@click.option("-f", "--filter", "needle", help="Substring filter on name/description/tags.")
@click.option("--names-only", is_flag=True, help="Print server names only, one per line.")
def cmd_list(needle: str | None, names_only: bool) -> None:
    """List configured MCP servers."""
    servers = load_servers()
    rows = []
    for name in sorted(servers):
        s = servers[name]
        if needle:
            hay = " ".join([s.name, s.description or "", " ".join(s.tags)]).lower()
            if needle.lower() not in hay:
                continue
        rows.append(s)
    if names_only:
        for s in rows:
            click.echo(s.name)
        return
    _print(
        {
            "count": len(rows),
            "servers": [
                {
                    "name": s.name,
                    "transport": s.transport,
                    "description": s.description,
                    "tags": s.tags,
                }
                for s in rows
            ],
        }
    )


@main.command("tools")
@click.argument("server")
@click.option("--summary", is_flag=True, help="Return only names and descriptions.")
@click.option(
    "--tool",
    "tool_names",
    multiple=True,
    help="Return full schemas for named tools only.",
)
def cmd_tools(server: str, summary: bool, tool_names: tuple[str, ...]) -> None:
    """List tools for SERVER (spawns the server if needed)."""

    async def _run() -> dict[str, Any]:
        servers = load_servers()
        if server not in servers:
            _die(f"unknown server: {server}")
        async with ProxyClient(servers) as proxy:
            tools = await proxy.list_tools(server)
        if tool_names:
            wanted = set(tool_names)
            tools = [t for t in tools if t.name in wanted]
            return {
                "server": server,
                "tools": [
                    {
                        "name": t.name,
                        "description": t.description or "",
                        "inputSchema": t.inputSchema,
                    }
                    for t in tools
                ],
            }
        if summary:
            return {
                "server": server,
                "tools": [{"name": t.name, "description": t.description or ""} for t in tools],
            }
        return {
            "server": server,
            "tools": [
                {
                    "name": t.name,
                    "description": t.description or "",
                    "inputSchema": t.inputSchema,
                }
                for t in tools
            ],
        }

    _print(_run_async(_run(), server=server))


@main.command("call")
@click.argument("server")
@click.argument("tool")
@click.option("--args", "args_json", help="Tool arguments as JSON object.")
@click.option("--args-file", help="Read tool arguments from a JSON file.")
def cmd_call(server: str, tool: str, args_json: str | None, args_file: str | None) -> None:
    """Call TOOL on SERVER with optional JSON ARGS."""
    args = _parse_args(args_json, args_file)

    async def _run() -> dict[str, Any]:
        servers = load_servers()
        if server not in servers:
            _die(f"unknown server: {server}")
        async with ProxyClient(servers) as proxy:
            result = await proxy.call_tool(server, tool, args)
        content = []
        for block in result.content:
            if getattr(block, "type", None) == "text":
                content.append({"type": "text", "text": block.text})
            else:
                content.append({"type": getattr(block, "type", "?"), "repr": str(block)})
        return {
            "server": server,
            "tool": tool,
            "isError": bool(result.isError),
            "content": content,
        }

    _print(_run_async(_run(), server=server))


@main.command("search")
@click.argument("query")
@click.option("--limit", default=20, show_default=True, type=int)
@click.option(
    "--load",
    is_flag=True,
    help="Load tool schemas for ALL servers before searching (slow; spawns every server).",
)
def cmd_search(query: str, limit: int, load: bool) -> None:
    """Search server metadata (and optionally tools) for QUERY."""

    async def _run() -> dict[str, Any]:
        servers = load_servers()
        tools_by_server: dict[str, Any] = {}
        if load:
            async with ProxyClient(servers) as proxy:
                for name in servers:
                    try:
                        tools_by_server[name] = await proxy.list_tools(name)
                    except Exception as e:
                        logger.warning("skipping %s: %s", name, e)
        hits = do_search(query, servers, tools_by_server, limit=limit)
        return {"count": len(hits), "hits": [h.to_dict() for h in hits]}

    _print(asyncio.run(_run()))


@main.group("auth")
def cmd_auth() -> None:
    """Manage keychain secrets for MCP servers."""


@cmd_auth.command("status")
@click.option("--server", default=None, help="Show status for a specific server only.")
def cmd_auth_status(server: str | None) -> None:
    """Show auth status for all servers with auth schemas."""
    from mcp_hub.auth import auth_status as get_auth_status
    from mcp_hub.auth import resolve_auth

    servers = load_servers()
    rows = []
    check = {server: servers[server]} if server and server in servers else servers
    if server and server not in servers:
        _die(f"unknown server: {server}")
    for name in sorted(check):
        spec = check[name]
        auth = resolve_auth(name, spec.auth)
        if auth is None:
            continue
        status = get_auth_status(name, auth)
        for s in status["secrets"]:
            rows.append(
                (
                    name,
                    s["env_var"],
                    s["label"],
                    "✓" if s["stored"] else "✗",
                    status["status"],
                )
            )
    if not rows:
        click.echo("No servers with auth schemas found.")
        return
    click.echo(f"{'SERVER':<25} {'ENV_VAR':<40} {'LABEL':<35} {'STORED':<8} {'STATUS'}")
    click.echo("-" * 115)
    for name, env_var, label, stored, status_val in rows:
        click.echo(f"{name:<25} {env_var:<40} {label:<35} {stored:<8} {status_val}")


@cmd_auth.command("provision")
@click.argument("server", required=False)
@click.option(
    "--all",
    "all_servers",
    is_flag=True,
    help="Provision all servers with auth schemas.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Re-prompt and overwrite secrets that are already stored (e.g. rotated/expired keys).",
)
def cmd_auth_provision(server: str | None, all_servers: bool, force: bool) -> None:
    """Collect and store secrets for SERVER (or all servers with --all)."""
    from mcp_hub.auth import get_secret, resolve_auth, set_secret

    servers = load_servers()
    targets: list[str] = []

    if all_servers:
        targets = sorted(
            name for name, spec in servers.items() if resolve_auth(name, spec.auth) is not None
        )
        if not targets:
            click.echo("No servers with auth schemas found.")
            return
    elif server:
        if server not in servers:
            _die(f"unknown server: {server}")
        auth = resolve_auth(server, servers[server].auth)
        if auth is None:
            _die(f"server '{server}' has no auth schema")
        targets = [server]
    else:
        click.echo("Specify a server name or use --all")
        raise SystemExit(1)

    for name in targets:
        spec = servers[name]
        auth = resolve_auth(name, spec.auth)
        if auth is None:
            continue
        present = [s for s in auth.secrets if s.state == "present"]
        if not present:
            continue
        click.echo(f"\n--- {name} ---")
        for secret in present:
            existing = get_secret(name, secret.env_var)
            if existing is not None and not force:
                click.echo(f"  {secret.label} ({secret.env_var}): already stored [skip]")
                continue
            if existing is not None:
                click.echo(
                    f"  {secret.label} ({secret.env_var}): already stored"
                    " — leave empty to keep current value"
                )
            if secret.create_url:
                click.echo(f"  {secret.label} ({secret.env_var})")
                click.echo(f"    Create one at: {secret.create_url}")
            if secret.sensitive:
                value = getpass.getpass(f"  Enter {secret.label}: ")
            else:
                value = click.prompt(f"  Enter {secret.label}")
            if value:
                set_secret(name, secret.env_var, value)
                click.echo("  Stored ✓")
            elif existing is not None:
                click.echo("  Kept existing (empty input)")
            else:
                click.echo("  Skipped (empty input)")

    click.echo(
        "\nDone. If mcp-hub is running, call the 'reload' tool or restart to pick up changes."
    )


@cmd_auth.command("rm")
@click.argument("server")
@click.argument("env_var", required=False)
def cmd_auth_rm(server: str, env_var: str | None) -> None:
    """Delete stored secret(s) for SERVER. If ENV_VAR given, remove only that secret."""
    from mcp_hub.auth import delete_learned, delete_secret, get_secret

    servers = load_servers()
    if server not in servers:
        _die(f"unknown server: {server}")

    if env_var:
        if get_secret(server, env_var) is None:
            click.echo(f"No secret stored for {server}/{env_var}")
        else:
            delete_secret(server, env_var)
            click.echo(f"Deleted {server}/{env_var} from keychain")
        delete_learned(server, env_var)
    else:
        from mcp_hub.auth import resolve_auth

        auth = resolve_auth(server, servers[server].auth)
        if auth is None:
            click.echo(f"No auth schema for server '{server}'")
            return
        removed = 0
        for s in auth.secrets:
            if get_secret(server, s.env_var) is not None:
                delete_secret(server, s.env_var)
                click.echo(f"Deleted {server}/{s.env_var}")
                removed += 1
        delete_learned(server)
        if removed == 0:
            click.echo(f"No secrets stored for '{server}'")


@cmd_auth.command("promote")
@click.argument("server")
def cmd_auth_promote(server: str) -> None:
    """Print YAML auth.secrets block for a learned schema (to paste into profile config)."""
    from mcp_hub.auth import load_learned

    learned = load_learned()
    if server not in learned:
        _die(f"no learned schema for server '{server}'")
    auth = learned[server]
    click.echo(f"# Add to your profile config under mcp_servers entry for '{server}':")
    click.echo("auth:")
    click.echo("  secrets:")
    for s in auth.secrets:
        click.echo(f"    - env_var: {s.env_var}")
        click.echo(f"      label: {s.label}")
        if s.create_url:
            click.echo(f"      create_url: {s.create_url}")
        if not s.sensitive:
            click.echo("      sensitive: false")


def _detect_runner() -> tuple[str, list[str]]:
    """Return (command, args) for launching the ``mcp-hub server`` subcommand.

    Inspects the parent process to detect if we were launched via ``uvx``.
    When found, reuses the same ``--from`` spec so the server entry in the
    client config matches exactly how the user invoked the CLI.

    Falls back to ``("mcp-hub", ["server"])`` when parent inspection fails or
    the parent is not uvx (e.g. installed with ``uv tool install`` or ``pip``).
    """
    try:
        ppid = os.getppid()
        proc = subprocess.run(
            ["ps", "-p", str(ppid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=3,
        )
        parent_line = proc.stdout.strip()
    except Exception:
        parent_line = ""

    try:
        parts = shlex.split(parent_line) if parent_line else []
    except ValueError:
        parts = parent_line.split() if parent_line else []

    prog = Path(parts[0]).name if parts else ""

    if prog == "uvx":
        from_spec: str | None = None
        i = 1
        while i < len(parts):
            arg = parts[i]
            if arg in ("--from", "-f") and i + 1 < len(parts):
                from_spec = parts[i + 1]
                break
            if arg.startswith("--from="):
                from_spec = arg[len("--from=") :]
                break
            i += 1

        if from_spec is None:
            from_spec = "mcp-hub"

        return "uvx", ["--from", from_spec, "mcp-hub", "server"]

    return "mcp-hub", ["server"]


@main.command("install")
@click.option(
    "--config",
    "config_path",
    default=".mcp.json",
    show_default=True,
    help="MCP client config file to install into.",
)
@click.option(
    "--name",
    default="mcp-hub",
    show_default=True,
    help="Key to use under mcpServers.",
)
@click.option(
    "--runner",
    default=None,
    metavar="CMD",
    help=(
        "Override the runner, e.g. 'uvx --from mcp-hub'. "
        "'mcp-hub server' is appended automatically."
    ),
)
@click.option("--dry-run", is_flag=True, help="Print the entry without writing the file.")
def cmd_install(config_path: str, name: str, runner: str | None, dry_run: bool) -> None:
    """Install mcp-hub into an MCP client config file.

    Auto-detects the runner from the current process: if launched via uvx the
    same --from spec is reused, otherwise falls back to 'mcp-hub server'.

    \b
    Examples:
      mcp-hub install
      mcp-hub install --config ~/Library/Application\\ Support/Claude/claude_desktop_config.json
      mcp-hub install --config .mcp.json --dry-run
      mcp-hub install --config .mcp.json --runner 'uvx --from mcp-hub'
    """
    if runner is not None:
        try:
            parts = shlex.split(runner)
        except ValueError as e:
            _die(f"invalid --runner value: {e}")
        if not parts:
            _die("--runner value is empty")
        cmd, args = parts[0], parts[1:] + ["mcp-hub", "server"]
    else:
        cmd, args = _detect_runner()

    entry: dict[str, Any] = {"command": cmd}
    if args:
        entry["args"] = args

    if dry_run:
        click.echo(json.dumps({"mcpServers": {name: entry}}, indent=2))
        return

    path = Path(config_path)
    if path.exists():
        try:
            existing: dict[str, Any] = json.loads(path.read_text())
        except json.JSONDecodeError as e:
            _die(f"could not parse {path}: {e}")
    else:
        existing = {}

    if not isinstance(existing.get("mcpServers"), dict):
        existing["mcpServers"] = {}

    action = "Updated" if name in existing["mcpServers"] else "Installed"
    existing["mcpServers"][name] = entry

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(existing, indent=2) + "\n")

    click.echo(f"{action} '{name}' in {path}")
    click.echo(f"  command: {cmd}")
    if args:
        click.echo(f"  args:    {args}")


@main.group("config")
def cmd_config() -> None:
    """Inspect mcp-hub configuration."""


@cmd_config.command("path")
def cmd_config_path() -> None:
    """Print resolved config sources and the file `add` will write to."""
    paths = config_paths()
    sources = [{"path": str(p), "exists": p.exists(), "format": _path_format(p)} for p in paths]
    target = _resolve_write_target()
    _print(
        {
            "sources": sources,
            "write_target": str(target),
            "write_target_exists": target.exists(),
            "write_target_format": _path_format(target),
        }
    )


def _lint_server(
    name: str,
    spec: dict[str, Any],
    source: str,
    errors: list[dict[str, Any]],
    warnings: list[dict[str, Any]],
) -> None:
    has_url = bool(spec.get("url"))
    has_command = bool(spec.get("command"))
    if has_url and has_command:
        errors.append(
            {
                "server": name,
                "source": source,
                "code": "command_and_url",
                "message": f"{name}: has both 'command' and 'url' — choose one.",
            }
        )
    elif not has_url and not has_command:
        errors.append(
            {
                "server": name,
                "source": source,
                "code": "missing_command_or_url",
                "message": f"{name}: needs 'command' (stdio) or 'url' (http/sse).",
            }
        )

    transport = spec.get("transport")
    if transport is not None:
        if has_url:
            if transport not in ("streamable-http", "sse"):
                errors.append(
                    {
                        "server": name,
                        "source": source,
                        "code": "invalid_transport",
                        "message": (
                            f"{name}: transport '{transport}' is not 'streamable-http' or 'sse'."
                        ),
                    }
                )
        else:
            warnings.append(
                {
                    "server": name,
                    "source": source,
                    "code": "transport_without_url",
                    "message": f"{name}: 'transport' is ignored without 'url'.",
                }
            )

    env = spec.get("env")
    if isinstance(env, dict):
        for key, value in env.items():
            if (
                _is_secretish_key(str(key))
                and isinstance(value, str)
                and not _is_placeholder(value)
            ):
                errors.append(
                    {
                        "server": name,
                        "source": source,
                        "code": "raw_secret",
                        "field": f"env.{key}",
                        "message": (
                            f"{name}: env.{key} looks like a raw secret — move it to "
                            "auth.secrets (re-run `mcp-hub add` or hand-edit) and provision it."
                        ),
                    }
                )

    headers = spec.get("headers")
    if isinstance(headers, dict):
        for key, value in headers.items():
            if isinstance(value, str) and _header_is_secretish(str(key), value):
                errors.append(
                    {
                        "server": name,
                        "source": source,
                        "code": "raw_secret",
                        "field": f"headers.{key}",
                        "message": (
                            f"{name}: headers.{key} looks like a raw secret — keep "
                            "credentials out of config (use a ${{VAR}} placeholder "
                            "or auth.secrets)."
                        ),
                    }
                )


@main.command("validate")
@click.option(
    "--config",
    "config_path",
    default=None,
    help="Validate a single config file instead of all configured sources.",
)
def cmd_validate(config_path: str | None) -> None:
    """Lint config specs; flag raw secrets. Non-zero exit on error."""
    if config_path:
        paths = [_expand_path(config_path)]
    else:
        paths = config_paths()

    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    merged: dict[str, tuple[dict[str, Any], str]] = {}
    checked: list[str] = []

    for path in paths:
        if not path.exists():
            continue
        checked.append(str(path))
        try:
            servers_map = _servers_map(_parse_config_text(path))
        except Exception as exc:
            errors.append({"source": str(path), "code": "parse_error", "message": str(exc)})
            continue
        for name, spec in servers_map.items():
            if not isinstance(spec, dict):
                errors.append(
                    {
                        "server": name,
                        "source": str(path),
                        "code": "not_a_mapping",
                        "message": f"{name}: entry is not a mapping.",
                    }
                )
                continue
            merged[name] = (spec, str(path))

    for name, (spec, source) in merged.items():
        _lint_server(name, spec, source, errors, warnings)

    ok = not errors
    _print(
        {
            "ok": ok,
            "sources": checked,
            "server_count": len(merged),
            "errors": errors,
            "warnings": warnings,
        }
    )
    if not ok:
        sys.exit(1)


def _parse_kv_list(pairs: tuple[str, ...], flag: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in pairs:
        if "=" not in item:
            _die(f"--{flag} expects KEY=VALUE, got: {item!r}")
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            _die(f"--{flag} has an empty key: {item!r}")
        out[key] = value
    return out


def _parse_secret_flags(values: tuple[str, ...]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in values:
        parts = item.split(":", 2)
        env_var = parts[0].strip()
        if not env_var:
            _die(f"--secret has an empty env var: {item!r}")
        label = (
            parts[1].strip() if len(parts) > 1 and parts[1].strip() else _titlecase_label(env_var)
        )
        secret: dict[str, Any] = {"env_var": env_var, "label": label}
        if len(parts) > 2 and parts[2].strip():
            secret["create_url"] = parts[2].strip()
        out.append(secret)
    return out


def _parse_from_json(raw: str, name: str) -> dict[str, Any]:
    """Extract a single server spec dict from a docs snippet (wrapped or not)."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        _die(f"--from-json: invalid JSON: {exc}")
    if not isinstance(data, dict):
        _die("--from-json: expected a JSON object")
    if isinstance(data.get("mcpServers"), dict):
        servers = data["mcpServers"]
        if name in servers:
            spec = servers[name]
        elif len(servers) == 1:
            spec = next(iter(servers.values()))
        else:
            _die(
                "--from-json: multiple servers found; name one of "
                f"{sorted(servers)} as the <name> argument"
            )
        if not isinstance(spec, dict):
            _die("--from-json: server entry must be an object")
        return dict(spec)
    if "command" in data or "url" in data:
        return dict(data)
    if len(data) == 1:
        only = next(iter(data.values()))
        if isinstance(only, dict):
            return dict(only)
    _die("--from-json: could not find a server spec (expected 'command' or 'url')")


def _build_entry(
    *,
    command: str | None,
    url: str | None,
    transport: str | None,
    args: list[str],
    env: dict[str, str],
    headers: dict[str, str],
    secrets: list[dict[str, Any]],
    description: str | None,
    tags: list[str],
    expose_prompts: bool,
    expose_resources: bool,
    connect_timeout: float | None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {}
    if url:
        entry["url"] = url
        if transport and transport != "streamable-http":
            entry["transport"] = transport
        if headers:
            entry["headers"] = headers
    else:
        entry["command"] = command
        if args:
            entry["args"] = args
        if env:
            entry["env"] = env
    if secrets:
        entry["auth"] = {"secrets": secrets}
    if description:
        entry["description"] = description
    if tags:
        entry["tags"] = tags
    if expose_prompts:
        entry["expose_prompts"] = True
    if expose_resources:
        entry["expose_resources"] = True
    if connect_timeout is not None:
        entry["connect_timeout_seconds"] = connect_timeout
    return entry


@main.command("add")
@click.argument("name")
@click.option("--command", help="Executable for a stdio server.")
@click.option("--arg", "arg_list", multiple=True, help="Argument for the command (repeatable).")
@click.option(
    "--env", "env_list", multiple=True, metavar="K=V", help="Plaintext env var (repeatable)."
)
@click.option("--url", help="Endpoint URL for an http/sse server.")
@click.option("--transport", type=click.Choice(["streamable-http", "sse"]), help="HTTP transport.")
@click.option(
    "--header", "header_list", multiple=True, metavar="K=V", help="HTTP header (repeatable)."
)
@click.option("--description", help="One-line description for discovery/search.")
@click.option("--tag", "tag_list", multiple=True, help="Tag (repeatable).")
@click.option(
    "--secret",
    "secret_list",
    multiple=True,
    metavar="ENV_VAR[:Label[:create_url]]",
    help="Declare a keychain secret (repeatable).",
)
@click.option("--expose-prompts/--no-expose-prompts", default=None, help="Surface child prompts.")
@click.option(
    "--expose-resources/--no-expose-resources", default=None, help="Surface child resources."
)
@click.option("--connect-timeout", type=float, default=None, help="Connect+enumerate budget (s).")
@click.option(
    "--config",
    "config_path",
    default=None,
    help="Write to this file instead of the default target.",
)
@click.option(
    "--from-json", "from_json", default=None, help="A docs snippet (wrapped or single entry)."
)
@click.option(
    "--keep-env-secrets",
    is_flag=True,
    help="Do not auto-move likely-secret env vars into auth.secrets.",
)
@click.option("--force", is_flag=True, help="Overwrite an existing entry with the same name.")
@click.option("--dry-run", is_flag=True, help="Print the entry without writing.")
def cmd_add(
    name: str,
    command: str | None,
    arg_list: tuple[str, ...],
    env_list: tuple[str, ...],
    url: str | None,
    transport: str | None,
    header_list: tuple[str, ...],
    description: str | None,
    tag_list: tuple[str, ...],
    secret_list: tuple[str, ...],
    expose_prompts: bool | None,
    expose_resources: bool | None,
    connect_timeout: float | None,
    config_path: str | None,
    from_json: str | None,
    keep_env_secrets: bool,
    force: bool,
    dry_run: bool,
) -> None:
    """Add (or update) a server in the config, moving secrets to the keychain.

    \b
    Examples:
      mcp-hub add mongodb --from-json '{"mcpServers":{"MongoDB":{"command":"npx",
        "args":["-y","mongodb-mcp-server@latest"],
        "env":{"MDB_MCP_CONNECTION_STRING":"mongodb+srv://u:p@h/db"}}}}' --arg --readOnly
      mcp-hub add linear --command npx --arg -y --arg linear-mcp-server \\
        --secret 'LINEAR_API_KEY:Linear API key:https://linear.app/settings/api'
      mcp-hub add metrics --url https://metrics.example.com/sse --transport sse
    """
    base = _parse_from_json(from_json, name) if from_json else {}

    command = command or base.get("command")
    url = url or base.get("url")
    transport = transport or base.get("transport")

    args = [str(a) for a in (base.get("args") or [])] + list(arg_list)

    env: dict[str, str] = {}
    if isinstance(base.get("env"), dict):
        env.update({str(k): str(v) for k, v in base["env"].items()})
    env.update(_parse_kv_list(env_list, "env"))

    headers: dict[str, str] = {}
    if isinstance(base.get("headers"), dict):
        headers.update({str(k): str(v) for k, v in base["headers"].items()})
    headers.update(_parse_kv_list(header_list, "header"))

    tags = list(dict.fromkeys([str(t) for t in (base.get("tags") or [])] + list(tag_list)))
    description = description or base.get("description")

    secrets: list[dict[str, Any]] = []
    seen: set[str] = set()
    base_auth = base.get("auth")
    if isinstance(base_auth, dict):
        for s in base_auth.get("secrets") or []:
            if isinstance(s, dict) and s.get("env_var"):
                secrets.append(
                    {
                        k: v
                        for k, v in s.items()
                        if k in ("env_var", "label", "create_url", "sensitive")
                    }
                )
                seen.add(s["env_var"])

    if expose_prompts is None:
        expose_prompts = bool(base.get("expose_prompts", False))
    if expose_resources is None:
        expose_resources = bool(base.get("expose_resources", False))
    if connect_timeout is None and base.get("connect_timeout_seconds") is not None:
        connect_timeout = float(base["connect_timeout_seconds"])

    moved: list[str] = []
    if not keep_env_secrets:
        for key in list(env.keys()):
            if _is_secretish_key(key) and not _is_placeholder(env[key]):
                if key not in seen:
                    secrets.append({"env_var": key, "label": _titlecase_label(key)})
                    seen.add(key)
                moved.append(key)
                del env[key]

    for secret in _parse_secret_flags(secret_list):
        env_var = secret["env_var"]
        env.pop(env_var, None)
        if env_var in seen:
            secrets = [x for x in secrets if x.get("env_var") != env_var]
        secrets.append(secret)
        seen.add(env_var)

    if url and command:
        _die("provide either --command (stdio) or --url (http), not both")
    if not url and not command:
        _die("need --command (stdio) or --url (http) — directly or via --from-json")

    entry = _build_entry(
        command=command,
        url=url,
        transport=transport,
        args=args,
        env=env,
        headers=headers,
        secrets=secrets,
        description=description,
        tags=tags,
        expose_prompts=bool(expose_prompts),
        expose_resources=bool(expose_resources),
        connect_timeout=connect_timeout,
    )

    target = _expand_path(config_path) if config_path else _resolve_write_target()

    if dry_run:
        if _path_format(target) == "yaml":
            snippet = yaml.safe_dump(
                {name: entry}, sort_keys=False, default_flow_style=False, allow_unicode=True
            ).rstrip()
        else:
            snippet = json.dumps({name: entry}, indent=2)
        if moved:
            click.echo(f"# would move secrets to keychain schema: {', '.join(moved)}", err=True)
        click.echo(f"# target: {target}", err=True)
        click.echo(snippet)
        return

    if target.exists() and target.read_text().strip():
        full_doc, servers_map, _ = _load_config_doc(target)
    elif _path_format(target) == "json":
        full_doc = {"mcpServers": {}}
        servers_map = full_doc["mcpServers"]
    else:
        full_doc = {}
        servers_map = full_doc

    if name in servers_map and not force:
        _die(f"server '{name}' already exists in {target} (use --force to overwrite)")

    action = "Updated" if name in servers_map else "Added"
    servers_map[name] = entry
    _dump_config_doc(target, full_doc)

    click.echo(f"{action} '{name}' in {target}")
    if moved:
        click.echo(
            f"  moved {len(moved)} likely-secret env var(s) to auth.secrets "
            f"(raw values dropped): {', '.join(moved)}"
        )
    if secrets:
        click.echo(f"  secrets to provision: {', '.join(s['env_var'] for s in secrets)}")
        click.echo(f"  next: mcp-hub auth provision {name}")
    click.echo("  then: mcp-hub validate  &&  call the `reload` tool (or restart the host)")


# --- bundled agent skills (package data under mcp_hub/skills/) ---

DEFAULT_SKILL = "mcp-hub"


def _skills_root():
    return importlib.resources.files("mcp_hub") / "skills"


def _iter_skill_names() -> list[str]:
    root = _skills_root()
    names: list[str] = []
    try:
        for child in root.iterdir():
            if child.is_dir() and (child / "SKILL.md").is_file():
                names.append(child.name)
    except (FileNotFoundError, NotADirectoryError):
        pass
    return sorted(names)


def _parse_frontmatter(text: str) -> dict[str, Any]:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    try:
        data = yaml.safe_load(text[3:end]) or {}
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _copy_traversable(src, dest: Path) -> list[Path]:
    """Recursively copy a Traversable (filesystem or zip) into dest."""
    written: list[Path] = []
    if src.is_dir():
        dest.mkdir(parents=True, exist_ok=True)
        for child in src.iterdir():
            written += _copy_traversable(child, dest / child.name)
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(src.read_bytes())
        written.append(dest)
    return written


@main.group("skill")
def cmd_skill() -> None:
    """Show or install the bundled mcp-hub agent skill(s)."""


@cmd_skill.command("list")
def cmd_skill_list() -> None:
    """List bundled skills (name + description)."""
    rows = []
    for nm in _iter_skill_names():
        fm = _parse_frontmatter((_skills_root() / nm / "SKILL.md").read_text())
        rows.append({"name": fm.get("name", nm), "description": fm.get("description", "")})
    _print({"count": len(rows), "skills": rows})


@cmd_skill.command("show")
@click.argument("name", required=False, default=DEFAULT_SKILL)
def cmd_skill_show(name: str) -> None:
    """Print a bundled skill's SKILL.md to stdout (default: mcp-hub)."""
    skill_file = _skills_root() / name / "SKILL.md"
    if not skill_file.is_file():
        _die(f"unknown skill: {name} (available: {', '.join(_iter_skill_names()) or 'none'})")
    click.echo(skill_file.read_text())


@cmd_skill.command("install")
@click.argument("name", required=False, default=DEFAULT_SKILL)
@click.option(
    "--client",
    type=click.Choice(["claude", "cursor"]),
    default="claude",
    show_default=True,
    help="Target client (.claude/skills/ or .cursor/skills/).",
)
@click.option(
    "--dir", "dest_dir", default=None, help="Explicit destination dir (overrides --client)."
)
@click.option("--force", is_flag=True, help="Overwrite an existing skill directory.")
def cmd_skill_install(name: str, client: str, dest_dir: str | None, force: bool) -> None:
    """Install a bundled skill into a client's skills directory (default: Claude)."""
    src = _skills_root() / name
    if not (src / "SKILL.md").is_file():
        _die(f"unknown skill: {name} (available: {', '.join(_iter_skill_names()) or 'none'})")

    if dest_dir:
        base = Path(dest_dir).expanduser()
    elif client == "cursor":
        base = Path(".cursor/skills")
    else:
        base = Path(".claude/skills")
    dest = base / name

    if dest.exists() and not force:
        _die(f"{dest} already exists (use --force to overwrite)")
    if dest.exists():
        shutil.rmtree(dest)

    written = _copy_traversable(src, dest)
    click.echo(f"Installed skill '{name}' to {dest}")
    for path in written:
        click.echo(f"  {path}")


if __name__ == "__main__":
    main()
