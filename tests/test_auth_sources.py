from __future__ import annotations

import asyncio
import json
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml
from click.testing import CliRunner
from keyring.errors import PasswordDeleteError
from mcp_hub import auth
from mcp_hub.cli import main
from mcp_hub.config import ServerSpec, load_servers
from mcp_hub.proxy import ProxyClient
from mcp_hub.tools import _handle_auth_status, _handle_authenticate, _handle_reload, handle_tool


@pytest.fixture(autouse=True)
def keychain(monkeypatch, tmp_path):
    values = {}
    monkeypatch.setattr(auth, "LEARNED_AUTH_PATH", tmp_path / "learned.json")
    monkeypatch.setattr(
        auth.keyring, "get_password", lambda service, key: values.get((service, key))
    )
    monkeypatch.setattr(
        auth.keyring,
        "set_password",
        lambda service, key, value: values.__setitem__((service, key), value),
    )

    def delete(service, key):
        try:
            del values[service, key]
        except KeyError:
            raise PasswordDeleteError("not stored") from None

    monkeypatch.setattr(auth.keyring, "delete_password", delete)
    return values


@pytest.fixture
def schema():
    return auth.AuthConfig([auth.SecretSpec("TOKEN", "API token")])


@pytest.fixture
def config(monkeypatch, tmp_path):
    path = tmp_path / "servers.yml"
    path.write_text(
        yaml.safe_dump(
            {
                "example": {
                    "command": "example-mcp",
                    "auth": {"secrets": {"TOKEN": {"label": "API token"}}},
                }
            }
        )
    )
    monkeypatch.setenv("CONFIG_FILE", str(path))
    return path


def command(code):
    return [sys.executable, "-c", code]


def test_source_storage_and_removal_leave_value_intact(keychain):
    argv = ["op", "read", "op://Work/Item/credential"]
    auth.set_secret("example", "TOKEN", "old-value")
    auth.set_command_source("example", "TOKEN", argv)
    assert json.loads(keychain[auth.COMMAND_SOURCE_SERVICE, "example:TOKEN"]) == argv
    assert auth.get_command_source("example", "TOKEN") == argv
    auth.delete_command_source("example", "TOKEN")
    auth.delete_command_source("example", "TOKEN")
    assert auth.get_command_source("example", "TOKEN") is None
    assert auth.get_secret("example", "TOKEN") == "old-value"


@pytest.mark.parametrize("raw", ["not-json", "{}", '[""]', '["op", 1]', '["op\\u0000"]', "null"])
def test_malformed_source_fails_closed(keychain, raw):
    keychain[auth.COMMAND_SOURCE_SERVICE, "example:TOKEN"] = raw
    with pytest.raises(RuntimeError, match="Invalid credential source"):
        auth.get_command_source("example", "TOKEN")


@pytest.mark.parametrize("argv", [[], [""], ["op", "\0"]])
def test_invalid_command_rejected(argv):
    with pytest.raises(ValueError):
        auth.set_command_source("example", "TOKEN", argv)


@pytest.mark.parametrize("operation", ["get_password", "set_password", "delete_password"])
def test_keyring_failures_are_explicit_and_sanitized(monkeypatch, operation):
    def fail(*args):
        raise RuntimeError("sensitive backend details")

    monkeypatch.setattr(auth.keyring, operation, fail)
    action = {
        "get_password": lambda: auth.get_command_source("example", "TOKEN"),
        "set_password": lambda: auth.set_command_source("example", "TOKEN", ["op"]),
        "delete_password": lambda: auth.delete_command_source("example", "TOKEN"),
    }[operation]
    with pytest.raises(RuntimeError, match="credential source for example/TOKEN") as error:
        action()
    assert "sensitive backend" not in str(error.value)
    assert error.value.__suppress_context__


async def test_command_resolution_preserves_whitespace_and_keychain_fallback(schema):
    auth.set_secret("example", "TOKEN", "old-value")
    assert await auth.resolve_secrets("example", schema) == {"TOKEN": "old-value"}
    auth.set_command_source("example", "TOKEN", command("print('  fresh value  \\r')"))
    assert await auth.resolve_secrets("example", schema) == {"TOKEN": "  fresh value  "}
    assert auth.get_secret("example", "TOKEN") == "old-value"


async def test_resolution_is_scoped_to_present_schema_entries(schema):
    auth.set_command_source("example", "TOKEN", command("print('present')"))
    auth.set_command_source("other", "TOKEN", ["must-not-execute"])
    auth.set_command_source("example", "UNDECLARED", ["must-not-execute"])
    auth.set_command_source("example", "ABSENT", ["must-not-execute"])
    schema.secrets.append(auth.SecretSpec("ABSENT", "Retired", state="absent"))
    assert await auth.resolve_secrets("example", schema) == {"TOKEN": "present"}


async def test_resolved_credentials_are_not_passed_to_other_commands(schema, monkeypatch):
    monkeypatch.delenv("TOKEN", raising=False)
    auth.set_secret("example", "TOKEN", "private-value")
    schema.secrets.append(auth.SecretSpec("OTHER", "Other"))
    auth.set_command_source(
        "example",
        "OTHER",
        command("import os; assert 'TOKEN' not in os.environ; print('other-value')"),
    )
    assert await auth.resolve_secrets("example", schema) == {
        "TOKEN": "private-value",
        "OTHER": "other-value",
    }


@pytest.mark.parametrize("size", [65536, 65537])
async def test_exact_output_limit(schema, size):
    auth.set_command_source(
        "example", "TOKEN", command(f"import sys; sys.stdout.write('x' * {size})")
    )
    if size == 65536:
        result = await auth.resolve_secrets("example", schema)
        assert len(result["TOKEN"]) == size
    else:
        with pytest.raises(RuntimeError, match="output limit"):
            await auth.resolve_secrets("example", schema)


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["/nonexistent/credential-command"], "could not start"),
        (
            command(
                "import sys; print('private-output'); sys.stderr.write('private-error'); sys.exit(7)"
            ),
            "exited with status 7",
        ),
        (command("print()"), "empty or invalid"),
        (command("import sys; sys.stdout.buffer.write(b'\\xff')"), "invalid UTF-8"),
        (command("import sys; sys.stdout.buffer.write(b'abc\\0def')"), "empty or invalid"),
        (command("print('x' * 70000)"), "output limit"),
    ],
)
async def test_failures_do_not_fall_back_or_expose_output(schema, argv, message, caplog):
    auth.set_secret("example", "TOKEN", "stale-fallback")
    auth.set_command_source("example", "TOKEN", argv)
    with pytest.raises(RuntimeError, match=message) as error:
        await auth.resolve_secrets("example", schema)
    assert "private-output" not in str(error.value) + caplog.text
    assert "private-error" not in str(error.value) + caplog.text
    assert "stale-fallback" not in str(error.value) + caplog.text


async def test_timeout_kills_and_reaps_command(monkeypatch, schema):
    monkeypatch.setattr(auth, "COMMAND_TIMEOUT_SECONDS", 0.1)
    original = asyncio.create_subprocess_exec
    processes = []

    async def spawn(*args, **kwargs):
        process = await original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    auth.set_command_source("example", "TOKEN", command("import time; time.sleep(60)"))
    with pytest.raises(RuntimeError, match="timed out"):
        await auth.resolve_secrets("example", schema)
    assert processes[0].returncode is not None


async def test_cancellation_kills_and_reaps_command(monkeypatch, schema):
    original = asyncio.create_subprocess_exec
    started = asyncio.Event()
    processes = []

    async def spawn(*args, **kwargs):
        process = await original(*args, **kwargs)
        processes.append(process)
        started.set()
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    auth.set_command_source("example", "TOKEN", command("import time; time.sleep(60)"))
    task = asyncio.create_task(auth.resolve_secrets("example", schema))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert processes[0].returncode is not None


@pytest.mark.skipif(os.name != "posix", reason="POSIX process group cleanup")
async def test_timeout_cleans_up_descendants_holding_stdout(monkeypatch, schema):
    monkeypatch.setattr(auth, "COMMAND_TIMEOUT_SECONDS", 0.1)
    auth.set_command_source(
        "example",
        "TOKEN",
        command(
            "import subprocess, sys; "
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])"
        ),
    )
    with pytest.raises(RuntimeError, match="timed out"):
        await asyncio.wait_for(auth.resolve_secrets("example", schema), timeout=3)


async def test_command_is_not_shell_expanded_and_stdin_is_closed(schema):
    auth.set_command_source(
        "example",
        "TOKEN",
        [
            sys.executable,
            "-c",
            "import sys; assert sys.stdin.read() == ''; print(sys.argv[1])",
            "$(must-not-execute)",
        ],
    )
    assert await auth.resolve_secrets("example", schema) == {"TOKEN": "$(must-not-execute)"}


def test_status_never_executes_command_or_exposes_argv(schema):
    auth.set_command_source("example", "TOKEN", ["must-not-execute", "private-argument"])
    auth.set_secret("example", "TOKEN", "old-value")
    status = auth.auth_status("example", schema)
    assert status["status"] == "configured"
    assert status["secrets"][0]["source"] == "command"
    assert status["secrets"][0]["stored"] is False
    assert status["secrets"][0]["configured"] is True
    assert "private-argument" not in json.dumps(status)
    schema.secrets.append(auth.SecretSpec("OTHER", "Other"))
    assert auth.auth_status("example", schema)["status"] == "partial"
    auth.set_secret("example", "OTHER", "value")
    assert auth.auth_status("example", schema)["status"] == "configured"
    auth.delete_command_source("example", "TOKEN")
    assert auth.auth_status("example", schema)["status"] == "authenticated"


def test_cli_set_status_remove_without_config_changes(config, keychain):
    runner = CliRunner()
    before = config.read_bytes()
    result = runner.invoke(
        main,
        [
            "auth",
            "source",
            "set",
            "example",
            "TOKEN",
            "--",
            "op",
            "read",
            "--no-newline",
            "op://Work/Item/credential",
        ],
    )
    assert result.exit_code == 0, result.output
    assert auth.get_command_source("example", "TOKEN") == [
        "op",
        "read",
        "--no-newline",
        "op://Work/Item/credential",
    ]
    for args in [
        ["auth", "status", "--server", "example"],
        ["auth", "source", "status", "example"],
    ]:
        result = runner.invoke(main, args)
        assert result.exit_code == 0, result.output
        assert "command (unverified)" in result.output
        assert "configured" in result.output
        assert "op://" not in result.output
    result = runner.invoke(main, ["auth", "source", "remove", "example", "TOKEN"])
    assert result.exit_code == 0, result.output
    assert auth.get_command_source("example", "TOKEN") is None
    assert config.read_bytes() == before


@pytest.mark.parametrize(
    ("server", "env_var", "message"),
    [("unknown", "TOKEN", "unknown server"), ("example", "UNKNOWN", "present auth schema")],
)
def test_cli_rejects_unknown_targets(config, server, env_var, message):
    result = CliRunner().invoke(
        main,
        [
            "auth",
            "source",
            "set",
            server,
            env_var,
            "--",
            "op",
            "read",
        ],
    )
    assert result.exit_code != 0
    assert message in result.output


def test_cli_rejects_http_and_absent_entries(config):
    config.write_text(
        yaml.safe_dump(
            {
                "remote": {"url": "https://example.test/mcp", "auth": {"secrets": {"TOKEN": {}}}},
                "retired": {
                    "command": "example-mcp",
                    "auth": {"secrets": {"TOKEN": {"state": "absent"}}},
                },
            }
        )
    )
    for server, message in [("remote", "only for stdio"), ("retired", "present auth schema")]:
        result = CliRunner().invoke(
            main,
            [
                "auth",
                "source",
                "set",
                server,
                "TOKEN",
                "--",
                "op",
            ],
        )
        assert result.exit_code != 0
        assert message in result.output


def test_cli_can_remove_orphaned_and_malformed_sources(keychain):
    keychain[auth.COMMAND_SOURCE_SERVICE, "removed:TOKEN"] = "invalid json"
    result = CliRunner().invoke(main, ["auth", "source", "remove", "removed", "TOKEN"])
    assert result.exit_code == 0, result.output
    assert auth.get_command_source("removed", "TOKEN") is None


def test_cli_accepts_learned_secret_without_changing_schema(config, schema):
    config.write_text(yaml.safe_dump({"example": {"command": "example-mcp"}}))
    auth.save_learned("example", schema)
    before = auth.LEARNED_AUTH_PATH.read_bytes()
    result = CliRunner().invoke(
        main,
        [
            "auth",
            "source",
            "set",
            "example",
            "TOKEN",
            "--",
            "op",
            "private-reference",
        ],
    )
    assert result.exit_code == 0, result.output
    assert auth.LEARNED_AUTH_PATH.read_bytes() == before
    promoted = CliRunner().invoke(main, ["auth", "promote", "example"])
    assert promoted.exit_code == 0, promoted.output
    assert "TOKEN:" in promoted.output
    assert "private-reference" not in promoted.output


@pytest.mark.parametrize("force", [False, True])
def test_provision_skips_command_and_still_provisions_other_secrets(config, force):
    data = yaml.safe_load(config.read_text())
    data["example"]["auth"]["secrets"]["OTHER"] = {"label": "Other", "sensitive": False}
    config.write_text(yaml.safe_dump(data))
    auth.set_command_source("example", "TOKEN", ["must-not-execute"])
    result = CliRunner().invoke(
        main,
        [
            "auth",
            "provision",
            "example",
            *(["--force"] if force else []),
        ],
        input="new-other-value\n",
    )
    assert result.exit_code == 0, result.output
    assert "command-managed [skip]" in result.output
    assert auth.get_secret("example", "TOKEN") is None
    assert auth.get_secret("example", "OTHER") == "new-other-value"


@pytest.mark.parametrize("force", [False, True])
async def test_authenticate_command_only_does_not_need_elicitation(config, force):
    auth.set_command_source("example", "TOKEN", ["must-not-execute"])
    proxy = SimpleNamespace(invalidate_server=AsyncMock())
    state = SimpleNamespace(host_session=None)
    result = await _handle_authenticate(
        {"server": "example", "force": force},
        load_servers(),
        proxy,
        state,
    )
    payload = json.loads(result[0].text)
    assert payload["status"] == "configured"
    proxy.invalidate_server.assert_awaited_once_with("example")
    status = _handle_auth_status({"server": "example"}, load_servers())
    assert json.loads(status[0].text)["servers"][0]["status"] == "configured"


async def test_authenticate_force_prompts_only_for_keychain_secrets(config):
    auth.set_command_source("example", "TOKEN", ["must-not-execute"])
    servers = load_servers()
    servers["example"].auth.secrets.append(auth.SecretSpec("OTHER", "Other"))
    host = SimpleNamespace(
        elicit_form=AsyncMock(
            return_value=SimpleNamespace(
                action="accept",
                content={"value": "new-value"},
            )
        )
    )
    result = await _handle_authenticate(
        {"server": "example", "force": True},
        servers,
        SimpleNamespace(invalidate_server=AsyncMock()),
        SimpleNamespace(host_session=host),
    )
    assert json.loads(result[0].text)["status"] == "configured"
    host.elicit_form.assert_awaited_once()
    assert "Other" in host.elicit_form.call_args.kwargs["message"]
    assert auth.get_secret("example", "OTHER") == "new-value"
    assert auth.get_secret("example", "TOKEN") is None


@pytest.mark.parametrize("all_secrets", [False, True])
def test_auth_rm_deletes_value_and_registration(config, all_secrets):
    auth.set_secret("example", "TOKEN", "old-value")
    auth.set_command_source("example", "TOKEN", ["op"])
    result = CliRunner().invoke(
        main,
        [
            "auth",
            "rm",
            "example",
            *([] if all_secrets else ["TOKEN"]),
        ],
    )
    assert result.exit_code == 0, result.output
    assert auth.get_secret("example", "TOKEN") is None
    assert auth.get_command_source("example", "TOKEN") is None


def test_absent_reconciliation_deletes_value_and_registration(schema):
    auth.set_secret("example", "TOKEN", "old-value")
    auth.set_command_source("example", "TOKEN", ["op"])
    schema.secrets[0].state = "absent"
    auth.reconcile_absent("example", schema)
    assert auth.get_secret("example", "TOKEN") is None
    assert auth.get_command_source("example", "TOKEN") is None


async def test_generated_config_replacement_preserves_source(config):
    auth.set_command_source("example", "TOKEN", command("print('fresh')"))
    data = yaml.safe_load(config.read_text())
    data["example"]["args"] = ["--updated"]
    config.write_text(yaml.safe_dump(data))
    spec = load_servers()["example"]
    assert spec.args == ["--updated"]
    assert await auth.resolve_secrets("example", spec.auth) == {"TOKEN": "fresh"}


async def test_connection_injects_secret_and_targeted_reload_picks_up_rotation(
    tmp_path, monkeypatch
):
    server_path = tmp_path / "child.py"
    server_path.write_text("""
import json
import os
import sys

for line in sys.stdin:
    request = json.loads(line)
    if "id" not in request:
        continue
    if request["method"] == "initialize":
        result = {
            "protocolVersion": request["params"]["protocolVersion"],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "auth-source-test", "version": "1"},
        }
    elif request["method"] == "tools/call":
        result = {"content": [{
            "type": "text", "text": os.environ["TOKEN"] + ":" + os.environ["SETTING"],
        }]}
    else:
        result = {"tools": [{"name": "read", "inputSchema": {"type": "object"}}]}
    print(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}), flush=True)
""")
    value_path = tmp_path / "value"
    value_path.write_text("first")
    auth.set_command_source(
        "example",
        "TOKEN",
        command(f"from pathlib import Path; print(Path({str(value_path)!r}).read_text())"),
    )
    spec = ServerSpec(
        name="example",
        transport="stdio",
        command=sys.executable,
        args=[str(server_path)],
        env={"TOKEN": "config-value", "SETTING": "preserved"},
        auth=auth.AuthConfig([auth.SecretSpec("TOKEN", "Token")]),
    )
    monkeypatch.setenv("TOKEN", "inherited-value")
    servers = {"example": spec}
    async with ProxyClient(servers) as proxy:
        first = await proxy.call_tool("example", "read", {})
        assert first.content[0].text == "first:preserved"
        value_path.write_text("rotated")
        unchanged = await proxy.call_tool("example", "read", {})
        assert unchanged.content[0].text == "first:preserved"
        await _handle_reload({"server": "example"}, servers, proxy, None)
        rotated = await proxy.call_tool("example", "read", {})
        assert rotated.content[0].text == "rotated:preserved"
        auth.delete_command_source("example", "TOKEN")
        auth.set_secret("example", "TOKEN", "keychain-value")
        await _handle_reload({"server": "example"}, servers, proxy, None)
        restored = await proxy.call_tool("example", "read", {})
        assert restored.content[0].text == "keychain-value:preserved"


async def test_mcp_connection_error_does_not_expose_command_output(config, caplog):
    auth.set_command_source(
        "example",
        "TOKEN",
        command("import sys; print('do-not-leak'); sys.stderr.write('do-not-leak'); sys.exit(1)"),
    )
    servers = load_servers()
    async with ProxyClient(servers) as proxy:
        result = await handle_tool(
            "call_tool",
            {
                "server": "example",
                "tool": "anything",
            },
            servers,
            proxy,
        )
    assert "exited with status 1" in result[0].text
    assert "do-not-leak" not in result[0].text + caplog.text


async def test_child_startup_diagnostics_redact_command_credentials(config, caplog, capsys):
    auth.set_command_source("example", "TOKEN", command("print('command-secret-not-real')"))
    servers = load_servers()
    servers["example"].command = sys.executable
    servers["example"].args = [
        "-c",
        "import os, sys; sys.stderr.write('token=' + os.environ['TOKEN']); sys.exit(1)",
    ]
    async with ProxyClient(servers) as proxy:
        result = await handle_tool(
            "call_tool",
            {
                "server": "example",
                "tool": "anything",
            },
            servers,
            proxy,
        )
    diagnostic = result[0].text + caplog.text + capsys.readouterr().err
    assert "[REDACTED]" in diagnostic
    assert "command-secret-not-real" not in diagnostic


async def test_failed_connection_can_retry_with_a_new_source(schema):
    auth.set_command_source("example", "TOKEN", command("raise SystemExit(1)"))
    spec = ServerSpec(
        name="example",
        transport="stdio",
        command="unused",
        auth=schema,
    )
    async with ProxyClient({"example": spec}) as proxy:
        with pytest.raises(RuntimeError, match="status 1"):
            await proxy.session("example")
        assert "example" not in proxy._holders
        auth.set_command_source("example", "TOKEN", command("raise SystemExit(2)"))
        with pytest.raises(RuntimeError, match="status 2"):
            await proxy.session("example")
        assert "example" not in proxy._holders


async def test_cancelled_credential_resolution_unblocks_connection_waiter(schema, monkeypatch):
    started = asyncio.Event()

    async def resolve(*args):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(auth, "resolve_secrets", resolve)
    spec = ServerSpec(name="example", transport="stdio", command="unused", auth=schema)
    async with ProxyClient({"example": spec}) as proxy:
        connection = asyncio.create_task(proxy.session("example"))
        await started.wait()
        proxy._holders["example"].task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(connection, timeout=1)
        assert "example" not in proxy._holders
