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
import json
import logging
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import click
from dotenv import load_dotenv

from mcp_hub.config import load_servers
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
                click.echo(f"  {secret.label} ({secret.env_var}): already stored [overwriting]")
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


if __name__ == "__main__":
    main()
